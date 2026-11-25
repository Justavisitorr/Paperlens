"""A citation tree must expose provider-reported edges, never search similarity."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_reference_links import make_references

from paperlens.reference_tree import ReferenceTreeService, _identifiers, work_id
from paperlens.reference_tree_routes import register_reference_tree_routes


def work(
    identity="W1", *, refs=(), title="Attention Is All You Need", year=2017, author="Ashish Vaswani"
):
    return {
        "id": "https://openalex.org/" + identity,
        "doi": "https://doi.org/10.1234/example",
        "title": title,
        "publication_year": year,
        "referenced_works": ["https://openalex.org/" + value for value in refs],
        "referenced_works_count": len(refs),
        "authorships": [{"author": {"display_name": author}}],
    }


def service(tmp_path, handler, *, allow_network=lambda: True):
    return ReferenceTreeService(
        tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        allow_network=allow_network,
    )


def test_real_edges_paginate_in_source_order_and_do_not_accept_unrequested_summaries(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "api.openalex.org"
        if request.url.path != "/works":
            return httpx.Response(200, json=work(refs=[f"W{i}" for i in range(2, 25)] + ["W2"]))
        identities = request.url.params["filter"].removeprefix("openalex_id:").split("|")
        # W3 no longer has a full record. An unrelated extra result must not form a new edge.
        return httpx.Response(
            200,
            json={
                "results": [work(identity) for identity in reversed(identities) if identity != "W3"]
                + [work("W999")]
            },
        )

    lookup = service(tmp_path, handler)
    first = lookup.branch("DOI: 10.1234/example")
    assert first["state"] == "available" and first["total"] == 23
    assert first["next_offset"] == 20
    assert [item["id"] for item in first["references"]] == [f"W{i}" for i in range(2, 22)]
    assert first["references"][1]["title"] == "文献信息暂缺 · W3"
    assert first["source_url"] == "https://api.openalex.org/works/W1"
    assert len(requests) == 2
    second = lookup.branch("DOI: 10.1234/example", offset=20)
    assert [item["id"] for item in second["references"]] == ["W22", "W23", "W24"]
    assert second["next_offset"] is None and len(requests) == 3
    assert lookup.branch("DOI: 10.1234/example")["cached"] is True
    assert len(requests) == 3


def test_child_expansion_uses_its_own_outbound_edges_including_a_cycle(tmp_path):
    def handler(request):
        if request.url.path == "/works/W2":
            return httpx.Response(200, json=work("W2", refs=["W1", "W3"]))
        return httpx.Response(200, json={"results": [work("W1"), work("W3")]})

    branch = service(tmp_path, handler).branch("ignored", node="W2")
    assert branch["node"]["id"] == "W2"
    assert [item["id"] for item in branch["references"]] == ["W1", "W3"]


def test_no_reference_records_never_claim_paper_has_no_citations(tmp_path):
    lookup = service(tmp_path, lambda request: httpx.Response(200, json=work()))
    result = lookup.branch("DOI: 10.1234/example")
    assert result["state"] == "unavailable"
    assert "不代表原论文没有引用" in result["message"]
    assert result["references"] == []


@pytest.mark.parametrize(
    "code,state", [(404, "not_found"), (429, "rate_limited"), (409, "rate_limited"), (500, "error")]
)
def test_external_errors_are_distinct_and_not_cached(tmp_path, code, state):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(code, json={})

    lookup = service(tmp_path, handler)
    assert lookup.branch("DOI: 10.1234/example")["state"] == state
    assert lookup.branch("DOI: 10.1234/example")["state"] == state
    assert count == 2


def test_identifiers_normalize_arxiv_and_do_not_fetch_printed_hosts(tmp_path):
    assert _identifiers("arXiv:1706.03762v2", "https://arxiv.org/pdf/1706.03762.pdf") == [
        "doi:10.48550/arxiv.1706.03762"
    ]
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=work())

    lookup = service(tmp_path, handler)
    lookup.branch("https://localhost/admin DOI: 10.1234/example", "http://127.0.0.1:8080/secrets")
    assert len(requests) == 1 and requests[0].url.host == "api.openalex.org"
    assert "localhost" not in str(requests[0].url)
    assert lookup.branch("10.1234/a and 10.1234/b")["state"] == "ambiguous"
    assert len(requests) == 1
    assert _identifiers("doi:10.48550/ARXIV.1706.03762 arXiv:1706.03762v1", "") == [
        "doi:10.48550/arxiv.1706.03762"
    ]
    for identity in ["https://evil.test/W1", "../../W1", "W0", "W123?x=y"]:
        assert work_id(identity) == ""
        with pytest.raises(ValueError):
            lookup.branch("ignored", node=identity)


def test_search_requires_title_first_author_and_year_and_unique_result(tmp_path):
    citation = "A. Vaswani, N. Shazeer. Attention Is All You Need. 2017."
    requests = []
    candidates = [
        work("W1", author="Other Person"),
        work("W2", year=2018),
        work("W3", title="Attention Is All You Need: A survey"),
        work("W4"),
    ]

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"results": candidates})

    lookup = service(tmp_path, handler)
    result = lookup.branch(citation)
    assert result["node"]["id"] == "W4"
    assert requests[0].url.params["search"] == citation
    candidates.append(work("W5"))
    assert lookup.branch(citation, refresh=True)["state"] == "ambiguous"
    candidates.pop()
    candidates.pop()
    assert lookup.branch(citation, refresh=True)["state"] == "unresolved"


def test_same_query_is_coalesced_between_concurrent_callers(tmp_path):
    count = 0
    lock = threading.Lock()

    def handler(request):
        nonlocal count
        with lock:
            count += 1
        time.sleep(0.04)
        return httpx.Response(200, json=work())

    lookup = service(tmp_path, handler)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: lookup.branch("DOI: 10.1234/example"), range(4)))
    assert count == 1
    assert all(result["node"]["id"] == "W1" for result in results)


def test_disabled_network_can_read_existing_cache_but_makes_no_new_request(tmp_path):
    enabled, calls = True, []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=work())

    lookup = service(tmp_path, handler, allow_network=lambda: enabled)
    lookup.branch("DOI: 10.1234/example")
    enabled = False
    assert lookup.branch("DOI: 10.1234/example")["cached"]
    assert lookup.branch("DOI: 10.1234/new")["state"] == "disabled"
    assert len(calls) == 1


def test_endpoint_checks_local_paper_and_reference_before_external_lookup(tmp_path):
    report = make_references(["DOI: 10.1234/example"])
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=work())

    def paper_loader(request, identity):
        if identity != "owned":
            raise HTTPException(404, "not in this workspace")
        return report

    app = FastAPI()
    register_reference_tree_routes(
        app, paper_loader=paper_loader, cache_dir=tmp_path, service=service(tmp_path, handler)
    )
    with TestClient(app) as client:
        assert client.get("/api/library/other/references/r1/tree").status_code == 404
        assert client.get("/api/library/owned/references/missing/tree").status_code == 404
        assert not calls
        assert (
            client.get("/api/library/owned/references/r1/tree?node=https://evil/W2").status_code
            == 422
        )
        assert client.get("/api/library/owned/references/r1/tree?offset=1").status_code == 422
        assert not calls
        result = client.get("/api/library/owned/references/r1/tree")
        assert result.status_code == 200 and result.json()["node"]["id"] == "W1"
        assert len(calls) == 1


def test_redirects_to_other_hosts_are_not_followed(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    lookup = service(tmp_path, handler)
    assert lookup.branch("DOI: 10.1234/example")["state"] == "error"
    assert len(calls) == 1
