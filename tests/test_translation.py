"""Offline tests for on-demand reading translation (no paid calls)."""

import json

import httpx
import pytest
from test_conversation import make_report

from paperlens import translation
from paperlens.config import Settings
from paperlens.control import processing_control
from paperlens.models import ReadingDocument, ReadingItem, ReadingPart
from paperlens.provider import JsonProvider, RequestBudget
from paperlens.translation import TITLE_ID, TranslationService


@pytest.fixture
def report():
    report = make_report("Attention", "c", ["Attention is all you need [1].", "We use $d_k$ keys."])
    report.reading = ReadingDocument(
        source_version="v1",
        items=[
            ReadingItem(id="h1", kind="heading", parts=[ReadingPart(block_id="b1", end=9)]),
            ReadingItem(id="p1", kind="paragraph", parts=[ReadingPart(block_id="b2", end=18)]),
            ReadingItem(id="f1", kind="formula", parts=[ReadingPart(block_id="b2", end=4)]),
        ],
    )
    return report


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        llm_api_key="fake-private-key-for-test",
        paperlens_cache_dir=tmp_path / "cache",
        llm_model="reader-model",
    )


def transport(monkeypatch, reply):
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(reply)}}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 10},
            },
        )

    monkeypatch.setattr(
        translation,
        "JsonProvider",
        lambda config: JsonProvider(
            config, transport=httpx.MockTransport(handle), sleeper=lambda _: None
        ),
    )
    return requests


def test_translation_is_stored_beside_the_report_and_keyed_to_source(
    tmp_path, report, settings, monkeypatch
):
    requests = transport(
        monkeypatch,
        {
            "items": [
                {"id": "p1", "zh": "我们使用 $d_k$ 维的键。"},
                {"id": "h1", "zh": "注意力"},
                {"id": "invented", "zh": "不该出现"},
            ]
        },
    )
    service = TranslationService(tmp_path / "t")
    out = service.translate("paper", report, settings, {"item_ids": ["h1", "p1", "f1"]})
    assert set(out["items"]) == {"h1", "p1"}
    assert out["items"]["p1"]["text"] == "我们使用 $d_k$ 维的键。"
    assert out["warnings"]  # the formula stays in the original
    sent = json.loads(requests[0]["messages"][-1]["content"])
    assert [i["id"] for i in sent["items"]] == ["h1", "p1"]
    assert sent["items"][1]["text"] == "We use $d_k$ keys."
    assert set(service.list("paper", report)["items"]) == {"h1", "p1"}
    # Already translated passages never trigger another request.
    again = service.translate("paper", report, settings, {"item_ids": ["p1"]})
    assert again["items"] == {} and len(requests) == 1
    # Changed source text hides the stale translation.
    report.document.blocks[1].text = "We use $d_v$ values."
    assert set(service.list("paper", report)["items"]) == {"h1"}


def test_title_and_secret_scrubbing(tmp_path, report, settings, monkeypatch):
    transport(monkeypatch, {"items": [{"id": TITLE_ID, "zh": "注意力 fake-private-key-for-test"}]})
    out = TranslationService(tmp_path).translate("p", report, settings, {"item_ids": [TITLE_ID]})
    assert out["items"][TITLE_ID]["text"] == "注意力 [已隐藏凭据]"


def test_nothing_to_translate_makes_no_request(tmp_path, report, settings, monkeypatch):
    monkeypatch.setattr(translation, "JsonProvider", lambda _: pytest.fail("paid call"))
    out = TranslationService(tmp_path).translate("p", report, settings, {"item_ids": ["f1"]})
    assert out["items"] == {} and out["warnings"]


def test_long_passage_is_translated_in_full_and_old_partial_cache_is_ignored(
    tmp_path, report, settings, monkeypatch
):
    source = "a" * 6500 + " final qualifier."
    report.document.blocks[1].text = source
    report.reading.items[1].parts[0].end = len(source)
    service = TranslationService(tmp_path / "translations")
    path = service._path("paper", report)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {"items": {"p1": {"text": "旧的不完整译文", "source": translation._digest(source)}}}
        ),
        encoding="utf-8",
    )
    assert "p1" not in service.list("paper", report)["items"]
    requests = transport(monkeypatch, {"items": [{"id": "p1", "zh": "完整译文，保留末尾限定。"}]})
    out = service.translate("paper", report, settings, {"item_ids": ["p1"]})
    assert json.loads(requests[0]["messages"][-1]["content"])["items"][0]["text"] == source
    assert out["items"] == service.list("paper", report)["items"]


def test_oversized_passage_does_not_spend_or_block_a_short_passage(
    tmp_path, report, settings, monkeypatch
):
    settings = settings.model_copy(update={"llm_max_input_chars": 2000})
    source = '"' * 2100
    report.document.blocks[1].text = source
    report.reading.items[1].parts[0].end = len(source)
    requests = transport(monkeypatch, {"items": [{"id": "h1", "zh": "注意力"}]})
    service = TranslationService(tmp_path / "translations")
    out = service.translate("paper", report, settings, {"item_ids": ["p1", "h1"]})
    assert set(out["items"]) == {"h1"}
    assert out["remaining"] == ["p1"] and "输入预算" in out["warnings"][0]
    messages = requests[0]["messages"]
    assert sum(len(m["content"]) for m in messages) <= settings.llm_max_input_chars
    assert [i["id"] for i in json.loads(messages[-1]["content"])["items"]] == ["h1"]
    again = service.translate("paper", report, settings, {"item_ids": ["p1"]})
    assert again["usage"] == {"requests": 0} and len(requests) == 1
    assert "p1" not in service.list("paper", report)["items"]


def test_translation_utf16_parts_preserve_original_evidence(
    tmp_path, report, settings, monkeypatch
):
    source = "prefix😀 the claim may hold [7]. suffix"
    report.document.blocks[1].text = source
    part = report.reading.items[1].parts[0]
    part.start = len("prefix😀 ".encode("utf-16-le")) // 2
    part.end = part.start + len("the claim may hold [7].")
    original = report.model_dump(mode="json")
    requests = transport(monkeypatch, {"items": [{"id": "p1", "zh": "该结论可能成立 [7]。"}]})
    TranslationService(tmp_path).translate(
        "../paper/../../", report, settings, {"item_ids": ["p1"]}
    )
    sent = json.loads(requests[0]["messages"][-1]["content"])
    assert sent["items"][0]["text"] == "the claim may hold [7]."
    assert report.model_dump(mode="json") == original
    assert len(list(tmp_path.glob("*/*/translations.json"))) == 1


def auto_transport(calls):
    def handle(request):
        body = json.loads(request.content)
        payload = json.loads(body["messages"][-1]["content"])
        calls.append(payload)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "items": [
                                        {"id": i["id"], "zh": "译文保留 $d_k$ 与引用 [1]。"}
                                        for i in payload["items"]
                                    ]
                                }
                            )
                        },
                    }
                ],
                "usage": {"prompt_tokens": 30, "completion_tokens": 10},
            },
        )

    return httpx.MockTransport(handle)


def test_automatic_translation_embeds_offline_and_resumes_without_more_calls(
    tmp_path, report, settings
):
    calls = []
    service = TranslationService(tmp_path / "translations")
    original = report.document.model_dump()
    with JsonProvider(
        settings, transport=auto_transport(calls), budget=RequestBudget(4)
    ) as provider:
        result = service.generate(report, settings, provider)
        assert result["status"] == "complete" and result["translated"] == 3
        assert set(report.translations) == {TITLE_ID, "h1", "p1"}
        assert provider.usage.requests == 1
        assert (
            service.list(report.run.id, report)["items"]["p1"]["text"]
            == report.translations["p1"].text
        )
        report.run.id = "new-run-after-pause"
        report.translations = {}
        again = service.generate(report, settings, provider)
        assert again["cache_hits"] == 3 and len(calls) == 1
    assert report.document.model_dump() == original
    # Embedded translations work even on another machine without the sidecar.
    assert len(TranslationService(tmp_path / "empty").list("offline", report)["items"]) == 3


def test_automatic_translation_parallel_budget_keeps_partial_results(tmp_path, report, settings):
    report.reading.items = [
        ReadingItem(id=f"p{i}", kind="paragraph", content="The method may improve [1].")
        for i in range(80)
    ]
    calls = []
    budget = RequestBudget(2)
    with JsonProvider(settings, transport=auto_transport(calls), budget=budget) as provider:
        with processing_control(lambda: None, lambda: 3):
            result = TranslationService(tmp_path).generate(report, settings, provider)
    assert budget.used == len(calls) == 2
    assert result["status"] == "partial"
    assert result["translated"] == len(report.translations) == 32
    assert result["translated"] + len(result["remaining"]) == 81


def test_automatic_translation_skips_chinese_and_reference_content(tmp_path, report, settings):
    report.analysis.title = "中文论文"
    report.reading.items = [
        ReadingItem(
            id="cn", kind="paragraph", content="这里讨论注意力机制与 Transformer 的数学原理。"
        ),
        ReadingItem(id="ref", kind="reference", content="Attention Is All You Need"),
        ReadingItem(id="formula", kind="formula", content="x + y"),
    ]
    calls = []
    with JsonProvider(settings, transport=auto_transport(calls)) as provider:
        result = TranslationService(tmp_path).generate(report, settings, provider)
    assert result["status"] == "skipped" and calls == []
