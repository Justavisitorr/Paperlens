"""Preserve actionable failures and honor Responses JSON mode at the input boundary."""

import asyncio
import json

import httpx
import pytest
from test_ai_direct import response_result
from test_reading_projection import b, report

from paperlens.config import Settings
from paperlens.errors import ProviderRejected
from paperlens.jobs import Job, JobManager
from paperlens.models import StageFailure
from paperlens.provider import JsonProvider
from paperlens.provider_stream import stream_json
from paperlens.recovery import attach_failures, failure_detail, stage_failures
from paperlens.storage import write_json


def settings(tmp_path):
    return Settings(
        _env_file=None,
        llm_api_key="test-secret",
        llm_base_url="https://api.openai.com/v1",
        llm_max_retries=2,
        paperlens_cache_dir=tmp_path / "cache",
        paperlens_output_dir=tmp_path / "outputs",
    )


def test_responses_json_gate_reads_input_even_when_instructions_already_say_json(tmp_path):
    calls = []

    def send(request):
        payload = json.loads(request.content)
        calls.append(payload)
        texts = [
            p["text"] for m in payload["input"] for p in m["content"] if p["type"] == "input_text"
        ]
        # Reproduce the real gate: instructions are excluded from the input check.
        if not any("json" in text.casefold() for text in texts):
            return httpx.Response(
                400,
                json={"error": {"message": "Input messages must contain json", "param": "input"}},
            )
        assert json.loads(texts[0]) == {"items": ["original text"]}
        return httpx.Response(200, json=response_result({"reviews": []}))

    with JsonProvider(
        settings(tmp_path), responses_api=True, transport=httpx.MockTransport(send)
    ) as provider:
        assert provider.json(
            "Return JSON", '{"items":["original text"]}', images=["data:image/png;base64,QQ=="]
        ) == {"reviews": []}
    assert len(calls) == 1
    assert calls[0]["input"][0]["content"][-1]["type"] == "input_image"


def test_rejected_parameter_is_visible_and_redacted_without_retries(tmp_path):
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(
            400,
            json={
                "error": {
                    "type": "invalid_request_error",
                    "code": "bad_input",
                    "param": "input",
                    "message": "Need JSON; test-secret Bearer private-token sk-private",
                }
            },
        )

    with JsonProvider(
        settings(tmp_path), responses_api=True, transport=httpx.MockTransport(send)
    ) as provider:
        with pytest.raises(ProviderRejected) as caught:
            provider.json("Review", "{}")
        assert provider.usage.requests == 1
    detail = caught.value.details
    assert detail["http_status"] == 400 and detail["param"] == "input"
    assert "Need JSON" in str(caught.value)
    events = [
        json.loads(p.read_text("utf-8"))["events"]
        for p in (tmp_path / "cache/provider").glob("*.json")
    ]
    assert len(calls) == 1 and events[0][0]["provider_error"] == detail
    public = json.dumps({"detail": detail, "message": str(caught.value), "events": events})
    assert all(secret not in public for secret in ("test-secret", "private-token", "sk-private"))


def test_stream_rejection_reads_error_body_before_reporting(tmp_path):
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"error":{"message":"Invalid input", "param":"input"}}'

    async def send(request):
        return httpx.Response(400, stream=Body())

    async def consume(provider):
        async for _ in stream_json(provider, "system", "user", transport=httpx.MockTransport(send)):
            pytest.fail("Rejected requests cannot produce answer chunks")

    with JsonProvider(settings(tmp_path)) as provider:
        with pytest.raises(ProviderRejected, match="Invalid input") as caught:
            asyncio.run(consume(provider))
        assert caught.value.details["param"] == "input"
        assert provider.usage.requests == 1


def test_legacy_audits_explain_stage_pages_and_actual_model_without_rewriting_report(tmp_path):
    write_json(
        tmp_path / "reading-verification.json",
        {
            "math_review": {
                "batches": [
                    {
                        "status": "unavailable",
                        "pages": [4],
                        "ids": ["a"],
                        "reason": "模型接口返回 HTTP 400。",
                    },
                    {
                        "status": "unavailable",
                        "pages": [5, 4],
                        "ids": ["b"],
                        "reason": "模型接口返回 HTTP 400。",
                    },
                    {"status": "completed", "pages": [6], "ids": ["c"]},
                ]
            }
        },
    )
    write_json(
        tmp_path / "text-review.json",
        {
            "batches": [
                {
                    "status": "unavailable",
                    "pages": [1, 2],
                    "ids": ["d"],
                    "reason": "连接断开",
                    "error_code": "provider_outcome_unknown",
                }
            ]
        },
    )
    current = report([b("b", "Source")])
    current.run.status = "partial"
    current.run.model = "main-model"
    current.run.model_routing = {"vision": {"model": "review-model"}}
    attach_failures(current, tmp_path)
    first, second = current.run.failures
    assert (first.label, first.model, first.pages, first.batches, first.items) == (
        "公式审校",
        "review-model",
        [4, 5],
        2,
        2,
    )
    assert not first.retryable and "原样重试" in first.guidance
    assert second.retryable and "计费" in second.guidance
    attach_failures(current, tmp_path)
    assert len(current.run.failures) == 2
    assert not (tmp_path / "report.json").exists()


def test_optional_stage_and_preflight_errors_keep_details(tmp_path):
    error = {"http_status": 401, "param": "model"}
    write_json(
        tmp_path / "auto-notes-verification.json",
        {
            "model": "notes-model",
            "errors": [{"item_id": "n1", "message": "账户无权限", "provider_error": error}],
        },
    )
    write_json(
        tmp_path / "text-review.json",
        {"status": "unavailable", "error": "图像调用未启用", "error_code": "provider_error"},
    )
    found = stage_failures(tmp_path)
    note = next(f for f in found if f.stage == "auto_notes")
    assert (note.http_status, note.parameter, note.model, note.items) == (
        401,
        "model",
        "notes-model",
        1,
    )
    assert "权限" in note.guidance and not note.retryable
    assert any(f.message == "图像调用未启用" for f in found)
    budget = failure_detail("auto_notes", "model", {"input_budget": {"required": 20000}})
    assert not budget.retryable and "预算弹窗" in budget.guidance


def test_jobs_restore_partial_failure_details_and_serialize_for_ui(tmp_path):
    configured = settings(tmp_path)
    ident = "a" * 32
    job = Job(
        id=ident,
        status="partial",
        retryable=True,
        source_sha256="fixture",
        report_relative_path=f"{ident}/run/dashboard.html",
        configuration={"llm_model": "main-model"},
    )
    write_json(configured.paperlens_cache_dir / "jobs" / ident / "job.json", job.model_dump())
    write_json(
        configured.paperlens_output_dir / ident / "run/text-review.json",
        {
            "model": "review-model",
            "batches": [{"status": "unavailable", "pages": [2], "reason": "HTTP 400"}],
        },
    )
    manager = JobManager(
        configured,
        runner=lambda *args, **kwargs: pytest.fail("Reading failure details must not run a model"),
    )
    try:
        public = manager.get(ident).public()
        failure = StageFailure.model_validate(public["failures"][0])
        assert failure.stage == "text_review" and failure.model == "review-model"
        assert public["actions"] == ["retry"]
    finally:
        manager.close()
