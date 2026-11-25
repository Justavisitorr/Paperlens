"""Public bibliography metadata is resolved conservatively, without paid calls."""

import json
import threading
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_conversation import make_report

from paperlens import reference_links
from paperlens.models import ReadingDocument, ReadingItem, ReadingPart
from paperlens.reference_links import resolve_references

CITATION = (
    "ADOMAVICIUS G, TUZHILIN A. Context-aware recommender systems[M]. US: Springer, 2015: 191-226."
)
DOI = "10.1007/978-1-4899-7637-6_6"


def make_references(texts, *, source_texts=None):
    report = make_report("References", "a", source_texts or texts)
    report.reading = ReadingDocument(
        source_version="test",
        items=[],
        references=[
            ReadingItem(
                id=f"r{i}",
                kind="reference",
                content=text,
                reference_number=str(i),
                parts=[ReadingPart(block_id=f"b{i}", end=len(source.encode("utf-16-le")) // 2)],
            )
            for i, (text, source) in enumerate(zip(texts, source_texts or texts, strict=True), 1)
        ],
    )
    return report


def work(
    *,
    doi=DOI,
    title="Context-aware recommender systems",
    year=2015,
    family="Adomavicius",
    pages="191-226",
):
    return {
        "DOI": doi,
        "title": [title],
        "author": [{"family": family}],
        "published": {"date-parts": [[year]]},
        "page": pages,
        "type": "book-chapter",
    }


def metadata_client(items, requests):
    def handle(request):
        requests.append(request)
        assert request.url.host == "api.crossref.org"
        assert request.extensions["timeout"]["read"] == 8
        return httpx.Response(200, json={"message": {"items": items}})

    return httpx.Client(transport=httpx.MockTransport(handle))


def test_same_title_year_and_pages_select_correct_chapter_and_keep_evidence(tmp_path):
    report = make_references([CITATION], source_texts=["broken OCR bibliography"])
    before = report.model_dump()
    requests = []
    with metadata_client(
        [
            work(year=2008, doi="10.1145/1111111.2222222"),
            work(year=2010, doi="10.1007/old"),
            work(pages="1-35", doi="10.1007/wrong-pages"),
            work(),
        ],
        requests,
    ) as client:
        audit = resolve_references(report, tmp_path, client=client)
    assert len(requests) == audit["requests"] == 1
    assert requests[0].url.params["query.bibliographic"] == CITATION
    assert report.reading.references[0].href == f"https://doi.org/{DOI}"
    entry = audit["entries"][0]
    assert entry["state"] == "resolved"
    assert entry["matched_first_author"] == "Adomavicius"
    assert entry["matched_year"] == 2015 and entry["matched_pages"] == "191-226"
    assert entry["source_urls"][0].startswith("https://api.crossref.org/works?")
    after = report.model_dump()
    after["reading"]["references"][0]["href"] = ""
    assert after == before


@pytest.mark.parametrize(
    "candidate",
    [
        work(title="Recommender systems"),
        work(title="Context-aware recommender systems: A survey"),
        work(family="Tuzhilin"),
        work(year=2010),
        work(pages="1-35"),
        work(doi="not-a-doi"),
    ],
)
def test_partial_title_wrong_author_year_pages_or_doi_never_get_link(tmp_path, candidate):
    report = make_references([CITATION])
    with metadata_client([candidate], []) as client:
        audit = resolve_references(report, tmp_path, client=client)
    assert report.reading.references[0].href == ""
    assert audit["entries"][0]["state"] == "unmatched"


def test_ambiguous_complete_matches_remain_empty_and_duplicate_dois_are_one_match(tmp_path):
    report = make_references([CITATION])
    with metadata_client([work(), work(doi="10.1007/different-edition")], []) as client:
        audit = resolve_references(report, tmp_path, client=client)
    assert audit["entries"][0]["state"] == "ambiguous"
    assert report.reading.references[0].href == ""
    with metadata_client([work(), work()], []) as client:
        audit = resolve_references(report, tmp_path / "another-cache", client=client)
    assert audit["entries"][0]["state"] == "resolved"


def test_existing_and_printed_destinations_are_preserved_without_network(tmp_path):
    report = make_references(
        [
            CITATION,
            CITATION + f" DOI: {DOI}.",
            "SMITH J. Paper[J]. 2020. https://example.org/paper.",
        ],
        source_texts=[CITATION, "source text", "source text"],
    )
    report.reading.references[0].href = "https://publisher.example/source"
    audit = resolve_references(report, tmp_path, allow_network=False)
    assert [entry["state"] for entry in audit["entries"]] == ["existing", "direct", "direct"]
    assert [item.href for item in report.reading.references] == [
        "https://publisher.example/source",
        f"https://doi.org/{DOI}",
        "https://example.org/paper",
    ]
    assert audit["requests"] == 0


def test_same_doi_in_different_case_is_one_destination_without_network(tmp_path):
    text = (
        "Many Authors. Phi-3 technical report. 2024. "
        "doi: 10.48550/ARXIV.2404.14219. "
        "URL https://doi.org/10.48550/arXiv.2404.14219."
    )
    report = make_references([text])
    audit = resolve_references(report, tmp_path, allow_network=False)
    assert audit["requests"] == 0
    assert audit["entries"][0]["state"] == "direct"
    assert report.reading.references[0].href == "https://doi.org/10.48550/ARXIV.2404.14219"
    assert report.reading.references[0].content == text


def test_tex_url_wrap_and_broken_or_unsafe_destinations(tmp_path):
    report = make_references([
        "A paper. \\url{https://doi.org/10.48550/\n  arXiv.2404.14219}.",
        "An incomplete identifier: https://doi.org/10.1234/\n remainder",
        "An ambiguous line break doi:10.1234/partial\n continuation",
        "An ambiguous line break https://example.org/partial\n continuation",
        "Truncated citation: https://example.org/incomplete...",
        "Sensitive destination https://user:password@example.org/private.",
        "Unsafe command 10.1234/\\input{file}.",
    ])
    audit = resolve_references(report, tmp_path, allow_network=False)
    assert report.reading.references[0].href == "https://doi.org/10.48550/arXiv.2404.14219"
    assert all(not item.href for item in report.reading.references[1:])
    assert audit["requests"] == 0


def test_doi_in_search_or_other_url_path_does_not_become_a_doi_destination(tmp_path):
    report = make_references([
        "A search https://search.crossref.org/?q=10.1234/not-a-cited-doi",
        "An article at https://dl.acm.org/doi/10.1145/1234567.",
    ])
    audit = resolve_references(report, tmp_path, allow_network=False)
    assert audit["entries"][0]["state"] == "search"
    assert report.reading.references[0].href.startswith("https://search.crossref.org/")
    assert report.reading.references[1].href == "https://dl.acm.org/doi/10.1145/1234567"


def test_explicit_arxiv_and_balanced_doi_are_linked_without_lookup(tmp_path):
    report = make_references([
        "H. Assumpção. Codeevolve. arXiv:2510.14150, 2025.",
        "Older preprint arXiv:hep-th/9901001v2.",
        "A paper (doi:10.1234/a(b)).",
        "Incomplete identifier arXiv:2026.11111.",
    ])
    audit = resolve_references(report, tmp_path, allow_network=False)
    assert [item.href for item in report.reading.references] == [
        "https://arxiv.org/abs/2510.14150",
        "https://arxiv.org/abs/hep-th/9901001v2",
        "https://doi.org/10.1234/a(b)",
        "",
    ]
    assert [entry["state"] for entry in audit["entries"]] == [
        "direct", "direct", "direct", "insufficient_metadata",
    ]
    assert audit["requests"] == 0


def test_search_destinations_and_multiple_identifiers_are_not_mislabelled(tmp_path):
    report = make_references([
        "An existing search.",
        "Search at https://scholar.google.com/scholar?q=paper.",
        "Two identifiers: 10.1234/a and 10.1234/b.",
        "Two preprints: arXiv:2510.14150 and arXiv:2511.02864.",
    ])
    report.reading.references[0].href = "https://search.crossref.org/?q=title"
    audit = resolve_references(report, tmp_path, allow_network=False)
    assert [entry["state"] for entry in audit["entries"]] == [
        "existing_search", "search", "ambiguous", "ambiguous",
    ]
    assert report.reading.references[0].href == "https://search.crossref.org/?q=title"
    assert report.reading.references[1].href == "https://scholar.google.com/scholar?q=paper"
    assert not report.reading.references[2].href and not report.reading.references[3].href


def test_semantic_reference_does_not_borrow_neighbor_doi_from_broad_evidence(tmp_path):
    source = CITATION + " DOI: 10.1007/a-neighbor-reference."
    report = make_references([CITATION], source_texts=[source])
    with metadata_client([work()], []) as client:
        audit = resolve_references(report, tmp_path, client=client)
    assert audit["entries"][0]["state"] == "resolved"
    assert report.reading.references[0].href == f"https://doi.org/{DOI}"


def test_source_parts_fallback_uses_utf16_offsets(tmp_path):
    report = make_references(["😀 " + CITATION])
    item = report.reading.references[0]
    item.content = None
    item.parts[0].start = 3
    with metadata_client([work()], []) as client:
        audit = resolve_references(report, tmp_path, client=client)
    assert audit["entries"][0]["state"] == "resolved"


def test_versioned_cache_reused_offline_and_changed_content_needs_new_lookup(tmp_path):
    with metadata_client([work()], []) as client:
        resolve_references(make_references([CITATION]), tmp_path, client=client)
    fresh = make_references([CITATION])
    audit = resolve_references(fresh, tmp_path, allow_network=False)
    assert audit["requests"] == 0 and audit["entries"][0]["cached"] is True
    assert fresh.reading.references[0].href == f"https://doi.org/{DOI}"
    changed = make_references([CITATION.replace("2015", "2010")])
    audit = resolve_references(changed, tmp_path, allow_network=False)
    assert audit["entries"][0]["state"] == "offline"
    assert changed.reading.references[0].href == ""


@pytest.mark.parametrize("alteration", ["version", "expired", "invalid_destination", "broken_json"])
def test_corrupt_stale_or_old_version_cache_is_ignored(tmp_path, alteration):
    with metadata_client([work()], []) as client:
        resolve_references(make_references([CITATION]), tmp_path, client=client)
    path = next((tmp_path / "reference-links").glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    if alteration == "version":
        payload["version"] = "old"
    elif alteration == "expired":
        payload["result"]["checked_at"] = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    elif alteration == "invalid_destination":
        payload["result"]["href"] = "https://unverified.example"
    path.write_text(
        "{broken" if alteration == "broken_json" else json.dumps(payload), encoding="utf-8"
    )
    fresh = make_references([CITATION])
    audit = resolve_references(fresh, tmp_path, allow_network=False)
    assert audit["entries"][0]["state"] == "offline"
    assert fresh.reading.references[0].href == ""


@pytest.mark.parametrize("failure", ["timeout", "http", "malformed"])
def test_network_failures_do_not_abort_or_poison_retry_cache(tmp_path, failure):
    def handle(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        if failure == "http":
            return httpx.Response(503)
        return httpx.Response(200, json={"message": None})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        audit = resolve_references(make_references([CITATION]), tmp_path, client=client)
    assert audit["entries"][0]["state"] == "network_error"
    assert not list(tmp_path.rglob("*.json"))
    with metadata_client([work()], []) as client:
        audit = resolve_references(make_references([CITATION]), tmp_path, client=client)
    assert audit["entries"][0]["state"] == "resolved"


def test_duplicate_references_share_one_request_and_cap_leaves_rest_pending(tmp_path):
    report = make_references([CITATION, CITATION, CITATION.replace("2015", "2014")])
    requests = []
    with metadata_client([work()], requests) as client:
        audit = resolve_references(report, tmp_path, max_requests=1, client=client)
    assert len(requests) == audit["requests"] == 1
    assert [entry["state"] for entry in audit["entries"]] == [
        "resolved",
        "resolved",
        "request_limit",
    ]


def test_hard_request_and_concurrency_limits(tmp_path):
    report = make_references([CITATION.replace("2015", str(1950 + i)) for i in range(25)])
    lock = threading.Lock()
    active = maximum = count = 0

    def handle(request):
        nonlocal active, maximum, count
        with lock:
            active += 1
            count += 1
            maximum = max(active, maximum)
        time.sleep(0.01)
        with lock:
            active -= 1
        return httpx.Response(200, json={"message": {"items": []}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        audit = resolve_references(report, tmp_path, max_requests=100, client=client)
    assert count == audit["requests"] == 20
    assert maximum <= reference_links.MAX_CONCURRENCY == 3
    assert [entry["state"] for entry in audit["entries"]].count("request_limit") == 5


def test_missing_year_and_disabled_network_never_make_requests(tmp_path):
    report = make_references(["SMITH J. Incomplete title[J].", CITATION])
    audit = resolve_references(report, tmp_path, allow_network=False)
    assert audit["requests"] == 0
    assert [entry["state"] for entry in audit["entries"]] == ["insufficient_metadata", "offline"]
