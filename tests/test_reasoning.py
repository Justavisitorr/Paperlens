"""Fault and data-integrity tests; these do not measure paper-analysis quality."""

import json

import httpx
import pytest

from paperlens.analysis.engine import document_chunks
from paperlens.analysis.evidence import EvidenceRegistry
from paperlens.config import Settings
from paperlens.errors import ProviderError, ProviderOutcomeUnknown
from paperlens.exporting import report_html
from paperlens.graphs import build_views, escape_label
from paperlens.models import Block, Box, Page, PaperAnalysis, PaperDocument, PaperNode
from paperlens.provider import JsonProvider
from paperlens.storage import StageStore, write_json


def sample_document(text="An exact quotation supports the result."):
    return PaperDocument(
        title="Test",
        source_name="test.pdf",
        sha256="abc",
        parser="fixture",
        pages=[Page(number=1, width=600, height=800, text_chars=len(text))],
        blocks=[
            Block(
                id="p1b1",
                page=1,
                text=text,
                raw_text=text,
                bbox=Box(x0=0.1, y0=0.1, x1=0.9, y1=0.2),
                order=0,
            )
        ],
    )


def settings(tmp_path, **kwargs):
    return Settings(
        _env_file=None,
        llm_api_key="test-secret-not-real",
        paperlens_cache_dir=tmp_path / "cache",
        **kwargs,
    )


def response(content, **kwargs):
    return httpx.Response(
        200,
        json={
            "choices": [
                {"message": {"content": content}, "finish_reason": kwargs.get("finish", "stop")}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


def test_quote_must_exist_in_an_allowed_block():
    registry = EvidenceRegistry(sample_document())
    assert registry.resolve("p1b1", "quotation supports the result", {"p1b1"})
    assert registry.resolve("p1b1", "quotation supports a false result", {"p1b1"}) is None
    assert registry.resolve("p1b1", "quotation supports the result", {"different"}) is None
    assert registry.resolve("madeup", "quotation supports the result", {"madeup"}) is None
    assert len(registry.items) == 1


def test_normalized_quote_preserves_punctuation_and_does_not_join_versions():
    registry = EvidenceRegistry(sample_document("The inter-\nvention yields 5.0 percent."))
    eid = registry.resolve("p1b1", "The intervention yields 5.0 percent.", {"p1b1"})
    assert registry.items[eid].match == "normalized"
    assert registry.resolve("p1b1", "The intervention yields 50 percent.", {"p1b1"}) is None
    doc = sample_document("12345678")
    doc.blocks[0].raw_text = "abcdefgh"
    assert EvidenceRegistry(doc).resolve("p1b1", "efgh1234", {"p1b1"}) is None


def test_chunks_cover_every_character_and_keep_late_appendix():
    text = "重要正文" * 3000 + "末尾附录独有结论"
    document = sample_document(text)
    chunks = document_chunks(document, 2000)
    restored = "".join(b["text"] for chunk in chunks for b in chunk)
    assert restored == text
    assert all(b["block_id"] == "p1b1" for chunk in chunks for b in chunk)
    assert len(chunks) > 4


@pytest.mark.parametrize("status", [401, 402, 403, 404])
def test_auth_and_configuration_failures_are_not_retried_or_echoed(tmp_path, status):
    called = []

    def request(req):
        called.append(req)
        return httpx.Response(status, text="test-secret-not-real echoed by bad gateway")

    with JsonProvider(
        settings(tmp_path), transport=httpx.MockTransport(request), sleeper=lambda _: None
    ) as api:
        with pytest.raises(ProviderError) as error:
            api.json("test", "{}")
        assert "test-secret" not in str(error.value)
        assert len(called) == 1


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_failures_retry_and_account_for_attempts(tmp_path, status):
    replies = [httpx.Response(status), response('{"ok":true}')]
    with JsonProvider(
        settings(tmp_path),
        transport=httpx.MockTransport(lambda _: replies.pop(0)),
        sleeper=lambda _: None,
    ) as api:
        assert api.json("test", "{}") == {"ok": True}
        assert api.usage.requests == 2
        assert api.usage.prompt_tokens == 10


def test_truncated_and_invalid_responses_recover_within_budget(tmp_path):
    replies = [
        response('{"truncated":', finish="length"),
        response(""),
        response('```json\n{"ok":true}\n```'),
    ]
    with JsonProvider(
        settings(tmp_path),
        transport=httpx.MockTransport(lambda _: replies.pop(0)),
        sleeper=lambda _: None,
    ) as api:
        assert api.json("test", "{}") == {"ok": True}
        assert api.usage.requests == 3
        assert api.usage.completion_tokens == 15


def test_request_budget_is_hard_bound(tmp_path):
    with JsonProvider(
        settings(tmp_path, llm_max_requests=1),
        transport=httpx.MockTransport(lambda _: response("{}")),
    ) as api:
        api.json("test", "{}")
        with pytest.raises(ProviderError, match="请求预算"):
            api.json("test", "{}")
        assert api.usage.requests == 1


def test_truncation_at_user_output_ceiling_does_not_repeat_identical_paid_attempt(tmp_path):
    calls = []

    def truncated(request):
        calls.append(request)
        return response('{"truncated":', finish="length")

    with JsonProvider(
        settings(tmp_path, llm_max_output_tokens=512),
        transport=httpx.MockTransport(truncated), sleeper=lambda _: None,
    ) as api:
        with pytest.raises(ProviderError, match="输出被截断"):
            api.json("test", "{}", max_tokens=512)
        assert api.usage.requests == len(calls) == 1


@pytest.mark.parametrize(
    "error_type",
    [httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.WriteError,
     httpx.RemoteProtocolError, httpx.ProxyError],
)
def test_uncertain_submission_is_not_retried_and_records_unknown_usage(tmp_path, error_type):
    def timeout(req):
        raise error_type("test-secret-not-real", request=req)

    with JsonProvider(
        settings(tmp_path), transport=httpx.MockTransport(timeout), sleeper=lambda _: None
    ) as api:
        with pytest.raises(ProviderOutcomeUnknown, match="实际用量未知") as error:
            api.json("test", "{}")
        assert "test-secret" not in str(error.value)
        assert error.value.code == "provider_outcome_unknown"
        assert api.usage.requests == 1
    saved = json.loads(next((tmp_path / "cache/provider").glob("*.json")).read_text("utf-8"))
    assert len(saved["events"]) == 1
    assert saved["events"][0]["usage_unknown"] is True
    assert saved["events"][0]["retryable"] is False
    assert saved["events"][0]["error_type"] == error_type.__name__
    assert "test-secret" not in json.dumps(saved)


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
def test_failure_before_submission_can_retry_within_budget(tmp_path, error_type):
    calls = []

    def request(req):
        calls.append(req)
        if len(calls) == 1:
            raise error_type("test-secret-not-real", request=req)
        return response('{"ok":true}')

    with JsonProvider(
        settings(tmp_path), transport=httpx.MockTransport(request), sleeper=lambda _: None,
    ) as api:
        assert api.json("test", "{}") == {"ok": True}
        assert api.usage.requests == len(calls) == 2
    saved = json.loads(next((tmp_path / "cache/provider").glob("*.json")).read_text("utf-8"))
    assert saved["events"][0]["usage_unknown"] is False
    assert saved["events"][0]["retryable"] is True


def test_http_request_timeout_does_not_replay_an_uncertain_submission(tmp_path):
    replies = [httpx.Response(408), response('{"ok":true}')]
    with JsonProvider(
        settings(tmp_path), transport=httpx.MockTransport(lambda _: replies.pop(0)),
        sleeper=lambda _: None,
    ) as api:
        with pytest.raises(ProviderOutcomeUnknown, match="实际用量未知"):
            api.json("test", "{}")
        assert api.usage.requests == 1
        assert len(replies) == 1
    saved = json.loads(next((tmp_path / "cache/provider").glob("*.json")).read_text("utf-8"))
    assert saved["events"][0]["http_status"] == 408
    assert saved["events"][0]["usage_unknown"] is True


def test_cache_rejects_corruption_and_atomic_writes_have_no_temp_files(tmp_path):
    store = StageStore(tmp_path, "key")
    (store.root / "broken.json").write_text('{"bad":', encoding="utf-8")
    assert store.load("broken") is None
    store.save("stage", {"内容": [1, 2]})
    assert store.load("stage") == {"内容": [1, 2]}
    write_json(store.root / "stage.json", {"updated": True})
    assert json.loads((store.root / "stage.json").read_text()) == {"updated": True}
    assert not list(store.root.glob("*.tmp"))


def test_graph_compiler_escapes_content_and_keeps_full_view():
    analysis = PaperAnalysis(
        title="x",
        summary="x",
        research_question="x",
        nodes=[
            PaperNode(id="root", title='A"] --> hacked["', kind="theme", summary="x"),
            PaperNode(id="child", title="child", kind="method", summary="x", parent_id="root"),
            PaperNode(id="leaf", title="leaf", kind="evidence", summary="x", parent_id="child"),
        ],
        edges=[],
    )
    views = build_views(analysis)
    assert set(views[1].node_ids) == {"root", "child", "leaf"}
    assert 'A"] --> hacked["' not in views[1].mermaid
    assert "class nroot pl_root;\nclass nroot kind_theme;" in views[1].mermaid
    assert "#34;" in escape_label('"')
    assert len(analysis.edges) == 2
    build_views(analysis)
    assert len(analysis.edges) == 2


def test_html_data_cannot_close_its_script(monkeypatch):
    class FakeReport:
        def model_dump(self, **kwargs):
            return {"title": '</script><script>alert("x")</script>\u2028'}

    monkeypatch.setattr(
        "paperlens.exporting.get_dashboard_template",
        lambda: '<script type="application/json">__PAPERLENS_DATA__</script>',
    )
    rendered = report_html(FakeReport())
    assert rendered.count("</script>") == 1
    payload = rendered.split(">", 1)[1].rsplit("<", 1)[0]
    assert json.loads(payload)["title"].startswith("</script>")


def test_overview_preserves_primary_branches_before_expanding_limitations():
    root = PaperNode(id="root", title="论文", kind="theme", summary="x")
    branches = [
        PaperNode(id=kind, title=kind, kind=kind, summary="x", parent_id="root")
        for kind in ("question", "method", "evidence", "contribution", "conclusion")
    ]
    limitations = [
        PaperNode(
            id=f"limit{i}", title=f"边界{i}", kind="limitation", summary="x", parent_id="conclusion"
        )
        for i in range(8)
    ]
    analysis = PaperAnalysis(
        title="论文",
        summary="x",
        research_question="x",
        nodes=[root, *branches, *limitations],
        edges=[],
    )
    overview, complete, _ = build_views(analysis)
    assert {"root", *[n.id for n in branches]} <= set(overview.node_ids)
    assert len(overview.node_ids) <= 9
    assert len(complete.node_ids) == len(analysis.nodes)


def test_license_insertion_preserves_bundled_javascript(monkeypatch):
    class FakeReport:
        def model_dump(self, **kwargs):
            return {"title": "test"}

    template = '<body><script>const end = "</body>";</script>__PAPERLENS_DATA__</body>'
    monkeypatch.setattr("paperlens.exporting.get_dashboard_template", lambda: template)
    rendered = report_html(FakeReport())
    assert 'const end = "</body>";' in rendered
    assert rendered.count('id="paperlens-open-source-notices"') == 1
    assert rendered.endswith("</script></body>")


def test_export_tree_layout_preserves_cross_relations_in_report_data():
    from paperlens.graphs import build_views
    from paperlens.models import PaperAnalysis, PaperEdge, PaperNode

    analysis = PaperAnalysis(
        title="布局",
        summary="摘要",
        research_question="问题",
        nodes=[
            PaperNode(id="root", title="主题", kind="theme", summary="主题"),
            PaperNode(id="a", title="方法", kind="method", summary="方法", parent_id="root"),
            PaperNode(id="b", title="局限", kind="limitation", summary="局限", parent_id="root"),
        ],
        edges=[PaperEdge(id="cross", source="b", target="a", relation="limits")],
    )
    graph = build_views(analysis)[1]
    assert "cross" in graph.edge_ids
    assert "nb -.-> na" not in graph.mermaid
    assert "nroot --> na" in graph.mermaid and "nroot --> nb" in graph.mermaid
    assert any(e.id == "cross" for e in analysis.edges)
