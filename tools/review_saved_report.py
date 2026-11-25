"""Boundedly review a saved report without paying for full-paper analysis again.

Source findings stay intact; glossary edits require matched original quotations.
Optional visual cache comes from analyze_visuals; original block text must match.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from paperlens.analysis.engine import _structured
from paperlens.analysis.evidence import EvidenceRegistry
from paperlens.analysis.glossary import GLOSSARY_VERSION, repair_glossary, review_glossary
from paperlens.config import Settings
from paperlens.exporting import write_report
from paperlens.graphs import build_views
from paperlens.models import (
    DashboardReport,
    PaperDocument,
    QuantitativeResult,
    RunInfo,
    VisualEvidence,
)
from paperlens.provider import JsonProvider
from paperlens.storage import StageStore, fingerprint, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--visual-cache", type=Path)
    parser.add_argument("--output", type=Path, default=Path("outputs/round1-reviewed"))
    args = parser.parse_args()
    raw = args.report.read_bytes()
    report = DashboardReport.model_validate_json(raw)
    parent = report.run
    source_digest = hashlib.sha256(raw).hexdigest()
    settings = Settings().model_copy(update={"llm_max_requests": 24})
    record = {
        "input_report_sha256": source_digest,
        "parent_run": parent.model_dump(mode="json"),
        "glossary_version": GLOSSARY_VERSION,
        "visual_cache_applied": False,
    }
    visual_warnings = None
    if args.visual_cache:
        visual = json.loads(args.visual_cache.read_text(encoding="utf-8"))
        document = PaperDocument.model_validate(visual["document"])

        def source_blocks(doc):
            return [(b.id, b.page, b.text, b.raw_text, b.bbox) for b in doc.blocks]

        if document.sha256 != report.document.sha256 or source_blocks(document) != source_blocks(
            report.document
        ):
            raise ValueError("Visual repair changed source text or document identity")
        report.document = document
        report.visuals = [VisualEvidence.model_validate(v) for v in visual["visuals"]]
        report.quantitative_results = [
            QuantitativeResult.model_validate(q) for q in visual["quantitative_results"]
        ]
        record.update(
            visual_cache_applied=True,
            visual_cache_sha256=hashlib.sha256(args.visual_cache.read_bytes()).hexdigest(),
            visual_audit=visual["audit"],
        )
        visual_warnings = visual["warnings"]
    registry = EvidenceRegistry(report.document)
    for evidence in report.evidence:
        resolved = registry.resolve(evidence.block_id, evidence.quote, {evidence.block_id})
        if not resolved:
            raise ValueError("Saved report contains a quotation that no longer matches")
        # Preserve externally assigned old IDs after independently checking the quote.
        registry.items[evidence.id] = evidence
    key = fingerprint(
        {
            "report": source_digest,
            "version": GLOSSARY_VERSION,
            "model": settings.llm_model,
            "base_url": settings.llm_base_url,
            "reasoning": settings.llm_reasoning_effort,
        }
    )
    store = StageStore(settings.paperlens_cache_dir / "report-review", key)
    started = time.monotonic()
    with JsonProvider(settings) as provider:

        def request(stage, model, system, payload):
            print(stage, flush=True)
            return _structured(provider, store, stage, model, system, payload)

        first = review_glossary(
            report.analysis, registry, report.document, request, stage="saved-review"
        )
        repairs = repair_glossary(report.analysis, registry, report.document, request)
        final = review_glossary(
            report.analysis, registry, report.document, request, stage="saved-recheck"
        )
        usage = provider.usage.model_copy()
    record.update(
        first=first.reviews,
        repairs=repairs,
        final=final.reviews,
        node_issues=final.node_issues,
        usage=usage.model_dump(),
        cache_hits=store.hits,
    )
    nodes = {n.id: n for n in report.analysis.nodes}
    report.analysis.warnings = [w for w in report.analysis.warnings if not w.startswith("术语“")]
    for issue in final.node_issues:
        node = nodes[issue["node_id"]]
        if node.support == "supported" or issue["support"] == "unverified":
            node.support = issue["support"]
        report.analysis.warnings.append(f"{node.title}：术语一致性复核：{issue['reason']}")
    for term in report.analysis.glossary:
        if term.support != "supported":
            report.analysis.warnings.append(
                f"术语“{term.term}”待核查：{'；'.join(term.review_notes)}"
            )
    report.evidence = list(registry.items.values())
    if visual_warnings is not None:
        report.quality.warnings = list(
            dict.fromkeys([*report.document.warnings, *report.analysis.warnings, *visual_warnings])
        )
    else:
        report.quality.warnings = list(
            dict.fromkeys([*report.quality.warnings, *report.analysis.warnings])
        )
    report.quality.checks.append(
        "保存报告的术语与跨词条重新复核；基础论点沿用源报告，不冒称重新全文分析。"
    )
    report.run = RunInfo(
        id=uuid.uuid4().hex,
        created_at=datetime.now(UTC).isoformat(),
        model=settings.llm_model,
        parser=parent.parser,
        status=parent.status,
        prompt_version=parent.prompt_version,
        usage=usage,
        usage_scope="current_invocation",
        usage_by_model={settings.llm_model: usage},
        provider_base_url=settings.llm_base_url,
        derived_from_run_id=parent.id,
        processing_mode="review",
        timings={"review": round(time.monotonic() - started, 3)},
        cache_hits={"review": store.hits},
    )
    report = DashboardReport.model_validate(report.model_dump(mode="json"))
    destination = args.output / report.run.id
    destination.mkdir(parents=True, exist_ok=False)
    write_json(destination / "review-audit.json", record)
    original = args.report.parent / "source.pdf"
    if original.is_file():
        shutil.copyfile(original, destination / "source.pdf")
    write_json(destination / "document.json", report.document.model_dump(mode="json"))
    report.graphs = build_views(report.analysis)
    path = write_report(report, destination, browser_channel=settings.paperlens_browser_channel)
    print(
        json.dumps(
            {"report": str(path), "requests": usage.requests, "parent_run": parent.id},
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
