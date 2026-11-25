from __future__ import annotations

import json
import threading
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_standalone import ORIGIN, login
from test_web_service import client_app, headers
from test_workspace_api import report as report

from paperlens.account_migration import (
    encrypt_file,
    export_bundle,
    import_bundle,
    recover_migrations,
    unpack_bundle,
)
from paperlens.account_store import AccountStore
from paperlens.jobs import Job, JobManager
from paperlens.standalone import create_standalone
from paperlens.standalone_config import StandaloneConfig
from paperlens.storage import write_json
from paperlens.web_policy import WorkspacePolicy, approved_endpoints
from paperlens.web_service import ServiceConfig, TenantSettings, initialize

PASSWORD = "migration-password-2026"


def test_authenticated_sites_export_to_independent_account(tmp_path, report):
    legacy = tmp_path / "old"
    initialize(legacy)
    config = ServiceConfig.load(legacy, ORIGIN)
    policy = WorkspacePolicy.for_user(b"alice", ORIGIN, config.endpoints, config.vault_key)
    workspace = legacy / "workspaces" / policy.workspace_id
    report_folder = workspace / "outputs" / report.run.id
    report_folder.mkdir(parents=True)
    write_json(report_folder / "report.json", report.model_dump(mode="json"))
    # Recovery and original bytes survive the copy even when not currently displayed.
    job_folder = workspace / "cache/jobs" / report.run.id
    job_folder.mkdir(parents=True)
    (job_folder / f"{report.run.id}.pdf").write_bytes(b"%PDF-preserved-original")
    write_json(job_folder / "job.json", Job(id=report.run.id, status="paused", report_relative_path=report.run.id + "\\dashboard.html").model_dump(mode="json"))
    write_json(workspace / "cache/ai-direct/finished.json", {"completed_batch":"kept"})
    with TestClient(client_app(config), base_url=ORIGIN) as old:
        saved = old.post("/api/connections/accounts", headers=headers("alice"), json={"provider_id":"deepseek", "name":"original connection", "api_key":"keep-my-paid-key"})
        assert saved.status_code == 201
        original_key = (workspace / ".paperlens/connections.json").read_text("utf-8")
        assert old.post("/api/account-export", headers=headers("bob"), json={"password":PASSWORD,"browser":{}}).status_code == 200
        archive = old.post("/api/account-export", headers=headers("alice"), json={"password":PASSWORD,"browser":{"paperlens.terms":"[]", "paperlens:reading-position:paper":"{\"id\":\"p1\",\"fraction\":0.4}"}})
        assert archive.status_code == 200, archive.text[:200] if archive.status_code != 200 else ""
        assert b"keep-my-paid-key" not in archive.content
    mail = {}
    app = create_standalone(StandaloneConfig(tmp_path / "new", ORIGIN, smtp_host="example", smtp_from="test@example.com"),
                            mail_sender=lambda config, ident, email, code: mail.update({ident:(email, code)}))
    with TestClient(app, base_url=ORIGIN) as client:
        bound = login(client, mail)
        response = client.post("/auth/migration/import", headers=bound, files={"archive":("data.plbackup",archive.content)}, data={"password":PASSWORD})
        assert response.status_code == 200, response.text
        service = app.state.standalone
        target = service.config.root / "workspaces" / bound["X-PaperLens-Workspace"]
        assert (target / "cache/ai-direct/finished.json").read_bytes() == (workspace / "cache/ai-direct/finished.json").read_bytes()
        assert (target / "cache/jobs" / report.run.id / f"{report.run.id}.pdf").read_bytes() == b"%PDF-preserved-original"
        assert client.get("/api/library", headers=bound).json()["papers"][0]["id"] == report.run.id
        assert client.get("/api/jobs", headers=bound).json()["jobs"][0]["status"] == "paused"
        assert json.loads((target / "cache/jobs" / report.run.id / "job.json").read_text("utf-8"))["report_relative_path"] == report.run.id + "/dashboard.html"
        assert "original connection" in client.get("/api/connections", headers=bound).text
        new_key = json.loads((target / ".paperlens/connections.json").read_text("utf-8"))["accounts"][0]["api_key"]
        uid = client.get("/auth/session").json()["user"]["id"]
        assert service.policy(uid).unseal(new_key) == "keep-my-paid-key"
        assert (workspace / ".paperlens/connections.json").read_text("utf-8") == original_key
        assert client.get("/auth/data", headers=bound).json()["items"]["paperlens.terms"]["value"] == "[]"
        again = client.post("/auth/migration/import", headers=bound, files={"archive":("data.plbackup",archive.content)}, data={"password":PASSWORD})
        assert again.status_code == 409
        exported = client.post("/auth/migration/export", headers=bound, json={"password":PASSWORD})
        assert exported.status_code == 200, exported.text[:200] if exported.status_code != 200 else ""


def test_invalid_archive_paths_password_and_rollback(tmp_path, monkeypatch):
    store = AccountStore(tmp_path / "accounts.sqlite3", b"a" * 32)
    uid = store.identity("email", "test@example.com", "test")
    policy = WorkspacePolicy.for_user(uid.encode(), ORIGIN, approved_endpoints(), b"b" * 32)
    source = tmp_path / "source"
    (source / "outputs").mkdir(parents=True)
    (source / "outputs/test.txt").write_text("personal paper", encoding="utf-8")
    archive = tmp_path / "good.plbackup"
    export_bundle(source, policy, {"paperlens.terms":"[]"}, PASSWORD, archive)
    with pytest.raises(ValueError, match="密码"):
        unpack_bundle(archive, "wrong-password-123", tmp_path / "wrong", policy)
    corrupted = tmp_path / "corrupted.plbackup"
    data = bytearray(archive.read_bytes())
    data[-1] ^= 1
    corrupted.write_bytes(data)
    with pytest.raises(ValueError, match="损坏"):
        unpack_bundle(corrupted, PASSWORD, tmp_path / "corrupted", policy)
    # A correctly encrypted hostile bundle must still not be able to escape staging.
    for index, name in enumerate(["../escape", "workspace/outputs/../../escape", "workspace/outputs/C:evil", "workspace/outputs/CON"]):
        plain, bad = tmp_path / f"bad{index}.zip", tmp_path / f"bad{index}.plbackup"
        with zipfile.ZipFile(plain, "w") as bundle:
            bundle.writestr(name, "bad")
        encrypt_file(plain, bad, PASSWORD)
        with pytest.raises(ValueError, match="路径"):
            unpack_bundle(bad, PASSWORD, tmp_path / f"unpack{index}", policy)
    assert not (tmp_path / "escape").exists()
    # Failed activation before commit leaves the original directory untouched.
    target = tmp_path / "workspaces" / policy.workspace_id
    target.mkdir(parents=True)
    (target / "placeholder.txt").write_text("keep", encoding="utf-8")
    original = Path.rename

    def fail_incoming(self, dest):
        if self.name == "workspace":
            raise OSError("simulated disk error")
        return original(self, dest)

    monkeypatch.setattr(Path, "rename", fail_incoming)
    with pytest.raises(OSError, match="simulated"):
        import_bundle(archive, PASSWORD, tmp_path, store, uid, policy)
    assert (target / "placeholder.txt").read_text() == "keep"
    assert store.snapshot(uid, policy) == {}


def test_crash_recovery_before_and_after_commit(tmp_path):
    store = AccountStore(tmp_path / "accounts.sqlite3", b"a" * 32)
    uid = store.identity("email", "test@example.com", "test")
    for committed in (False, True):
        workspace_id = ("b" if committed else "a") * 64
        tx = tmp_path / "migration" / ("2" if committed else "1")
        previous = tx / "previous"
        previous.mkdir(parents=True)
        (previous / "old.txt").write_text("old")
        target = tmp_path / "workspaces" / workspace_id
        target.mkdir(parents=True)
        (target / "new.txt").write_text("new")
        write_json(tx / "transaction.json", {"user_id":uid, "workspace_id":workspace_id})
        if committed:
            with store.db() as db:
                db.execute("INSERT INTO migrations VALUES (?,?)", (tx.name, uid))
        recover_migrations(tmp_path, store)
        assert (target / ("new.txt" if committed else "old.txt")).is_file()
        assert not tx.exists()


def test_import_rejects_job_path_override(tmp_path):
    policy = WorkspacePolicy.for_user(b"owner", ORIGIN, approved_endpoints(), b"b" * 32)
    source = tmp_path / "source"
    job = source / "cache/jobs" / ("a" * 32) / "job.json"
    write_json(job, {"configuration":{"paperlens_cache_dir":"/some-other-account"}})
    archive = tmp_path / "bad-job.plbackup"
    export_bundle(source, policy, {}, PASSWORD, archive)
    with pytest.raises(ValueError, match="配置"):
        unpack_bundle(archive, PASSWORD, tmp_path / "import", policy)


def test_global_processing_capacity_keeps_other_jobs_queued(tmp_path):
    slots = threading.BoundedSemaphore(1)
    entered, release = threading.Event(), threading.Event()
    managers = []
    try:
        for i in range(2):
            settings = TenantSettings(paperlens_output_dir=tmp_path / str(i) / "outputs", paperlens_cache_dir=tmp_path / str(i) / "cache")
            manager = JobManager(settings, runner=lambda *args, **kwargs: None, global_slots=slots, worker_threads=1)
            managers.append(manager)

            def process(job_id, manager=manager):
                entered.set()
                release.wait(3)
                manager._update(job_id, status="complete")

            manager._process = process
            with manager._condition:
                job = Job(id=str(i + 1) * 32)
                manager._jobs[job.id] = job
                manager._pending.append(job.id)
                manager._condition.notify_all()
            if i == 0:
                assert entered.wait(2)
        time.sleep(0.3)
        assert managers[1].health()["queued_jobs"] == 1
        assert sum(m.health()["running_jobs"] for m in managers) == 1
        release.set()
        for _ in range(100):
            if all(m.get(str(i+1)*32).status == "complete" for i, m in enumerate(managers)):
                break
            time.sleep(0.02)
        assert all(m.get(str(i+1)*32).status == "complete" for i, m in enumerate(managers))
    finally:
        release.set()
        for manager in managers:
            manager.close()
