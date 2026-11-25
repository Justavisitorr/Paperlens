"""Rebuild presentation from saved analysis without making any model request."""

import argparse
from pathlib import Path

from paperlens.exporting import write_report
from paperlens.graphs import build_views
from paperlens.models import DashboardReport


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("report_json", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = DashboardReport.model_validate_json(args.report_json.read_text(encoding="utf-8"))
    report.graphs = build_views(report.analysis)
    report.quality.checks = [c for c in report.quality.checks if "三幅导图经" not in c]
    path = write_report(report, args.output or args.report_json.parent)
    print(path)


if __name__ == "__main__":
    main()
