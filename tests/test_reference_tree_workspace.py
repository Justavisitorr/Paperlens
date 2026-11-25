"""Exercise citation lookup through the real account-scoped application router."""

import httpx
from fastapi.testclient import TestClient
from test_reference_links import make_references
from test_web_service import client_app, headers, namespace
from test_web_service import config as config

from paperlens import reference_tree_routes
from paperlens.reference_tree import ReferenceTreeService
from paperlens.storage import write_json


def test_tree_route_is_account_scoped_and_obeys_saved_network_setting(config, monkeypatch):
    requests = []

    def provider(request):
        requests.append(request)
        return httpx.Response(200, json={
            "id": "https://openalex.org/W1",
            "title": "Public bibliography fixture",
            "publication_year": 2017,
            "referenced_works": [],
        })

    def service(cache_dir, *, allow_network):
        return ReferenceTreeService(
            cache_dir, allow_network=allow_network,
            client=httpx.Client(transport=httpx.MockTransport(provider)),
        )

    monkeypatch.setattr(reference_tree_routes, "ReferenceTreeService", service)
    report = make_references(["DOI: 10.1234/example"])
    report.run.id = "a" * 32
    destination = config.root / "workspaces" / namespace("alice") / "outputs" / "fixture"
    write_json(destination / "report.json", report.model_dump(mode="json"))
    endpoint = f"/api/library/{report.run.id}/references/r1/tree"
    with TestClient(client_app(config), base_url=config.origin) as client:
        alice, bob = headers("alice"), headers("bob")
        assert "references.tree" in client.get("/api/health", headers=alice).json()["features"]
        assert client.get(endpoint, headers=bob).status_code == 404
        assert not requests

        disabled = client.put("/api/settings", headers=alice, json={
            "values": {"PAPERLENS_REFERENCE_NETWORK": False},
        })
        assert "errors" not in disabled.json()
        response = client.get(endpoint, headers=alice)
        assert response.status_code == 200 and response.json()["state"] == "disabled"
        assert not requests

        client.put("/api/settings", headers=alice, json={
            "values": {"PAPERLENS_REFERENCE_NETWORK": True},
        })
        response = client.get(endpoint, headers=alice)
        assert response.status_code == 200 and response.json()["state"] == "unavailable"
        assert response.json()["node"]["id"] == "W1"
        assert len(requests) == 1
        assert client.get(endpoint, headers=bob).status_code == 404
        assert len(requests) == 1
        assert not (config.root / "workspaces" / namespace("bob") / "cache" / "reference-tree").exists()
