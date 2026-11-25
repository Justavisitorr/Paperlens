"""Full-document map/synthesis/review with quote checks and resumable stages.

The language model proposes interpretations; it cannot allocate evidence IDs.
Only passages found by EvidenceRegistry become references in the final graph.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextvars import copy_context
from dataclasses import dataclass
from functools import partial
from typing import Literal, TypeVar

from pydantic import Field
from pydantic import ValidationError as SchemaError

from ..config import Settings
from ..control import (
    capture_concurrency_limit,
    checkpoint,
    ordered_parallel_map,
    processing_control,
    processing_stage,
)
from ..errors import ValidationError
from ..models import Evidence, NodeKind, PaperAnalysis, PaperDocument, Record
from ..profiles import analysis_limits
from ..provider import JsonProvider
from ..reading_parallel import request_slots
from ..storage import StageStore, fingerprint
from .checks import metric_issues
from .evidence import EvidenceRegistry
from .glossary import glossary_review_fingerprint, repair_glossary, review_glossary
from .prompts import (
    EXTRACT_PROMPT,
    EXTRACTION_VERSION,
    PROMPT_VERSION,
    REVIEW_PROMPT,
    SYNTHESIS_PROMPT,
)
from .repair import REPAIR_PROMPT, introduces_source_absence, repair_context

Progress = Callable[[str, float, str], None]
T = TypeVar("T", bound=Record)


class ProposedQuote(Record):
    block_id: str
    quote: str


class Finding(Record):
    title: str
    kind: NodeKind
    summary: str
    explanation: str = ""
    origin: Literal["author", "inference", "reader_guide"] = "author"
    evidence: list[ProposedQuote] = Field(default_factory=list, max_length=6)


class SectionNotes(Record):
    section_summary: str
    findings: list[Finding] = Field(max_length=30)


class Review(Record):
    node_id: str
    support: Literal["supported", "partial", "unverified"]
    reason: str


class Reviews(Record):
    reviews: list[Review]


class RepairedNode(Record):
    node_id: str
    title: str | None = Field(default=None, min_length=1, max_length=160)
    summary: str
    explanation: str
    origin: Literal["author", "inference", "reader_guide"]
    evidence: list[ProposedQuote]


class Repairs(Record):
    nodes: list[RepairedNode]


@dataclass
class AnalysisResult:
    analysis: PaperAnalysis
    evidence: list[Evidence]
    audit: dict


def _review_payload(nodes, registry, contexts):
    """Send shared source blocks once while retaining each claim's source scope."""
    blocks, packets = {}, []
    for node, sources in zip(nodes, contexts, strict=True):
        blocks.update((block["block_id"], block) for block in sources)
        packets.append({
            "node_id": node.id, "title": node.title, "summary": node.summary,
            "explanation": node.explanation, "origin": node.origin,
            "sources": registry.contexts(node.evidence_ids),
            "context_block_ids": [block["block_id"] for block in sources],
        })
    return {"nodes": packets, "context_blocks": list(blocks.values())}


def document_chunks(document: PaperDocument, max_chars: int) -> list[list[dict]]:
    """Visit every non-reference character, splitting large blocks without loss.

    Split fragments retain the original block ID so quotations still resolve to
    the original page. JSON metadata is included in the approximate size budget.
    """
    chunks: list[list[dict]] = []
    current: list[dict] = []
    used = 0
    fragment_size = max(500, max_chars - 700)
    for block in sorted(document.blocks, key=lambda b: (b.page, b.order)):
        if block.kind == "reference" or not block.text.strip():
            continue
        for start in range(0, len(block.text), fragment_size):
            item = {
                "block_id": block.id,
                "page": block.page,
                "section": block.section,
                "kind": block.kind,
                "text": block.text[start : start + fragment_size],
            }
            if block.source_pages:
                item["source_pages"] = block.source_pages
            size = len(json.dumps(item, ensure_ascii=False))
            if current and used + size > max_chars:
                chunks.append(current)
                current, used = [], 0
            current.append(item)
            used += size
    if current:
        chunks.append(current)
    return chunks


def _structured(
    provider: JsonProvider,
    store: StageStore,
    stage: str,
    model: type[T],
    system: str,
    payload: dict,
    *,
    max_tokens: int = 16000,
    reasoning_effort: str | None = None,
) -> T:
    cached = store.load(stage)
    if cached is not None:
        try:
            return model.model_validate(cached)
        except SchemaError:
            pass
    user = json.dumps(payload, ensure_ascii=False)
    for attempt in range(3):
        stage_name = next((name for prefix, name in (
            ("section-", "extraction"), ("synthesis-", "synthesis"),
            ("glossary-review", "glossary_review"),
            ("glossary-repair", "glossary_repair"),
            ("glossary-recheck", "glossary_recheck"),
            ("review-", "node_review"), ("repair-", "node_repair"),
            ("recheck-", "node_recheck"),
        ) if stage.startswith(prefix)), "unknown")
        with processing_stage(stage_name):
            result = provider.json(
                system, user, max_tokens=max_tokens, reasoning_effort=reasoning_effort
            )
        try:
            validated = model.model_validate(result)
        except SchemaError as exc:
            # Validation summaries omit input values, which may contain a paper's
            # hostile instructions. The original data remains explicitly data.
            problems = [
                {"field": ".".join(str(p) for p in e["loc"]), "error": e["msg"]}
                for e in exc.errors(include_input=False, include_url=False)
            ][:20]
            store.save(
                f"{stage}-validation-{attempt + 1}",
                {"schema": model.__name__, "attempt": attempt + 1, "errors": problems},
            )
            user = json.dumps(
                {
                    "original_task": payload,
                    "invalid_response": result,
                    "validation_errors": problems,
                    "instruction": "修正结构并返回完整 JSON。",
                },
                ensure_ascii=False,
            )
            if attempt == 2:
                raise ValidationError("模型结果连续未通过结构校验；已保存此前进度。") from exc
        else:
            store.save(stage, validated.model_dump(mode="json"))
            return validated
    raise AssertionError("unreachable")


@request_slots()
def analyze_document(
    document: PaperDocument,
    provider: JsonProvider,
    settings: Settings,
    *,
    progress: Progress | None = None,
    supplemental_context: dict | None = None,
) -> AnalysisResult:
    def emit(stage: str, fraction: float, message: str) -> None:
        if progress:
            progress(stage, fraction, message)

    reading_provider = provider.for_role("reading") if hasattr(provider, "for_role") else provider
    verification_provider = (
        provider.for_role("verification") if hasattr(provider, "for_role") else provider
    )
    role_identities = {
        role: {
            "model": resolved.llm_model,
            "base_url": resolved.llm_base_url,
            "protocol": resolved.llm_protocol,
            "account_id": resolved.llm_account_id,
        }
        for role in ("main", "reading", "verification")
        for resolved in (settings.for_role(role),)
    }
    key = fingerprint(
        {
            "document": document.model_dump(mode="json", exclude={"source_name"}),
            "model": settings.llm_model,
            "roles": role_identities,
            "base_url": settings.llm_base_url,
            "prompt": EXTRACTION_VERSION,
            "chunk_chars": settings.llm_max_input_chars,
            "reasoning_effort": settings.llm_reasoning_effort,
            "processing_profile": settings.paperlens_processing_profile,
            "max_output_tokens": settings.llm_max_output_tokens,
            "reading_output_tokens": settings.paperlens_reading_output_tokens,
            "text_policy_version": 1,
            "supplemental_context": supplemental_context,
        }
    )
    store = StageStore(settings.paperlens_cache_dir / "analysis", key)
    registry = EvidenceRegistry(document)
    limits = analysis_limits(settings)
    profile = settings.paperlens_processing_profile
    extraction_prompt, synthesis_prompt = EXTRACT_PROMPT, SYNTHESIS_PROMPT
    if profile == "economy":
        extraction_prompt = extraction_prompt.replace("优先给出 4–12", "优先给出 3–6")
        extraction_prompt += (
            "\n精简解读：合并重复发现，summary和explanation简短；保留关键定义、"
            "方法、实验条件、结论及局限的原文证据，不能因精简遗漏相反结果或限定条件。"
        )
        synthesis_prompt = synthesis_prompt.replace(
            "输出 14–26 个有内容的节点，允许针对很短论文更少、较长论文更多但最多 36 个。",
            "输出 8–12 个有内容的核心节点；复杂论文可适当增加以保留关键论证和条件。",
        ).replace("summary 通常80–180汉字，explanation 通常40–100汉字",
                  "summary 通常40–90汉字，explanation 通常20–50汉字")
        synthesis_prompt = synthesis_prompt.replace("glossary 为 4–10", "glossary 为 3–5")
    elif profile == "detailed":
        synthesis_prompt = synthesis_prompt.replace("输出 14–26", "输出 24–36")
    chunks = document_chunks(document, settings.llm_max_input_chars)
    if not chunks:
        raise ValidationError(
            "PDF 未提取到可分析正文。当前版本不支持纯扫描件，请先用 OCR 转为含文本层的 PDF。"
        )
    notes: list[dict] = []
    emit("analysis", 0.20, f"分析全文，共 {len(chunks)} 段")

    def extract_section(item: tuple[int, list[dict]]) -> SectionNotes:
        index, chunk = item
        return _structured(
            reading_provider,
            store,
            f"section-{index:04d}",
            SectionNotes,
            extraction_prompt,
            {"blocks": chunk},
            max_tokens=limits["reading_output_tokens"],
            reasoning_effort="none",
        )

    extracted_sections = ordered_parallel_map(
        extract_section,
        enumerate(chunks),
        on_complete=lambda _index, count: emit(
            "analysis",
            0.20 + 0.36 * count / len(chunks),
            f"已分析全文 {count}/{len(chunks)} 段",
        ),
    )
    # Resolve quotes on the coordinator in document order: IDs and synthesis
    # inputs do not depend on network completion order.
    for chunk, extracted in zip(chunks, extracted_sections, strict=True):
        pages = sorted({page for item in chunk for page in item.get("source_pages", [item["page"]])})
        findings = []
        allowed = {item["block_id"] for item in chunk}
        for finding in extracted.findings:
            evidence_ids = []
            for proposal in finding.evidence:
                evidence_id = registry.resolve(proposal.block_id, proposal.quote, allowed)
                if evidence_id and evidence_id not in evidence_ids:
                    evidence_ids.append(evidence_id)
            data = finding.model_dump(exclude={"evidence"})
            data["evidence_ids"] = evidence_ids
            if not evidence_ids:
                data["evidence_warning"] = "引句匹配未通过，不能视为已核实事实"
            findings.append(data)
        notes.append(
            {"pages": pages, "section_summary": extracted.section_summary, "findings": findings}
        )

    emit("synthesis", 0.58, "综合全文论证、贡献与应用边界")
    # All section notes are retained. Very long papers fail the configured request
    # budget explicitly instead of silently dropping their tail or appendix.
    payload = {
        "schema": PaperAnalysis.model_json_schema(),
        "sections": notes,
        # Show what each citation actually says before synthesizing claims.
        # The registry already deduplicates and validates these short quotes.
        "evidence": [
            item.model_dump(include={"id", "page", "quote"})
            | ({"source_pages": item.source_pages} if item.source_pages else {})
            for item in registry.items.values()
        ],
    }
    if supplemental_context:
        payload["supplemental_context"] = supplemental_context
    analysis = _structured(
        provider,
        store,
        f"synthesis-{PROMPT_VERSION}",
        PaperAnalysis,
        synthesis_prompt,
        payload,
        max_tokens=limits["synthesis_output_tokens"],
    )
    if not analysis.nodes:
        raise ValidationError("全文综合未生成任何论点，无法制作可靠看板。")
    existing = set(registry.items)
    warnings = []
    for node in analysis.nodes:
        invalid = set(node.evidence_ids) - existing
        if invalid:
            warnings.append(f"节点“{node.title}”的未知证据引用已移除，需核查。")
        node.evidence_ids = list(dict.fromkeys(e for e in node.evidence_ids if e in existing))
        node.support = "unverified"
    for term in analysis.glossary:
        term.evidence_ids = list(dict.fromkeys(e for e in term.evidence_ids if e in existing))
        term.support = "unverified"
        term.review_notes = []

    reviews: list[dict] = []
    batches = [analysis.nodes[i : i + 6] for i in range(0, len(analysis.nodes), 6)]

    def verify_batch(item: tuple[int, list]) -> Reviews:
        index, batch = item
        review_input = _review_payload(batch, registry, [
            repair_context(n, registry, document, related_nodes=analysis.nodes) for n in batch
        ])
        return _structured(
            verification_provider,
            store,
            f"review-{PROMPT_VERSION}-{index:03d}",
            Reviews,
            REVIEW_PROMPT,
            review_input,
        )

    request = partial(_structured, verification_provider, store)
    initial_glossary = analysis.model_copy(deep=True)
    initial_glossary_input = glossary_review_fingerprint(analysis, registry, document)
    live_workers = capture_concurrency_limit()

    def initial_review_work(branch: str):
        inherited = copy_context()

        def branch_width():
            width = live_workers()
            if width >= 4:
                return width
            if branch == "nodes":
                return max(1, width - 1) if analysis.glossary else width
            return 1

        if branch == "nodes":
            # Both branches share the paper's limit, rather than each opening k
            # requests. The original nodes remain unchanged until both join.
            with processing_control(lambda: inherited.copy().run(checkpoint), branch_width):
                return ordered_parallel_map(
                    verify_batch,
                    enumerate(batches),
                    on_complete=lambda _index, count: emit(
                        "verification",
                        0.65 + 0.10 * count / len(batches),
                        f"已核对论点与原文 {count}/{len(batches)} 组",
                    ),
                )
        # Glossary review updates only this private snapshot. Its node input and
        # the evidence registry are read-only; repairs start after the join.
        with processing_control(lambda: inherited.copy().run(checkpoint), branch_width):
            return review_glossary(
                initial_glossary,
                registry,
                document,
                request,
                stage="glossary-review",
                progress=lambda done, total: emit(
                    "verification", 0.75, f"已核对术语定义 {done}/{total} 组"
                ),
            )

    with request_slots() as review_slots:
        review_slots.reserve_branches(("node_review", "glossary_review"))

        def initial_review(branch: str):
            try:
                return initial_review_work(branch)
            finally:
                review_slots.release_branch(
                    "node_review" if branch == "nodes" else "glossary_review"
                )

        checked_batches, glossary_initial = ordered_parallel_map(
            initial_review, ("nodes", "glossary")
        )
    # Preserve the serial application order: node judgments first, then term
    # states and definition issues. A supported node cannot erase a term issue.
    for batch, checked in zip(batches, checked_batches, strict=True):
        # Duplicate/missing judgments are not treated as successful verification.
        counts: dict[str, int] = {}
        by_id: dict[str, Review] = {}
        for review in checked.reviews:
            counts[review.node_id] = counts.get(review.node_id, 0) + 1
            by_id[review.node_id] = review
        for node in batch:
            judgment = by_id.get(node.id)
            if judgment and counts[node.id] == 1 and node.evidence_ids:
                node.support = judgment.support
                reviews.append(judgment.model_dump())
            else:
                reviews.append(
                    {
                        "node_id": node.id,
                        "support": "unverified",
                        "reason": "缺少可匹配证据或核查响应不完整",
                    }
                )
    first_reviews = [review.copy() for review in reviews]
    review_by_id = {r["node_id"]: r for r in reviews}
    for term, checked in zip(analysis.glossary, initial_glossary.glossary, strict=True):
        term.support = checked.support
        term.review_notes = checked.review_notes[:]

    def apply_definition_issues(issues: list[dict]) -> None:
        by_id = {node.id: node for node in analysis.nodes}
        for issue in issues:
            node = by_id[issue["node_id"]]
            if node.support == "supported" or issue["support"] == "unverified":
                node.support = issue["support"]
            judgment = review_by_id[node.id]
            judgment["support"] = node.support
            judgment["reason"] = "；".join(
                [judgment["reason"], f"术语“{issue['term']}”一致性核查：{issue['reason']}"]
            )

    apply_definition_issues(glossary_initial.node_issues)
    glossary_repairs = repair_glossary(analysis, registry, document, request)
    repair_candidates = [n for n in analysis.nodes if n.support != "supported" or metric_issues(n)]
    repair_records = []
    repair_rejections = []
    repair_batches = [repair_candidates[i : i + 3] for i in range(0, len(repair_candidates), 3)]
    # Freeze source packets before parallel repairs. Each chain owns its nodes
    # and quote registry; only the coordinator publishes their joined results.
    repair_inputs = []
    for index, batch in enumerate(repair_batches):
        packets = [
            {
                "node_id": n.id,
                "title": n.title,
                "summary": n.summary,
                "explanation": n.explanation,
                "origin": n.origin,
                "issue": "；".join([review_by_id[n.id]["reason"], *metric_issues(n)]),
                "metric_check": "如有准确率提升，核对百分点/百分比以及表格行列。",
                "blocks": repair_context(n, registry, document, related_nodes=analysis.nodes),
            }
            for n in batch
        ]
        repair_inputs.append((index, [n.model_copy(deep=True) for n in batch], packets))

    def repair_batch(item):
        index, batch, packets = item
        local_registry = EvidenceRegistry(document)
        local_registry.items = registry.items.copy()
        records, rejected = [], []
        repaired = _structured(
            verification_provider,
            store,
            f"repair-{PROMPT_VERSION}-{index:03d}",
            Repairs,
            REPAIR_PROMPT,
            {"nodes": packets},
        )
        by_id = {n.node_id: n for n in repaired.nodes}
        for node, packet in zip(batch, packets, strict=True):
            candidate = by_id.get(node.id)
            if candidate is None or sum(n.node_id == node.id for n in repaired.nodes) != 1:
                continue
            before = " ".join((node.title, node.summary, node.explanation))
            after = " ".join((candidate.title or node.title,
                              candidate.summary, candidate.explanation))
            if introduces_source_absence(before, after):
                rejected.append({"node_id": node.id,
                                 "reason": "修正新增未经证实的原文缺失断言，保留原论点并复核"})
                continue
            allowed = {b["block_id"] for b in packet["blocks"]}
            ids = [local_registry.resolve(q.block_id, q.quote, allowed)
                   for q in candidate.evidence]
            ids = list(dict.fromkeys(eid for eid in ids if eid))
            if not ids:
                continue
            records.append(
                {"node_id": node.id, "before": node.model_dump(), "reason": packet["issue"]}
            )
            node.summary, node.explanation, node.origin = (
                candidate.summary,
                candidate.explanation,
                candidate.origin,
            )
            if candidate.title is not None:
                node.title = candidate.title
            # New quotes supplement, rather than erase, previously matched
            # sources. Recheck the union against the rewritten claim below.
            node.evidence_ids = list(dict.fromkeys([*ids, *node.evidence_ids]))
            node.support = "unverified"
        checked = _structured(
            verification_provider,
            store,
            f"recheck-{PROMPT_VERSION}-{index:03d}",
            Reviews,
            REVIEW_PROMPT,
            _review_payload(batch, local_registry, [packet["blocks"] for packet in packets]),
        )
        judgments_by_id = {}
        for node in batch:
            judgments = [r for r in checked.reviews if r.node_id == node.id]
            if len(judgments) == 1 and node.evidence_ids:
                node.support = judgments[0].support
                judgments_by_id[node.id] = judgments[0].model_dump()
        return batch, local_registry, records, rejected, judgments_by_id

    repaired_batches = ordered_parallel_map(
        repair_batch, repair_inputs,
        on_complete=lambda _index, count: emit(
            "verification", 0.76 + 0.09 * count / max(1, len(repair_batches)),
            f"已修正并复核论点 {count}/{len(repair_batches)} 组",
        ),
    )
    original_nodes = {node.id: node for node in analysis.nodes}
    for batch, local_registry, records, rejected, judgments in repaired_batches:
        registry.items.update(local_registry.items)
        registry.warnings.extend(local_registry.warnings)
        repair_records.extend(records)
        repair_rejections.extend(rejected)
        review_by_id.update(judgments)
        for updated in batch:
            node = original_nodes[updated.id]
            for field in ("title", "summary", "explanation", "origin", "evidence_ids", "support"):
                setattr(node, field, getattr(updated, field))
    # Recheck the final definitions against the final node texts. This is a
    # bounded last pass, not an open-ended self-correction loop.
    reuse_glossary_review = (
        glossary_review_fingerprint(analysis, registry, document) == initial_glossary_input
    )
    if analysis.glossary and not reuse_glossary_review:
        emit("verification", 0.86, "复核修正后的术语与论点，标记仍不确定的定义")
    if reuse_glossary_review:
        glossary_final = glossary_initial
    else:
        glossary_final = review_glossary(
            analysis,
            registry,
            document,
            request,
            stage="glossary-recheck",
            progress=lambda done, total: emit(
                "verification", 0.86, f"已复核最终术语与论点 {done}/{total} 组"
            ),
        )
        apply_definition_issues(glossary_final.node_issues)
    for node in analysis.nodes:
        issues = metric_issues(node)
        if issues:
            if node.support == "supported":
                node.support = "partial"
            judgment = review_by_id[node.id]
            judgment["support"] = node.support
            judgment["reason"] = "；".join([judgment["reason"], *issues])
    reviews = list(review_by_id.values())
    for node in analysis.nodes:
        if node.support != "supported":
            warnings.append(f"{node.title}：{review_by_id[node.id]['reason']}")
    for term in analysis.glossary:
        if term.support != "supported":
            warnings.append(f"术语“{term.term}”待核查：{'；'.join(term.review_notes)}")
    if registry.warnings:
        warnings.append(
            f"分段分析中 {len(registry.warnings)} 条引句未通过精确/规范化匹配，已排除。"
        )
    analysis.warnings = list(dict.fromkeys(warnings))
    used_ids = {e for n in analysis.nodes for e in n.evidence_ids}
    used_ids.update(e for t in analysis.glossary for e in t.evidence_ids)
    evidence = [e for e in registry.items.values() if e.id in used_ids]
    audit = {
        "prompt_version": PROMPT_VERSION,
        "cache_key": key,
        "cache_hits": store.hits,
        "chunks": len(chunks),
        "covered_block_ids": sorted({b["block_id"] for c in chunks for b in c}),
        "quote_rejections": registry.warnings,
        "reviews": reviews,
        "initial_reviews": first_reviews,
        "repairs": repair_records,
        "repair_rejections": repair_rejections,
        "glossary_reviews": glossary_final.reviews,
        "initial_glossary_reviews": glossary_initial.reviews,
        "glossary_repairs": glossary_repairs,
        "glossary_final_review_reused": reuse_glossary_review,
        "definition_conflicts": {
            "initial": glossary_initial.node_issues,
            "final": glossary_final.node_issues,
        },
        "verification_note": "支持性为模型二次核查，并非人工确认；引句存在性由程序检查。",
    }
    store.save(f"final-analysis-{PROMPT_VERSION}", analysis.model_dump(mode="json"))
    store.save(f"audit-{PROMPT_VERSION}", audit)
    return AnalysisResult(analysis=analysis, evidence=evidence, audit=audit)
