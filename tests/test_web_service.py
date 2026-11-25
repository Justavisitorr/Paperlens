"""Isolation and request-integrity regressions for the actual PaperLens API."""

import hashlib
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from test_workspace_api import report as report

from paperlens import server
from paperlens.config import Settings
from paperlens.models import ReadingDocument
from paperlens.provider import JsonProvider
from paperlens.storage import write_json
from paperlens.web_service import ServiceConfig, TenantSettings, initialize, service_app


def namespace(user):
    return hashlib.sha256(b"paperlens-sites-v1\0" + user.encode()).hexdigest()


def headers(user):
    return {"x-test-user": user, "x-paperlens-workspace": namespace(user)}


def client_app(config):
    app = service_app(config)

    async def with_test_identity(scope, receive, send):
        # Test-only injection of the in-process principal. No listening socket.
        if scope["type"] == "http":
            selected = dict(scope["headers"]).get(b"x-test-user")
            scope = {**scope, "paperlens.principal": selected}
        await app(scope, receive, send)

    return with_test_identity


@pytest.fixture
def config(tmp_path):
    initialize(tmp_path)
    return ServiceConfig.load(tmp_path, "https://research.example")


def test_host_credentials_are_never_inherited(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "host-key-must-stay-private")
    monkeypatch.setenv(
        "LLM_ROUTES",
        '{"main":{"account_id":"host","model":"host","base_url":"https://api.openai.com/v1","api_key":"secret"}}',
    )
    tenant = TenantSettings()
    assert tenant.llm_api_key.get_secret_value() == ""
    assert tenant.llm_routes == {}


def test_identity_is_not_accepted_from_http_headers(config):
    with TestClient(service_app(config)) as client:
        result = client.get("/api/library", headers={"x-paperlens-principal": "owner"})
        assert result.status_code == 403
    assert not (config.root / "workspaces").exists()


def test_accounts_and_runtime_are_isolated_and_encrypted(config):
    a, b = headers("alice"), headers("bob")
    app = client_app(config)
    with TestClient(app, base_url=config.origin) as client:
        result = client.post(
            "/api/connections/accounts",
            headers=a,
            json={
                "provider_id": "openai",
                "name": "Alice OpenAI",
                "api_key": "alice-private-api-key",
            },
        )
        assert result.status_code == 201
        aid = result.json()["account"]["id"]
        assert "alice-private-api-key" not in result.text
        assert "Alice OpenAI" not in client.get("/api/connections", headers=b).text
        assert (
            client.patch(
                "/api/connections/accounts/" + aid, headers=b, json={"name": "bad"}
            ).status_code
            == 422
        )
        state = client.put(
            "/api/settings",
            headers=a,
            json={
                "values": {
                    "LLM_BASE_URL": "https://api.openai.com/v1",
                    "LLM_MODEL": "gpt-6.1-terra",
                    "LLM_REASONING_EFFORT": "xhigh",
                    "LLM_API_KEY": "alice-runtime-api-key",
                }
            },
        )
        assert "errors" not in state.json()
        assert "alice-runtime-api-key" not in state.text
        assert "gpt-6.1-terra" not in client.get("/api/settings", headers=b).text
        assert all(p["limits"].get("ai_direct") for p in client.get(
            "/api/processing-profiles", headers=a
        ).json()["profiles"])
        assert all(p["limits"].get("ai_direct") for p in client.get(
            "/api/processing-profiles", headers=b
        ).json()["profiles"])
        stale = {**b, "x-paperlens-workspace": namespace("alice")}
        assert client.put("/api/settings", headers=stale, json={"values": {}}).status_code == 409
        assert client.get("/api/library", headers=stale).status_code == 409
        rejected = client.post(
            "/api/connections/accounts",
            headers=a,
            json={
                "provider_id": "custom",
                "base_url": "http://169.254.169.254",
                "api_key": "secret",
            },
        )
        assert rejected.status_code == 422
    for path in config.root.rglob("*"):
        if path.is_file() and path.suffix in {".json", ".env"}:
            content = path.read_bytes()
            assert b"alice-private-api-key" not in content
            assert b"alice-runtime-api-key" not in content
    with TestClient(client_app(config), base_url=config.origin) as client:
        assert "Alice OpenAI" in client.get("/api/connections", headers=a).text
        assert "Alice OpenAI" not in client.get("/api/connections", headers=b).text
        assert "gpt-6.1-terra" in client.get("/api/settings", headers=a).text
        for user_headers in (a, b):
            fields = {field["key"]: field for group in client.get(
                "/api/settings", headers=user_headers
            ).json()["groups"] for field in group["fields"]}
            assert "PAPERLENS_READING_MODE" not in fields


def test_library_jobs_annotations_and_deletion_are_scoped(config, report, monkeypatch):
    def no_paid_pipeline(pdf_path, settings, **kwargs):
        output = settings.paperlens_output_dir / pdf_path.parent.name / "dashboard.html"
        output.parent.mkdir(parents=True)
        output.write_text("<html>Fixture</html>")
        return SimpleNamespace(
            report_path=output, report=SimpleNamespace(run=SimpleNamespace(status="complete"))
        )

    monkeypatch.setattr(server, "run_pipeline", no_paid_pipeline)
    a, b = headers("alice"), headers("bob")
    root = config.root / "workspaces" / namespace("alice") / "outputs" / "fixture"
    report.document.sha256 = hashlib.sha256(b"%PDF-original").hexdigest()
    report.reading = ReadingDocument(version="3", source_version="fixture", items=[])
    write_json(root / "report.json", report.model_dump(mode="json"))
    (root / "source.pdf").write_bytes(b"%PDF-original")
    path = "/api/library/" + report.run.id
    with TestClient(client_app(config), base_url=config.origin) as client:
        assert len(client.get("/api/library", headers=a).json()["papers"]) == 1
        assert client.get("/api/library", headers=b).json()["papers"] == []
        for suffix in ["/report", "/pdf", "/notes", "/annotations"]:
            assert client.get(path + suffix, headers=b).status_code in {404, 422}
        assert client.patch(path, headers=b, json={"title": "Intruder"}).status_code == 404
        assert client.delete(path, headers=b).status_code == 404
        note = client.post(
            path + "/annotations",
            headers=a,
            json={
                "block_id": "b1",
                "start": 4,
                "end": 7,
                "text": "稀疏度",
                "note": "Alice's note",
            },
        )
        assert note.status_code == 201
        assert "Alice" in client.get(path + "/annotations", headers=a).text
        assert client.get(path + "/annotations", headers=b).status_code == 404
        sample = client.post(
            "/api/connections/accounts",
            headers=a,
            json={
                "provider_id": "openai",
                "name": "Test only",
                "api_key": "test-not-live",
            },
        ).json()["account"]["id"]
        client.put(
            "/api/connections/routing",
            headers=a,
            json={"routing": {"main": {"account_id": sample, "model": "gpt-6.1-terra"}}},
        )

        submitted = client.post(
            "/api/jobs",
            headers=a,
            files={"file": ("new.pdf", b"%PDF-test")},
            data={"unlimited_confirmed": "true"},
        )
        assert submitted.status_code == 202
        jid = submitted.json()["id"]
        assert client.get("/api/jobs/" + jid, headers=b).status_code == 404
        assert client.post("/api/jobs/" + jid + "/cancel", headers=b).status_code == 404
        assert client.get("/api/jobs/" + jid + "/report", headers=b).status_code == 404
        assert client.get("/api/jobs/" + jid + "/source.pdf", headers=b).status_code == 404
        original = client.get("/api/jobs/" + jid + "/source.pdf", headers=a)
        assert original.status_code == 200 and original.content == b"%PDF-test"
        for _ in range(40):
            if client.get("/api/jobs/" + jid, headers=a).json()["status"] not in {
                "queued",
                "running",
            }:
                break
            time.sleep(0.02)
        assert client.get("/api/jobs/" + jid, headers=a).json()["status"] == "complete"
        assert (
            client.get("/api/library/" + report.run.id + "/pdf", headers=a).content
            == b"%PDF-original"
        )


def test_online_bootstrap_is_private_and_offline_template_is_self_contained(config):
    a = headers("alice")
    with TestClient(client_app(config), base_url=config.origin) as client:
        document = client.get(
            "/", headers={**a, "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}
        )
        assert "/_paperlens/client.js?workspace=" + namespace("alice") in document.text
        exported = client.get("/", headers={**a, "sec-fetch-dest": "empty"})
        assert "/_paperlens/client.js" not in exported.text
        first = client.get("/_paperlens/client.js?workspace=" + namespace("alice"), headers=a)
        assert "secretbox" in first.text
        assert (
            client.get(
                "/_paperlens/client.js?workspace=" + namespace("alice"), headers=headers("bob")
            ).status_code
            == 409
        )
        assert (
            client.get("/api/library", headers={**a, "sec-fetch-site": "cross-site"}).status_code
            == 403
        )


@pytest.mark.parametrize("model,effort", [("gpt-6.1-terra", "xhigh"), ("gpt-6-astra", "max")])
def test_openai_selected_effort_reaches_actual_http_payload(tmp_path, model, effort):
    captured = []

    def transport(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}],
                "usage": {},
            },
        )

    settings = Settings(
        _env_file=None,
        llm_api_key="test",
        llm_base_url="https://api.openai.com/v1",
        llm_model=model,
        llm_reasoning_effort=effort,
        paperlens_cache_dir=tmp_path,
    )
    with JsonProvider(settings, transport=httpx.MockTransport(transport)) as provider:
        assert provider.json("test", "test", reasoning_effort="none") == {"ok": True}
    assert captured[0]["reasoning_effort"] == effort
    assert "max_completion_tokens" in captured[0]
