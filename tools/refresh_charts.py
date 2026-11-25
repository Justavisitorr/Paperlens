"""Upgrade a saved report from its existing visual cache, with zero requests.

Usage: python tools/refresh_charts.py report.json --output NEW_EMPTY_DIRECTORY
Only report.json and chart-refresh-audit.json are written. Source text, reader
layout/anchors, analysis and historical run usage remain intact. No model,
settings, PDF decoding, image rendering or HTTP client is used.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from collections import defaultdict, deque
from dataclasses import asdict
from pathlib import Path

from paperlens.chart_evidence import data_description
from paperlens.chart_regions import attach_chart_regions, native_chart_text
from paperlens.models import DashboardReport, QuantitativeResult
from paperlens.storage import write_json
from paperlens.visual_response import validate_visual_response

ROOT = Path(__file__).resolve().parents[1]


def _identity(row):
    return (row.visual_id, row.metric, row.method or row.series, row.dataset,
            row.condition, row.x, row.panel, row.source, row.axis)


def _quantitative(row, visual):
    data = row.model_dump(exclude={"source"})
    data["method"] = row.method or row.series
    context = [row.condition]
    if row.panel:
        context.append(f"子图 {row.panel}")
    if row.x is not None:
        context.append(f"x={row.x}")
    if row.axis == "y_right":
        context.append("右纵轴")
    data["condition"] = "；".join(part for part in context if part)
    return QuantitativeResult(
        id="pending", **data, source=row.source,
        precision={"figure_label": "reported", "visual_estimate": "approximate",
                   "figure_only": "unavailable"}[row.source],
        page=visual.page, figure_label=visual.label, visual_id=visual.id,
        visual_bbox=visual.bbox,
    )


def _reading_items(reading):
    if not reading:
        return
    yield from reading.get("items", [])
    yield from reading.get("references", [])
    for items in reading.get("information_items", {}).values():
        yield from items


def refresh_report(
    report_path: Path | str, output: Path | str, *, cache_root: Path | str | None = None,
) -> dict:
    """Revalidate cached responses without rediscovery or changing old V IDs."""
    report_path, output = Path(report_path), Path(output)
    if output.resolve() == report_path.parent.resolve():
        raise ValueError("Output must differ from the original report directory")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("Output must be a new or empty directory; existing files are never overwritten")
    raw = report_path.read_bytes()
    original = json.loads(raw)
    report = DashboardReport.model_validate(original)
    if len({q.id for q in report.quantitative_results}) != len(report.quantitative_results):
        raise ValueError("Saved quantitative result IDs must be unique")
    verification = json.loads((report_path.parent / "visual-verification.json").read_text(encoding="utf-8"))
    key = verification.get("cache_key")
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
        raise ValueError("visual-verification.json must contain a valid cache_key")
    cache_dir = Path(cache_root or ROOT / ".cache/paperlens/visuals") / key
    # The final document may contain recovered formulas, so its fingerprint must
    # NOT be recomputed as the old cache key. The saved audit is the exact pointer.
    attach_chart_regions(report.document, report.visuals)
    native = {v.id: native_chart_text(report.document, v) for v in report.visuals}
    audit = {
        "version": "1.0", "requests": 0, "historical_usage_preserved": True,
        "source_report_sha256": hashlib.sha256(raw).hexdigest(),
        "cache_key": key, "cached_pages_found": 0, "cached_pages_validated": 0,
        "refreshed_visual_ids": [], "preserved_visual_ids": [],
        "recovered_readings": 0, "reused_result_ids": 0, "new_result_ids": 0,
        "numeric_readings_before": sum(q.visual_id is not None and q.value is not None
                                       for q in report.quantitative_results),
        "pages": [],
    }
    old_by_visual = defaultdict(list)
    for row in report.quantitative_results:
        old_by_visual[row.visual_id].append(row)
    old_slots = defaultdict(deque)
    for row in report.quantitative_results:
        old_slots[_identity(row)].append(row.id)
    next_number = 1 + max(
        (int(match.group(1)) for row in report.quantitative_results
         if (match := re.fullmatch(r"Q(\d+)", row.id))), default=0,
    )
    replacements = {}
    for page in sorted({v.page for v in report.visuals if v.kind != "formula"}):
        candidates = [v for v in report.visuals if v.page == page]
        charts = [v for v in candidates if v.kind != "formula"]
        cache_path = cache_dir / f"page-{page:04d}.json"
        page_audit = {"page": page, "cache_file": cache_path.name, "issues": [], "normalizations": []}
        audit["pages"].append(page_audit)
        if not cache_path.is_file():
            page_audit["status"] = "missing_cache"
            audit["preserved_visual_ids"].extend(v.id for v in charts)
            continue
        audit["cached_pages_found"] += 1
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            page_audit.update(status="invalid_cache", error=str(exc))
            audit["preserved_visual_ids"].extend(v.id for v in charts)
            continue
        if not isinstance(cached, dict) or "response" not in cached:
            page_audit["status"] = "failed_cached_request"
            audit["preserved_visual_ids"].extend(v.id for v in charts)
            continue
        checked = validate_visual_response(cached["response"], candidates, native)
        audit["cached_pages_validated"] += 1
        page_audit.update(
            status="partial" if checked.issues else "complete",
            issues=[asdict(issue) for issue in checked.issues],
            normalizations=checked.normalizations,
        )
        items = {item.visual_id: item for item in checked.items}
        bad_ids = {issue.visual_id for issue in checked.issues}
        for visual in charts:
            item = items.get(visual.id)
            previous = old_by_visual[visual.id]
            if item is None or item.status != "analyzed":
                audit["preserved_visual_ids"].append(visual.id)
                continue
            numeric = [row for row in item.results if row.source != "figure_only"]
            diagram = item.chart is not None and item.chart.chart_type == "diagram"
            prior_numeric = any(row.source != "figure_only" for row in previous)
            # An empty/invalid response is not evidence that previously retained
            # values disappeared. A recognized diagram can legitimately clear them.
            if not numeric and not diagram and (prior_numeric or not item.results):
                audit["preserved_visual_ids"].append(visual.id)
                continue
            fresh = [_quantitative(row, visual) for row in (numeric or item.results)]
            for row in fresh:
                slots = old_slots[_identity(row)]
                if slots:
                    row.id = slots.popleft()
                    audit["reused_result_ids"] += 1
                else:
                    row.id = f"Q{next_number}"
                    next_number += 1
                    audit["new_result_ids"] += 1
            has_issues = visual.id in bad_ids or None in bad_ids
            if has_issues:
                fresh_keys = {_identity(row) for row in fresh}
                # Good rows replace matching identities; unrelated old good rows
                # survive partial validation failure. Empty old placeholders can
                # be retired once real readings have been recovered.
                retained = [row for row in previous if _identity(row) not in fresh_keys
                            and (row.source != "figure_only" or not numeric)]
                fresh = [*retained, *fresh]
            replacements[visual.id] = fresh
            visual.status = item.status
            visual.chart = item.chart or visual.chart
            visual.description = data_description(item, has_issues=has_issues)
            visual.warnings = list(dict.fromkeys([
                *visual.warnings, *item.warnings,
                "已从既有图片响应缓存离线恢复；图像读数仍是模型解读，尚未经人工核实。",
            ]))
            audit["refreshed_visual_ids"].append(visual.id)
            audit["recovered_readings"] += len(numeric)

    final_rows = []
    written = set()
    for row in report.quantitative_results:
        if row.visual_id not in replacements:
            final_rows.append(row)
        elif row.visual_id not in written:
            final_rows.extend(replacements[row.visual_id])
            written.add(row.visual_id)
    for visual_id, rows in replacements.items():
        if visual_id not in written:
            final_rows.extend(rows)
    refreshed = copy.deepcopy(original)
    # Keep every untouched result's original JSON shape, including historic notes.
    old_raw = {row["id"]: row for row in original.get("quantitative_results", [])}
    replaced_qids = {row.id for rows in replacements.values() for row in rows}
    refreshed["quantitative_results"] = [
        row.model_dump(mode="json") if row.id in replaced_qids else old_raw[row.id]
        for row in final_rows
    ]
    refreshed["visuals"] = [visual.model_dump(mode="json") for visual in report.visuals]
    by_id = {row.id: row for row in final_rows}
    prior_by_id = {row.id: row for row in report.quantitative_results}
    final_by_visual = defaultdict(list)
    for row in final_rows:
        final_by_visual[row.visual_id].append(row.id)
    for item in _reading_items(refreshed.get("reading")):
        kept = [qid for qid in item.get("result_ids", []) if qid in by_id
                and (qid not in prior_by_id or prior_by_id[qid].visual_id not in replacements)]
        additions = [qid for vid in item.get("visual_ids", []) if vid in replacements
                     for qid in final_by_visual[vid]]
        if additions or len(kept) != len(item.get("result_ids", [])):
            item["result_ids"] = list(dict.fromkeys([*kept, *additions]))
    DashboardReport.model_validate(refreshed)
    if len(by_id) != len(final_rows):
        raise ValueError("Refreshed quantitative result IDs are not unique")
    for item in _reading_items(refreshed.get("reading")):
        if not set(item.get("result_ids", [])) <= by_id.keys():
            raise ValueError("A refreshed reading result reference is dangling")
    audit["retained_numeric_readings"] = sum(row.source != "figure_only" for row in final_rows)
    audit["numeric_readings_after"] = sum(row.visual_id is not None and row.value is not None
                                         for row in final_rows)
    audit["numeric_readings_added"] = audit["numeric_readings_after"] - audit["numeric_readings_before"]
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "report.json", refreshed)
    write_json(output / "chart-refresh-audit.json", audit)
    return audit


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, default=ROOT / ".cache/paperlens/visuals")
    args = parser.parse_args(argv)
    audit = refresh_report(args.report, args.output, cache_root=args.cache_root)
    print(json.dumps({"requests": 0, "recovered_readings": audit["recovered_readings"],
                      "refreshed_visuals": len(audit["refreshed_visual_ids"]),
                      "output": str(args.output.resolve())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
