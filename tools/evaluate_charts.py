"""Offline chart inventory and independent-ground-truth evaluation.

Run ``python tools/evaluate_charts.py --inventory`` to list existing figures.
Run ``python tools/evaluate_charts.py --evaluate truth.json --output DIR``.
Truth format: {"ground_truth_source": "human", "cases": [{"id": "...",
"expected": [{"series": "A", "x": "2020", "value": 12,
"source": "figure_label"}], "actual": [...], "tolerance": 0.5}]}.
Expected sources default to figure_label; visual_estimate uses case tolerance
(absolute, or {"absolute": 0.5, "relative": 0.01}). Null/figure_only expected
readings are unscored. Unknown extra keys are only confirmed extras when
ground_truth_complete=true (default); set false for partial annotations.
No API, application settings, credentials or embedded media are loaded/exported.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ESTIMATES = {"estimate", "estimated", "visual_estimate", "approximate"}
UNREADABLE = {"figure_only", "unreadable", "non_numeric"}


def number(value):
    """Accept finite JSON numbers, never booleans or unparsed label strings."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isfinite(value):
            return float(value)
    return None


def text(value):
    """Export a bounded scalar, excluding embedded media and credential-like text."""
    value = " ".join(str("" if value is None else value).split())
    if "base64," in value.lower() or value.lower().startswith("data:"):
        return ""
    return value[:1000]


def identity(item):
    if "series" not in item or "x" not in item:
        raise ValueError("Each reading requires series and x; do not match by value alone.")
    x = item["x"]
    if number(x) is not None and float(x).is_integer():
        x = int(x)
    return text(item["series"]), text(x)


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def tolerance_for(case, expected):
    if expected.get("source", "figure_label") not in ESTIMATES:
        return 0.0
    tolerance = case.get("tolerance", 0.0)
    if isinstance(tolerance, dict):
        absolute = tolerance.get("absolute", 0.0)
        relative = tolerance.get("relative", 0.0)
    else:
        absolute, relative = tolerance, 0.0
    if any(number(v) is None or v < 0 for v in (absolute, relative)):
        raise ValueError("Tolerance must contain non-negative finite numbers.")
    return max(float(absolute), abs(expected["value"]) * float(relative))


def evaluate_case(case):
    expected_all, actual_all = case.get("expected"), case.get("actual")
    if not isinstance(expected_all, list) or not isinstance(actual_all, list):
        raise ValueError("Each case needs expected and actual lists.")
    expected, unscored = {}, set()
    for item in expected_all:
        key = identity(item)
        if key in expected or key in unscored:
            raise ValueError("Expected series+x must be unique; include panel/metric in series.")
        if number(item.get("value")) is None or item.get("source") in UNREADABLE:
            unscored.add(key)
        else:
            expected[key] = item
            tolerance_for(case, item)  # Validate even when no prediction matches.
    result = {
        "id": text(case.get("id")),
        "chart_type": text(case.get("chart_type", "unknown")),
        "status": "evaluated",
        "expected_numeric": len(expected),
        "actual_numeric": 0,
        "correct": 0,
        "identity_matches": 0,
        "missing": 0,
        "extra_items": 0,
        "unverified_extra_items": 0,
        "duplicate_items": 0,
        "wrong_values": 0,
        "ignored_expected": len(unscored),
        "ignored_actual": 0,
        "explicit_expected": 0,
        "explicit_correct": 0,
        "estimated_expected": 0,
        "estimated_matched": 0,
        "estimated_within_tolerance": 0,
        "estimated_absolute_error_sum": 0.0,
        "details": [],
    }
    if case.get("numeric") is False or not expected:
        result["status"] = "not_evaluated"
        result["reason"] = "Non-numeric or no independently annotated numeric truth."
        result["ignored_actual"] = len(actual_all)
        return add_rates(result)
    predictions = {}
    complete = case.get("ground_truth_complete", True)
    for item in actual_all:
        key = identity(item)
        value = number(item.get("value"))
        if value is None or key in unscored:
            result["ignored_actual"] += 1
            continue
        if key not in expected:
            field = "extra_items" if complete else "unverified_extra_items"
            result[field] += 1
            if complete:
                result["actual_numeric"] += 1
            result["details"].append({"series": key[0], "x": key[1], "status": field})
        elif key in predictions:
            result["actual_numeric"] += 1
            result["duplicate_items"] += 1
            result["extra_items"] += 1
        else:
            result["actual_numeric"] += 1
            predictions[key] = value
    for key, item in expected.items():
        estimate = item.get("source", "figure_label") in ESTIMATES
        result["estimated_expected" if estimate else "explicit_expected"] += 1
        detail = {"series": key[0], "x": key[1], "source": "estimate" if estimate else "label"}
        if key not in predictions:
            result["missing"] += 1
            detail["status"] = "missing"
        else:
            result["identity_matches"] += 1
            error = abs(predictions[key] - float(item["value"]))
            limit = tolerance_for(case, item)
            correct = error <= limit or (
                limit > 0 and math.isclose(error, limit, rel_tol=1e-12, abs_tol=1e-12)
            )
            result["correct" if correct else "wrong_values"] += 1
            if estimate:
                result["estimated_matched"] += 1
                result["estimated_absolute_error_sum"] += error
                result["estimated_within_tolerance"] += int(correct)
            else:
                result["explicit_correct"] += int(correct)
            detail.update(
                status="correct" if correct else "wrong_value",
                absolute_error=error,
                tolerance=limit,
            )
        result["details"].append(detail)
    return add_rates(result)


def add_rates(result):
    result.update(
        precision=ratio(result["correct"], result["actual_numeric"]),
        recall=ratio(result["correct"], result["expected_numeric"]),
        explicit_label_match_rate=ratio(result["explicit_correct"], result["explicit_expected"]),
        estimated_mae=ratio(result["estimated_absolute_error_sum"], result["estimated_matched"]),
        estimated_within_tolerance_rate=ratio(
            result["estimated_within_tolerance"], result["estimated_matched"]
        ),
        estimated_coverage=ratio(result["estimated_matched"], result["estimated_expected"]),
    )
    return result


def evaluate(payload):
    """Micro-average counts; MAE pools only series+x matched estimates."""
    if not isinstance(payload.get("cases"), list):
        raise ValueError("Ground truth must contain a cases list.")
    truth_source = text(payload.get("ground_truth_source", "unconfirmed"))
    if truth_source.lower() in {"model", "cache", "model_output", "same_model"}:
        raise ValueError("Model/cache output cannot serve as independent ground truth.")
    cases = [evaluate_case(case) for case in payload["cases"]]
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)) or any(not value for value in ids):
        raise ValueError("Cases require unique, non-empty IDs.")
    scored = [case for case in cases if case["status"] == "evaluated"]
    counts = {
        key: sum(case[key] for case in scored)
        for key in (
            "expected_numeric",
            "actual_numeric",
            "correct",
            "identity_matches",
            "missing",
            "extra_items",
            "unverified_extra_items",
            "duplicate_items",
            "wrong_values",
            "ignored_expected",
            "ignored_actual",
            "explicit_expected",
            "explicit_correct",
            "estimated_expected",
            "estimated_matched",
            "estimated_within_tolerance",
            "estimated_absolute_error_sum",
        )
    }
    return {
        "schema_version": 1,
        "offline": True,
        "ground_truth_source": truth_source,
        "independent_truth_declared": truth_source.lower()
        in {"human", "manual", "original_data", "source_data"},
        "note": "Expected values require independent human/source verification; "
        "the tool records the caller's declaration, not an independent audit. "
        "Extra items mean unsupported by complete truth, not proven intentional fabrication. "
        "Null denominators are unscored, not perfect scores. MAE is per-case only: "
        "different metrics/units must not be pooled.",
        "total_cases": len(cases),
        "evaluated_cases": len(scored),
        "not_evaluated_cases": len(cases) - len(scored),
        "aggregate": {
            key: value
            for key, value in add_rates(counts).items()
            if key not in {"estimated_mae", "estimated_absolute_error_sum"}
        },
        "cases": cases,
    }


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def local_path(path, root):
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def inventory(root=ROOT, limit=30):
    """Read reports/audits and replay matching cache metadata; never infer truth."""
    root = Path(root)
    cache_root = root / ".cache/paperlens/visuals"
    cache_files = list(cache_root.glob("*/page*.json"))
    records, errors, seen_caches = {}, [], set()
    pdf_by_hash = defaultdict(list)
    for directory in (root / "outputs", root / "tests/corpus", root / "examples"):
        if directory.exists():
            for path in directory.rglob("*.pdf"):
                with path.open("rb") as handle:
                    digest = hashlib.file_digest(handle, "sha256").hexdigest()
                pdf_by_hash[digest].append(local_path(path, root))
    reports = sorted(
        set((root / "outputs").rglob("report.json"))
        | set((root / "outputs").rglob("report-draft.json"))
    )
    for path in reports:
        try:
            report = load_json(path)
            doc = report.get("document", {})
            sha = text(doc.get("sha256"))
            audit_path = path.parent / "visual-verification.json"
            audit = load_json(audit_path) if audit_path.exists() else {}
            cache_key = text(audit.get("cache_key"))
            # Resolve only a direct cache child, never paths from untrusted report text.
            if cache_key and ("/" in cache_key or "\\" in cache_key or cache_key in {".", ".."}):
                cache_key = ""
            quantities = report.get("quantitative_results", [])
            for visual in report.get("visuals", []):
                if visual.get("kind") not in {"figure", "chart"}:
                    continue
                page, visual_id = visual.get("page"), text(visual.get("id"))
                if not isinstance(page, int) or page < 1:
                    continue
                label = text(visual.get("label"))
                box = visual.get("bbox")
                bbox = (
                    {k: number(box.get(k)) for k in ("x0", "y0", "x1", "y1")}
                    if isinstance(box, dict)
                    else None
                )
                blocks = sorted(text(v) for v in visual.get("source_block_ids", []))
                unique = json.dumps(
                    [
                        sha or text(doc.get("source_name")),
                        page,
                        label,
                        bbox,
                        blocks if not label else [],
                    ],
                    sort_keys=True,
                )
                unique_id = "chart-" + hashlib.sha256(unique.encode()).hexdigest()[:16]
                cache = cache_root / cache_key / f"page-{page:04d}.json" if cache_key else None
                replay = []
                if cache and cache.is_file():
                    seen_caches.add(local_path(cache, root))
                    cached = load_json(cache)
                    replay = [
                        item
                        for item in cached.get("response", {}).get("items", [])
                        if item.get("visual_id") == visual_id
                    ]
                readings = [v for v in quantities if v.get("visual_id") == visual_id]
                cached_readings = [r for item in replay for r in item.get("results", [])]
                count = sum(number(v.get("value")) is not None for v in readings or cached_readings)
                record = records.setdefault(
                    unique_id,
                    {
                        "id": unique_id,
                        "title": text(doc.get("title")),
                        "page": page,
                        "label": label,
                        "kind": text(visual.get("kind")),
                        "bbox": bbox,
                        "source_sha256": sha,
                        "source_name": text(doc.get("source_name")),
                        "source_pdf_candidates": sorted(pdf_by_hash.get(sha, [])),
                        "source_block_ids": blocks,
                        "visual_ids": [],
                        "report_paths": [],
                        "cache_paths": [],
                        "cached_numeric_readings_max": 0,
                        "review_status": "unreviewed",
                        "chart_type": "unknown",
                    },
                )
                record["cached_numeric_readings_max"] = max(
                    record["cached_numeric_readings_max"], count
                )
                for field, value in (
                    ("visual_ids", visual_id),
                    ("report_paths", local_path(path, root)),
                    ("cache_paths", local_path(cache, root) if replay else ""),
                ):
                    if value and value not in record[field]:
                        record[field].append(value)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            errors.append({"path": local_path(path, root), "error_type": type(exc).__name__})
    # Failed/incomplete runs can have an audit and document but no report. Keep
    # source captions without guessing which cached visual ID belongs to them.
    audits = sorted((root / "outputs").rglob("visual-verification.json"))
    caption_pattern = re.compile(r"^\s*((?:图)\s*\d+[A-Za-z]?|fig(?:ure)?\.?\s*\d+[A-Za-z]?)", re.I)
    for audit_path in audits:
        if any(
            (audit_path.parent / name).exists() for name in ("report.json", "report-draft.json")
        ):
            continue
        document_path = audit_path.parent / "document.json"
        if not document_path.exists():
            continue
        try:
            doc, audit = load_json(document_path), load_json(audit_path)
            sha, cache_key = text(doc.get("sha256")), text(audit.get("cache_key"))
            if "/" in cache_key or "\\" in cache_key or cache_key in {".", ".."}:
                cache_key = ""
            for block in doc.get("blocks", []):
                match = caption_pattern.match(block.get("text", ""))
                if not match or block.get("kind") not in {"caption", "text", "heading"}:
                    continue
                if re.match(r"\s*(?:[~～—–-]|至|到)", block["text"][match.end() :]):
                    continue
                page, label = block.get("page"), text(match.group(1))
                if not isinstance(page, int) or page < 1:
                    continue
                unique = json.dumps(
                    [sha or text(doc.get("source_name")), page, label, None, []], sort_keys=True
                )
                unique_id = "chart-" + hashlib.sha256(unique.encode()).hexdigest()[:16]
                cache = cache_root / cache_key / f"page-{page:04d}.json" if cache_key else None
                record = records.setdefault(
                    unique_id,
                    {
                        "id": unique_id,
                        "title": text(doc.get("title")),
                        "page": page,
                        "label": label,
                        "kind": "figure",
                        "bbox": None,
                        "source_sha256": sha,
                        "source_name": text(doc.get("source_name")),
                        "source_pdf_candidates": sorted(pdf_by_hash.get(sha, [])),
                        "source_block_ids": [],
                        "visual_ids": [],
                        "report_paths": [],
                        "document_paths": [],
                        "cache_paths": [],
                        "cached_numeric_readings_max": 0,
                        "review_status": "unreviewed",
                        "chart_type": "unknown",
                        "cache_association": "page_only; visual ID unresolved",
                    },
                )
                for field, value in (
                    ("source_block_ids", text(block.get("id"))),
                    ("document_paths", local_path(document_path, root)),
                    ("cache_paths", local_path(cache, root) if cache and cache.is_file() else ""),
                ):
                    # A caption may also exist in a report from a different run.
                    record.setdefault(field, [])
                    if value and value not in record[field]:
                        record[field].append(value)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            errors.append({"path": local_path(audit_path, root), "error_type": type(exc).__name__})
    # Round-robin documents so one paper cannot consume all thirty review slots.
    grouped = defaultdict(deque)
    for record in sorted(
        records.values(),
        key=lambda v: (-bool(v["cached_numeric_readings_max"]), v["page"], v["label"]),
    ):
        grouped[record["source_sha256"] or record["source_name"]].append(record)
    chosen = []
    while grouped and (not limit or len(chosen) < limit):
        for key in list(grouped):
            if limit and len(chosen) >= limit:
                break
            chosen.append(grouped[key].popleft())
            if not grouped[key]:
                del grouped[key]
    return {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "offline": True,
        "note": "Figure candidates, not verified charts or ground truth. Cached numeric "
        "readings are model claims; inspect original PDF and annotate independently.",
        "reports_scanned": len(reports),
        "audits_scanned": len(audits),
        "cache_pages_available": len(cache_files),
        "cache_pages_replayed": len(seen_caches),
        "cache_paths_without_exact_replay": sorted(
            local_path(path, root)
            for path in cache_files
            if local_path(path, root) not in seen_caches
        ),
        "total_candidates": len(records),
        "selected_candidates": len(chosen),
        "selection": "document round-robin; numeric-first",
        "cases": chosen,
        "errors": errors,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--inventory", action="store_true")
    mode.add_argument("--evaluate", type=Path, metavar="TRUTH_JSON")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--limit", type=int, default=30, help="Inventory limit; 0 selects all")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/validation/charts")
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error("--limit must be non-negative")
    try:
        payload = (
            inventory(args.root, args.limit)
            if args.inventory
            else evaluate(load_json(args.evaluate))
        )
    except (ValueError, OSError, TypeError, AttributeError) as exc:
        parser.error(f"Invalid input ({type(exc).__name__}); check JSON schema and paths.")
    args.output.mkdir(parents=True, exist_ok=True)
    name = "inventory.json" if args.inventory else "evaluation.json"
    destination = args.output / name
    destination.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    print(f"Saved {destination}")


if __name__ == "__main__":
    main()
