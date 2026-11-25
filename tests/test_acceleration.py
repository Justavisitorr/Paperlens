"""Real thread scheduling, pause/cache reuse and role routing without API cost."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from paperlens.analysis.engine import SectionNotes, _structured, analyze_document
from paperlens.config import ModelRoute, Settings
from paperlens.control import (
    ProcessingPaused,
    checkpoint,
    ordered_parallel_map,
    processing_control,
)
from paperlens.jobs import Job, JobManager
from paperlens.models import Block, Box, Page, PaperDocument
from paperlens.storage import StageStore


def config(tmp_path, **kwargs):
    return Settings(
        _env_file=None,
        llm_api_key="test-main-secret",
        paperlens_cache_dir=tmp_path / "cache",
        paperlens_output_dir=tmp_path / "outputs",
        **kwargs,
    )


def until(predicate):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for worker")


def test_dynamic_boost_starts_work_before_first_request_finishes_and_orders_results():
    entered = [threading.Event() for _ in range(3)]
    release = [threading.Event() for _ in range(3)]
    completed = []
    limit = [1]

    def stage(index):
        checkpoint()  # The worker inherits the job's ContextVar.
        entered[index].set()
        assert release[index].wait(4)
        return index * 10

    def run():
        with processing_control(lambda: None, lambda: limit[0]):
            return ordered_parallel_map(
                stage, range(3), on_complete=lambda i, _: completed.append(i)
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(run)
        try:
            assert entered[0].wait(2)
            assert not entered[1].wait(0.15)
            limit[0] = 3
            assert entered[1].wait(2) and entered[2].wait(2)
            release[2].set()
            until(lambda: completed == [2])
            release[1].set()
            until(lambda: completed == [2, 1])
            release[0].set()
            assert result.result(3) == [0, 10, 20]
        finally:
            for event in release:
                event.set()


def test_pause_waits_for_inflight_cache_and_resume_only_requests_missing_stages(tmp_path):
    entered = [threading.Event() for _ in range(5)]
    release = threading.Event()
    paused = threading.Event()
    store = StageStore(tmp_path, "stages")
    calls = []

    class Provider:
        def json(self, system, user, **kwargs):
            index = json.loads(user)["index"]
            calls.append(index)
            entered[index].set()
            assert release.wait(4)
            return {"section_summary": str(index), "findings": []}

    provider = Provider()

    def check():
        if paused.is_set():
            raise ProcessingPaused("pause")

    def stage(index):
        checkpoint()
        return _structured(
            provider, store, f"section-{index}", SectionNotes, "test", {"index": index}
        )

    def run():
        with processing_control(check, lambda: 3):
            return ordered_parallel_map(stage, range(5))

    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(run)
        try:
            assert all(event.wait(2) for event in entered[:3])
            paused.set()
            time.sleep(0.15)
            assert not result.done()  # Existing paid requests are still awaited.
            assert not entered[3].is_set()
            release.set()
            with pytest.raises(ProcessingPaused):
                result.result(3)
        finally:
            release.set()
    assert all(store.load(f"section-{index}") is not None for index in range(3))
    paused.clear()
    assert [part.section_summary for part in run()] == [str(i) for i in range(5)]
    assert sorted(calls) == list(range(5))


def test_default_runs_two_and_explicit_serial_context_restores():
    active = 0
    maximum = 0
    lock = threading.Lock()

    def stage(value):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        return value

    assert ordered_parallel_map(stage, range(4)) == list(range(4))
    assert maximum == 2
    maximum = 0
    with processing_control(lambda: None, lambda: 1):
        assert ordered_parallel_map(stage, range(4)) == list(range(4))
    assert maximum == 1
    with processing_control(lambda: (_ for _ in ()).throw(ProcessingPaused())):
        with pytest.raises(ProcessingPaused):
            ordered_parallel_map(stage, [1])
    assert ordered_parallel_map(stage, []) == []


def test_job_acceleration_actions_and_persistence_change_a_running_job(tmp_path):
    settings = config(tmp_path)
    entered = [threading.Event() for _ in range(3)]
    release = threading.Event()
    seen_budgets = []

    def runner(path, settings, **kwargs):
        seen_budgets.append(settings.llm_max_requests)

        def stage(index):
            entered[index].set()
            assert release.wait(4)
            return index

        assert ordered_parallel_map(stage, range(3)) == [0, 1, 2]
        path = settings.paperlens_output_dir / "result.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("<html>test</html>", encoding="utf-8")
        return SimpleNamespace(
            report_path=path, report=SimpleNamespace(run=SimpleNamespace(status="complete"))
        )

    manager = JobManager(settings, runner=runner)
    ident = "a" * 32
    try:
        manager.submit(ident, "test.pdf")
        assert entered[0].wait(2)
        assert manager.get(ident).public()["actions"] == ["accelerate", "pause", "cancel"]
        accelerated = manager.action(ident, "accelerate")
        assert accelerated["acceleration"] == {"enabled": True, "mode": "fast", "concurrency": 3}
        assert accelerated["actions"] == ["pause", "cancel"]
        assert entered[1].wait(2) and entered[2].wait(2)
        stored = json.loads((manager.root / ident / "job.json").read_text(encoding="utf-8"))
        assert stored["acceleration_enabled"] is True
        assert "test-main-secret" not in json.dumps(stored)
        release.set()
        until(lambda: manager.get(ident).status == "complete")
        assert seen_budgets == [settings.llm_max_requests]
    finally:
        release.set()
        manager.close()


@pytest.mark.parametrize("stage", ["pausing", "cancelling"])
def test_stopping_job_never_offers_accelerate(stage):
    assert Job(id="a" * 32, status="running", stage=stage).public()["actions"] == []


@pytest.mark.parametrize("initial_accelerated", [False, True])
def test_restart_restores_original_accounts_and_never_serializes_route_keys(
    tmp_path, initial_accelerated
):
    settings = config(
        tmp_path,
        llm_routes={
            "reading": ModelRoute(
                account_id="reading-account",
                model="small",
                base_url="https://example.org/v1",
                api_key="test-reading-secret",
            )
        },
    )
    entered, release = threading.Event(), threading.Event()

    def runner(path, settings, **kwargs):
        entered.set()
        assert release.wait(4)
        checkpoint()

    manager = JobManager(settings, runner=runner)
    ident = "b" * 32
    try:
        manager.submit(ident, "test.pdf", accelerated=initial_accelerated)
        assert entered.wait(2)
        if not initial_accelerated:
            manager.action(ident, "accelerate")
        manager.action(ident, "pause")
        release.set()
        until(lambda: manager.get(ident).status == "paused")
        source = manager.root / ident / f"{ident}.pdf"
        source.write_bytes(b"%PDF-test")
        raw = (source.parent / "job.json").read_text(encoding="utf-8")
        assert "test-reading-secret" not in raw and "test-main-secret" not in raw
    finally:
        release.set()
        manager.close()
    restored = []

    def restore(saved):
        restored.append(saved)
        return settings.model_copy(deep=True)

    manager = JobManager(settings, runner=runner, settings_restorer=restore)
    try:
        assert manager.get(ident).public()["acceleration"]["enabled"] is True
        manager.action(ident, "resume")
        assert restored[0]["llm_routes"]["reading"]["account_id"] == "reading-account"
    finally:
        manager.close()


def test_analysis_routes_roles_and_cache_identity_excludes_secrets(tmp_path):
    text = "This exact source quotation supports the research finding. " * 30
    document = PaperDocument(
        title="Test",
        source_name="test.pdf",
        sha256="abc",
        parser="fixture",
        pages=[Page(number=1, width=600, height=800)],
        blocks=[
            Block(
                id=f"b{i}",
                page=1,
                order=i,
                text=text,
                raw_text=text,
                bbox=Box(x0=0.1, y0=0.1, x1=0.9, y1=0.2),
            )
            for i in range(3)
        ],
    )
    calls = []

    class Provider:
        def __init__(self, role="main"):
            self.role = role

        def for_role(self, role):
            return Provider(role)

        def json(self, system, user, **kwargs):
            task = json.loads(user)
            calls.append(self.role)
            if "blocks" in task:
                block = task["blocks"][0]
                return {
                    "section_summary": block["block_id"],
                    "findings": [
                        {
                            "title": "Finding",
                            "kind": "contribution",
                            "summary": "Claim",
                            "evidence": [{"block_id": block["block_id"], "quote": text[:56]}],
                        }
                    ],
                }
            if "sections" in task:
                ids = task["sections"][0]["findings"][0]["evidence_ids"]
                return {
                    "title": "Test",
                    "summary": "Summary",
                    "research_question": "Question",
                    "nodes": [
                        {
                            "id": f"n{i}",
                            "title": "Finding",
                            "kind": "contribution",
                            "summary": "Claim",
                            "evidence_ids": ids,
                        }
                        for i in range(7)
                    ],
                    "edges": [],
                }
            return {
                "reviews": [
                    {"node_id": n["node_id"], "support": "supported", "reason": "Matches"}
                    for n in task["nodes"]
                ]
            }

    settings = config(tmp_path, llm_max_input_chars=2000)
    with processing_control(lambda: None, lambda: 3):
        result = analyze_document(document, Provider(), settings)
    assert calls.count("reading") == result.audit["chunks"]
    assert calls.count("main") == 1 and calls.count("verification") == 2
    assert all(node.support == "supported" for node in result.analysis.nodes)
    calls.clear()
    changed_secret = settings.model_copy(update={"llm_api_key": "different-secret"})
    # A different key for the same selected account/model must not invalidate work.
    again = analyze_document(document, Provider(), changed_secret)
    assert again.audit["cache_key"] == result.audit["cache_key"] and calls == []
    changed_model = settings.model_copy(
        update={
            "llm_routes": {
                "reading": ModelRoute(
                    account_id="other",
                    model="other",
                    base_url="https://example.org/v1",
                    api_key="other-secret",
                )
            }
        }
    )
    other = analyze_document(document, Provider(), changed_model)
    assert other.audit["cache_key"] != result.audit["cache_key"]
