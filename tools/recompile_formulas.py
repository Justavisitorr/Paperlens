"""Recompile saved LaTeX offline, preserving analysis and original PDF text.

This creates a derived report; it does not re-run OCR or semantic verification.
"""

from __future__ import annotations

import argparse
import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

from paperlens.exporting import write_report
from paperlens.formulas import compile_formula
from paperlens.models import DashboardReport, RunInfo, Usage
from paperlens.storage import write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve() == args.report.parent.resolve():
        parser.error("Use a separate directory to retain the source report.")
    if (args.output / "report.json").exists():
        parser.error("The destination already contains a report.")
    raw = args.report.read_bytes()
    report = DashboardReport.model_validate_json(raw)
    parent = report.run
    changes = []
    for block in report.document.blocks:
        if not block.formula or not block.formula.latex:
            continue
        before = block.formula
        after = compile_formula(before.latex, source=before.source)
        # Syntax conversion never upgrades semantic verification.
        after.verified = before.verified
        block.formula = after
        changes.append({"block_id": block.id, "before": before.status, "after": after.status})
        for visual in report.visuals:
            if visual.kind == "formula" and block.id in visual.source_block_ids:
                visual.status = "analyzed" if after.status == "renderable" else "unreadable"
                visual.warnings = [w for w in visual.warnings if w not in before.warnings]
                visual.warnings.extend(after.warnings)
    report.quality.warnings = [
        w for w in report.quality.warnings if "个图表/公式区域尚未可靠读取" not in w
    ]
    omitted = sum(v.status != "analyzed" for v in report.visuals)
    if omitted:
        report.quality.warnings.append(
            f"{omitted} 个图表/公式区域尚未可靠读取；请查看原页及各项原因。"
        )
    report.quality.checks.append("离线重新编译已保存 LaTeX；正文、模型分析与定量结果未重写。")
    report.run = RunInfo(
        id=uuid.uuid4().hex,
        created_at=datetime.now(UTC).isoformat(),
        model=parent.model,
        parser=parent.parser,
        status=parent.status,
        prompt_version=parent.prompt_version,
        usage=Usage(),
        usage_scope="current_invocation",
        usage_by_model={},
        provider_base_url=parent.provider_base_url,
        derived_from_run_id=parent.id,
        processing_mode="review",
        cache_hits={"saved_formula_candidates": len(changes)},
    )
    report = DashboardReport.model_validate(report.model_dump(mode="json"))
    path = write_report(report, args.output)
    write_json(
        args.output / "render-repair-audit.json",
        {
            "source_report_sha256": hashlib.sha256(raw).hexdigest(),
            "source_run": parent.model_dump(mode="json"),
            "derived_run": report.run.id,
            "new_api_requests": 0,
            "changes": changes,
            "note": "Only saved formula rendering was repaired; this is not a fresh analysis.",
        },
    )
    print(path)


if __name__ == "__main__":
    main()
