"""Local measurement selection, frozen job limits and the 1..5 worker ceiling."""

import json
import threading
from datetime import UTC, datetime, timedelta

import pytest
from test_acceleration import config, until
from test_workspace_api import fake_result

from paperlens.concurrency_policy import (
    execution_concurrency,
    job_execution_concurrency,
    measured_concurrency,
)
from paperlens.config import ModelRoute
from paperlens.control import concurrency_limit, ordered_parallel_map, processing_concurrency
from paperlens.jobs import Job, JobManager
from paperlens.storage import write_json


def sample(settings, **updates):
    return {
        "endpoint": settings.llm_base_url, "model": settings.llm_model,
        "recorded_at": datetime.now(UTC).isoformat(), "experimental": True,
        "rows": [{"width": width, "wall_seconds": seconds, "actual_requests": 5,
                  "errors": int(width in {1, 4}), "retries": 0,
                  "observed_max_inflight": width}
                 for width, seconds in [(1, 5.2), (2, 2.8), (3, 2.15), (4, 1.93), (5, 1.21)]],
        **updates,
    }


def save(settings, entry):
    write_json(settings.paperlens_cache_dir / "telemetry/concurrency.json",
               {"version": 1, "models": [entry]})


def test_single_round_measurement_keeps_ordinary_conservative_and_fast_uses_best(tmp_path):
    settings = config(tmp_path)
    assert measured_concurrency(sample(settings)) == (3, 5)
    save(settings, sample(settings))
    assert execution_concurrency(settings, False) == 3
    assert execution_concurrency(settings, True) == 5
    replicated = sample(settings, experimental=False)
    assert measured_concurrency(replicated) == (5, 5)


@pytest.mark.parametrize("change", [
    {"model": "unmeasured-model"}, {"endpoint": "https://unmeasured.example"},
    {"recorded_at": (datetime.now(UTC) - timedelta(days=8)).isoformat()},
    {"recorded_at": (datetime.now(UTC) + timedelta(days=1)).isoformat()},
    {"recorded_at": "invalid"}, {"rows": []}, {"rows": None},
])
def test_unrelated_stale_future_or_invalid_measurement_uses_defaults(tmp_path, change):
    settings = config(tmp_path)
    save(settings, sample(settings, **change))
    assert execution_concurrency(settings, False) == 2
    assert execution_concurrency(settings, True) == 3


def test_absent_or_corrupt_file_never_starts_network_work(tmp_path):
    settings = config(tmp_path)
    assert execution_concurrency(settings, False) == 2
    path = settings.paperlens_cache_dir / "telemetry/concurrency.json"
    path.parent.mkdir(parents=True)
    path.write_text("not-json")
    assert execution_concurrency(settings, True) == 3


@pytest.mark.parametrize("width", [1, 2, 3, 4, 5])
def test_explicit_worker_ceiling_is_really_supported(width):
    barrier = threading.Barrier(width)

    def work(index):
        barrier.wait(timeout=3)
        return index

    with processing_concurrency(width):
        assert concurrency_limit() == width
        assert ordered_parallel_map(work, range(width)) == list(range(width))


def test_submitted_limits_are_frozen_persisted_and_old_jobs_keep_defaults(tmp_path):
    settings = config(tmp_path)
    save(settings, sample(settings))
    entered, release = threading.Event(), threading.Event()
    seen = []

    def runner(path, selected, **kwargs):
        seen.append(concurrency_limit())
        entered.set()
        assert release.wait(3)
        return fake_result(selected)

    manager = JobManager(settings, runner=runner)
    ident = "f" * 32
    try:
        manager.submit(ident, "test.pdf", accelerated=True)
        assert entered.wait(2)
        assert manager.get(ident).public()["acceleration"]["concurrency"] == 5
        save(settings, sample(settings, recorded_at="invalid"))
        assert manager._concurrency(ident) == 5  # Active work stays at its submitted snapshot.
        release.set()
        until(lambda: manager.get(ident).status == "complete")
        stored = json.loads((manager.root / ident / "job.json").read_text(encoding="utf-8"))
        assert (stored["normal_concurrency"], stored["accelerated_concurrency"]) == (3, 5)
    finally:
        release.set()
        manager.close()
    restored = JobManager(settings, runner=runner)
    try:
        assert restored.get(ident).public()["acceleration"]["concurrency"] == 5 and seen == [5]
    finally:
        restored.close()
    legacy = Job(id="e" * 32)
    assert legacy.public()["acceleration"]["concurrency"] == 2
    legacy.acceleration_enabled = True
    assert legacy.public()["acceleration"]["concurrency"] == 3


def test_job_does_not_apply_one_models_result_to_an_unmeasured_role(tmp_path):
    settings = config(tmp_path, llm_routes={"verification": ModelRoute(
        account_id="other", model="unmeasured", base_url="https://other.example", api_key="fake",
    )})
    save(settings, sample(settings))

    def runner(path, selected, **kwargs):
        return fake_result(selected)

    manager = JobManager(settings, runner=runner)
    try:
        job = manager.submit("d" * 32, "test.pdf", accelerated=True)
        assert (job.normal_concurrency, job.accelerated_concurrency) == (2, 3)
    finally:
        manager.close()


def test_shared_job_policy_counts_only_enabled_chat_work_and_actual_vision_model(tmp_path):
    settings = config(tmp_path, paperlens_vision_enabled=False, llm_routes={"chat": ModelRoute(
        account_id="other", model="unmeasured", base_url="https://other.example", api_key="fake",
    )})
    save(settings, sample(settings))
    assert job_execution_concurrency(settings, True) == 5
    assert job_execution_concurrency(settings.model_copy(update={
        "paperlens_auto_translate_enabled": True,
    }), True) == 3
    assert job_execution_concurrency(settings.model_copy(update={
        "paperlens_vision_enabled": True, "paperlens_vision_model": "unmeasured-vision",
    }), True) == 3
