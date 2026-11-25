"""Scoped experimental ultra mode, real thread limits and legacy API compatibility."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from test_acceleration import config, until
from test_chat_stream import SplitStream, sse
from test_concurrency_policy import sample, save
from test_workspace_api import fake_result, wait_status

from paperlens import provider as provider_module
from paperlens import provider_stream, server
from paperlens.concurrency_policy import (
    execution_concurrency,
    job_execution_concurrency,
    measured_ultra,
)
from paperlens.config import ModelRoute
from paperlens.control import concurrency_limit, ordered_parallel_map, processing_concurrency
from paperlens.errors import ProviderError
from paperlens.jobs import JobManager
from paperlens.provider import NETWORK_REQUEST_LIMIT, JsonProvider, RequestBudget


def ultra_sample(**updates):
    return {
        "recorded_at": datetime.now(UTC).isoformat(),
        "rows": [{"width": width, "wall_seconds": seconds, "actual_requests": 16,
                  "errors": 0, "retries": 0, "observed_http_peak": width}
                 for width, seconds in [(5, 4.8), (8, 2.8), (12, 2.6), (16, 1.3)]],
        **updates,
    }


def seed(settings, **updates):
    save(settings, sample(settings, ultra_study=ultra_sample(**updates)))


def test_ultra_requires_clear_reliable_gain_and_uses_smallest_near_fast_width():
    assert measured_ultra(ultra_sample()) == 16
    marginal = ultra_sample()
    for row in marginal["rows"]:
        row["wall_seconds"] = 4.8 if row["width"] == 5 else 4.5
    assert measured_ultra(marginal) is None
    near = ultra_sample()
    for row in near["rows"]:
        if row["width"] != 5:
            row["wall_seconds"] = {8: 1.4, 12: 1.35, 16: 1.3}[row["width"]]
    assert measured_ultra(near) == 8
    assert measured_ultra(ultra_sample(rows=None)) is None


@pytest.mark.parametrize("expired", [False, True])
def test_ultra_is_exact_model_scoped_and_expiry_falls_back_to_fast(tmp_path, expired):
    settings = config(tmp_path)
    updates = {"recorded_at": (datetime.now(UTC) - timedelta(days=8)).isoformat()} if expired else {}
    seed(settings, **updates)
    assert execution_concurrency(settings, speed_mode="normal") == 3
    assert execution_concurrency(settings, speed_mode="fast") == 5
    assert execution_concurrency(settings, speed_mode="ultra") == (5 if expired else 16)
    other = settings.model_copy(update={"llm_model": "unmeasured-model"})
    assert execution_concurrency(other, speed_mode="ultra") == 3
    custom = settings.model_copy(update={"llm_base_url": "https://custom.example"})
    save(custom, sample(custom, ultra_study=ultra_sample()))
    assert execution_concurrency(custom, speed_mode="ultra") == 5


def test_ultra_does_not_expand_an_unmeasured_role_in_same_job(tmp_path):
    settings = config(tmp_path, llm_routes={"verification": ModelRoute(
        account_id="other", model="other", base_url="https://other.example", api_key="fake",
    )})
    seed(settings)
    assert job_execution_concurrency(settings, speed_mode="ultra") == 3


@pytest.mark.parametrize("width", [8, 12, 16])
def test_explicit_ultra_width_is_real_not_hidden_by_old_five_worker_ceiling(width):
    barrier = threading.Barrier(width)

    def stage(index):
        barrier.wait(timeout=4)
        return index

    with processing_concurrency(width):
        assert concurrency_limit() == width
        assert ordered_parallel_map(stage, range(width)) == list(range(width))


def test_running_job_upgrades_three_to_five_to_sixteen_and_restores_snapshot(tmp_path):
    settings = config(tmp_path)
    seed(settings)
    entered, release = [threading.Event() for _ in range(16)], threading.Event()

    def runner(path, selected, **kwargs):
        def stage(index):
            entered[index].set()
            assert release.wait(5)
            return index

        assert ordered_parallel_map(stage, range(16)) == list(range(16))
        return fake_result(selected)

    manager = JobManager(settings, runner=runner)
    ident = "e" * 32
    try:
        manager.submit(ident, "test.pdf", speed_mode="normal")
        assert all(event.wait(2) for event in entered[:3])
        assert not entered[3].is_set()
        assert manager.get(ident).public()["actions"] == ["accelerate", "ultra", "pause", "cancel"]
        assert manager.action(ident, "accelerate")["acceleration"]["concurrency"] == 5
        assert entered[4].wait(2)
        upgraded = manager.action(ident, "ultra")
        assert upgraded["speed_mode"] == upgraded["acceleration"]["mode"] == "ultra"
        assert upgraded["acceleration"]["concurrency"] == 16
        assert all(event.wait(2) for event in entered)
        raw = json.loads((manager.root / ident / "job.json").read_text(encoding="utf-8"))
        assert raw["ultra_mode"] and raw["ultra_concurrency"] == 16
        assert raw["configuration"]["llm_max_requests"] == settings.llm_max_requests
        release.set()
        until(lambda: manager.get(ident).status == "complete")
    finally:
        release.set()
        manager.close()
    restored = JobManager(settings, runner=runner)
    try:
        assert restored.get(ident).worker_limit() == 16
    finally:
        restored.close()


@pytest.mark.parametrize("mode,accelerated,expected", [
    ("normal", True, 3), ("fast", False, 5), ("ultra", False, 16),
])
def test_submission_explicit_speed_mode_overrides_old_boolean(
    tmp_path, monkeypatch, mode, accelerated, expected
):
    settings = config(tmp_path)
    seed(settings)
    seen = []

    def run(path, selected, **kwargs):
        seen.append(concurrency_limit())
        return fake_result(selected)

    monkeypatch.setattr(server, "run_pipeline", run)
    with TestClient(server.create_app(settings)) as client:
        response = client.post("/api/jobs", data={
            "speed_mode": mode, "accelerated": str(accelerated).lower(),
            "unlimited_confirmed": "true",
        }, files={"file": ("test.pdf", b"%PDF-test")})
        assert response.status_code == 202
        job = wait_status(client, response.json()["id"], "complete")
        assert job["speed_mode"] == mode and job["acceleration"]["concurrency"] == expected
        assert seen == [expected]
        invalid = client.post("/api/jobs", data={"speed_mode": "2500"},
                              files={"file": ("test.pdf", b"%PDF-test")})
        assert invalid.status_code == 422


def test_estimate_endpoint_propagates_ultra_mode_without_model_calls(tmp_path, monkeypatch):
    settings = config(tmp_path)
    monkeypatch.setattr(server.estimates, "estimate", lambda current, **kwargs: {
        "speed_mode": kwargs["speed_mode"], "accelerated": kwargs["accelerated"],
    })
    with TestClient(server.create_app(settings)) as client:
        response = client.post("/api/estimates", data={"pages": 3, "speed_mode": "ultra"})
        assert response.json() == {"speed_mode": "ultra", "accelerated": True}


def test_running_http_upgrade_accepts_ultra_speed_on_legacy_accelerate_route(tmp_path, monkeypatch):
    settings = config(tmp_path)
    seed(settings)
    entered, release = threading.Event(), threading.Event()

    def run(path, selected, **kwargs):
        entered.set()
        assert release.wait(4)
        return fake_result(selected)

    monkeypatch.setattr(server, "run_pipeline", run)
    with TestClient(server.create_app(settings)) as client:
        response = client.post("/api/jobs", data={"speed_mode": "fast", "unlimited_confirmed": "true"},
                               files={"file": ("test.pdf", b"%PDF-test")})
        ident = response.json()["id"]
        try:
            assert entered.wait(2)
            assert client.post(f"/api/jobs/{ident}/ultra").status_code == 409
            invalid = client.post(f"/api/jobs/{ident}/accelerate", json={
                "confirmed": True, "speed_mode": "2500",
            })
            assert invalid.status_code == 422
            upgraded = client.post(f"/api/jobs/{ident}/accelerate", json={
                "confirmed": True, "speed_mode": "ultra",
            })
            assert upgraded.status_code == 200 and upgraded.json()["speed_mode"] == "ultra"
            assert upgraded.json()["acceleration"]["concurrency"] == 16
        finally:
            release.set()
        wait_status(client, ident, "complete")


def test_import_speed_mode_is_validated_before_download_and_applies_to_first_work(tmp_path, monkeypatch):
    settings = config(tmp_path)
    seed(settings)
    seen, downloads = [], []

    class Sources:
        def download(self, candidate, path, max_bytes):
            downloads.append(candidate)
            path.parent.mkdir(parents=True)
            path.write_bytes(b"%PDF-test")
            return {"title": "Small local fixture"}

    def run(path, selected, **kwargs):
        seen.append(concurrency_limit())
        return fake_result(selected)

    monkeypatch.setattr(server, "run_pipeline", run)
    app = server.create_app(settings)
    with TestClient(app) as client:
        app.state.importer = Sources()
        for invalid in ("2500", [], True):
            response = client.post("/api/imports", json={"candidate_id": "fixture", "speed_mode": invalid})
            assert response.status_code == 422 and not downloads
        response = client.post("/api/imports", json={"candidate_id": "fixture", "speed_mode": "ultra",
                                                     "accelerated": False, "unlimited_confirmed": True})
        assert response.status_code == 202
        job = wait_status(client, response.json()["id"], "complete")
        assert job["speed_mode"] == "ultra" and seen == [16] and downloads == ["fixture"]


def test_multiple_jobs_share_bounded_global_network_capacity(tmp_path):
    settings = config(tmp_path, llm_max_retries=0)
    seed(settings)
    assert NETWORK_REQUEST_LIMIT == 32
    lock, release, reached = threading.Lock(), threading.Event(), threading.Event()
    state = [0, 0]
    budget = RequestBudget(40)

    def respond(request):
        with lock:
            state[0] += 1
            state[1] = max(state)
            if state[0] == 32:
                reached.set()
        try:
            assert release.wait(6)
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            })
        finally:
            with lock:
                state[0] -= 1

    transport = httpx.MockTransport(respond)

    def request(index):
        with JsonProvider(settings, budget=budget, transport=transport) as provider:
            return provider.json("test", "{}")

    with ThreadPoolExecutor(max_workers=40) as pool:
        futures = [pool.submit(request, i) for i in range(40)]
        try:
            assert reached.wait(4) and state[1] == 32
            release.set()
            assert all(future.result(4) == {} for future in futures)
        finally:
            release.set()
    assert state[1] == 32 and budget.used == 40


@pytest.mark.parametrize("mixed_endpoints", [False, True])
def test_unmeasured_providers_keep_former_shared_six_request_limit(tmp_path, mixed_endpoints):
    settings = config(tmp_path, llm_max_retries=0)
    lock, release, reached, overflow = (
        threading.Lock(), threading.Event(), threading.Event(), threading.Event()
    )
    state, budget = [0, 0], RequestBudget(12)

    def respond(request):
        with lock:
            state[0] += 1
            state[1] = max(state)
            if state[0] == 6:
                reached.set()
            if state[0] > 6:
                overflow.set()
        try:
            assert release.wait(4)
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            })
        finally:
            with lock:
                state[0] -= 1

    def request(index):
        selected = settings.model_copy(update={"llm_base_url": "https://other.example"}) if (
            mixed_endpoints and index % 2
        ) else settings
        with JsonProvider(selected, budget=budget, transport=httpx.MockTransport(respond)) as provider:
            return provider.json("test", "{}")

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(request, index) for index in range(12)]
        try:
            assert reached.wait(3)
            assert not overflow.wait(0.15)
            release.set()
            assert all(future.result(3) == {} for future in futures)
        finally:
            release.set()
    assert state[1] == 6 and budget.used == 12


def test_reserved_budget_wait_releases_both_network_pools(tmp_path, monkeypatch):
    settings = config(tmp_path, llm_max_retries=0)
    conservative, total = threading.BoundedSemaphore(1), threading.BoundedSemaphore(1)
    monkeypatch.setattr(provider_module, "_CONSERVATIVE_NETWORK_SLOTS", conservative)
    monkeypatch.setattr(provider_module, "_NETWORK_SLOTS", total)
    budget = RequestBudget(1)
    owner, waiter = budget.reserve(), budget.reserve()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }))
    with JsonProvider(settings, budget=owner, transport=transport) as sibling:
        with JsonProvider(settings, budget=waiter, transport=transport) as waiting:
            with ThreadPoolExecutor(max_workers=2) as pool:
                blocked = pool.submit(waiting.json, "test", "{}")
                try:
                    assert pool.submit(sibling.json, "test", "{}").result(3) == {}
                    owner.close()
                    with pytest.raises(ProviderError, match="请求预算"):
                        blocked.result(3)
                finally:
                    owner.close()
                    waiter.close()
    for slots in (conservative, total):
        assert slots.acquire(blocking=False)
        slots.release()


def test_stream_cancel_releases_both_shared_network_pools(tmp_path, monkeypatch):
    conservative, total = threading.BoundedSemaphore(1), threading.BoundedSemaphore(1)
    monkeypatch.setattr(provider_module, "_CONSERVATIVE_NETWORK_SLOTS", conservative)
    monkeypatch.setattr(provider_stream, "_NETWORK_SLOTS", total)
    settings = config(tmp_path)
    upstream = SplitStream(sse('{"answer":"' + "Unfinished text. " * 30))

    async def handle(request):
        return httpx.Response(200, stream=upstream)

    async def run(provider):
        chunks = provider_stream.stream_json(provider, "s", "u", transport=httpx.MockTransport(handle))
        await anext(chunks)
        await chunks.aclose()

    with JsonProvider(settings) as provider:
        asyncio.run(run(provider))
        assert provider.usage.requests == 1 and upstream.closed
    for slots in (conservative, total):
        assert slots.acquire(blocking=False)
        slots.release()


def test_stream_waiting_reserved_budget_does_not_block_async_loop_or_sibling(tmp_path, monkeypatch):
    conservative, total = threading.BoundedSemaphore(1), threading.BoundedSemaphore(1)
    monkeypatch.setattr(provider_module, "_CONSERVATIVE_NETWORK_SLOTS", conservative)
    monkeypatch.setattr(provider_module, "_NETWORK_SLOTS", total)
    monkeypatch.setattr(provider_stream, "_NETWORK_SLOTS", total)
    settings, budget = config(tmp_path), RequestBudget(1)
    owner, waiting = budget.reserve(), budget.reserve()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }))
    with JsonProvider(settings, budget=owner, transport=transport) as sibling:
        with JsonProvider(settings, budget=waiting) as waiter:
            async def run():
                chunks = provider_stream.stream_json(waiter, "s", "u", transport=transport)
                pending = asyncio.create_task(anext(chunks))
                await asyncio.sleep(0.12)
                assert not pending.done()
                assert sibling.json("test", "{}") == {}
                owner.close()
                with pytest.raises(ProviderError, match="请求预算"):
                    await asyncio.wait_for(pending, 2)
                await chunks.aclose()

            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(lambda: asyncio.run(run()))
                try:
                    future.result(3)
                finally:
                    # Also unstick a regression that calls blocking take() inside
                    # the async generator while the sibling owns its reservation.
                    owner.close()
                    waiting.close()
            assert waiter.usage.requests == 0 and sibling.usage.requests == budget.used == 1
