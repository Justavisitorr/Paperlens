"""Prepare immutable, model-label-blind audit packets; validate recorded reviews.

This tool does not judge semantic support. An independent reviewer must read the
paper and record judgments. It intentionally hides the producer's support labels
from the review packet and never promotes quote matching into claim accuracy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERDICTS = {"supported", "partial", "unsupported", "contradicted", "not_reviewed"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", text)
    return re.sub(r"\s+", "", text)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def packet(report_path: Path, paper_id: str) -> dict:
    report = read_json(report_path)
    blocks = {b["id"]: b for b in report["document"]["blocks"]}
    evidence = {e["id"]: e for e in report["evidence"]}
    nodes = []
    for node in report["analysis"]["nodes"]:
        sources = []
        for evidence_id in node["evidence_ids"]:
            source = evidence[evidence_id]
            block = blocks[source["block_id"]]
            sources.append(
                {
                    "evidence_id": evidence_id,
                    "block_id": block["id"],
                    "page": source["page"],
                    "page_label": source.get("page_label"),
                    "section": source.get("section"),
                    "quote": source["quote"],
                    "block_text": block["text"],
                    "quote_matches_block": normalize(source["quote"]) in normalize(block["text"]),
                    "page_matches_block": source["page"] == block["page"],
                }
            )
        nodes.append(
            {key: node[key] for key in ("id", "kind", "title", "summary", "explanation", "origin")}
            | {"sources": sources}
        )
    return {
        "schema_version": 1,
        "paper_id": paper_id,
        "split": "dev",
        "report_path": report_path.relative_to(ROOT).as_posix(),
        "report_sha256": digest(report_path),
        "pdf_sha256": report["document"]["sha256"],
        "run": report["run"],
        "sampling": "all_nodes_census; no selection by producer support label",
        "unit": "one entire node (title, summary and explanation), not each sentence",
        "producer_labels_hidden": True,
        "nodes": nodes,
        "source_blocks": report["document"]["blocks"],
        "relationships": report["analysis"]["edges"],
    }


def prepare(results_path: Path, destination: Path) -> None:
    results = read_json(results_path)
    if results.get("split") != "dev":
        raise ValueError("Only development results are allowed before holdout freeze.")
    for record in results["records"]:
        if record["status"] not in {"complete", "partial"}:
            continue
        report_path = ROOT / Path(record["report"]).parent / "report.json"
        target = destination / f"{record['id']}.packet.json"
        data = packet(report_path, record["id"])
        if target.exists() and read_json(target)["report_sha256"] != data["report_sha256"]:
            raise ValueError(f"Refusing to overwrite changed paper snapshot: {target}")
        write_json(target, data)
        print(f"{record['id']}: {len(data['nodes'])} nodes -> {target.relative_to(ROOT)}")


def summarize(directory: Path) -> dict:
    totals: Counter = Counter()
    papers = []
    reviewer_types: set[str] = set()
    for review_path in sorted(directory.glob("*.review.json")):
        review = read_json(review_path)
        source = read_json(directory / f"{review['paper_id']}.packet.json")
        if review["report_sha256"] != source["report_sha256"]:
            raise ValueError(f"Stale audit: {review_path}")
        current = ROOT / source["report_path"]
        if digest(current) != source["report_sha256"]:
            raise ValueError(f"Report changed after audit packet: {current}")
        nodes = {n["id"]: n for n in source["nodes"]}
        block_ids = {b["id"] for b in source["source_blocks"]}
        seen = set()
        counts: Counter = Counter()
        for judgment in review["judgments"]:
            node_id = judgment["node_id"]
            if node_id not in nodes or node_id in seen:
                raise ValueError(f"Unknown or duplicate node: {node_id}")
            seen.add(node_id)
            verdict = judgment["verdict"]
            if verdict not in VERDICTS:
                raise ValueError(f"Invalid verdict: {verdict}")
            if not judgment.get("rationale") or not judgment.get("checked_block_ids"):
                raise ValueError(f"Missing rationale or checked sources: {node_id}")
            if not set(judgment["checked_block_ids"]) <= block_ids:
                raise ValueError(f"Nonexistent checked source for {node_id}")
            counts[verdict] += 1
        counts["not_reviewed"] += len(nodes) - len(seen)
        totals.update(counts)
        reviewer_types.add(review["reviewer_type"])
        papers.append(
            {"paper_id": review["paper_id"], "available_nodes": len(nodes), "counts": dict(counts)}
        )
    reviewed = sum(v for k, v in totals.items() if k != "not_reviewed")
    return {
        "reviewer_types": sorted(reviewer_types),
        "papers": papers,
        "counts": dict(totals),
        "reviewed_nodes": reviewed,
        "target_nodes": 100,
        "fully_supported_fraction": totals["supported"] / reviewed if reviewed else None,
        "human_gold_available": False,
        "limitations": [
            "Independent AI source review is not a human annotation or human gold set.",
            "Census of available development reports is not a random cross-corpus sample.",
            "Quote matching checks source existence only; support verdicts require independent reading.",
            "A node counts as fully supported only if no material assertion exceeds its evidence.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--results", type=Path, required=True)
    prep.add_argument("--output", type=Path, required=True)
    summary = commands.add_parser("summarize")
    summary.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.results.resolve(), args.output.resolve())
    else:
        output = summarize(args.directory.resolve())
        write_json(args.directory / "summary.json", output)
        print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
