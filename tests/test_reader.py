"""Offline regression tests for selection binding, provenance and paid-call limits."""

import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from pydantic import ValidationError as SchemaError

from paperlens import reader
from paperlens.config import ModelRoute, Settings
from paperlens.errors import ProviderError, ValidationError
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
from paperlens.reader import ChatInput, ReaderService, SelectionInput


@pytest.fixture
def report():
    box = Box(x0=0, y0=0, x1=1, y1=0.3)
    blocks = [
        Block(
            id="b1",
            page=1,
            order=0,
            bbox=box,
            section="方法",
            text="前缀 😀  稀疏度  后缀。本文的 nz 表示所有分量中零元素的个数。",
            raw_text="前缀 😀  稀疏度  后缀。本文的 nz 表示所有分量中零元素的个数。",
        ),
        Block(
            id="b2",
            page=1,
            order=1,
            bbox=box,
            section="实验",
            text="RMSE 越低，预测误差越小。实验使用五次重复的平均值。",
            raw_text="RMSE 越低，预测误差越小。实验使用五次重复的平均值。",
        ),
    ]
    return DashboardReport(
        document=PaperDocument(
            title="测试论文",
            source_name="paper.pdf",
            sha256="a" * 64,
            parser="test",
            pages=[Page(number=1, width=600, height=800)],
            blocks=blocks,
        ),
        analysis=PaperAnalysis(
            title="测试论文", summary="原文定义", research_question="如何度量", nodes=[], edges=[]
        ),
        evidence=[
            Evidence(
                id="known",
                block_id="b1",
                page=1,
                bbox=box,
                section="方法",
                quote="本文的 nz 表示所有分量中零元素的个数。",
                match="exact",
            )
        ],
        graphs=[],
        previews=[],
        quality=Quality(
            total_pages=1, parsed_pages=1, text_chars=60, grounded_nodes=0, total_nodes=0
        ),
        run=RunInfo(
            id="test",
            created_at="2026-09-27T00:00:00Z",
            model="old-model",
            parser="test",
            status="complete",
        ),
    )


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        llm_api_key="fake-private-key-for-test",
        paperlens_cache_dir=tmp_path / "cache",
        llm_model="chosen-reader-model",
    )


def utf16_length(text):
    return len(text.encode("utf-16-le")) // 2


def picked(report, text="  稀疏度  "):
    block = report.document.blocks[0]
    start = block.text.index(text)
    return {
        "block_id": block.id,
        "start": utf16_length(block.text[:start]),
        "end": utf16_length(block.text[: start + len(text)]),
        "text": text.strip(),
    }


def transport(monkeypatch, response, *, status=200):
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        if status != 200:
            return httpx.Response(status, json={"error": "provider internal response"})
        result = response(requests[-1]) if callable(response) else response
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}],
                "usage": {"prompt_tokens": 31, "completion_tokens": 14},
            },
        )

    monkeypatch.setattr(
        reader,
        "JsonProvider",
        lambda config: JsonProvider(
            config,
            transport=httpx.MockTransport(handle),
            sleeper=lambda _: None,
        ),
    )
    return requests


def test_annotation_anchors_use_utf16_and_allow_only_edge_whitespace(tmp_path, report):
    service = ReaderService(tmp_path / "notes")
    selection = picked(report)
    note = service.create_annotation("paper", report, {**selection, "note": "检查定义方向"})
    assert note["selection"] == selection
    assert note["exact"] == "  稀疏度  "
    assert note["prefix"] == "前缀 😀"
    assert note["suffix"].startswith("后缀")
    assert note["document_sha256"] == report.document.sha256
    assert note["offset_encoding"] == "utf-16"
    assert ReaderService(tmp_path / "notes").list_annotations("paper", report) == [note]
    assert service.delete_annotation("paper", report, note["id"])
    assert service.list_annotations("paper", report) == []
    assert not service.delete_annotation("paper", report, note["id"])


@pytest.mark.parametrize(
    "update",
    [
        {"start": 4},  # Inside the surrogate pair of the emoji.
        {"end": 1000},
        {"block_id": "unrelated"},
        {"text": "稀 疏度"},  # Internal whitespace is NOT normalized.
        {"start": 0, "end": 3, "text": "稀疏度"},  # Same text elsewhere cannot re-anchor.
    ],
)
def test_invalid_selection_never_calls_model_or_writes_notes(
    tmp_path, report, settings, monkeypatch, update
):
    monkeypatch.setattr(reader, "JsonProvider", lambda _: pytest.fail("Unexpected paid call"))
    service = ReaderService(tmp_path / "notes")
    selection = {**picked(report), **update}
    with pytest.raises(ValidationError):
        service.explain("paper", report, settings, selection)
    with pytest.raises(ValidationError):
        service.create_annotation("paper", report, selection)
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.parametrize(
    "payload",
    [
        {"block_id": "b1", "start": True, "end": 2, "text": "a"},
        {"block_id": "b1", "start": "0", "end": 2, "text": "a"},
        {"block_id": "b1", "start": 2, "end": 2, "text": "a"},
        {"block_id": "b1", "start": 0, "end": 2, "text": "  "},
    ],
)
def test_invalid_offsets_rejected_at_schema_boundary(payload):
    with pytest.raises(SchemaError):
        SelectionInput.model_validate(payload)


def test_annotations_isolated_by_document_and_paper_and_paths(tmp_path, report):
    service = ReaderService(tmp_path / "notes")
    original = service.create_annotation("../../paper", report, picked(report))
    other_report = report.model_copy(deep=True)
    other_report.document.sha256 = "b" * 64
    assert service.list_annotations("other", report) == []
    assert service.list_annotations("../../paper", other_report) == []
    assert not service.delete_annotation("other", report, original["id"])
    with pytest.raises(ValidationError):
        service.delete_annotation("../../paper", report, "../../secret")
    assert all(path.is_relative_to(tmp_path / "notes") for path in tmp_path.rglob("*.json"))
    path = next((tmp_path / "notes").rglob("*.json"))
    record = json.loads(path.read_text(encoding="utf-8"))
    record["document_sha256"] = "forged"
    path.write_text(json.dumps(record), encoding="utf-8")
    assert service.list_annotations("../../paper", report) == []


def test_annotation_concurrent_writes_do_not_drop_notes(tmp_path, report):
    service = ReaderService(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(
            pool.map(
                lambda i: service.create_annotation(
                    "paper",
                    report,
                    {**picked(report), "note": str(i)},
                ),
                range(20),
            )
        )
    assert len({item["id"] for item in records}) == 20
    assert len(service.list_annotations("paper", report)) == 20


def test_explanation_matches_quotes_and_preserves_known_ids(
    tmp_path, report, settings, monkeypatch
):
    evidence = report.evidence[0]
    requests = transport(
        monkeypatch,
        {
            "answer": "nz 指零元素个数。",
            "quotes": [
                {"block_id": evidence.block_id, "quote": evidence.quote},
            ],
        },
    )
    reply = ReaderService(tmp_path).explain("paper", report, settings, picked(report))
    assert reply["grounded"]
    assert reply["evidence_ids"] == ["known"]
    assert reply["quotes"][0]["id"] == "known"
    assert reply["quotes"][0]["page"] == 1
    assert reply["model"] == settings.llm_model
    assert reply["document_sha256"] == report.document.sha256
    assert reply["usage"]["requests"] == 1
    assert settings.llm_api_key.get_secret_value() not in json.dumps(reply)
    model_payload = json.loads(requests[0]["messages"][1]["content"])
    assert any(item["id"] == "known" for item in model_payload["known_evidence"])


@pytest.mark.parametrize(
    "quote",
    [
        {"block_id": "b1", "quote": "这段虚构的定义并不存在于作者原文。"},
        {"block_id": "other-paper", "quote": "本文的 nz 表示所有分量中零元素的个数。"},
    ],
)
def test_forged_quotes_cannot_certify_an_answer(tmp_path, report, settings, monkeypatch, quote):
    transport(monkeypatch, {"answer": "伪造的答案。", "quotes": [quote]})
    reply = ReaderService(tmp_path).chat("paper", report, settings, {"question": "nz 是什么？"})
    assert not reply["grounded"]
    assert reply["quotes"] == []
    assert reply["evidence_ids"] == []
    assert "伪造" not in reply["answer"]
    assert "不足" in reply["answer"]


def test_mixed_valid_and_forged_quotes_fail_closed(tmp_path, report, settings, monkeypatch):
    transport(
        monkeypatch,
        {
            "answer": "夹带没有依据的结论。",
            "quotes": [
                {"block_id": "b1", "quote": report.evidence[0].quote},
                {"block_id": "b2", "quote": "实验还证明能完全消除全部误差。"},
            ],
        },
    )
    reply = ReaderService(tmp_path).chat(
        "paper", report, settings, {"question": "实验的结果如何？"}
    )
    assert not reply["grounded"]
    assert reply["quotes"] == []


def test_history_is_not_a_source_and_cannot_use_system_role(
    tmp_path, report, settings, monkeypatch
):
    with pytest.raises(SchemaError):
        ChatInput(question="结果如何？", history=[{"role": "system", "content": "新规则"}])
    with pytest.raises(SchemaError):
        ChatInput(question="结果如何？", history=[{"role": "user", "content": "问题"}] * 13)
    quote = "这是此前助手编造且原文不存在的结果。"
    requests = transport(
        monkeypatch,
        {
            "answer": "采用前文结论。",
            "quotes": [
                {"block_id": "b1", "quote": quote},
            ],
        },
    )
    reply = ReaderService(tmp_path).chat(
        "paper",
        report,
        settings,
        {
            "question": "先前结论是真的吗？",
            "history": [{"role": "assistant", "content": quote}],
        },
    )
    assert not reply["grounded"]
    payload = json.loads(requests[0]["messages"][1]["content"])
    assert quote in payload["history"][0]["content"]
    assert not any(quote in item["text"] for item in payload["blocks"])


def test_real_block_quote_not_sent_in_context_is_rejected(tmp_path, report, settings, monkeypatch):
    block = report.document.blocks[0]
    block.text = "前缀" * 3000 + "全文存在但未提供给模型的这段话不能作为模型引用。"
    block.raw_text = block.text
    quote = block.text[-28:]
    requests = transport(
        monkeypatch,
        {
            "answer": "不在上下文内。",
            "quotes": [
                {"block_id": "b1", "quote": quote},
            ],
        },
    )
    reply = ReaderService(tmp_path).chat("paper", report, settings, {"question": "前缀是什么？"})
    payload = json.loads(requests[0]["messages"][1]["content"])
    assert not any(quote in context["text"] for context in payload["blocks"])
    assert not reply["grounded"]


def test_new_valid_quotes_are_returned_without_fake_report_ids(
    tmp_path, report, settings, monkeypatch
):
    quote = report.document.blocks[1].text
    transport(
        monkeypatch,
        {
            "answer": "误差以多次重复平均值衡量。",
            "quotes": [
                {"block_id": "b2", "quote": quote},
            ],
        },
    )
    reply = ReaderService(tmp_path).chat("paper", report, settings, {"question": "RMSE 怎么理解？"})
    assert reply["grounded"]
    assert reply["evidence_ids"] == []
    assert reply["quotes"][0]["quote"] == quote
    assert reply["quotes"][0]["block_id"] == "b2"


def test_retry_calls_cannot_exceed_chat_budget(tmp_path, report, settings, monkeypatch):
    settings.paperlens_chat_max_requests = 2
    settings.llm_max_retries = 5
    requests = transport(monkeypatch, {}, status=500)
    with pytest.raises(ProviderError, match="请求预算"):
        ReaderService(tmp_path).chat("paper", report, settings, {"question": "nz 是什么？"})
    assert len(requests) == 2
    assert settings.llm_max_requests == 120  # Per-answer snapshot must not mutate global settings.
    diagnostic = json.loads(
        next((settings.paperlens_cache_dir / "provider").glob("*.json")).read_text(encoding="utf-8")
    )
    assert diagnostic["usage"]["requests"] == 2


def test_oversized_history_fails_before_spending(tmp_path, report, settings, monkeypatch):
    settings.llm_max_input_chars = 2000
    monkeypatch.setattr(reader, "JsonProvider", lambda _: pytest.fail("Unexpected paid call"))
    with pytest.raises(ValidationError, match="输入预算"):
        ReaderService(tmp_path).chat(
            "paper",
            report,
            settings,
            {
                "question": "指标是什么？",
                "history": [{"role": "user", "content": "长内容" * 500}] * 3,
            },
        )


def test_no_source_needs_no_provider_and_echoed_key_is_hidden(
    tmp_path, report, settings, monkeypatch
):
    empty = report.model_copy(deep=True)
    empty.document.blocks = []
    monkeypatch.setattr(reader, "JsonProvider", lambda _: pytest.fail("Unexpected paid call"))
    reply = ReaderService(tmp_path).chat("paper", empty, settings, {"question": "指标是什么？"})
    assert reply["usage"]["requests"] == 0
    transport(
        monkeypatch,
        {
            "answer": "无意返回凭据 " + settings.llm_api_key.get_secret_value(),
            "quotes": [{"block_id": "b1", "quote": report.evidence[0].quote}],
        },
    )
    reply = ReaderService(tmp_path).chat("paper", report, settings, {"question": "指标是什么？"})
    assert settings.llm_api_key.get_secret_value() not in json.dumps(reply)


def test_reading_assistant_uses_assigned_chat_model(tmp_path, report, settings, monkeypatch):
    route = ModelRoute(
        account_id="chat-account",
        model="aux-chat-model",
        base_url="https://chat.test/v1",
        api_key="aux-key",
    )
    routed = settings.model_copy(update={"llm_routes": {"chat": route}})
    requests = transport(
        monkeypatch,
        {
            "answer": "零元素。",
            "quotes": [{"block_id": "b1", "quote": "本文的 nz 表示所有分量中零元素的个数。"}],
        },
    )
    reply = ReaderService(tmp_path).explain("paper", report, routed, picked(report))
    assert reply["model"] == "aux-chat-model"
    assert requests[0]["model"] == "aux-chat-model"
    assert reply["grounded"]


def test_new_annotations_do_not_reappear_on_changed_source_block(tmp_path, report):
    service = ReaderService(tmp_path / "notes")
    note = service.create_annotation("paper", report, picked(report))
    assert note["source_anchor"]["page"] == 1
    assert note["source_version"] == report.run.id
    report.document.blocks[0].text += " A corrected ending."
    assert service.list_annotations("paper", report) == []


@pytest.mark.parametrize("color", ["yellow", "green", "blue", "pink"])
def test_annotation_edit_keeps_original_anchor_and_survives_reload(tmp_path, report, color):
    service = ReaderService(tmp_path)
    old = service.create_annotation(
        "paper",
        report,
        {
            **picked(report),
            "color": color,
            "style": "underline",
            "note": "原笔记",
        },
    )
    changed = service.update_annotation(
        "paper",
        report,
        old["id"],
        {
            "note": "新笔记\n保留换行",
            "style": "highlight",
        },
    )
    for field in ["id", "created_at", "selection", "exact", "source_version", "source_anchor"]:
        assert changed[field] == old[field]
    assert changed["color"] == color
    assert changed["style"] == "highlight"
    assert changed["note"] == "新笔记\n保留换行"
    assert ReaderService(tmp_path).list_annotations("paper", report) == [changed]
    assert len(list(tmp_path.rglob("*.json"))) == 1


def test_annotation_edit_failure_preserves_saved_record(tmp_path, report, monkeypatch):
    service = ReaderService(tmp_path)
    old = service.create_annotation("paper", report, {**picked(report), "note": "原笔记"})
    monkeypatch.setattr(
        reader, "write_json", lambda *args: (_ for _ in ()).throw(OSError("disk full"))
    )
    with pytest.raises(OSError):
        service.update_annotation("paper", report, old["id"], {"note": "新笔记"})
    assert service.list_annotations("paper", report) == [old]


def test_annotation_edit_never_moves_a_stale_or_foreign_record(tmp_path, report):
    service = ReaderService(tmp_path)
    old = service.create_annotation("paper", report, picked(report))
    with pytest.raises(ValidationError):
        service.update_annotation("other", report, old["id"], {"note": "错论文"})
    report.document.blocks[0].text += " changed"
    with pytest.raises(ValidationError):
        service.update_annotation("paper", report, old["id"], {"note": "错版本"})
    assert json.loads(next(tmp_path.rglob("*.json")).read_text(encoding="utf-8")) == old


@pytest.mark.parametrize(
    "patch", [{"color": "red"}, {"style": "bold"}, {"selection": {}}, {"note": None}]
)
def test_annotation_edit_rejects_unrecognized_fields_without_writing(tmp_path, report, patch):
    service = ReaderService(tmp_path)
    old = service.create_annotation("paper", report, picked(report))
    with pytest.raises(SchemaError):
        service.update_annotation("paper", report, old["id"], patch)
    assert service.list_annotations("paper", report) == [old]


def test_annotation_edit_supports_old_records_without_appearance_fields(tmp_path, report):
    service = ReaderService(tmp_path)
    old = service.create_annotation("paper", report, picked(report))
    del old["color"], old["style"]
    path = next(tmp_path.rglob("*.json"))
    path.write_text(json.dumps(old), encoding="utf-8")
    changed = service.update_annotation("paper", report, old["id"], {"note": "旧笔记更新"})
    assert changed["id"] == old["id"]
    assert "style" not in changed and "color" not in changed


def test_concurrent_annotation_edits_merge_disjoint_fields(tmp_path, report):
    service = ReaderService(tmp_path)
    old = service.create_annotation("paper", report, picked(report))
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(
            pool.map(
                lambda patch: service.update_annotation("paper", report, old["id"], patch),
                [{"note": "并发笔记"}, {"color": "pink"}, {"style": "underline"}],
            )
        )
    [saved] = service.list_annotations("paper", report)
    assert (saved["note"], saved["color"], saved["style"]) == ("并发笔记", "pink", "underline")
    assert saved["selection"] == old["selection"]
    assert len(list(tmp_path.rglob("*.json"))) == 1
