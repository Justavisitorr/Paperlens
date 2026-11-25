"""Sequential real-API evaluation with frozen inputs and an honest denominator.

Holdout execution requires a recorded code/configuration snapshot. Selection
metadata describes intended coverage, not a ground-truth layout annotation.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import time
from datetime import UTC, datetime
from pathlib import Path

from paperlens.config import Settings
from paperlens.errors import PaperLensError
from paperlens.pipeline import run_pipeline
from paperlens.storage import fingerprint, write_json

ROOT = Path(__file__).resolve().parents[1]
RESULTS_VERSION = 2
STATUSES = ("complete", "partial", "failed", "interrupted", "running", "invalidated")


def file_digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def runtime_snapshot() -> dict:
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "packages": dict(
            sorted(
                (dist.metadata["Name"].lower(), dist.version)
                for dist in importlib.metadata.distributions()
                if dist.metadata["Name"]
            )
        ),
    }




def snapshot(settings: Settings) -> dict:
    source_files = sorted((ROOT / "src/paperlens").rglob("*.py"))
    source_files += sorted(p for p in (ROOT / "src/paperlens/assets").rglob("*") if p.is_file())
    source_files += [
        ROOT / name
        for name in (
            "tools/evaluate_corpus.py",
            "pyproject.toml",
            "requirements.txt",
            "frontend/package.json",
            "frontend/package-lock.json",
            "tests/corpus/manifest.json",
        )
    ]
    hashes = {path.relative_to(ROOT).as_posix(): file_digest(path) for path in source_files}
    return {
        "schema_version": RESULTS_VERSION,
        "sources": hashes,
        # Explicitly exclude the credential, including its masked representation.
        # Keep all other settings so newly added settings cannot silently escape freezing.
        "configuration": settings.model_dump(mode="json", exclude={"llm_api_key"}),
        "runtime": runtime_snapshot(),
        "local_models": {"required": False, "route": "ai_direct"},
        "cache_policy": "Existing stage caches may be reused; timings are not cold-start benchmarks.",
    }


def assert_same_snapshot(recorded: dict, current: dict) -> None:
    if recorded.get("fingerprint") != fingerprint(recorded.get("snapshot")):
        raise SystemExit("Frozen snapshot is missing or its fingerprint is inconsistent.")
    if recorded["fingerprint"] != fingerprint(current):
        raise SystemExit(
            "Code, dependencies, local models or configuration changed after freezing. "
            "Use a new output directory; do not tune on holdout results."
        )


def update_counts(results: dict) -> None:
    """Count papers by latest outcome, but retain and separately count every attempt."""
    selected = set(results["selected_ids"])
    records = results["records"]
    if any(record["id"] not in selected for record in records):
        raise SystemExit("Results contain papers outside the recorded selection.")
    if any(record["status"] not in STATUSES for record in records):
        raise SystemExit("Results contain an unknown attempt status.")
    latest = {record["id"]: record for record in records}
    for status in STATUSES:
        results[status] = sum(record["status"] == status for record in latest.values())
    results["total"] = len(selected)
    results["pending"] = len(selected - latest.keys())
    results["attempted_papers"] = len(latest)
    results["attempts"] = len(records)
    results["attempt_status_counts"] = {
        status: sum(record["status"] == status for record in records) for status in STATUSES
    }
    results["complete_fraction"] = results["complete"] / len(selected) if selected else 0.0


def prepare_results(old: dict, current: dict, split: str, selected_ids: list[str]) -> dict:
    if old:
        if (
            old.get("schema_version") != RESULTS_VERSION
            or old.get("snapshot_fingerprint") != fingerprint(current)
            or old.get("snapshot") != current
        ):
            raise SystemExit(
                "Existing results lack this exact snapshot; choose a new output directory."
            )
        if old.get("split") != split or old.get("selected_ids") != selected_ids:
            raise SystemExit("Resume must use the original split and complete paper selection.")
    results = {
        "schema_version": RESULTS_VERSION,
        "split": split,
        "snapshot": current,
        "snapshot_fingerprint": fingerprint(current),
        "selected_ids": selected_ids,
        "records": old.get("records", []),
        "note": "Model support labels are not independent human correctness measurements.",
    }
    # --resume is an explicit declaration that the previous invocation stopped.
    # Preserve unfinished attempts instead of silently dropping them from statistics.
    for record in results["records"]:
        if record["status"] == "running":
            record.update(
                status="interrupted",
                error="Previous invocation ended without recording a terminal result.",
            )
    update_counts(results)
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=["dev", "holdout"], default="dev")
    parser.add_argument("--ids", help="Comma-separated subset; omitted means the entire split")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/validation/evaluation")
    parser.add_argument(
        "--freeze", action="store_true", help="Record a new frozen evaluation snapshot"
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="After the previous invocation stopped, reuse completed cases from the same snapshot",
    )
    args = parser.parse_args()
    settings = Settings()
    current = snapshot(settings)
    freeze_path = args.output / "freeze.json"
    if args.freeze:
        if freeze_path.exists():
            raise SystemExit(
                "A snapshot already exists; use a new output directory for a new evaluation."
            )
        write_json(
            freeze_path,
            {
                "created_at": datetime.now(UTC).isoformat(),
                "snapshot": current,
                "fingerprint": fingerprint(current),
            },
        )
    if args.split == "holdout" and not freeze_path.is_file():
        raise SystemExit(
            "Holdout evaluation requires --freeze or an existing matching freeze.json."
        )
    if freeze_path.is_file():
        frozen = json.loads(freeze_path.read_text(encoding="utf-8"))
        assert_same_snapshot(frozen, current)
    manifest = json.loads((ROOT / "tests/corpus/manifest.json").read_text(encoding="utf-8"))
    selected = {paper_id.strip() for paper_id in args.ids.split(",")} if args.ids else None
    papers = [
        p
        for p in manifest["papers"]
        if p["split"] == args.split and (selected is None or p["id"] in selected)
    ]
    if not papers or (selected is not None and selected != {p["id"] for p in papers}):
        raise SystemExit("Unknown paper IDs or no papers in the selected split.")
    args.output.mkdir(parents=True, exist_ok=True)
    results_path = args.output / f"{args.split}-results.json"
    if results_path.exists() and not args.resume:
        raise SystemExit("Results already exist; use --resume or choose a new output directory.")
    old = (
        json.loads(results_path.read_text(encoding="utf-8"))
        if args.resume and results_path.exists()
        else {}
    )
    results = prepare_results(old, current, args.split, [paper["id"] for paper in papers])
    write_json(results_path, results)
    for index, paper in enumerate(papers):
        previous = next(
            (record for record in reversed(results["records"]) if record["id"] == paper["id"]),
            None,
        )
        if previous is not None and previous["status"] == "complete":
            continue
        assert_same_snapshot(
            {"snapshot": current, "fingerprint": fingerprint(current)}, snapshot(settings)
        )
        path = ROOT / paper["path"]
        started = time.monotonic()
        record = {
            "id": paper["id"],
            "status": "running",
            "started_at": datetime.now(UTC).isoformat(),
            "source_sha256": paper["sha256"],
            "language": paper["language"],
            "intended_type": paper["paper_type"],
        }
        results["records"].append(record)
        update_counts(results)
        write_json(results_path, results)
        print(f"[{index + 1}/{len(papers)}] {paper['id']}", flush=True)
        try:
            if file_digest(path) != paper["sha256"]:
                record.update(status="failed", error="Input checksum differs from the manifest.")
                raise SystemExit(f"Input checksum changed: {paper['id']}")
            outcome = run_pipeline(
                path,
                settings.model_copy(
                    update={
                        "paperlens_output_dir": ROOT
                        / "outputs/evaluation"
                        / args.output.name
                        / args.split,
                    }
                ),
                progress=lambda stage, value, message: print(
                    f"  {value:.0%} {message}", flush=True
                ),
            )
            report = outcome.report
            record.update(
                {
                    "status": report.run.status,
                    "report": outcome.report_path.relative_to(ROOT).as_posix(),
                    "pages": len(report.document.pages),
                    "parsed_pages": report.quality.parsed_pages,
                    "nodes": len(report.analysis.nodes),
                    "evidence": len(report.evidence),
                    "matched_reference_nodes": report.quality.grounded_nodes,
                    "model_supported_nodes": sum(
                        n.support == "supported" for n in report.analysis.nodes
                    ),
                    "mermaid_validated": all(g.validated for g in report.graphs),
                    "warnings": len(report.quality.warnings),
                    "usage": report.run.usage.model_dump(),
                    "stage_seconds": report.run.timings,
                }
            )
            if fingerprint(snapshot(settings)) != fingerprint(current):
                record.update(
                    status="invalidated",
                    error="Frozen implementation or environment changed during this attempt.",
                )
                raise SystemExit("Evaluation environment changed during the attempt; stopping.")
        except KeyboardInterrupt:
            record.update(status="interrupted", error="Evaluation interrupted by operator.")
            raise
        except SystemExit:
            if record["status"] == "running":
                record.update(status="interrupted", error="Evaluation exited before completion.")
            raise
        except Exception as exc:
            record.update(
                status="failed",
                error=str(exc) if isinstance(exc, PaperLensError) else type(exc).__name__,
            )
        finally:
            record["seconds"] = round(time.monotonic() - started, 3)
            record["finished_at"] = datetime.now(UTC).isoformat()
            update_counts(results)
            write_json(results_path, results)
    print(
        f"Completed {results.get('complete', 0)}/{len(papers)}; all attempts retained.", flush=True
    )


if __name__ == "__main__":
    main()
