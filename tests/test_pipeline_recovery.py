"""Failed reading requests stay recoverable; ordinary sampling stays successful."""

import asyncio
import json
import threading
from contextlib import nullcontext
from types import SimpleNamespace

import httpx
import pytest
from test_edition_text_review import edition
from test_reading_projection import b, report

from paperlens import pipeline, provider_stream
from paperlens.config import Settings
from paperlens.control import processing_concurrency
from paperlens.errors import ProviderError
from paperlens.jobs import Job
from paperlens.models import Usage
from paperlens.provider import JsonProvider


@pytest.mark.parametrize(
    ("error_stage", "error_code", "preflight_status"),
    [(None, None, None)]
    + [(stage, code, None) for stage in ("reading", "math", "text")
       for code in ("provider_error", "provider_outcome_unknown", "provider_rejected")]
    + [(stage, "provider_error", status) for stage in ("math", "text")
       for status in ("unavailable", "partial")],
)
def test_pipeline_only_marks_failed_provider_batches_retryable(
    tmp_path, monkeypatch, error_stage, error_code, preflight_status
):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7 test fixture; parsing is stubbed")
    current = report([b("source", "Readable original source.")])
    configured = Settings(
        _env_file=None, llm_api_key="test", paperlens_parser="pdfplumber",
        paperlens_cache_dir=tmp_path / "cache", paperlens_output_dir=tmp_path / "output",
        paperlens_math_enabled=False, paperlens_reference_network=False,
        paperlens_auto_translate_enabled=False, paperlens_auto_notes_enabled=False,
    )

    class Provider:
        def __init__(self, *args, **kwargs):
            self.usage = Usage()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def usage_record(self):
            return {"model": configured.llm_model, "usage": self.usage.model_dump()}

        def usage_records(self):
            return []

    def audit(stage):
        failed = error_stage == stage
        result = {
            "status": "partial" if failed or stage == "math" else "completed",
            "cache_hits": 0, "reviewed": 0, "candidates": 30,
            "patches": [], "removals": [],
            "batches": [{"status": "unavailable", "error_code": error_code}]
            if failed else [{"status": "completed"}],
        }
        if failed and preflight_status:
            result.update(status=preflight_status, error_code=error_code, batches=[])
        return result

    def native_audit():
        return audit("reading")

    monkeypatch.setattr(pipeline, "validate_path", lambda path, limit: path)
    monkeypatch.setattr(pipeline, "get_dashboard_template", lambda: "unused template")
    monkeypatch.setattr(pipeline, "browser_session", lambda *args: nullcontext())
    monkeypatch.setattr(pipeline, "read_native", lambda *args, **kwargs: SimpleNamespace(
        report=current.model_copy(deep=True), audit=native_audit()))
    monkeypatch.setattr(pipeline, "native_input_mode", lambda *args: "images")
    monkeypatch.setattr(pipeline, "JsonProvider", Provider)
    monkeypatch.setattr(pipeline, "ProviderPool", Provider)
    monkeypatch.setattr(pipeline, "analyze_document", lambda *args, **kwargs: SimpleNamespace(
        analysis=current.analysis, evidence=[], audit={"chunks": 1},
    ))
    monkeypatch.setattr(pipeline, "build_views", lambda analysis: [])
    monkeypatch.setattr(pipeline, "review_math", lambda *args, **kwargs: audit("math"))
    monkeypatch.setattr(pipeline, "review_text", lambda *args, **kwargs: audit("text"))
    monkeypatch.setattr(pipeline, "_run_postprocessing", lambda *args, **kwargs: ({}, []))
    monkeypatch.setattr(pipeline, "resolve_references", lambda *args, **kwargs: {})
    monkeypatch.setattr(pipeline, "report_html", lambda report: "<html>Local report</html>")

    def write_report(report, output, **kwargs):
        path = output / "dashboard.html"
        path.write_text("<html>Local report</html>", encoding="utf-8")
        return path

    monkeypatch.setattr(pipeline, "write_report", write_report)
    result = pipeline.run_pipeline(source, configured)
    assert result.retryable is (error_stage is not None)
    assert result.report.run.status == ("partial" if error_stage else "complete")
    assert result.report_path.exists()
    saved = json.loads((result.output_dir / "report.json").read_text("utf-8"))
    assert saved["run"]["status"] == result.report.run.status
    assert any("尚未完成" in warning for warning in saved["quality"]["warnings"]) is result.retryable


@pytest.mark.parametrize(
    ("error_type", "http_status"),
    [(httpx.ConnectError, None), (httpx.ReadTimeout, None), (None, 401)],
    ids=["pre-submit", "unknown-after-submit", "http-rejection"],
)
def test_real_provider_failure_reaches_job_and_explicit_rerun_reuses_success(
    tmp_path, monkeypatch, error_type, http_status
):
    """Mock transport joins actual provider, text review, pipeline and job state."""
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7 test fixture; parsing is stubbed")
    current = edition([(page, "Readable source.") for page in range(1, 4)])
    configured = Settings(
        _env_file=None, llm_api_key="test", llm_base_url="https://api.openai.com/v1",
        llm_max_retries=1, paperlens_parser="pdfplumber",
        paperlens_cache_dir=tmp_path / "cache", paperlens_output_dir=tmp_path / "output",
        paperlens_math_enabled=False, paperlens_reference_network=False,
        paperlens_auto_translate_enabled=False, paperlens_auto_notes_enabled=False,
        paperlens_vision_allow_unverified=True,
    )
    attempts: dict[str, int] = {}
    analysis_inputs = []
    failing = True

    def request(req):
        body = json.loads(req.content)
        content = body["messages"][-1]["content"]
        payload = json.loads(content[0]["text"] if isinstance(content, list) else content)
        ident = payload["items"][0]["id"]
        attempts[ident] = attempts.get(ident, 0) + 1
        if ident == "item-2" and failing:
            if http_status:
                return httpx.Response(http_status)
            raise error_type("simulated transport failure", request=req)
        reviews = {"reviews": [{"id": row["id"], "status": "ok"} for row in payload["items"]]}
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(reviews)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    class MockProvider(JsonProvider):
        def __init__(self, settings, **kwargs):
            super().__init__(
                settings, transport=httpx.MockTransport(request), sleeper=lambda _: None, **kwargs
            )

    class MockPool:
        def __init__(self, *args, **kwargs):
            self.usage = Usage()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def usage_records(self):
            return []

    def native_audit():
        return {"status": "completed", "cache_hits": 0, "batches": []}

    monkeypatch.setattr(pipeline, "validate_path", lambda path, limit: path)
    monkeypatch.setattr(pipeline, "get_dashboard_template", lambda: "unused template")
    monkeypatch.setattr(pipeline, "browser_session", lambda *args: nullcontext())
    monkeypatch.setattr(pipeline, "read_native", lambda *args, **kwargs: SimpleNamespace(
        report=current.model_copy(deep=True), audit=native_audit()))
    monkeypatch.setattr(pipeline, "native_input_mode", lambda *args: "images")
    monkeypatch.setattr(pipeline, "JsonProvider", MockProvider)
    monkeypatch.setattr(pipeline, "ProviderPool", MockPool)
    def analyze(document, *args, **kwargs):
        analysis_inputs.append(document.model_dump())
        return SimpleNamespace(analysis=current.analysis, evidence=[], audit={"chunks": 1})

    monkeypatch.setattr(pipeline, "analyze_document", analyze)
    monkeypatch.setattr(pipeline, "build_views", lambda analysis: [])
    monkeypatch.setattr(pipeline, "review_math", lambda *args, **kwargs: {
        "status": "completed", "batches": [],
    })
    monkeypatch.setattr("paperlens.edition_text_review.page_image", lambda *args: "data:image/png;base64,QQ==")
    monkeypatch.setattr(pipeline, "_run_postprocessing", lambda *args, **kwargs: ({}, []))
    monkeypatch.setattr(pipeline, "resolve_references", lambda *args, **kwargs: {})
    monkeypatch.setattr(pipeline, "report_html", lambda report: "<html>Local report</html>")

    def write_report(report, output, **kwargs):
        path = output / "dashboard.html"
        path.write_text("<html>Local report</html>", encoding="utf-8")
        return path

    monkeypatch.setattr(pipeline, "write_report", write_report)
    with processing_concurrency(1):
        first = pipeline.run_pipeline(source, configured)
    audit = json.loads((first.output_dir / "text-review.json").read_text("utf-8"))
    uncertain = error_type is httpx.ReadTimeout
    failed_attempts = 2 if error_type is httpx.ConnectError else 1
    assert audit["status"] == "partial"
    assert [batch["status"] for batch in audit["batches"]] == ["completed", "unavailable"]
    assert audit["batches"][-1]["error_code"] == (
        "provider_outcome_unknown" if uncertain else "provider_rejected" if http_status else "provider_error"
    )
    assert attempts == {"item-0": 1, "item-2": failed_attempts}
    assert first.report.run.status == "partial" and first.retryable
    assert analysis_inputs == []
    assert Job(id="a" * 32, status="partial", retryable=first.retryable).public()["actions"] == ["retry"]
    events = [
        event
        for path in (tmp_path / "cache" / "provider").glob("*.json")
        for event in json.loads(path.read_text("utf-8"))["events"]
        if (event.get("error_type") == error_type.__name__ if error_type
            else event.get("http_status") == http_status)
    ]
    assert len(events) == failed_attempts
    if http_status:
        assert all(event["usage_unknown"] is False for event in events)
        failure = first.report.run.failures[0]
        assert failure.stage == "text_review" and failure.http_status == http_status
        assert not failure.retryable and failure.pages == [3]
    else:
        assert all(event["usage_unknown"] is uncertain for event in events)

    # The second invocation stands for an explicit retry; nothing is submitted before it.
    failing = False
    with processing_concurrency(1):
        resumed = pipeline.run_pipeline(source, configured)
    resumed_audit = json.loads((resumed.output_dir / "text-review.json").read_text("utf-8"))
    assert resumed.report.run.status == "complete" and not resumed.retryable
    assert resumed.report.run.failures == []
    assert resumed_audit["calls"] == resumed_audit["cache_hits"] == 1
    assert attempts == {"item-0": 1, "item-2": failed_attempts + 1}
    assert len(analysis_inputs) == 1
    first_source = json.loads((first.output_dir / "original-reading.json").read_text("utf-8"))
    resumed_source = json.loads((resumed.output_dir / "original-reading.json").read_text("utf-8"))
    assert first_source == resumed_source


def test_stream_read_timeout_after_content_does_not_replay_and_releases_slot(tmp_path, monkeypatch):
    configured = Settings(
        _env_file=None, llm_api_key="test", llm_base_url="https://api.openai.com/v1",
        paperlens_cache_dir=tmp_path / "cache", llm_max_retries=2,
    )
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(provider_stream, "_NETWORK_SLOTS", slots)
    requests, chunks = [], []

    class InterruptedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            raise httpx.ReadTimeout("simulated interrupted stream")

    async def request(req):
        requests.append(req)
        return httpx.Response(200, stream=InterruptedStream())

    async def consume(provider):
        async for chunk in provider_stream.stream_json(
            provider, "system", "user", transport=httpx.MockTransport(request)
        ):
            chunks.append(chunk)

    with JsonProvider(configured) as provider:
        with pytest.raises(ProviderError):
            asyncio.run(consume(provider))
        assert provider.usage.requests == 1
        assert provider._events[-1]["outcome"] == "network_error"
        assert not provider._events[-1]["usage_reported"]
    assert len(requests) == 1 and chunks == ["partial"]
    assert slots.acquire(blocking=False)
    slots.release()
