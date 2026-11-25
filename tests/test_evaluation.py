"""Evaluation accounting and frozen provenance, without APIs or corpus content."""

import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

from paperlens.config import Settings
from paperlens.storage import fingerprint, write_json
from tools import evaluate_corpus as evaluation


@pytest.fixture
def evaluation_root(tmp_path, monkeypatch):
    for name in (
        "src/paperlens/config.py",
        "src/paperlens/assets/dashboard.html",
        "src/paperlens/assets/THIRD_PARTY_NOTICES.txt",
        "tools/evaluate_corpus.py",
        "pyproject.toml",
        "requirements.txt",
        "frontend/package.json",
        "frontend/package-lock.json",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")
    papers = []
    for paper_id in ("first", "second"):
        source = tmp_path / f"{paper_id}.pdf"
        source.write_bytes(paper_id.encode())
        papers.append(
            {
                "id": paper_id,
                "split": "dev",
                "path": source.name,
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "language": "en",
                "paper_type": "fixture",
            }
        )
    write_json(tmp_path / "tests/corpus/manifest.json", {"papers": papers})
    monkeypatch.setattr(evaluation, "ROOT", tmp_path)
    monkeypatch.setattr(evaluation, "runtime_snapshot", lambda: {"python": "test"})
    return tmp_path


@pytest.fixture
def settings(monkeypatch):
    configured = Settings(_env_file=None, llm_api_key="test-only-secret")
    monkeypatch.setattr(
        evaluation, "Settings", lambda **kwargs: configured.model_copy(update=kwargs)
    )
    return configured


def invoke(monkeypatch, output, *arguments):
    monkeypatch.setattr(sys, "argv", ["evaluate_corpus.py", "--output", str(output), *arguments])
    evaluation.main()


def outcome(root, status="complete"):
    report = SimpleNamespace(
        run=SimpleNamespace(
            status=status,
            usage=SimpleNamespace(model_dump=lambda: {"requests": 0}),
            timings={},
        ),
        document=SimpleNamespace(pages=[1]),
        quality=SimpleNamespace(parsed_pages=1, grounded_nodes=1, warnings=[]),
        analysis=SimpleNamespace(nodes=[SimpleNamespace(support="supported")]),
        evidence=[1],
        graphs=[SimpleNamespace(validated=True)],
    )
    return SimpleNamespace(report=report, report_path=root / "outputs/report.json")


def test_snapshot_covers_configuration_locks_assets_and_never_credentials(
    evaluation_root, settings
):
    before = evaluation.snapshot(settings)
    encoded = json.dumps(before)
    assert "llm_api_key" not in encoded
    assert "test-only-secret" not in encoded
    assert "**********" not in encoded
    assert before["configuration"]["llm_timeout_seconds"] == settings.llm_timeout_seconds
    assert before["configuration"]["paperlens_preview_pages"] == settings.paperlens_preview_pages
    assert "frontend/package-lock.json" in before["sources"]
    assert "requirements.txt" in before["sources"]
    assert "requirements-docling.txt" not in before["sources"]
    assert before["local_models"] == {"required": False, "route": "ai_direct"}
    assert "tests/corpus/manifest.json" in before["sources"]
    assert "tools/evaluate_corpus.py" in before["sources"]
    notices = evaluation_root / "src/paperlens/assets/THIRD_PARTY_NOTICES.txt"
    notices.write_text("changed license payload", encoding="utf-8")
    assert fingerprint(evaluation.snapshot(settings)) != fingerprint(before)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("llm_timeout_seconds", 900),
        ("llm_max_retries", 5),
        ("paperlens_text_review_enabled", False),
        ("paperlens_preview_pages", 1),
        ("paperlens_browser_channel", "msedge"),
    ],
)
def test_freeze_rejects_previously_untracked_settings(evaluation_root, settings, field, value):
    before = evaluation.snapshot(settings)
    after = evaluation.snapshot(settings.model_copy(update={field: value}))
    with pytest.raises(SystemExit, match="changed after freezing"):
        evaluation.assert_same_snapshot(
            {"snapshot": before, "fingerprint": fingerprint(before)}, after
        )




def test_counts_keep_attempts_and_full_selection_denominator():
    results = {
        "selected_ids": ["complete", "retry", "partial", "failed", "running", "invalid", "pending"],
        "records": [
            {"id": "complete", "status": "complete"},
            {"id": "retry", "status": "failed"},
            {"id": "retry", "status": "interrupted"},
            {"id": "partial", "status": "partial"},
            {"id": "failed", "status": "failed"},
            {"id": "running", "status": "running"},
            {"id": "invalid", "status": "invalidated"},
        ],
    }
    evaluation.update_counts(results)
    assert results["total"] == 7
    assert results["attempted_papers"] == 6
    assert results["attempts"] == 7
    assert results["complete_fraction"] == 1 / 7
    assert results["attempt_status_counts"]["failed"] == 2
    assert results["pending"] == 1
    assert sum(results[status] for status in evaluation.STATUSES) + results["pending"] == 7


def test_resume_requires_same_selection_and_preserves_interrupted_attempt():
    current = {"version": 2}
    results = evaluation.prepare_results({}, current, "dev", ["first", "second"])
    results["records"] = [{"id": "first", "status": "running"}]
    resumed = evaluation.prepare_results(results, current, "dev", ["first", "second"])
    assert resumed["interrupted"] == 1
    assert resumed["pending"] == 1
    assert resumed["attempts"] == 1
    with pytest.raises(SystemExit, match="complete paper selection"):
        evaluation.prepare_results(results, current, "dev", ["first"])
    with pytest.raises(SystemExit, match="exact snapshot"):
        evaluation.prepare_results(results, {"version": 3}, "dev", ["first", "second"])


def test_running_and_complete_results_always_store_snapshot(evaluation_root, settings, monkeypatch):
    output = evaluation_root / "evaluation"

    def run(*args, **kwargs):
        current = json.loads((output / "dev-results.json").read_text(encoding="utf-8"))
        assert current["snapshot"]
        assert current["running"] == 1
        assert current["total"] == 2
        return outcome(evaluation_root)

    monkeypatch.setattr(evaluation, "run_pipeline", run)
    invoke(monkeypatch, output)
    completed = json.loads((output / "dev-results.json").read_text(encoding="utf-8"))
    assert completed["complete"] == 2
    assert completed["attempts"] == 2
    assert completed["complete_fraction"] == 1
    with pytest.raises(SystemExit, match="already exist"):
        invoke(monkeypatch, output)
    invoke(monkeypatch, output, "--resume")
    resumed = json.loads((output / "dev-results.json").read_text(encoding="utf-8"))
    assert resumed["attempts"] == 2


@pytest.mark.parametrize(
    "exception", [KeyboardInterrupt(), RuntimeError("do not serialize details")]
)
def test_interruption_and_failure_keep_pending_denominator(
    evaluation_root, settings, monkeypatch, exception
):
    output = evaluation_root / "evaluation"

    def run(*args, **kwargs):
        raise exception

    monkeypatch.setattr(evaluation, "run_pipeline", run)
    if isinstance(exception, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            invoke(monkeypatch, output)
    else:
        invoke(monkeypatch, output)
    results = json.loads((output / "dev-results.json").read_text(encoding="utf-8"))
    assert results["total"] == 2
    assert results["complete_fraction"] == 0
    if isinstance(exception, KeyboardInterrupt):
        assert results["interrupted"] == 1
        assert results["pending"] == 1
    else:
        assert results["failed"] == 2
        assert results["records"][0]["error"] == "RuntimeError"


def test_mid_attempt_code_change_invalidates_and_stops(evaluation_root, settings, monkeypatch):
    output = evaluation_root / "evaluation"

    def run(*args, **kwargs):
        (evaluation_root / "src/paperlens/config.py").write_text("changed", encoding="utf-8")
        return outcome(evaluation_root)

    monkeypatch.setattr(evaluation, "run_pipeline", run)
    with pytest.raises(SystemExit, match="changed during the attempt"):
        invoke(monkeypatch, output)
    results = json.loads((output / "dev-results.json").read_text(encoding="utf-8"))
    assert results["invalidated"] == 1
    assert results["pending"] == 1
    assert results["complete"] == 0


def test_holdout_requires_freeze_without_reading_pdf(evaluation_root, settings, monkeypatch):
    output = evaluation_root / "evaluation"
    with pytest.raises(SystemExit, match="requires --freeze"):
        invoke(monkeypatch, output, "--split", "holdout")
    assert not (output / "holdout-results.json").exists()
