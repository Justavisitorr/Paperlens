"""Uploaded content identity remains stable across filenames and server restarts."""

import hashlib
import json
from pathlib import Path

import pytest

from paperlens.config import Settings
from paperlens.jobs import Job, JobManager
from paperlens.storage import write_json


@pytest.fixture
def manager_factory(tmp_path, monkeypatch):
    # These tests exercise queue persistence without starting processing or model requests.
    monkeypatch.setattr(JobManager, "_worker", lambda self, index: None)
    settings = Settings(
        _env_file=None,
        paperlens_cache_dir=tmp_path / "cache",
        paperlens_output_dir=tmp_path / "outputs",
        llm_api_key="source-identity-secret",
    )
    managers = []

    def create():
        manager = JobManager(
            settings, runner=lambda *_args, **_kwargs: pytest.fail("No processing")
        )
        managers.append(manager)
        return manager

    yield create
    for manager in managers:
        manager.close()


def put_source(manager, ident, content):
    folder = manager.root / ident
    folder.mkdir(parents=True, exist_ok=True)
    source = folder / f"{ident}.pdf"
    source.write_bytes(content)
    return source


def test_submission_hashes_content_in_chunks_and_keeps_distinct_submission_ids(
    manager_factory, monkeypatch
):
    manager = manager_factory()
    content = b"%PDF-fixture" + b"same page bytes" * 150_000
    first_id, second_id, different_id = "a" * 32, "b" * 32, "c" * 32
    for ident, data in [
        (first_id, content),
        (second_id, content),
        (different_id, content + b"changed"),
    ]:
        put_source(manager, ident, data)
    monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail("Hashing must stream"))
    first = manager.submit(first_id, "original.pdf")
    second = manager.submit(second_id, "renamed.pdf")
    different = manager.submit(different_id, "original.pdf")
    expected = hashlib.sha256(content).hexdigest()
    assert first.source_sha256 == second.source_sha256 == expected
    assert different.source_sha256 != expected
    assert first.id == first_id and second.id == second_id
    assert first.public()["source_sha256"] == expected
    stored = json.loads((manager.root / first.id / "job.json").read_text(encoding="utf-8"))
    assert stored["source_sha256"] == expected
    assert "source-identity-secret" not in json.dumps(stored)
    assert "source-identity-secret" not in json.dumps(first.public())
    assert "configuration" not in first.public() and "llm_api_key" not in first.public()


def test_direct_submission_without_source_preserves_legacy_contract(manager_factory):
    manager = manager_factory()
    job = manager.submit("d" * 32, "test.pdf")
    assert job.source_sha256 == ""
    assert job.public()["source_sha256"] == ""


@pytest.mark.parametrize("filename", ["job.json", "archived-job.json"])
def test_historical_identity_is_backfilled_once_without_changing_reports(
    manager_factory, filename, monkeypatch
):
    original = manager_factory()
    ident, content = "e" * 32, b"%PDF-historical"
    source = put_source(original, ident, content)
    legacy = Job(id=ident, source_name="historic.pdf", status="paused").model_dump(mode="json")
    legacy.pop("source_sha256")
    write_json(source.parent / filename, legacy)
    report = original.output_root / ident / "report.json"
    report.parent.mkdir(parents=True)
    report.write_text('{"immutable":true}', encoding="utf-8")
    restored = manager_factory()
    assert restored.get(ident).source_sha256 == hashlib.sha256(content).hexdigest()
    assert (
        json.loads((source.parent / filename).read_text(encoding="utf-8"))["source_sha256"]
        == hashlib.sha256(content).hexdigest()
    )
    assert report.read_text(encoding="utf-8") == '{"immutable":true}'
    monkeypatch.setattr(
        JobManager,
        "_source_hash",
        lambda *_args: pytest.fail("Persisted identities need no second PDF read"),
    )
    assert manager_factory().get(ident).source_sha256 == restored.get(ident).source_sha256


def test_unreadable_historical_source_does_not_hide_job(manager_factory, monkeypatch):
    original = manager_factory()
    ident = "f" * 32
    source = put_source(original, ident, b"%PDF-source")
    write_json(source.parent / "job.json", Job(id=ident, status="paused").model_dump(mode="json"))
    open_path = Path.open

    def unreadable(path, *args, **kwargs):
        if path == source:
            raise PermissionError("Fixture source unavailable")
        return open_path(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", unreadable)
    restored = manager_factory()
    assert restored.get(ident).status == "paused"
    assert restored.get(ident).source_sha256 == ""
    assert restored.list()[0]["id"] == ident
