"""Freeze reproducible claim samples from a completed corpus evaluation.

This selects review work only. It creates no verdicts and makes no claim about
correctness. Whole nodes are the units; the result is not an atomic-claim metric.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path

from audit_reports import ROOT, packet, write_json


def select_nodes(packets: dict[str, dict], count: int, seed: int) -> tuple[list, list]:
    population = sorted(
        (paper_id, node["id"]) for paper_id, source in packets.items() for node in source["nodes"]
    )
    if count > len(population):
        raise ValueError(f"Requested {count} nodes, only {len(population)} available.")
    sampled = sorted(random.Random(seed).sample(population, count))
    # Review these in addition to the random sample; do not mix the two
    # denominators or imply the targeted portion was sampled randomly.
    critical = sorted(
        (paper_id, node["id"])
        for paper_id, source in packets.items()
        for node in source["nodes"]
        if node["kind"] in {"contribution", "limitation", "conclusion"}
        or any(character.isdigit() for character in node["summary"] + node["explanation"])
    )
    return sampled, critical


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, action="append", required=True)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20260923)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        raise SystemExit("Review snapshots are immutable; choose an empty output directory.")
    packets = {}
    result_hashes = {}
    run_fingerprints = set()
    expected = set()
    for result_path in args.results:
        raw = result_path.read_bytes()
        results = json.loads(raw)
        result_hashes[result_path.resolve().relative_to(ROOT).as_posix()] = hashlib.sha256(
            raw
        ).hexdigest()
        run_fingerprints.add(results["snapshot_fingerprint"])
        selected = set(results["selected_ids"])
        if expected & selected:
            raise SystemExit("Evaluation selections overlap; do not double-count papers.")
        expected.update(selected)
        latest = {r["id"]: r for r in results["records"]}
        if set(latest) != selected or any(
            r["status"] in {"running", "interrupted"} for r in latest.values()
        ):
            raise SystemExit(
                "Finish or explicitly resolve pending/interrupted attempts before sampling."
            )
        for paper_id, record in latest.items():
            if record["status"] not in {"complete", "partial"}:
                continue
            report_path = (ROOT / record["report"]).with_name("report.json")
            data = packet(report_path, paper_id)
            data["split"] = results["split"]
            packets[paper_id] = data
    if len(run_fingerprints) != 1:
        raise SystemExit("All result shards must share one frozen implementation fingerprint.")
    sampled, critical = select_nodes(packets, args.count, args.seed)
    for paper_id, source in packets.items():
        source["random_sample_node_ids"] = [node for paper, node in sampled if paper == paper_id]
        source["critical_node_ids"] = [node for paper, node in critical if paper == paper_id]
        write_json(args.output / f"{paper_id}.packet.json", source)
    manifest = {
        "schema_version": 1,
        "seed": args.seed,
        "requested_sample": args.count,
        "unit": "whole node including title, summary and explanation",
        "sampling": "uniform without replacement over all available report nodes",
        "evaluated_papers": len(expected),
        "papers_with_reports": len(packets),
        "papers_without_reports": sorted(expected - packets.keys()),
        "population_nodes": sum(len(p["nodes"]) for p in packets.values()),
        "random_sample": [{"paper_id": paper, "node_id": node} for paper, node in sampled],
        "additional_targeted_review": [
            {"paper_id": paper, "node_id": node} for paper, node in critical
        ],
        "results_sha256": result_hashes,
        "snapshot_fingerprint": next(iter(run_fingerprints)),
        "judgments_available": False,
        "note": "Failed papers stay in the completion denominator and have no nodes to sample. Targeted judgments must not inflate the random-sample denominator.",
    }
    write_json(args.output / "sample.json", manifest)
    print(
        f"Prepared {len(sampled)} random nodes and {len(critical)} targeted nodes; no judgments recorded."
    )


if __name__ == "__main__":
    main()
