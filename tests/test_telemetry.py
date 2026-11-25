"""Timing observations must be useful without affecting or exposing paid calls."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import httpx
import pytest

from paperlens import telemetry
from paperlens.config import Settings
from paperlens.provider import JsonProvider
from paperlens.provider_stream import stream_json


def settings_for(tmp_path):
    return Settings(
        _env_file=None,
        llm_api_key="private-key-never-log",
        llm_base_url="https://api.deepseek.com",
        paperlens_cache_dir=tmp_path,
    )


def response(content='{"ok": true}', **usage):
    return httpx.Response(200, json={
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20},
    })


def samples(tmp_path):
    return [json.loads(line) for line in (tmp_path / "telemetry/llm.jsonl").read_text().splitlines()]


def test_only_successful_attempts_record_their_own_usage_without_content(tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        return response("not-json" if len(seen) == 1 else '{"private-response": true}')

    with JsonProvider(settings_for(tmp_path), transport=httpx.MockTransport(handler),
                      sleeper=lambda _: None) as provider:
        provider.json("private-system", "private-document", reasoning_effort="none")
    [sample] = samples(tmp_path)
    assert sample["retry_index"] == 1
    assert sample["effective_reasoning_effort"] == "none"
    assert sample["input_tokens"] == 100 and sample["output_tokens"] == 20
    assert sample["duration_seconds"] > 0 and sample["concurrency"] == 1
    assert "private-" not in json.dumps(sample)


def test_overlapping_endpoint_requests_mark_both_samples(tmp_path):
    barrier = Barrier(2)

    def handler(_):
        barrier.wait(timeout=3)
        return response()

    with JsonProvider(settings_for(tmp_path), transport=httpx.MockTransport(handler)) as provider:
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda _: provider.json("s", "u"), range(2)))
    assert [sample["concurrency"] for sample in samples(tmp_path)] == [2, 2]
    assert not telemetry._active


def test_unwritable_telemetry_does_not_fail_successful_request(tmp_path):
    (tmp_path / "telemetry").write_text("occupied")
    with JsonProvider(settings_for(tmp_path), transport=httpx.MockTransport(lambda _: response())) as p:
        assert p.json("s", "u") == {"ok": True}


def test_log_is_bounded_and_keeps_complete_records(tmp_path, monkeypatch):
    monkeypatch.setattr(telemetry, "_MAX_BYTES", 2000)
    settings = settings_for(tmp_path)
    for _ in range(20):
        timing = telemetry.start_request(settings)
        telemetry.finish_request(timing)
        telemetry.record_request(settings, timing, {"prompt_tokens": 100, "completion_tokens": 50},
                                 effective_reasoning_effort="none")
    assert 1 <= len(samples(tmp_path)) < 20
    assert (tmp_path / "telemetry/llm.jsonl").stat().st_size < 3000


def test_stage_and_reasoning_breakdown_are_recorded_as_usage_not_added_to_total(tmp_path):
    settings = settings_for(tmp_path)
    timing = telemetry.start_request(settings)
    telemetry.finish_request(timing)
    telemetry.record_request(settings, timing, {
        "prompt_tokens": 100, "completion_tokens": 500,
        "completion_tokens_details": {"reasoning_tokens": 400},
    }, effective_reasoning_effort="low", task_stage="node_review")
    [row] = samples(tmp_path)
    assert row["task_stage"] == "node_review"
    assert row["output_tokens"] == 500
    assert row["reasoning_tokens"] == 400 and row["visible_output_tokens"] == 100


@pytest.mark.asyncio
async def test_stream_sample_uses_current_request_not_cumulative_usage(tmp_path):
    payload = {
        "choices": [{"delta": {"content": '{"answer":"ok"}'}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 40, "completion_tokens": 20},
    }

    def handler(_):
        return httpx.Response(200, text="data: " + json.dumps(payload) + "\n\ndata: [DONE]\n\n")

    with JsonProvider(settings_for(tmp_path)) as provider:
        provider.usage.prompt_tokens = 900
        for _ in range(2):
            chunks = [chunk async for chunk in stream_json(provider, "s", "u",
                       transport=httpx.MockTransport(handler))]
            assert chunks == ['{"answer":"ok"}']
    result = samples(tmp_path)
    assert len(result) == 2
    assert all(item["input_tokens"] == 40 and item["stream"] for item in result)
    assert not telemetry._active
