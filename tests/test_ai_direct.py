"""Production direct reading: real transport shapes, isolation and resumable work."""

import base64
import io
import json
import time
from collections import Counter
from contextlib import nullcontext
from pathlib import Path
from threading import Lock

import httpx
import pypdfium2 as pdfium
import pytest
from PIL import Image

from paperlens import edition_native, pipeline
from paperlens.analysis.engine import AnalysisResult
from paperlens.config import ModelRoute, Settings
from paperlens.control import processing_concurrency
from paperlens.errors import ProviderError, ProviderOutcomeUnknown
from paperlens.models import Box, Evidence, Page
from paperlens.provider import JsonProvider, RequestBudget
from paperlens.reading_edition import EditionBatch
from paperlens.runtime import RuntimeSettings


def settings(tmp_path, **values):
    return Settings(_env_file=None, **{
        "llm_api_key": "test-key", "llm_model": "gpt-6.1-terra",
        "llm_base_url": "https://api.openai.com/v1", "llm_reasoning_effort": "max",
        "paperlens_reading_mode": "ai_direct", "paperlens_vision_allow_unverified": True,
        "paperlens_cache_dir": tmp_path / "cache", "paperlens_output_dir": tmp_path / "outputs",
        "paperlens_reference_network": False, **values,
    })


def make_pdf(path, pages=2):
    with pdfium.PdfDocument.new() as doc:
        for _ in range(pages):
            page = doc.new_page(600, 800)
            page.close()
        doc.save(path)
    return path


def picture():
    stream = io.BytesIO()
    with Image.new("RGB", (2, 2), "white") as image:
        image.save(stream, "JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(stream.getvalue()).decode()


def batch(pages):
    return {"items": [{"kind": "paragraph", "page": page, "box": [.1, .2, .9, .3],
                       "content": f"Source text on physical page {page}."} for page in pages]}


def response_result(value, *, status="completed"):
    return {"status": status, "model": "gpt-6.1-terra",
            "output": [{"type": "message", "content": [
                {"type": "output_text", "text": json.dumps(value)},
            ]}], "usage": {"input_tokens": 20, "output_tokens": 8,
                           "input_tokens_details": {"cached_tokens": 3}}}


def responses_reply(value):
    events = [{"type": "response.created", "response": {"id": "mock-response"}},
              {"type": "response.completed", "response": response_result(value)}]
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          text="\n\n".join("data: " + json.dumps(event) for event in events))


def chat_reply(value):
    return httpx.Response(200, json={
        "choices": [{"message": {"content": json.dumps(value)}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4},
    })


def native_pages(payload):
    content = payload["input"][0]["content"] if "input" in payload else payload["messages"][-1]["content"]
    text = next(part["text"] for part in content if part["type"] in {"text", "input_text"})
    return json.loads(text[text.index("{"):])["requested_pages"]


def test_legacy_settings_migrate_to_only_direct_without_losing_other_values(tmp_path):
    configured = settings(tmp_path, paperlens_reading_mode="classic")
    manager = RuntimeSettings(configured)
    assert configured.paperlens_reading_mode == "ai_direct"
    assert "schema" in manager.update({"LLM_MAX_REQUESTS": 137})
    stored = json.loads(manager._path.read_text("utf-8"))
    from paperlens.runtime import _RETIRED_FIELDS
    stored["values"].update({key: "historical value" for key in _RETIRED_FIELDS})
    stored["values"]["PAPERLENS_READING_MODE"] = "classic"
    manager._path.write_text(json.dumps(stored), encoding="utf-8")
    restored = RuntimeSettings(configured)
    assert restored.snapshot().paperlens_reading_mode == "ai_direct"
    assert restored.snapshot().llm_max_requests == 137
    assert restored.snapshot().llm_api_key == configured.llm_api_key
    fields = {f["key"] for group in restored.public()["groups"] for f in group["fields"]}
    assert not fields.intersection(_RETIRED_FIELDS)
    assert "PAPERLENS_MATH_ENABLED" in fields
    assert "PAPERLENS_TEXT_REVIEW_ENABLED" in fields
    assert "errors" in restored.update({"PAPERLENS_READING_MODE": "classic"})


def test_responses_pdf_shape_selected_account_stream_and_usage(tmp_path):
    pdf = make_pdf(tmp_path / "source.pdf").read_bytes()
    configured = settings(tmp_path, llm_api_key="main-only")
    configured.llm_routes["vision"] = ModelRoute(
        account_id="vision-account", model="gpt-6.1-terra", base_url="https://api.openai.com/v1",
        api_key="vision-only", vision_confirmed=True,
    )
    seen = []

    def send(req):
        seen.append(req)
        assert req.url.path == "/v1/responses"
        assert req.headers["authorization"] == "Bearer vision-only"
        payload = json.loads(req.content)
        assert "messages" not in payload and payload["stream"] and not payload["store"]
        assert payload["reasoning"] == {"effort": "max"}
        assert payload["text"] == {"format": {"type": "json_object"}}
        file = next(x for x in payload["input"][0]["content"] if x["type"] == "input_file")
        assert file["detail"] == "high" and file["filename"].endswith(".pdf")
        assert base64.b64decode(file["file_data"].split(",", 1)[1]) == pdf
        return responses_reply({"ok": True})

    with JsonProvider(configured.for_role("vision"), transport=httpx.MockTransport(send)) as provider:
        assert provider.json("Return JSON", "Read original", pdf_bytes=pdf, max_attempts=1) == {"ok": True}
        assert provider.usage.prompt_tokens == 20
        assert provider.usage.completion_tokens == 8
        assert provider.usage.prompt_cache_hit_tokens == 3
        assert provider.usage.prompt_cache_miss_tokens == 17
    assert len(seen) == 1


def test_cut_responses_stream_is_unknown_and_never_automatically_replayed(tmp_path):
    attempts = []

    def send(req):
        attempts.append(req)
        event = {"type": "response.output_text.delta", "delta": '{"ok":true}'}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text="data: " + json.dumps(event) + "\n\n")

    with JsonProvider(settings(tmp_path, llm_max_retries=3), responses_api=True,
                      transport=httpx.MockTransport(send), sleeper=lambda _: None) as provider:
        with pytest.raises(ProviderOutcomeUnknown):
            provider.json("Return JSON", "Read", images=[picture()])
        assert provider.usage.requests == 1
        assert provider._events[-1]["usage_unknown"]
    assert len(attempts) == 1


def test_pdf_is_not_sent_to_unverified_pdf_endpoint(tmp_path):
    seen = []
    with JsonProvider(settings(tmp_path, llm_base_url="https://image.example/v1"),
                      transport=httpx.MockTransport(lambda req: seen.append(req))) as provider:
        with pytest.raises(ProviderError, match="原页图片"):
            provider.json("JSON", "read", pdf_bytes=b"%PDF-1.7 test")
    assert not seen


def test_image_only_reading_unknown_isolation_and_explicit_resume(tmp_path, monkeypatch):
    source = make_pdf(tmp_path / "source.pdf", 6)
    configured = settings(tmp_path, llm_base_url="https://api.deepseek.com", llm_model="deepseek-flash",
                          paperlens_vision_enabled=False, paperlens_transcription_max_regions=1,
                          paperlens_processing_profile="economy")
    monkeypatch.setattr(edition_native, "page_image", lambda *args: picture())
    monkeypatch.setattr(pdfium.PdfPage, "get_textpage", lambda *args: pytest.fail("Local text extraction forbidden"))
    attempts, lock, active, peak = Counter(), Lock(), 0, 0
    failing = True

    def send(req):
        nonlocal active, peak
        payload = json.loads(req.content)
        assert req.url.path == "/chat/completions" and "input" not in payload
        content = payload["messages"][-1]["content"]
        assert len([x for x in content if x["type"] == "image_url"]) == 2
        pages = tuple(native_pages(payload))
        assert all(word not in json.dumps(payload) for word in ["input_file", "Source text on physical"])
        with lock:
            attempts[pages] += 1
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(.02)
            if failing and pages == (1, 2):
                raise httpx.ReadTimeout("submitted but unknown", request=req)
            return chat_reply(batch(pages))
        finally:
            with lock:
                active -= 1

    with JsonProvider(configured, transport=httpx.MockTransport(send)) as provider, processing_concurrency(8):
        first = edition_native.read_native(source, configured, provider)
    assert peak == 2 and len(attempts) == 3
    assert first.audit["status"] == "partial" and first.audit["pages"] == [3, 4, 5, 6]
    assert first.audit["batches"][0]["error_code"] == "provider_outcome_unknown"
    assert [p.status for p in first.report.document.pages] == ["failed", "failed", "ok", "ok", "ok", "ok"]
    failing = False
    with JsonProvider(configured, transport=httpx.MockTransport(send)) as provider:
        second = edition_native.read_native(source, configured, provider)
        assert provider.usage.requests == 1
    assert second.audit["status"] == "completed" and second.audit["cache_hits"] == 2
    assert attempts == {(1, 2): 2, (3, 4): 1, (5, 6): 1}
    assert second.report.document.parser == "model-page-images"
    assert all(b.source_ref == edition_native.POSITION_SOURCE for b in second.report.document.blocks)


def test_native_cache_is_account_isolated_and_budget_failure_not_complete(tmp_path, monkeypatch):
    source = make_pdf(tmp_path / "source.pdf", 4)
    configured = settings(tmp_path)
    calls = []

    def send(req):
        pages = native_pages(json.loads(req.content))
        calls.append(pages)
        return responses_reply(batch(pages))

    with JsonProvider(configured, budget=RequestBudget(1), transport=httpx.MockTransport(send)) as provider:
        first = edition_native.read_native(source, configured, provider)
    assert first.audit["status"] == "partial" and len(first.audit["pages"]) == 2
    with JsonProvider(configured, transport=httpx.MockTransport(send)) as provider:
        second = edition_native.read_native(source, configured, provider)
    assert second.audit["status"] == "completed" and second.audit["cache_hits"] == 1
    other = configured.model_copy(update={"llm_account_id": "other-account"})
    with JsonProvider(other, transport=httpx.MockTransport(send)) as provider:
        isolated = edition_native.read_native(source, other, provider)
    assert isolated.audit["cache_hits"] == 0 and len(calls) == 4


def test_native_repeated_formulas_keep_three_positions_with_unique_ids(tmp_path):
    source = make_pdf(tmp_path / "source.pdf", 1)
    rows = [{"kind": "formula", "page": 1, "box": [.2, y, .8, y + .05],
             "content": r"\int_{-1/4}^{1/4} f(x)\,dx=1."} for y in [.2, .4, .6]]
    report = edition_native.project_native(
        source, [Page(number=1, width=600, height=800)],
        [EditionBatch.model_validate({"items": rows})], settings(tmp_path), "pdf",
    )
    items = report.reading.items
    assert len(items) == len({item.id for item in items}) == 3
    assert len({item.parts[0].block_id for item in items}) == 3
    assert len({item.content for item in items}) == 1


def test_empty_or_missing_page_cannot_be_reported_as_complete():
    with pytest.raises(ValueError, match="全部物理页"):
        edition_native.validate_native(batch([1]), [1, 2])
    empty = batch([1, 2])
    empty["items"][1]["content"] = ""
    with pytest.raises(ValueError, match="空白或无法辨认"):
        edition_native.validate_native(empty, [1, 2])


@pytest.mark.parametrize("raw", [
    {**batch([1]), "blank_pages": [1, 2]},
    {**batch([1]), "blank_pages": [2, 2]},
    {**batch([1]), "blank_pages": [3]},
    {"items": [], "blank_pages": [True, 2]},
])
def test_blank_pages_require_disjoint_exact_physical_coverage(raw):
    with pytest.raises(ValueError):
        edition_native.validate_native(raw, [1, 2])


def test_blank_pages_complete_without_fabricated_body_and_resume_from_cache(tmp_path):
    source = make_pdf(tmp_path / "source.pdf", 4)
    configured = settings(tmp_path)
    calls = []

    def send(req):
        payload = json.loads(req.content)
        assert "无法辨认、模糊或读取失败不等于空白" in payload["instructions"]
        pages = native_pages(payload)
        calls.append(pages)
        return responses_reply({**batch([1]), "blank_pages": [2]} if pages == [1, 2]
                               else {"items": [], "blank_pages": [3, 4]})

    with JsonProvider(configured, transport=httpx.MockTransport(send)) as provider:
        first = edition_native.read_native(source, configured, provider)
    assert first.audit["status"] == "completed" and first.audit["remaining_pages"] == []
    assert first.audit["blank_pages"] == [2, 3, 4]
    assert [p.status for p in first.report.document.pages] == ["ok", "blank", "blank", "blank"]
    assert len(first.report.reading.items) == len(first.report.document.blocks) == 1
    assert first.report.quality.parsed_pages == 4
    with JsonProvider(configured, transport=httpx.MockTransport(send)) as provider:
        resumed = edition_native.read_native(source, configured, provider)
        assert provider.usage.requests == 0
    assert resumed.audit["cache_hits"] == 2 and len(calls) == 2


@pytest.mark.parametrize("profile", ["economy", "standard", "detailed"])
@pytest.mark.parametrize("outcome", ["complete", "partial", "blank"])
def test_production_pipeline_native_keeps_downstream_and_resumable_partial(tmp_path, monkeypatch, outcome, profile):
    incomplete = outcome == "partial"
    source = make_pdf(tmp_path / "source.pdf")
    configured = settings(tmp_path, paperlens_vision_enabled=False, paperlens_processing_profile=profile,
                          paperlens_auto_translate_enabled=True, paperlens_auto_notes_enabled=True)
    calls, stages = [], []

    def send(req):
        assert req.url.path == "/v1/responses"
        if incomplete:
            raise httpx.ReadTimeout("unknown", request=req)
        if outcome == "blank":
            return responses_reply({"items": [], "blank_pages": [1, 2]})
        return responses_reply(batch(native_pages(json.loads(req.content))))

    class MockProvider(JsonProvider):
        def __init__(self, configured, **kwargs):
            super().__init__(configured, transport=httpx.MockTransport(send), **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("Classic parser/OCR/reflow must not run in AI direct mode")

    def analyze(document, provider, settings, **kwargs):
        calls.append("analysis")
        assert len(document.blocks) == 2 and document.parser == "openai-pdf"
        seed = edition_native.project_native(source, document.pages,
                                              [EditionBatch.model_validate(batch([1, 2]))], settings, "pdf")
        block = document.blocks[0]
        return AnalysisResult(seed.analysis, [Evidence(id="e1", block_id=block.id, page=1,
                                                       quote=block.text, bbox=block.bbox)], {"chunks": 1})

    def math_review(report, source, provider, settings, **kwargs):
        calls.append("math")
        assert settings.paperlens_reading_mode == "ai_direct"
        return {"status": "partial", "cache_hits": 0, "batches": [{"status": "completed"}]}

    def text_review(*args, **kwargs):
        calls.append("text")
        return {"status": "completed", "cache_hits": 0, "batches": [], "reviewed": 2, "candidates": 2,
                "patches": [], "removals": [], "unresolved": []}

    def postprocessing(report, settings, *args, **kwargs):
        assert settings.paperlens_auto_translate_enabled and settings.paperlens_auto_notes_enabled
        calls.append("translation_notes")
        return {}, []

    def write_report(report, output, **kwargs):
        target = output / "dashboard.html"
        target.write_text("<html>mock rendering</html>", encoding="utf-8")
        return target

    monkeypatch.setattr(pipeline, "JsonProvider", MockProvider)
    monkeypatch.setattr(pipeline, "get_dashboard_template", lambda: "mock")
    monkeypatch.setattr(pipeline, "browser_session", lambda *args: nullcontext())
    for name in ["parse_pdf", "docling_available", "analyze_visuals", "recognize_math_regions",
                 "prepare_reading", "build_edition", "recover_inline_math", "docling_artifacts_path"]:
        assert not hasattr(pipeline, name)
    monkeypatch.setattr(pipeline, "analyze_document", analyze)
    monkeypatch.setattr(pipeline, "build_views", lambda *args: [])
    monkeypatch.setattr(pipeline, "render_page_previews", lambda *args, **kwargs: [])
    monkeypatch.setattr(pipeline, "review_math", math_review)
    monkeypatch.setattr(pipeline, "review_text", text_review)
    monkeypatch.setattr(pipeline, "_run_postprocessing", postprocessing)
    monkeypatch.setattr(pipeline, "resolve_references", lambda *args, **kwargs: {"checked": True})
    monkeypatch.setattr(pipeline, "write_report", write_report)
    monkeypatch.setattr(pipeline, "report_html", lambda *args: "<html>mock</html>")
    result = pipeline.run_pipeline(source, configured, progress=lambda name, *_: stages.append(name))
    assert result.retryable is incomplete
    assert result.report.run.status == ("partial" if incomplete else "complete")
    assert calls == (["math", "text", "analysis", "translation_notes"] if outcome == "complete" else [])
    assert not {"recognition", "inline_math", "transcription"}.intersection(stages)
    assert result.report.source_pdf and result.report_path.exists()
    if outcome == "complete":
        assert result.report.evidence[0].bbox == Box(x0=0, y0=0, x1=1, y1=1)
    elif outcome == "blank":
        assert result.report.quality.parsed_pages == 2
        assert not result.report.reading.items and not result.report.document.blocks
        assert {p.status for p in result.report.document.pages} == {"blank"}
    assert json.loads((result.output_dir / "reference-links.json").read_text("utf-8"))["checked"]




def test_saved_native_response_projects_offline_without_ocr(tmp_path, monkeypatch):
    import os

    capture = os.environ.get("PAPERLENS_TEST_NATIVE_CAPTURE")
    if not capture:
        pytest.skip("Set PAPERLENS_TEST_NATIVE_CAPTURE to a local model capture directory")
    root = Path(capture)
    files = sorted((root / "calibration/terra-max/native/requests").glob("request-*.json"))
    if not files or not (root / "source.pdf").exists():
        pytest.skip("Local immutable experiment responses are not part of the source distribution")
    raw = None
    for path in files:
        saved = json.loads(path.read_text("utf-8")).get("response", {})
        if saved.get("status") != "completed":
            continue
        text = "".join(p.get("text", "") for row in saved.get("output", [])
                       for p in row.get("content", []) if p.get("type") == "output_text")
        raw = json.loads(text)
        if any(item["page"] == 5 for item in raw["items"]):
            break
    assert raw
    monkeypatch.setattr(edition_native, "page_image", lambda *args: picture())
    monkeypatch.setattr(pdfium.PdfPage, "get_textpage", lambda *args: pytest.fail("No local OCR/text layer"))
    pages = sorted({item["page"] for item in raw["items"]})
    parsed = edition_native.validate_native(raw, pages)
    report = edition_native.project_native(root / "source.pdf", edition_native.page_specs(root / "source.pdf", 100),
                                            [parsed], settings(tmp_path), "pdf")
    assert set(report.reading.structured_pages) == set(pages)
    assert any(item.kind == "formula" and item.page == 5 for item in report.reading.items)
    assert report.document.parser == "openai-pdf"
