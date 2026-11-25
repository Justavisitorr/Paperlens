"""Original uploads remain readable without waiting for a generated report."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_server import PDF_BYTES, upload, wait_job

from paperlens import server
from paperlens.config import Settings
from paperlens.errors import InputError


@pytest.fixture
def source_settings(tmp_path):
    return Settings(
        _env_file=None, llm_api_key="test-only", paperlens_max_upload_mb=1,
        paperlens_output_dir=tmp_path / "outputs", paperlens_cache_dir=tmp_path / "cache",
    )


def test_original_is_readable_while_running_and_after_failure_restart(monkeypatch, source_settings):
    entered, release = threading.Event(), threading.Event()

    def unfinished(*args, **kwargs):
        entered.set()
        assert release.wait(4)
        raise InputError("Fixture processing failure")

    monkeypatch.setattr(server, "run_pipeline", unfinished)
    app = server.create_app(source_settings)
    with TestClient(app) as client:
        job_id = upload(client).json()["id"]
        assert entered.wait(3)
        try:
            running = client.get(f"/api/jobs/{job_id}").json()
            assert running["status"] == "running"
            assert running["report_url"] is None
            source_url = running["source_url"]
            assert source_url == f"/api/jobs/{job_id}/source.pdf"
            response = client.get(source_url)
            assert response.status_code == 200
            assert response.content == PDF_BYTES
            assert response.headers["content-type"] == "application/pdf"
            assert response.headers["content-disposition"].startswith("inline;")
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["x-content-type-options"] == "nosniff"
            partial = client.get(source_url, headers={"Range": "bytes=0-7"})
            assert partial.status_code == 206
            assert partial.content == PDF_BYTES[:8]
            assert "jobs.source" in client.get("/api/health").json()["features"]
        finally:
            release.set()
        assert wait_job(client, job_id)["status"] == "failed"
        assert client.get(source_url).content == PDF_BYTES
    with TestClient(server.create_app(source_settings)) as client:
        assert client.get(f"/api/jobs/{job_id}").json()["source_url"] == source_url
        assert client.get(source_url).content == PDF_BYTES


def test_missing_source_disables_link_and_unknown_tasks_cannot_read_files(monkeypatch, source_settings):
    def fail(*args, **kwargs):
        raise InputError("Fixture processing failure")

    monkeypatch.setattr(server, "run_pipeline", fail)
    app = server.create_app(source_settings)
    with TestClient(app) as client:
        job_id = upload(client).json()["id"]
        wait_job(client, job_id)
        path = app.state.jobs.source_path(job_id)
        path.unlink()
        assert client.get(f"/api/jobs/{job_id}").json()["source_url"] is None
        assert client.get(f"/api/jobs/{job_id}/source.pdf").status_code == 404
        assert client.get("/api/jobs/" + "a" * 32 + "/source.pdf").status_code == 404
        assert client.get("/api/jobs/not-a-task/source.pdf").status_code == 404


def test_symlink_cannot_expose_another_file(monkeypatch, source_settings, tmp_path):
    def fail(*args, **kwargs):
        raise InputError("Fixture processing failure")

    monkeypatch.setattr(server, "run_pipeline", fail)
    app = server.create_app(source_settings)
    with TestClient(app) as client:
        job_id = upload(client).json()["id"]
        wait_job(client, job_id)
        path = app.state.jobs.source_path(job_id)
        path.unlink()
        target = tmp_path / "other-account-private.pdf"
        target.write_bytes(b"PRIVATE")
        try:
            path.symlink_to(target)
        except OSError:
            pytest.skip("Host does not permit symlinks")
        assert client.get(f"/api/jobs/{job_id}").json()["source_url"] is None
        result = client.get(f"/api/jobs/{job_id}/source.pdf")
        assert result.status_code == 404
        assert b"PRIVATE" not in result.content


def test_resolved_path_escape_is_denied_on_hosts_without_symlink_privileges(
    monkeypatch, source_settings, tmp_path,
):
    def fail(*args, **kwargs):
        raise InputError("Fixture processing failure")

    monkeypatch.setattr(server, "run_pipeline", fail)
    app = server.create_app(source_settings)
    with TestClient(app) as client:
        job_id = upload(client).json()["id"]
        wait_job(client, job_id)
        path = app.state.jobs.source_path(job_id)
        outside = tmp_path / "another-workspace" / "private.pdf"
        original_resolve = Path.resolve

        def resolved(current, *args, **kwargs):
            return outside if current == path else original_resolve(current, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolved)
        assert client.get(f"/api/jobs/{job_id}").json()["source_url"] is None
        assert client.get(f"/api/jobs/{job_id}/source.pdf").status_code == 404
