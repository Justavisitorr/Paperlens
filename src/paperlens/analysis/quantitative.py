"""Conservative metric readouts from matched evidence; no model calls.

Numbers are extracted only from quotations supporting graph nodes. Conditions
come from the same local clause, with provenance back to the matched block.
This deliberately leaves complex tables, unlabelled numbers and implicit
associations for review rather than inventing experiment metadata.
"""

from __future__ import annotations

import math
import re
import unicodedata

from ..models import (
    Evidence,
    ExperimentCondition,
    PaperAnalysis,
    PaperDocument,
    QuantitativeResult,
)
from .evidence import _normalized_with_spans, normalize_quote, quote_span

METRIC = (
    r"accuracy|precision|recall|F1(?:[- ]score)?|AUROC|AUC|BLEU|ROUGE(?:-[12L])?|"
    r"RMSE|MAE|MSE|mAP|throughput|latency|error rate|"
    r"准确率|精确率|召回率|误差率|吞吐量|延迟"
)
NUMBER = r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
READOUT = re.compile(
    rf"(?P<metric>\b(?:{METRIC})\b|准确率|精确率|召回率|误差率|吞吐量|延迟)"
    rf"(?P<link>\s*(?:(?:is|was|of|reached|achieved|equals|improved|increased|decreased|"
    rf"reduced|improvement|increase|decrease|reduction|by|to|at|from|为|达到|提升|提高|降低|"
    rf"下降|增加|减少|了|至|到|约|=|:)\s*){{0,5}})"
    rf"(?P<number>{NUMBER})\s*(?P<unit>%|％|percentage points?|百分点|ms|milliseconds?|"
    rf"seconds?|s|FPS|samples/s)?",
    re.IGNORECASE,
)
CHANGE = re.compile(r"improv|increas|decreas|reduc|提升|提高|降低|下降|增加|减少", re.I)
CLASSIFICATION = re.compile(r"accuracy|precision|recall|F1|AUC|准确率|精确率|召回率", re.I)
RUNTIME = re.compile(r"throughput|latency|吞吐量|延迟", re.I)

# Every extracted value is an actual substring of the local source. Patterns
# require explicit labels instead of guessing dataset or method names.
CONDITIONS = {
    "dataset": re.compile(
        r"\b(?:dataset|benchmark)\s*[:=]\s*[A-Za-z][\w.+/-]*|"
        r"\bon\s+[A-Z][\w.+/-]*\s+(?:dataset|benchmark)\b|"
        r"\bon\s+(?:CIFAR-?(?:10|100)|MNIST|ImageNet|SQuAD|GLUE)\b|"
        r"[\w.+/-]+\s*(?:数据集|基准)",
    ),
    "split": re.compile(
        r"\b(?:held[- ]out\s+)?(?:test|validation|development|training)\s+"
        r"(?:split|set|data)\b|\bheld[- ]out\s+test\b|测试集|验证集|开发集|训练集",
        re.I,
    ),
    "sample_size": re.compile(
        r"\bn\s*=\s*\d(?:[\d,]*\d)?|\b\d(?:[\d,]*\d)?\s+"
        r"(?:samples|participants|subjects|patients|examples)\b|\d[\d,]*\s*(?:个样本|名参与者|名患者)",
        re.I,
    ),
    "baseline": re.compile(
        r"\b(?:compared (?:with|to)|versus|vs\.?|baseline\s*[:=])\s+"
        r"[A-Za-z][\w.+/-]*|(?:相比|相较于|对比基线|基线[:：])\s*[\w.+/-]+",
        re.I,
    ),
    "hyperparameters": re.compile(
        r"\b(?:batch size|learning rate|epochs?)\s*(?:of|is|was|[:=])?\s*"
        r"\d+(?:\.\d+)?(?:[eE][-+]?\d+)?|(?:批大小|学习率)\s*[:=：]?\s*\d+(?:\.\d+)?",
        re.I,
    ),
    "hardware": re.compile(
        r"\b(?:NVIDIA\s+)?(?:RTX\s*\d+|[AHV]100|TPU(?:\s*v\d+)?)\b|"
        r"\b(?:CPU|GPU|hardware)\s*[:=]\s*[\w.+/-]+",
        re.I,
    ),
    "evaluation_protocol": re.compile(
        r"\b\d+[- ]fold cross[- ]validation\b|\b(?:averaged|mean)\s+over\s+"
        r"\d+\s+(?:runs|seeds|trials)\b|\d+折交叉验证|\d+次(?:运行|重复|试验)",
        re.I,
    ),
}


def _local_clause(text: str, start: int, end: int, *, table_row: bool = False) -> str:
    """Use the containing sentence/row; never nearby unrelated experiments."""
    # Decimal dots are not sentence boundaries. Newlines terminate rows but
    # ordinary wrapped text remains available via its normalized quotation.
    separators = r"(?<!\d)\.|\.(?!\d)|[!?。！？;；]|\n\s*\n"
    if table_row:
        separators += r"|\n"
    boundaries = list(re.finditer(separators, text))
    left = max((m.end() for m in boundaries if m.end() <= start), default=0)
    right = min((m.start() for m in boundaries if m.start() >= end), default=len(text))
    return text[max(left, start - 350) : min(right, end + 350)].strip()


def _conditions(context: str, evidence_id: str) -> list[ExperimentCondition]:
    output = []
    for kind, pattern in CONDITIONS.items():
        values = list(dict.fromkeys(match.group().strip() for match in pattern.finditer(context)))
        # Multiple explicit values of the same kind may refer to distinct
        # experiments. Do not attribute any one of them to this readout.
        if len(values) == 1:
            output.append(
                ExperimentCondition(kind=kind, value=values[0], evidence_ids=[evidence_id])
            )
    return output


def missing_result_conditions(result: QuantitativeResult, *, comparison: bool = False) -> list[str]:
    if result.value is None:
        return []
    found = {condition.kind for condition in result.conditions}
    if result.dataset.strip():
        found.add("dataset")
    expected = ["dataset"]
    if CLASSIFICATION.search(result.metric):
        expected.append("split")
    if comparison:
        expected.append("baseline")
    if RUNTIME.search(result.metric):
        expected.append("hardware")
    return [kind for kind in expected if kind not in found]


def collect_quantitative_results(
    document: PaperDocument,
    analysis: PaperAnalysis,
    evidence: list[Evidence],
    existing: list[QuantitativeResult] | None = None,
) -> list[QuantitativeResult]:
    """Enrich legacy visual results and append grounded textual metric readouts."""
    results = [result.model_copy(deep=True) for result in existing or []]
    for result in results:
        if result.dataset and not any(c.kind == "dataset" for c in result.conditions):
            result.conditions.append(
                ExperimentCondition(
                    kind="dataset",
                    value=result.dataset,
                    evidence_ids=result.evidence_ids,
                )
            )
        if result.condition and not any(c.kind == "other" for c in result.conditions):
            result.conditions.append(
                ExperimentCondition(
                    kind="other",
                    value=result.condition,
                    evidence_ids=result.evidence_ids,
                )
            )
        result.missing_conditions = missing_result_conditions(result)
    blocks = {block.id: block for block in document.blocks}
    node_ids: dict[str, list[str]] = {}
    for node in analysis.nodes:
        if node.origin != "author":
            continue
        for evidence_id in node.evidence_ids:
            node_ids.setdefault(evidence_id, []).append(node.id)
    seen: dict[tuple[str, int, int], QuantitativeResult] = {}
    used_ids = {result.id for result in results}
    for item in evidence:
        if item.id not in node_ids:
            continue
        block = blocks.get(item.block_id)
        if block is None or block.kind in {"reference", "formula", "heading"}:
            continue
        source = block.text
        anchor = quote_span(source, item.quote)
        if anchor is None:
            source = block.raw_text
            anchor = quote_span(source, item.quote)
        if anchor is None:
            continue
        # NFKC improves detection while quote_span still maps each claim back
        # to unchanged source text; whitespace removal would erase row meaning.
        quote = unicodedata.normalize("NFKC", item.quote)
        normalized_source, source_spans = _normalized_with_spans(source[anchor[0] : anchor[1]])
        for match in READOUT.finditer(quote):
            # A "from X to Y" pair is not one unqualified value. Likewise,
            # confidence intervals/ranges need semantic reading, not guessing.
            following = quote[match.end() : match.end() + 30]
            if re.match(r"\s*(?:to\b|[-–±]|\+/-|至|到)\s*\d", following, re.I):
                continue
            if re.search(r"\bfrom\s*$", match.group("link"), re.I):
                continue
            value = float(match.group("number"))
            if not math.isfinite(value):
                continue
            offset = len(normalize_quote(quote[: match.start()]))
            needle = normalize_quote(match.group())
            if normalized_source[offset : offset + len(needle)] != needle:
                continue
            start = anchor[0] + source_spans[offset][0]
            end = anchor[0] + source_spans[offset + len(needle) - 1][1]
            key = (block.id, start, end)
            if key in seen:
                previous = seen[key]
                previous.evidence_ids = list(dict.fromkeys([*previous.evidence_ids, item.id]))
                previous.node_ids = list(dict.fromkeys([*previous.node_ids, *node_ids[item.id]]))
                continue
            context = _local_clause(source, start, end, table_row=block.kind == "table")
            conditions = _conditions(context, item.id)
            metric = match.group("metric")
            comparison = bool(CHANGE.search(match.group("link"))) and not re.search(
                r"(?:\bto|\bat|至|到)\s*$",
                match.group("link"),
                re.I,
            )
            if comparison:
                metric += " change"
            result_id = f"QT{len(results) + 1}"
            while result_id in used_ids:
                result_id += "_"
            used_ids.add(result_id)
            result = QuantitativeResult(
                id=result_id,
                metric=metric,
                value=value,
                value_text=match.group("number"),
                unit=match.group("unit") or "",
                page=item.page,
                source="table_text" if block.kind == "table" else "text",
                precision="approximate" if "约" in match.group("link") else "reported",
                evidence_ids=[item.id],
                node_ids=node_ids[item.id],
                conditions=conditions,
                dataset=next((c.value for c in conditions if c.kind == "dataset"), ""),
                condition="; ".join(c.value for c in conditions),
                notes="从已匹配原文摘录提取；条件仅来自同句局部上下文，关联仍需核查。",
            )
            result.missing_conditions = missing_result_conditions(result, comparison=comparison)
            if comparison and result.unit == "%" and not re.search(r"relative|相对", context, re.I):
                result.notes += " 百分号变化的百分点/相对百分比单位待核查。"
            results.append(result)
            seen[key] = result
    return results
