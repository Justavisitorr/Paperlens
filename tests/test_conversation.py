"""Offline tests for cross-paper questions and stored conversations (no paid calls)."""

import json

import httpx
import pytest
from pydantic import ValidationError as SchemaError

from paperlens import conversation
from paperlens.config import ModelRoute, Settings
from paperlens.conversation import ConversationService
from paperlens.errors import ValidationError
from paperlens.models import (
    Block,
    Box,
    DashboardReport,
    Evidence,
    Page,
    PaperAnalysis,
    PaperDocument,
    Quality,
    RunInfo,
)
from paperlens.provider import JsonProvider


def make_report(title, sha, texts):
    box = Box(x0=0, y0=0, x1=1, y1=0.3)
    blocks = [
        Block(id=f"b{i}", page=1, order=i, bbox=box, section="方法", text=t, raw_text=t)
        for i, t in enumerate(texts, start=1)
    ]
    return DashboardReport(
        document=PaperDocument(
            title=title,
            source_name="p.pdf",
            sha256=sha * 64,
            parser="test",
            pages=[Page(number=1, width=600, height=800)],
            blocks=blocks,
        ),
        analysis=PaperAnalysis(
            title=title, summary=f"{title}的机器摘要", research_question="", nodes=[], edges=[]
        ),
        evidence=[
            Evidence(
                id="known",
                block_id="b1",
                page=1,
                bbox=box,
                section="方法",
                quote=texts[0],
                match="exact",
            )
        ],
        graphs=[],
        previews=[],
        quality=Quality(
            total_pages=1, parsed_pages=1, text_chars=60, grounded_nodes=0, total_nodes=0
        ),
        run=RunInfo(
            id="t", created_at="2026-09-27T00:00:00Z", model="m", parser="test", status="complete"
        ),
    )


@pytest.fixture
def reports():
    return {
        "bert": make_report(
            "BERT", "a", ["BERT 使用掩码语言模型进行双向预训练。", "微调时所有参数都参与更新。"]
        ),
        "gpt": make_report("GPT", "b", ["GPT 使用从左到右的单向语言模型进行预训练。"]),
    }


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        llm_api_key="fake-private-key-for-test",
        paperlens_cache_dir=tmp_path / "cache",
        llm_model="reader-model",
    )


def transport(monkeypatch, response):
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": json.dumps(response)}}
                ],
                "usage": {"prompt_tokens": 40, "completion_tokens": 20},
            },
        )

    monkeypatch.setattr(
        conversation,
        "JsonProvider",
        lambda config: JsonProvider(
            config,
            transport=httpx.MockTransport(handle),
            sleeper=lambda _: None,
        ),
    )
    return requests


def ask(tmp_path, reports, settings, **payload):
    return ConversationService(tmp_path).ask(
        reports,
        settings,
        {"question": "两篇论文的预训练方式有什么不同？", "papers": ["bert", "gpt"], **payload},
    )


def test_quotes_from_several_papers_are_matched_in_their_own_paper(
    tmp_path, reports, settings, monkeypatch
):
    requests = transport(
        monkeypatch,
        {
            "answer": "BERT 双向 [1]，GPT 单向 [2]。",
            "quotes": [
                {"paper": "P1", "block_id": "b1", "quote": "BERT 使用掩码语言模型进行双向预训练。"},
                {"paper": "P2", "block_id": "b1", "quote": "从左到右的单向语言模型"},
            ],
        },
    )
    reply = ask(tmp_path, reports, settings)
    assert reply["grounded"] is True
    assert [(q["n"], q["paper_id"]) for q in reply["quotes"]] == [(1, "bert"), (2, "gpt")]
    assert reply["quotes"][0]["report_evidence_id"] == "known"
    assert reply["quotes"][1]["report_evidence_id"] is None
    assert [p["ref"] for p in reply["papers"]] == ["P1", "P2"]
    sent = json.loads(requests[0]["messages"][-1]["content"])
    assert {b["paper"] for b in sent["blocks"]} == {"P1", "P2"}


@pytest.mark.parametrize(
    "quote",
    [
        # Real text, but credited to the other paper.
        {"paper": "P2", "block_id": "b1", "quote": "BERT 使用掩码语言模型进行双向预训练。"},
        # The user's own term note is context, never evidence.
        {"paper": "P1", "block_id": "b1", "quote": "我自己写的术语笔记内容"},
        {"paper": "P9", "block_id": "b1", "quote": "BERT 使用掩码语言模型进行双向预训练。"},
    ],
)
def test_misattributed_or_unsourced_quotes_withhold_the_answer(
    tmp_path, reports, settings, monkeypatch, quote
):
    transport(monkeypatch, {"answer": "看似合理的回答。", "quotes": [quote]})
    reply = ask(
        tmp_path, reports, settings, terms=[{"term": "MLM", "note": "我自己写的术语笔记内容"}]
    )
    assert reply["grounded"] is False
    assert reply["quotes"] == []
    assert "未展示" in reply["warnings"][0]


def test_passages_bind_to_a_chosen_paper_before_any_call(tmp_path, reports, settings, monkeypatch):
    monkeypatch.setattr(conversation, "JsonProvider", lambda _: pytest.fail("paid call"))
    passage = {"paper_id": "bert", "block_id": "b1", "start": 0, "end": 4, "text": "BERT"}
    with pytest.raises(SchemaError):
        ask(tmp_path, reports, settings, papers=["gpt"], passages=[passage])
    with pytest.raises(ValidationError):
        ask(tmp_path, reports, settings, passages=[{**passage, "text": "GPT"}])


def test_picked_passage_leads_the_context(tmp_path, reports, settings, monkeypatch):
    requests = transport(monkeypatch, {"answer": "依据不足。", "quotes": []})
    passage = {"paper_id": "bert", "block_id": "b2", "start": 0, "end": 2, "text": "微调"}
    reply = ask(tmp_path, reports, settings, passages=[passage])
    sent = json.loads(requests[0]["messages"][-1]["content"])
    assert sent["blocks"][0]["block_id"] == "b2"
    assert sent["passages"] == [{"paper": "P1", "block_id": "b2", "text": "微调"}]
    assert reply["grounded"] is False  # No quotes: nothing shown as supported.


def test_oversized_request_fails_before_spending(tmp_path, reports, settings, monkeypatch):
    monkeypatch.setattr(conversation, "JsonProvider", lambda _: pytest.fail("paid call"))
    small = settings.model_copy(update={"llm_max_input_chars": 2000})
    history = [{"role": "user", "content": "长" * 1500}]
    with pytest.raises(ValidationError):
        ask(tmp_path, reports, small, history=history)


def test_conversations_round_trip_and_reject_bad_ids(tmp_path):
    store = ConversationService(tmp_path)
    cid = "c" * 32
    doc = {
        "id": cid,
        "title": "对比 BERT 与 GPT",
        "created_at": "2026-09-27T00:00:00Z",
        "refs": [{"kind": "paper", "paper_id": "bert"}, {"kind": "term", "term": "MLM"}],
        "messages": [
            {
                "id": "m1",
                "role": "user",
                "content": "有什么不同？",
                "created_at": "2026-09-27T00:00:00Z",
            }
        ],
    }
    saved = store.save(cid, doc)
    assert store.get(cid)["updated_at"] == saved["updated_at"]
    [summary] = store.list_all()
    assert summary["paper_ids"] == ["bert"] and summary["preview"] == "有什么不同？"
    with pytest.raises(ValidationError):
        store.save("d" * 32, doc)
    with pytest.raises(ValidationError):
        store.get("../../etc")
    with pytest.raises(SchemaError):
        store.save(cid, {**doc, "unexpected": 1})
    assert store.delete(cid) and not store.delete(cid)
    assert store.list_all() == []


def test_http_routes_store_threads_and_bind_papers(tmp_path, reports, monkeypatch):
    from fastapi.testclient import TestClient

    from paperlens import server
    from paperlens.storage import write_json

    settings = Settings(
        _env_file=None,
        llm_api_key="test-hidden-key",
        paperlens_cache_dir=tmp_path / "cache",
        paperlens_output_dir=tmp_path / "outputs",
    )
    write_json(
        settings.paperlens_output_dir / "bert" / "report.json",
        reports["bert"].model_dump(mode="json"),
    )
    monkeypatch.setattr(conversation, "JsonProvider", lambda _: pytest.fail("paid call"))
    cid = "e" * 32
    doc = {"id": cid, "title": "t", "created_at": "2026-09-27T00:00:00Z", "messages": []}
    with TestClient(server.create_app(settings)) as client:
        features = set(client.get("/api/health").json()["features"])
        assert {"chat.papers", "conversations"} <= features
        paper = client.get("/api/library").json()["papers"][0]["id"]
        missing = client.post("/api/chat", json={"question": "?", "papers": [paper, "nope"]})
        assert missing.status_code == 404
        assert (
            client.post("/api/chat", json={"question": " ", "papers": [paper]}).status_code == 422
        )
        assert client.put(f"/api/conversations/{cid}", json=doc).status_code == 200
        assert client.get("/api/conversations").json()["conversations"][0]["id"] == cid
        assert client.get(f"/api/conversations/{cid}").json()["title"] == "t"
        assert client.put(f"/api/conversations/{'f' * 32}", json=doc).status_code == 422
        assert client.delete(f"/api/conversations/{cid}").json() == {"deleted": True}
        assert client.get(f"/api/conversations/{cid}").status_code == 404


@pytest.mark.parametrize("role", ["chat", "conversation"])
def test_cross_paper_chat_uses_assigned_chat_model(tmp_path, reports, settings, monkeypatch, role):
    route = ModelRoute(
        account_id="chat-account",
        model="aux-chat-model",
        base_url="https://chat.test/v1",
        api_key="aux-key",
    )
    routed = settings.model_copy(update={"llm_routes": {role: route}})
    requests = transport(
        monkeypatch,
        {
            "answer": "双向预训练。",
            "quotes": [
                {"paper": "P1", "block_id": "b1", "quote": "BERT 使用掩码语言模型进行双向预训练。"}
            ],
        },
    )
    reply = ask(tmp_path, reports, routed, model_role=role)
    assert reply["model"] == "aux-chat-model"
    assert requests[0]["model"] == "aux-chat-model"
    assert requests[0]["max_tokens"] == settings.llm_max_output_tokens
    assert reply["grounded"]


def reading_report(reports):
    from paperlens.models import ReadingDocument, ReadingItem, ReadingPart, TableCell, TableGrid

    report = reports["bert"]
    report.reading = ReadingDocument(
        version="3",
        source_version="v1",
        items=[
            ReadingItem(
                id="before",
                kind="paragraph",
                content="公式之前的完整说明。",
                parts=[ReadingPart(block_id="b1", end=22)],
            ),
            ReadingItem(
                id="math",
                kind="formula",
                content=r"\sum_{i=1}^{n} x_i^2 = \alpha + \beta",
                parts=[
                    ReadingPart(block_id="b1", end=22),
                    ReadingPart(block_id="b2", end=15),
                ],
            ),
            ReadingItem(
                id="after",
                kind="paragraph",
                content="公式之后的完整限定条件。",
                parts=[ReadingPart(block_id="b2", end=15)],
            ),
            ReadingItem(
                id="grid",
                kind="table",
                content="完整表格标题",
                table=TableGrid(
                    rows=1,
                    cols=1,
                    cells=[
                        TableCell(
                            text="全部表格数据", row_start=0, row_end=1, col_start=0, col_end=1
                        )
                    ],
                ),
            ),
        ],
    )
    return report


def test_reading_item_chat_sends_whole_math_grid_and_neighbors(tmp_path, reports, settings):
    report = reading_report(reports)
    selected = [
        {"paper_id": "bert", "item_id": item, "source_version": "v1"} for item in ["math", "grid"]
    ]
    prepared = ConversationService(tmp_path)._prepare(
        reports,
        settings,
        {
            "question": "解释这个公式与表格",
            "papers": ["bert"],
            "reading_items": selected,
        },
    )
    body = json.loads(prepared[4])
    formula, grid = body["reading_context"]
    assert formula["content"] == report.reading.items[1].content
    assert formula["source_block_ids"] == ["b1", "b2"]
    assert [p["content"] for p in formula["neighbor_paragraphs"]] == [
        "公式之前的完整说明。",
        "公式之后的完整限定条件。",
    ]
    cell = dict(zip(grid["table"]["cell_fields"], grid["table"]["cells"][0], strict=True))
    assert cell["text"] == "全部表格数据"
    assert {b["block_id"] for b in body["blocks"]} == {"b1", "b2"}
    assert body["blocks"][0]["text"] == report.document.blocks[0].text
    assert not formula["transcription_verified"]


@pytest.mark.parametrize("item_id,version", [("missing", "v1"), ("math", "stale")])
def test_reading_item_is_validated_before_any_model_call(
    tmp_path, reports, settings, monkeypatch, item_id, version
):
    reading_report(reports)
    monkeypatch.setattr(conversation, "JsonProvider", lambda _: pytest.fail("paid call"))
    with pytest.raises(ValidationError):
        ask(
            tmp_path,
            reports,
            settings,
            reading_items=[{"paper_id": "bert", "item_id": item_id, "source_version": version}],
        )
    with pytest.raises(SchemaError):
        ask(
            tmp_path,
            reports,
            settings,
            papers=["gpt"],
            reading_items=[{"paper_id": "bert", "item_id": "math", "source_version": "v1"}],
        )


def test_whole_math_context_counts_against_budget_without_truncation(
    tmp_path, reports, settings, monkeypatch
):
    report = reading_report(reports)
    report.reading.items[1].content = r"x_i + " * 1600 + "TAIL_MUST_SURVIVE"
    monkeypatch.setattr(conversation, "JsonProvider", lambda _: pytest.fail("paid call"))
    selected = [{"paper_id": "bert", "item_id": "math", "source_version": "v1"}]
    with pytest.raises(ValidationError):
        ask(
            tmp_path,
            reports,
            settings.model_copy(update={"llm_max_input_chars": 2000}),
            reading_items=selected,
        )
    prepared = ConversationService(tmp_path)._prepare(
        reports,
        settings.model_copy(update={"llm_max_input_chars": 20000}),
        {
            "question": "解释公式",
            "papers": ["bert"],
            "reading_items": selected,
        },
    )
    assert json.loads(prepared[4])["reading_context"][0]["content"].endswith("TAIL_MUST_SURVIVE")


def test_rendered_formula_cannot_be_quoted_as_raw_source(tmp_path, reports, settings, monkeypatch):
    report = reading_report(reports)
    transport(
        monkeypatch,
        {
            "answer": "看起来有依据 [1]",
            "quotes": [
                {
                    "paper": "P1",
                    "block_id": "b1",
                    "quote": report.reading.items[1].content,
                }
            ],
        },
    )
    reply = ask(
        tmp_path,
        reports,
        settings,
        reading_items=[{"paper_id": "bert", "item_id": "math", "source_version": "v1"}],
    )
    assert not reply["grounded"] and reply["quotes"] == []


def test_default_chat_payload_is_unchanged(tmp_path, reports, settings):
    prepared = ConversationService(tmp_path)._prepare(
        reports, settings, {"question": "解释", "papers": ["bert"]}
    )
    assert "reading_context" not in json.loads(prepared[4])
