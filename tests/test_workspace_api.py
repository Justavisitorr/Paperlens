"""Backend contracts exercised through HTTP and durable local storage, without API fees."""

from __future__ import annotations

import hashlib
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from paperlens import server
from paperlens.config import Settings
from paperlens.jobs import Job
from paperlens.library import LibraryStore
from paperlens.models import (
    Block,
    Box,
    DashboardReport,
    Page,
    PaperAnalysis,
    PaperDocument,
    Quality,
    ReadingDocument,
    RunInfo,
)
from paperlens.storage import write_json


@pytest.fixture
def report():
    return DashboardReport(
        document=PaperDocument(
            title="论文",
            source_name="source.pdf",
            sha256="a" * 64,
            parser="test",
            pages=[Page(number=1, width=600, height=800)],
            blocks=[
                Block(
                    id="b1",
                    page=1,
                    order=0,
                    bbox=Box(x0=0, y0=0, x1=1, y1=1),
                    text="前缀😀稀疏度表示零元素占比。",
                    raw_text="前缀😀稀疏度表示零元素占比。",
                )
            ],
        ),
        analysis=PaperAnalysis(
            title="论文", summary="定义", research_question="如何度量", nodes=[], edges=[]
        ),
        evidence=[],
        graphs=[],
        previews=[],
        quality=Quality(
            total_pages=1, parsed_pages=1, text_chars=18, grounded_nodes=0, total_nodes=0
        ),
        run=RunInfo(
            id="a" * 32,
            created_at="2026-09-27T00:00:00Z",
            model="test",
            parser="test",
            status="complete",
        ),
    )


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        llm_api_key="test-hidden-key",
        paperlens_cache_dir=tmp_path / "cache",
        paperlens_output_dir=tmp_path / "outputs",
    )


def seed(settings, report):
    destination = settings.paperlens_output_dir / "paper"
    write_json(destination / "report.json", report.model_dump(mode="json"))
    (destination / "source.pdf").write_bytes(b"%PDF-test-copy")
    return destination


def test_library_settings_annotations_survive_service_restart(settings, report):
    seed(settings, report)
    ident = report.run.id
    route = f"/api/library/{ident}"
    with TestClient(server.create_app(settings)) as client:
        assert client.get("/api/library").json()["papers"][0]["id"] == ident
        assert client.get(route + "/report").json()["document"]["sha256"] == "a" * 64
        assert client.patch(route, json={"title": "新名称"}).status_code == 200
        result = client.post(
            route + "/annotations",
            json={"block_id": "b1", "start": 4, "end": 7, "text": "稀疏度", "note": "定义"},
        )
        assert result.status_code == 201, result.text
        annotation = result.json()["id"]
        assert (
            client.post(
                route + "/annotations",
                json={"block_id": "b1", "start": 3, "end": 7, "text": "稀疏度"},
            ).status_code
            == 422
        )
        assert (
            client.put("/api/settings", json={"values": {"LLM_MAX_INPUT_CHARS": 8000}}).json()[
                "schema"
            ]["meta"]["stale"]
            is False
        )
        assert client.put("/api/settings", json={"values": {"LLM_MAX_INPUT_CHARS": 10}}).json()[
            "errors"
        ]
        public = client.get("/api/settings").text
        assert "test-hidden-key" not in public
        assert client.post("/api/catalog/refresh").status_code == 409
        assert set(client.get("/api/health").json()["features"]) >= {
            "library",
            "explain",
            "chat",
            "annotations",
        }
    with TestClient(server.create_app(settings)) as client:
        assert client.get("/api/library").json()["papers"][0]["title"] == "新名称"
        assert client.get(route + "/annotations").json()["annotations"][0]["id"] == annotation
        assert client.delete(route + "/annotations/" + annotation).json() == {"deleted": True}
        assert client.delete(route).json()["deleted"]
        assert not client.get("/api/library").json()["papers"]
        assert client.get(route + "/report").status_code == 404


def test_library_corruption_and_duplicate_copies_refresh(settings, report):
    path = seed(settings, report) / "report.json"
    duplicate = settings.paperlens_output_dir / "z-copy" / "report.json"
    write_json(duplicate, report.model_dump(mode="json"))
    library = LibraryStore(settings.paperlens_output_dir)
    assert len(library.list()["papers"]) == 1
    path.write_text("broken", encoding="utf-8")
    library.refresh(force=True)
    assert library.get_record(report.run.id)["relative_path"] == "z-copy/report.json"
    duplicate.unlink()
    library.refresh(force=True)
    assert library.list()["papers"] == []


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("put", "/api/settings", {"values": {"LLM_API_KEY": "unsafe-new"}}),
        ("post", "/api/catalog/refresh", {}),
        ("delete", "/api/library/" + "a" * 32, {}),
        ("delete", "/api/jobs/history", {}),
    ],
)
def test_all_mutations_reject_foreign_origins(settings, method, path, payload):
    with TestClient(server.create_app(settings)) as client:
        response = client.request(
            method, path, json=payload, headers={"Origin": "https://evil.example"}
        )
        assert response.status_code == 403
        assert client.get("/api/health", headers={"Host": "evil.example"}).status_code == 400


def wait_status(client, ident, status):
    end = time.monotonic() + 4
    while time.monotonic() < end:
        data = client.get(f"/api/jobs/{ident}").json()
        if data["status"] == status:
            return data
        time.sleep(0.01)
    raise AssertionError(data)


def fake_result(settings):
    path = settings.paperlens_output_dir / "result.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("<html>test</html>", encoding="utf-8")
    return SimpleNamespace(
        report_path=path, report=SimpleNamespace(run=SimpleNamespace(status="complete"))
    )


@pytest.mark.parametrize("accelerated", [None, False, True])
def test_upload_acceleration_applies_at_first_request_and_survives_restart(
    monkeypatch, settings, accelerated
):
    from paperlens.control import concurrency_limit

    seen = []

    def run(path, config, **kwargs):
        seen.append(concurrency_limit())
        return fake_result(config)

    monkeypatch.setattr(server, "run_pipeline", run)
    with TestClient(server.create_app(settings)) as client:
        data = {"unlimited_confirmed": "true"}
        if accelerated is not None:
            data["accelerated"] = "true" if accelerated else "false"
        response = client.post(
            "/api/jobs", data=data, files={"file": ("test.pdf", b"%PDF-test")}
        )
        assert response.status_code == 202
        ident = response.json()["id"]
        complete = wait_status(client, ident, "complete")
        expected = {"enabled": bool(accelerated), "mode": "fast" if accelerated else "normal",
                    "concurrency": 3 if accelerated else 2}
        assert complete["acceleration"] == expected and seen == [expected["concurrency"]]
    with TestClient(server.create_app(settings)) as client:
        assert client.get(f"/api/jobs/{ident}").json()["acceleration"] == expected


@pytest.mark.parametrize("accelerated", [False, True])
def test_estimate_endpoint_propagates_acceleration_without_model_calls(
    monkeypatch, settings, accelerated
):
    seen = []

    def estimate(current, **kwargs):
        seen.append(kwargs["accelerated"])
        return {"accelerated": kwargs["accelerated"]}

    monkeypatch.setattr(server.estimates, "estimate", estimate)
    with TestClient(server.create_app(settings)) as client:
        response = client.post(
            "/api/estimates", data={"pages": "5", "accelerated": str(accelerated).lower()}
        )
        assert response.status_code == 200 and response.json()["accelerated"] is accelerated
    assert seen == [accelerated]


@pytest.mark.parametrize("accelerated", [False, True])
def test_selected_import_runs_once_with_chosen_profile(monkeypatch, settings, accelerated):
    from paperlens.control import concurrency_limit
    from paperlens.errors import InputError

    calls = []
    captured = []

    class Sources:
        def resolve(self, query):
            return {"candidates": [{"id": "candidate", "title": query}]}

        def download(self, ident, path, max_bytes):
            if ident == "unavailable":
                raise InputError("未找到公开 PDF")
            assert max_bytes == settings.paperlens_max_upload_mb * 1024 * 1024
            calls.append(ident)
            path.parent.mkdir(parents=True)
            path.write_bytes(b"%PDF-local-test")
            return {"title": "Source paper", "authors": ["Author"]}

    def run(path, config, **kwargs):
        captured.append(
            (path.read_bytes(), config.paperlens_processing_profile, kwargs["source_name"],
             concurrency_limit())
        )
        assert (path.parent / "source-metadata.json").is_file()
        return fake_result(config)

    monkeypatch.setattr(server, "run_pipeline", run)
    app = server.create_app(settings)
    with TestClient(app) as client:
        app.state.importer = Sources()
        response = client.post("/api/imports/resolve", json={"query": "Source paper"})
        assert response.status_code == 200 and response.json()["candidates"]
        assert client.get("/api/jobs").json()["jobs"] == [] and not calls
        response = client.post(
            "/api/imports", json={"candidate_id": "candidate", "profile": "detailed", "unlimited_confirmed": True,
                                  "accelerated": accelerated}
        )
        assert response.status_code == 202, response.text
        wait_status(client, response.json()["id"], "complete")
        assert calls == ["candidate"]
        assert captured == [(b"%PDF-local-test", "detailed", "Source paper.pdf", 3 if accelerated else 2)]
        job = client.get(f'/api/jobs/{response.json()["id"]}').json()
        assert job["acceleration"] == {"enabled": accelerated, "mode": "fast" if accelerated else "normal",
                                       "concurrency": 3 if accelerated else 2}
        unavailable = client.post("/api/imports", json={"candidate_id": "unavailable", "unlimited_confirmed": True})
        assert unavailable.status_code == 422
        assert len(client.get("/api/jobs").json()["jobs"]) == 1
        invalid = client.post("/api/imports", json={"candidate_id": "candidate", "accelerated": "false"})
        assert invalid.status_code == 422 and calls == ["candidate"]


def test_pause_resume_freezes_job_settings_and_controls_queue(monkeypatch, settings):
    entered = threading.Event()
    release = threading.Event()
    seen = []

    def run(path, config, *, progress, source_name=None):
        seen.append(config.llm_max_input_chars)
        entered.set()
        assert release.wait(4)
        progress("analysis", 0.4, "完成一个步骤")
        return fake_result(config)

    monkeypatch.setattr(server, "run_pipeline", run)
    with TestClient(server.create_app(settings)) as client:

        def upload():
            return client.post("/api/jobs", files={"file": ("test.pdf", b"%PDF-test")}, data={"unlimited_confirmed": "true"}).json()["id"]

        try:
            first = upload()
            assert entered.wait(2)
            second = upload()
            assert client.post(f"/api/jobs/{second}/pause").json()["status"] == "paused"
            assert client.post(f"/api/jobs/{first}/pause").json()["stage"] == "pausing"
            assert client.post(f"/api/jobs/{first}/pause").status_code == 409
            assert (
                client.put(
                    "/api/settings", json={"values": {"LLM_MAX_INPUT_CHARS": 8000}}
                ).status_code
                == 200
            )
            release.set()
            assert wait_status(client, first, "paused")["actions"] == ["resume", "cancel"]
            assert client.post(f"/api/jobs/{first}/resume").status_code == 200
            wait_status(client, first, "complete")
            assert seen == [18000, 18000]
            assert client.post(f"/api/jobs/{second}/cancel").json()["status"] == "cancelled"
            third = upload()
            wait_status(client, third, "complete")
            assert seen[-1] == 8000
            assert len(client.get("/api/jobs").json()["jobs"]) == 3
        finally:
            release.set()


def test_parallel_limit_admits_two_then_queues_third(monkeypatch, settings):
    settings = settings.model_copy(update={"paperlens_max_parallel_jobs": 2})
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    calls = []

    def run(path, config, **kwargs):
        with lock:
            calls.append(path)
            if len(calls) == 2:
                entered.set()
        assert release.wait(4)
        return fake_result(config)

    monkeypatch.setattr(server, "run_pipeline", run)
    with TestClient(server.create_app(settings)) as client:
        try:
            ids = [
                client.post("/api/jobs", files={"file": ("test.pdf", b"%PDF-test")}, data={"unlimited_confirmed": "true"}).json()["id"]
                for _ in range(3)
            ]
            assert entered.wait(2)
            assert client.get(f"/api/jobs/{ids[2]}").json()["status"] == "queued"
            assert client.get("/api/health").json()["running_jobs"] == 2
        finally:
            release.set()
        for ident in ids:
            wait_status(client, ident, "complete")


def test_newly_completed_paper_is_visible_inside_scan_throttle(settings, report):
    with TestClient(server.create_app(settings)) as client:
        assert client.get("/api/library").json()["papers"] == []
        seed(settings, report)
        assert client.get(f"/api/library/{report.run.id}/report").status_code == 200
        assert len(client.get("/api/library").json()["papers"]) == 1


def test_job_action_rolls_back_when_storage_fails(monkeypatch, settings):
    from paperlens.jobs import JobManager

    entered, release = threading.Event(), threading.Event()

    def run(path, config, **kwargs):
        entered.set()
        assert release.wait(5)
        return fake_result(config)

    monkeypatch.setattr(server, "run_pipeline", run)
    with TestClient(server.create_app(settings)) as client:
        try:
            first = client.post("/api/jobs", files={"file": ("a.pdf", b"%PDF-a")}, data={"unlimited_confirmed": "true"}).json()["id"]
            assert entered.wait(2)
            second = client.post("/api/jobs", files={"file": ("b.pdf", b"%PDF-b")}, data={"unlimited_confirmed": "true"}).json()["id"]
            with monkeypatch.context() as patch:

                def failed_save(self, job):
                    raise OSError("disk unavailable")

                patch.setattr(JobManager, "_save", failed_save)
                assert client.post(f"/api/jobs/{second}/pause").status_code == 503
                assert client.get(f"/api/jobs/{second}").json()["status"] == "queued"
                assert client.post(f"/api/jobs/{first}/cancel").status_code == 503
                assert client.get(f"/api/jobs/{first}").json()["stage"] != "cancelling"
            release.set()
            wait_status(client, first, "complete")
            wait_status(client, second, "complete")
        finally:
            release.set()


def test_pause_storage_failure_does_not_kill_worker(monkeypatch, settings):
    from paperlens.jobs import JobManager

    entered, release = threading.Event(), threading.Event()
    original_save = JobManager._save

    def run(path, config, *, progress, **kwargs):
        entered.set()
        assert release.wait(5)
        progress("analysis", 0.5, "检查暂停")
        return fake_result(config)

    def fail_paused(self, job):
        if job.status == "paused":
            raise OSError("disk unavailable")
        return original_save(self, job)

    monkeypatch.setattr(server, "run_pipeline", run)
    monkeypatch.setattr(JobManager, "_save", fail_paused)
    with TestClient(server.create_app(settings)) as client:
        try:
            first = client.post("/api/jobs", files={"file": ("a.pdf", b"%PDF-a")}, data={"unlimited_confirmed": "true"}).json()["id"]
            assert entered.wait(2)
            assert client.post(f"/api/jobs/{first}/pause").status_code == 200
            release.set()
            stopped = wait_status(client, first, "paused")
            assert "状态无法保存" in stopped["error"]
            second = client.post("/api/jobs", files={"file": ("b.pdf", b"%PDF-b")}, data={"unlimited_confirmed": "true"}).json()["id"]
            wait_status(client, second, "complete")
            assert client.get("/api/health").json()["ok"]
        finally:
            release.set()


def history_job(settings, number, status="complete"):
    job = Job(
        id=f"{number:032x}",
        status=status,
        created_at=f"2025-10-07T10:00:{number:02d}+00:00",
    )
    folder = settings.paperlens_cache_dir / "jobs" / job.id
    write_json(folder / "job.json", job.model_dump(mode="json"))
    (folder / f"{job.id}.pdf").write_bytes(b"%PDF-preserve-original")
    return job


def test_manual_history_cleanup_preserves_live_jobs_library_notes_and_links(settings, report):
    report.document.sha256 = hashlib.sha256(b"%PDF-test-copy").hexdigest()
    report.reading = ReadingDocument(version="3", source_version="fixture", items=[])
    destination = seed(settings, report)
    completed = [
        history_job(settings, n, state)
        for n, state in enumerate(["complete", "partial", "failed", "cancelled"], start=1)
    ]
    html = settings.paperlens_output_dir / completed[0].id / "dashboard.html"
    html.parent.mkdir(parents=True)
    html.write_text("<html>preserved</html>", encoding="utf-8")
    completed[0].report_relative_path = str(html.relative_to(settings.paperlens_output_dir))
    write_json(
        settings.paperlens_cache_dir / "jobs" / completed[0].id / "job.json",
        completed[0].model_dump(mode="json"),
    )
    note_file = settings.paperlens_output_dir / ".reader" / "notes" / "preserve.json"
    write_json(note_file, {"personal_note": "keep my notes"})
    before_note = note_file.read_bytes()
    app = server.create_app(settings)
    with TestClient(app) as client:
        live = [
            history_job(settings, n, state)
            for n, state in enumerate(["queued", "running", "paused"], start=10)
        ]
        with app.state.jobs._lock:
            for job in live:
                app.state.jobs._jobs[job.id] = job
        result = client.delete("/api/jobs/history")
        assert result.status_code == 200, result.text
        assert set(result.json()["removed_ids"]) == {j.id for j in completed}
        assert result.json()["failed"] == 0
        assert {j["id"] for j in client.get("/api/jobs").json()["jobs"]} == {j.id for j in live}
        assert client.delete("/api/jobs/history").json()["removed_ids"] == []
        assert client.get(f"/api/jobs/{completed[0].id}/report").text == "<html>preserved</html>"
        assert client.get(f"/api/library/{report.run.id}/report").status_code == 200
        assert client.get(f"/api/library/{report.run.id}/pdf").status_code == 200
        for job in completed:
            folder = settings.paperlens_cache_dir / "jobs" / job.id
            assert not (folder / "job.json").exists()
            assert (folder / "archived-job.json").is_file()
            assert (folder / f"{job.id}.pdf").read_bytes() == b"%PDF-preserve-original"
    with TestClient(server.create_app(settings)) as client:
        assert not (
            {j.id for j in completed} & {j["id"] for j in client.get("/api/jobs").json()["jobs"]}
        )
        assert client.get(f"/api/jobs/{completed[0].id}/report").status_code == 200
        assert client.get("/api/library").json()["papers"][0]["id"] == report.run.id
        assert (destination / "report.json").is_file()
        assert note_file.read_bytes() == before_note


def test_history_limit_startup_hot_change_disable_and_restart(settings):
    settings.paperlens_job_history_limit = 3
    jobs = [history_job(settings, n) for n in range(1, 6)]
    paused = history_job(settings, 6, "paused")
    with TestClient(server.create_app(settings)) as client:

        def listed():
            return {j["id"] for j in client.get("/api/jobs").json()["jobs"]}

        assert listed() == {j.id for j in [*jobs[-3:], paused]}
        saved = client.put("/api/settings", json={"values": {"PAPERLENS_JOB_HISTORY_LIMIT": 1}})
        assert "errors" not in saved.json()
        assert listed() == {jobs[-1].id, paused.id}
        for bad in [-1, 1001, 1.5]:
            assert (
                "errors"
                in client.put(
                    "/api/settings", json={"values": {"PAPERLENS_JOB_HISTORY_LIMIT": bad}}
                ).json()
            )
        assert listed() == {jobs[-1].id, paused.id}
        assert (
            "errors"
            not in client.put(
                "/api/settings", json={"values": {"PAPERLENS_JOB_HISTORY_LIMIT": 0}}
            ).json()
        )
    extra = [history_job(settings, n) for n in (7, 8)]
    with TestClient(server.create_app(settings)) as client:
        assert {j["id"] for j in client.get("/api/jobs").json()["jobs"]} == {
            j.id for j in [jobs[-1], paused, *extra]
        }


def test_history_auto_cleanup_after_completion_and_archived_retry(settings, monkeypatch):
    settings.paperlens_job_history_limit = 1
    old = history_job(settings, 1, "failed")
    monkeypatch.setattr(server, "run_pipeline", lambda path, config, **kw: fake_result(config))
    with TestClient(server.create_app(settings)) as client:
        new = client.post("/api/jobs", files={"file": ("new.pdf", b"%PDF-test")}, data={"unlimited_confirmed": "true"}).json()["id"]
        wait_status(client, new, "complete")
        deadline = time.monotonic() + 3
        while len(client.get("/api/jobs").json()["jobs"]) > 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert [j["id"] for j in client.get("/api/jobs").json()["jobs"]] == [new]
        assert client.post(f"/api/jobs/{old.id}/retry").status_code == 200
        wait_status(client, old.id, "complete")
        assert client.get(f"/api/jobs/{new}/report").status_code == 200


def test_history_cleanup_skips_finishing_workers_and_failed_disk_writes(settings, monkeypatch):
    from pathlib import Path

    job = history_job(settings, 1)
    app = server.create_app(settings)
    with TestClient(app) as client:
        manager = app.state.jobs
        with manager._lock:
            manager._working.add(job.id)
        assert client.delete("/api/jobs/history").json()["removed_ids"] == []
        with manager._lock:
            manager._working.remove(job.id)

        def fail(*args, **kwargs):
            raise OSError("disk unavailable")

        with monkeypatch.context() as patch:
            patch.setattr(Path, "replace", fail)
            assert client.delete("/api/jobs/history").json() == {"removed_ids": [], "failed": 1}
        assert [j["id"] for j in client.get("/api/jobs").json()["jobs"]] == [job.id]
        assert client.delete("/api/jobs/history").json()["removed_ids"] == [job.id]


def test_history_retention_orders_mixed_timezones(settings):
    settings.paperlens_job_history_limit = 1
    older = history_job(settings, 1)
    newer = history_job(settings, 2)
    older.created_at = "2026-10-07T17:00:00+08:00"
    newer.created_at = "2026-10-07T09:30:00Z"
    for job in [older, newer]:
        write_json(
            settings.paperlens_cache_dir / "jobs" / job.id / "job.json", job.model_dump(mode="json")
        )
    with TestClient(server.create_app(settings)) as client:
        assert [j["id"] for j in client.get("/api/jobs").json()["jobs"]] == [newer.id]
