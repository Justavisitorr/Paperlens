"""Review paper-specific definitions and their use across the reading graph.

A matching quotation proves that a passage exists, not that a definition is
correct. Terms therefore have their own review and bounded repair stages.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import Field

from ..control import ordered_parallel_map
from ..models import GlossaryTerm, PaperAnalysis, PaperDocument, PaperNode, Record
from ..storage import fingerprint
from .evidence import EvidenceRegistry
from .prompts import GROUNDING_RULES, PROMPT_VERSION
from .repair import repair_context

GLOSSARY_VERSION = "1.1"

GLOSSARY_REVIEW_PROMPT = (
    GROUNDING_RULES
    + """
你负责论文术语定义的独立二审及其在整份报告中的一致性。terms 是待审术语；nodes
包含完整节点标题、正文和解释，是待核查分析而非原文证据。sources 和 blocks 才是
论文上下文。不能因术语与节点相互一致就判正确，二者可能同时错；必须回到原文。
section 是自动识别的导航信息，可能受跨栏顺序影响，不能作为章节归属证据；
只有正文标题与所属段落关系明确时才写章节编号，否则直接说明核对依据，不猜位置。
逐项核对：本论文中符号的含义、分子与分母、单位、取值范围、增大或减小时的含义、
正负或零/非零、必要假设与适用条件。定义可能依论文而变，不能用熟悉的通用定义
覆盖作者的明确约定。公式文本损坏时不能靠猜测还原定义，说明哪项尚待核对。
other_terms 是完整词表中的其他释义，也只是待审解读而非证据。
务必跨词条比较相关定义；若一个词条断言某符号含义确定，而另一个宣称同一含义未知，
应依据原文纠正或降级当前有问题的词条，不允许冲突双方都获 supported。
原文支持术语、但报告节点错误时，术语可 supported，同时在 node_issues 中报告错误
节点；术语错误而节点正确时，修术语即可，不要把正确节点也报错。只有发现节点中
具体的错误或未解冲突才返回 node_issues，不能用一个正确术语替整个节点背书。
准确标准释义可作为读者解释；涉及本文指标、符号、数值方向的约定必须符合文内证据。
supported 表示关键定义和限定都有支持；partial 表示部分错误或限定不全；unverified
表示无支持或无法判断。每个 term_id 返回一次，reason 明确指出问题或核对依据。
输入术语全部返回，禁止漏项、重复或改编号。node_issues 只能使用输入节点的编号。
严格返回 JSON：
{"reviews":[{"term_id":"t0001","support":"partial","reason":"具体核查理由",
"node_issues":[{"node_id":"n01","support":"partial","reason":"节点的具体定义冲突"}]}]}
没有节点问题时 node_issues=[]。不要输出修正版，不新增原文事实。
"""
)

GLOSSARY_REPAIR_PROMPT = (
    GROUNDING_RULES
    + """
修正核查未通过的术语解释。保持 term_id 和术语名称不变，只修 explanation。
核对作者对符号、分子、分母、单位、零/非零、变化方向及适用条件的明确约定。
nodes 是可能含错误的报告解读，不是证据；blocks 才是原文。不因节点这样写就照抄。
用简短通俗解释说明本论文的含义，不引入未验证的性能、因果或泛化结论。
无法确认时保留可证实内容，并在解释中明确具体未知项，不能把损坏公式猜成确定结论。
每个修正给出覆盖定义的连续原文短摘录，不能拼接、翻译、造编号或引用未提供的块。
严格返回 JSON：
{"terms":[{"term_id":"t0001","explanation":"修正后供读者使用的解释",
"evidence":[{"block_id":"给定编号","quote":"该块中8–400字符连续原文"}]}]}
"""
)


class NodeDefinitionIssue(Record):
    node_id: str
    support: Literal["partial", "unverified"]
    reason: str


class TermReview(Record):
    term_id: str
    support: Literal["supported", "partial", "unverified"]
    reason: str
    node_issues: list[NodeDefinitionIssue] = Field(default_factory=list)


class TermReviews(Record):
    reviews: list[TermReview]


class DefinitionQuote(Record):
    block_id: str
    quote: str


class RepairedTerm(Record):
    term_id: str
    explanation: str = Field(min_length=1)
    evidence: list[DefinitionQuote] = Field(max_length=6)


class TermRepairs(Record):
    terms: list[RepairedTerm]


@dataclass
class GlossaryReviewResult:
    reviews: list[dict]
    node_issues: list[dict]


def _nodes(analysis: PaperAnalysis) -> list[dict]:
    # Cross-report consistency needs the explanations as well as the summaries.
    return [
        node.model_dump(include={"id", "title", "summary", "explanation", "origin"})
        for node in analysis.nodes
    ]


def _packet(
    index: int,
    term: GlossaryTerm,
    analysis: PaperAnalysis,
    registry: EvidenceRegistry,
    document: PaperDocument,
) -> dict:
    proxy = PaperNode(
        id=f"term_{index:04d}",
        title=term.term[:160] or "术语",
        kind="concept",
        summary=term.explanation,
        evidence_ids=term.evidence_ids,
    )
    return {
        "term_id": f"t{index:04d}",
        "term": term.term,
        "explanation": term.explanation,
        "sources": registry.contexts(term.evidence_ids),
        "blocks": repair_context(proxy, registry, document, related_nodes=analysis.nodes),
    }


def glossary_review_fingerprint(
    analysis: PaperAnalysis, registry: EvidenceRegistry, document: PaperDocument
) -> str:
    """Capture the complete reviewer input, excluding its own previous judgments."""
    return fingerprint({
        "terms": [
            _packet(index, term, analysis, registry, document)
            for index, term in enumerate(analysis.glossary, 1)
        ],
        "nodes": _nodes(analysis),
        "other_terms": [
            {"term_id": f"t{index:04d}", "term": term.term, "explanation": term.explanation}
            for index, term in enumerate(analysis.glossary, 1)
        ],
    })


def review_glossary(
    analysis: PaperAnalysis,
    registry: EvidenceRegistry,
    document: PaperDocument,
    request: Callable,
    *,
    stage: str,
    progress: Callable[[int, int], None] | None = None,
) -> GlossaryReviewResult:
    """Review every term; missing/duplicate judgments cannot earn support."""
    reviews: list[dict] = []
    node_issues: list[dict] = []
    known_nodes = {node.id for node in analysis.nodes}
    starts = list(range(0, len(analysis.glossary), 3))

    def check(start: int):
        batch = list(enumerate(analysis.glossary[start : start + 3], start + 1))
        checked = request(
            f"{stage}-{PROMPT_VERSION}-{GLOSSARY_VERSION}-{start:03d}",
            TermReviews,
            GLOSSARY_REVIEW_PROMPT,
            {
                "terms": [_packet(i, term, analysis, registry, document) for i, term in batch],
                "nodes": _nodes(analysis),
                "other_terms": [
                    {"term_id": f"t{i:04d}", "term": term.term, "explanation": term.explanation}
                    for i, term in enumerate(analysis.glossary, 1)
                ],
            },
        )
        return batch, checked

    results = ordered_parallel_map(
        check,
        starts,
        on_complete=lambda _index, done: progress(done, len(starts)) if progress else None,
    )
    for batch, checked in results:
        for index, term in batch:
            term_id = f"t{index:04d}"
            judgments = [r for r in checked.reviews if r.term_id == term_id]
            term.support = "unverified"
            term.review_notes = ["术语二审响应缺失或重复，定义尚待核查。"]
            if len(judgments) == 1:
                judgment = judgments[0]
                term.review_notes = [judgment.reason]
                if term.evidence_ids:
                    term.support = judgment.support
                else:
                    term.review_notes.append("缺少可匹配原文引用，术语定义尚未获得证据支持。")
                node_issues.extend(
                    {**issue.model_dump(), "term_id": term_id, "term": term.term}
                    for issue in judgment.node_issues
                    if issue.node_id in known_nodes
                )
            reviews.append(
                {
                    "term_id": term_id,
                    "term": term.term,
                    "support": term.support,
                    "reason": "；".join(term.review_notes),
                }
            )
    return GlossaryReviewResult(reviews=reviews, node_issues=node_issues)


def repair_glossary(
    analysis: PaperAnalysis,
    registry: EvidenceRegistry,
    document: PaperDocument,
    request: Callable,
) -> list[dict]:
    """One repair round only; a subsequent independent review decides support."""
    candidates = [
        (index, term)
        for index, term in enumerate(analysis.glossary, 1)
        if term.support != "supported"
    ]
    records = []

    def repair(start: int):
        batch = candidates[start : start + 3]
        packets = [
            {**_packet(i, term, analysis, registry, document), "issues": term.review_notes}
            for i, term in batch
        ]
        repaired = request(
            f"glossary-repair-{PROMPT_VERSION}-{GLOSSARY_VERSION}-{start:03d}",
            TermRepairs,
            GLOSSARY_REPAIR_PROMPT,
            {"terms": packets, "nodes": _nodes(analysis)},
        )
        return batch, packets, repaired

    results = ordered_parallel_map(repair, range(0, len(candidates), 3))
    for batch, packets, repaired in results:
        for (index, term), packet in zip(batch, packets, strict=True):
            term_id = f"t{index:04d}"
            matches = [candidate for candidate in repaired.terms if candidate.term_id == term_id]
            if len(matches) != 1:
                continue
            candidate = matches[0]
            allowed = {block["block_id"] for block in packet["blocks"]}
            ids = [registry.resolve(q.block_id, q.quote, allowed) for q in candidate.evidence]
            ids = list(dict.fromkeys(eid for eid in ids if eid))
            if not ids:
                # A fluent replacement without matched evidence is not a repair.
                continue
            records.append(
                {"term_id": term_id, "before": term.model_dump(), "reason": packet["issues"]}
            )
            term.explanation = candidate.explanation
            term.evidence_ids = list(dict.fromkeys([*ids, *term.evidence_ids]))
            term.support = "unverified"
            term.review_notes = ["定义已修正，等待独立复审。"]
    return records
