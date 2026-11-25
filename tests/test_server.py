"""HTTP/queue regression tests with fake processing, not paper-quality results."""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from paperlens import server
from paperlens.config import Settings
from paperlens.errors import InputError, ProviderError

PDF_BYTES = b"%PDF-1.7\nFake bytes for HTTP boundary tests only.\n%%EOF"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        llm_api_key="test-secret-must-never-reach-browser",
        paperlens_output_dir=tmp_path / "outputs",
        paperlens_cache_dir=tmp_path / "cache",
        paperlens_max_upload_mb=1,
    )


def fake_result(path: Path, settings: Settings, *, status: str = "complete"):
    assert path.name == f"{path.parent.name}.pdf"
    destination = settings.paperlens_output_dir / "result"
    destination.mkdir(parents=True, exist_ok=True)
    report = destination / "dashboard.html"
    report.write_text("<!doctype html><html><body>Test report</body></html>", encoding="utf-8")
    return SimpleNamespace(
        output_dir=destination,
        report_path=report,
        report=SimpleNamespace(run=SimpleNamespace(status=status)),
    )


def wait_job(client: TestClient, job_id: str) -> dict:
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        data = response.json()
        if data["status"] not in {"queued", "running"}:
            return data
        time.sleep(0.01)
    raise AssertionError("Test worker did not finish within the bounded timeout")


def upload(client: TestClient, content: bytes = PDF_BYTES):
    return client.post(
        "/api/jobs", files={"file": ("../../untrusted.pdf", content, "application/pdf")}
    , data={"unlimited_confirmed": "true"})


@pytest.mark.parametrize("status", ["complete", "partial"])
def test_upload_status_and_report(monkeypatch, settings: Settings, status: str):
    def fake(path, config, *, progress, source_name=None):
        assert source_name == "untrusted.pdf"
        progress("extracting", 0.4, "测试提取进度")
        return fake_result(path, config, status=status)

    monkeypatch.setattr(server, "run_pipeline", fake)
    app = server.create_app(settings)
    with TestClient(app) as client:
        assert client.get("/api/health").json()["ok"] is True
        response = upload(client)
        assert response.status_code == 202
        job_id = response.json()["id"]
        assert uuid.UUID(hex=job_id).hex == job_id
        data = wait_job(client, job_id)
        assert data["status"] == status
        assert data["source_name"] == "untrusted.pdf"
        assert data["progress"] == 1.0
        assert data["error"] is None
        assert client.get(data["report_url"]).status_code == 200
        assert "Test report" in client.get(data["report_url"]).text
        assert settings.llm_api_key.get_secret_value() not in json.dumps(data)
        assert "report_relative_path" not in data
        saved = settings.paperlens_cache_dir / "jobs" / job_id / "job.json"
        assert json.loads(saved.read_text(encoding="utf-8"))["status"] == status
        assert list(saved.parent.glob("*.pdf"))[0].name == f"{job_id}.pdf"
    # Completed/partial jobs and their report links survive a fresh app instance.
    with TestClient(server.create_app(settings)) as client:
        data = client.get(f"/api/jobs/{job_id}").json()
        assert data["status"] == status
        assert client.get(data["report_url"]).status_code == 200


@pytest.mark.parametrize("outcome_unknown", [False, True], ids=["known-failure", "unknown-outcome"])
def test_failed_review_exposes_explicit_retry_and_reuses_success_after_restart(
    monkeypatch, settings: Settings, outcome_unknown: bool
):
    from test_edition_text_review import Provider, edition

    from paperlens.control import processing_concurrency
    from paperlens.edition_text_review import review_text
    from paperlens.errors import ProviderOutcomeUnknown
    from paperlens.pipeline import _reading_needs_retry

    attempts, audits = {}, []
    monkeypatch.setattr("paperlens.edition_text_review.page_image", lambda *args: "local-image")

    def answer(payload):
        ident = payload["items"][0]["id"]
        attempts[ident] = attempts.get(ident, 0) + 1
        if ident == "item-0" and attempts[ident] == 1:
            error = ProviderOutcomeUnknown if outcome_unknown else ProviderError
            raise error("One prose batch failed.")
        return {"reviews": [{"id": row["id"], "status": "ok"} for row in payload["items"]]}

    def fake(path, config, *, progress, source_name=None):
        current = edition([(page, "Readable source.") for page in range(1, 4)])
        with processing_concurrency(1):
            audit = review_text(
                current, path, Provider(answer),
                config.model_copy(update={"paperlens_vision_allow_unverified": True}),
            )
        audits.append(audit)
        retryable = _reading_needs_retry({"text_review": audit})
        result = fake_result(path, config, status="partial" if retryable else "complete")
        result.retryable = retryable
        return result

    monkeypatch.setattr(server, "run_pipeline", fake)
    with TestClient(server.create_app(settings)) as client:
        job_id = upload(client).json()["id"]
        partial = wait_job(client, job_id)
        assert partial["status"] == "partial" and partial["actions"] == ["retry"]
        assert client.get(partial["report_url"]).status_code == 200
        first_attempts = {"item-0": 1, "item-2": 1} if outcome_unknown else {"item-0": 1}
        assert attempts == first_attempts
        assert "新增费用" in partial["message"]
    # Restart only restores the offer; it does not submit the unfinished batch.
    with TestClient(server.create_app(settings)) as client:
        assert client.get(f"/api/jobs/{job_id}").json()["actions"] == ["retry"]
        assert attempts == first_attempts
        assert client.post(f"/api/jobs/{job_id}/retry").status_code == 200
        complete = wait_job(client, job_id)
        assert complete["status"] == "complete" and complete["actions"] == []
        assert client.get(complete["report_url"]).status_code == 200
        assert attempts == {"item-0": 2, "item-2": 1}
        assert audits[-1]["calls"] == (1 if outcome_unknown else 2)
        assert audits[-1]["cache_hits"] == int(outcome_unknown)


def test_partial_without_unknown_requests_does_not_offer_retry(monkeypatch, settings: Settings):
    monkeypatch.setattr(
        server, "run_pipeline", lambda path, config, **kwargs: fake_result(path, config, status="partial")
    )
    with TestClient(server.create_app(settings)) as client:
        job_id = upload(client).json()["id"]
        partial = wait_job(client, job_id)
        assert partial["status"] == "partial" and partial["actions"] == []
        assert client.post(f"/api/jobs/{job_id}/retry").status_code == 409


@pytest.mark.parametrize("content", [b"", b"not a PDF", b"<html>file</html>"])
def test_rejects_non_pdf_and_removes_upload(settings: Settings, content: bytes):
    with TestClient(server.create_app(settings)) as client:
        response = upload(client, content)
        assert response.status_code == 422
        assert not list((settings.paperlens_cache_dir / "jobs").glob("*/*.pdf"))


def test_upload_size_limit_before_and_during_file_copy(settings: Settings):
    with TestClient(server.create_app(settings)) as client:
        # Large body is rejected before multipart processing.
        assert upload(client, b"%PDF-" + b"x" * (2 * 1024 * 1024)).status_code == 413
        # Only one byte over the file cap: multipart allowance must not admit it.
        assert upload(client, b"%PDF-" + b"x" * (1024 * 1024 - 4)).status_code == 413
        assert not list((settings.paperlens_cache_dir / "jobs").glob("*/*.pdf"))


def test_chunked_body_limit_without_content_length(settings: Settings):
    boundary = "paperlens-test-boundary"

    def chunks():
        yield (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="paper.pdf"'
            "\r\nContent-Type: application/pdf\r\n\r\n%PDF-"
        ).encode()
        for _ in range(20):
            yield b"x" * 65536
        yield f"\r\n--{boundary}--\r\n".encode()

    with TestClient(server.create_app(settings)) as client:
        response = client.post(
            "/api/jobs",
            content=chunks(),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
         data={"unlimited_confirmed": "true"})
        assert response.status_code == 413


def test_single_worker_queue_and_overflow(monkeypatch, settings: Settings):
    entered = threading.Event()
    release = threading.Event()
    threads: list[int] = []

    def fake(path, config, *, progress, source_name=None):
        threads.append(threading.get_ident())
        entered.set()
        assert release.wait(4)
        return fake_result(path, config)

    monkeypatch.setattr(server, "MAX_QUEUED_JOBS", 1)
    monkeypatch.setattr(server, "run_pipeline", fake)
    with TestClient(server.create_app(settings)) as client:
        try:
            first = upload(client).json()["id"]
            assert entered.wait(2)
            second = upload(client).json()["id"]
            assert client.get(f"/api/jobs/{second}").json()["status"] == "queued"
            overflow = upload(client)
            assert overflow.status_code == 503
            assert "队列" in overflow.json()["detail"]
            assert client.get(f"/api/jobs/{second}/report").status_code == 409
        finally:
            release.set()
        assert wait_job(client, first)["status"] == "complete"
        assert wait_job(client, second)["status"] == "complete"
        assert len(threads) == 2 and len(set(threads)) == 1
        assert len(list((settings.paperlens_cache_dir / "jobs").glob("*/*.pdf"))) == 2


@pytest.mark.parametrize("known_error", [True, False])
def test_failed_pipeline_reports_safe_error(monkeypatch, settings: Settings, known_error: bool):
    secret = settings.llm_api_key.get_secret_value()

    def fail(*args, **kwargs):
        if known_error:
            raise ProviderError(f"测试接口失败 {secret}")
        raise RuntimeError(f"Internal path C:/private/file {secret}")

    monkeypatch.setattr(server, "run_pipeline", fail)
    with TestClient(server.create_app(settings)) as client:
        job_id = upload(client).json()["id"]
        data = wait_job(client, job_id)
        assert data["status"] == "failed"
        assert data["report_url"] is None
        assert data["error"]
        assert secret not in json.dumps(data)
        assert "C:/private" not in json.dumps(data)
        diagnostic = json.loads(
            (settings.paperlens_cache_dir / "jobs" / job_id / "failure.json").read_text("utf-8")
        )
        assert diagnostic["stage"] == "starting"
        assert diagnostic["exception_type"] == ("ProviderError" if known_error else "RuntimeError")
        assert secret not in json.dumps(diagnostic)
        assert "Traceback" in diagnostic["traceback"]
        assert client.get(f"/api/jobs/{job_id}/report").status_code == 409


def test_restart_marks_queued_and_running_jobs_interrupted(settings: Settings):
    jobs_root = settings.paperlens_cache_dir / "jobs"
    ids = []
    for status in ("queued", "running"):
        job = server.Job(id=uuid.uuid4().hex, status=status)
        ids.append(job.id)
        directory = jobs_root / job.id
        directory.mkdir(parents=True)
        (directory / "job.json").write_text(job.model_dump_json(), encoding="utf-8")
    with TestClient(server.create_app(settings)) as client:
        for job_id in ids:
            data = client.get(f"/api/jobs/{job_id}").json()
            assert data["status"] == "failed"
            assert data["stage"] == "interrupted"
            assert "重新上传" in data["error"]
        assert client.get("/api/health").json()["queued_jobs"] == 0


def test_report_endpoint_cannot_read_outside_job_directory(settings: Settings):
    job_id = uuid.uuid4().hex
    directory = settings.paperlens_cache_dir / "jobs" / job_id
    directory.mkdir(parents=True)
    outside = settings.paperlens_output_dir.parent / "private.html"
    outside.write_text("private contents", encoding="utf-8")
    job = server.Job(id=job_id, status="complete", report_relative_path="../private.html")
    (directory / "job.json").write_text(job.model_dump_json(), encoding="utf-8")
    with TestClient(server.create_app(settings)) as client:
        assert client.get(f"/api/jobs/{job_id}/report").status_code == 404
        assert client.get("/api/jobs/not-a-uuid").status_code == 404
        assert client.get(f"/api/jobs/{uuid.uuid4().hex}").status_code == 404


def test_missing_key_and_cross_origin_requests_are_rejected(settings: Settings):
    with TestClient(server.create_app(settings)) as client:
        response = client.post(
            "/api/jobs",
            files={"file": ("paper.pdf", PDF_BYTES)},
            headers={"Origin": "https://unrelated.example"},
         data={"unlimited_confirmed": "true"})
        assert response.status_code == 403
    unconfigured = settings.model_copy(
        update={"llm_api_key": Settings(_env_file=None, llm_api_key="").llm_api_key}
    )
    with TestClient(server.create_app(unconfigured)) as client:
        response = upload(client)
        assert response.status_code == 503
        assert "LLM_API_KEY" in response.json()["detail"]


def test_shell_uses_null_data_and_reports_missing_assets(monkeypatch, settings: Settings):
    monkeypatch.setattr(
        server,
        "get_dashboard_template",
        lambda: '<script type="application/json">__PAPERLENS_DATA__</script>',
    )
    with TestClient(server.create_app(settings)) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert ">null</script>" in response.text
        monkeypatch.setattr(server, "get_dashboard_template", lambda: "broken template")
        assert client.get("/").status_code == 503


def test_invalid_pipeline_output_is_failed(monkeypatch, settings: Settings):
    def wrong_path(*args, **kwargs):
        return SimpleNamespace(report_path=Path("C:/not-the-assigned-output.html"))

    monkeypatch.setattr(server, "run_pipeline", wrong_path)
    with TestClient(server.create_app(settings)) as client:
        data = wait_job(client, upload(client).json()["id"])
        assert data["status"] == "failed"
        assert data["report_url"] is None


def test_worker_recovers_after_a_failed_job(monkeypatch, settings: Settings):
    calls = 0

    def fake(path, config, *, progress, source_name=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise InputError("测试损坏文件")
        return fake_result(path, config)

    monkeypatch.setattr(server, "run_pipeline", fake)
    with TestClient(server.create_app(settings)) as client:
        assert wait_job(client, upload(client).json()["id"])["status"] == "failed"
        assert wait_job(client, upload(client).json()["id"])["status"] == "complete"


def test_upload_preserves_unicode_display_name_without_path_components(
    monkeypatch, settings: Settings
):
    captured = {}

    def fake(path, config, *, progress, source_name=None):
        captured.update(path=path, source_name=source_name)
        return fake_result(path, config)

    monkeypatch.setattr(server, "run_pipeline", fake)
    with TestClient(server.create_app(settings)) as client:
        response = client.post(
            "/api/jobs",
            files={"file": ("C:\\fakepath\\中文 论文.pdf", PDF_BYTES, "application/pdf")},
         data={"unlimited_confirmed": "true"})
        data = wait_job(client, response.json()["id"])
        assert data["status"] == "complete"
        assert data["source_name"] == "中文 论文.pdf"
        assert captured["source_name"] == "中文 论文.pdf"
        assert captured["path"].name == f"{data['id']}.pdf"
