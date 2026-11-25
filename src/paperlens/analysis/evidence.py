"""Deterministically resolve model references to text that really exists."""

from __future__ import annotations

import hashlib
import re
import unicodedata

from paperlens.models import Block, Evidence, PaperDocument


def normalize_quote(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", text)
    return re.sub(r"\s+", "", text).casefold()


def _normalized_with_spans(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Normalize while retaining original offsets, including Unicode expansions.

    Canonical combining sequences and Hangul Jamo are composed together. This
    keeps NFKC composition (e.g. e + acute), ligatures and casefold expansions
    mapped to their full source spans instead of guessing offsets in raw text.
    """
    units = (
        (char, index, index + 1)
        for index, original in enumerate(text)
        for char in unicodedata.normalize("NFKD", original)
    )
    chars: list[str] = []
    spans: list[tuple[int, int]] = []
    group: list[tuple[str, int, int]] = []

    def flush() -> None:
        if not group:
            return
        normalized = unicodedata.normalize("NFKC", "".join(unit[0] for unit in group))
        span = (min(unit[1] for unit in group), max(unit[2] for unit in group))
        chars.extend(normalized)
        spans.extend([span] * len(normalized))
        group.clear()

    for unit in units:
        char = unit[0]
        if group and not unicodedata.combining(char):
            # Some compositions (Hangul and Indic vowel signs) have combining
            # class zero. Only retain a starter if it really composes with the
            # current group; consecutive syllables must keep separate offsets.
            prefix = unicodedata.normalize("NFC", "".join(unit[0] for unit in group))
            if unicodedata.normalize("NFC", prefix + char) == prefix + char:
                flush()
        group.append(unit)
    flush()
    normalized = "".join(chars)
    removed: set[int] = set()
    for match in re.finditer(r"(?<=\w)-\s*\n\s*(?=\w)", normalized):
        removed.update(range(match.start(), match.end()))
    output: list[str] = []
    offsets: list[tuple[int, int]] = []
    for index, char in enumerate(chars):
        if index in removed or char.isspace():
            continue
        folded = char.casefold()
        output.extend(folded)
        offsets.extend([spans[index]] * len(folded))
    return "".join(output), offsets


def quote_span(text: str, quote: str) -> tuple[int, int] | None:
    """Return an exact or normalized quote's offsets in the original string."""
    if not quote or not normalize_quote(quote):
        return None
    position = text.find(quote)
    if position >= 0:
        return position, position + len(quote)
    normalized, spans = _normalized_with_spans(text)
    needle = normalize_quote(quote)
    position = normalized.find(needle)
    if position < 0:
        return None
    return spans[position][0], spans[position + len(needle) - 1][1]


class EvidenceRegistry:
    def __init__(self, document: PaperDocument) -> None:
        self.blocks = {block.id: block for block in document.blocks}
        self.pages = {page.number: page for page in document.pages}
        self.items: dict[str, Evidence] = {}
        self.warnings: list[str] = []
        self._spans: dict[str, tuple[str, int, int]] = {}

    def resolve(self, block_id: str, quote: str, allowed_ids: set[str]) -> str | None:
        block = self.blocks.get(block_id)
        if block is None or block_id not in allowed_ids:
            self.warnings.append("模型引用了不属于本段输入的原文编号，已排除该引用。")
            return None
        quote = quote.strip()
        if len(quote) < 8 or len(quote) > 1000:
            self.warnings.append(f"第 {block.page} 页有不合适长度的模型摘录，未标记为已核对。")
            return None
        span = quote_span(block.text, quote)
        context_source = "text"
        if span is None:
            span = quote_span(block.raw_text, quote)
            context_source = "raw_text"
        if span is None:
            self.warnings.append(f"第 {block.page} 页有无法匹配原文的模型摘录，已排除该引用。")
            return None
        match = "exact" if quote in block.raw_text or quote in block.text else "normalized"
        digest = hashlib.sha256((block.id + "\0" + quote).encode("utf-8")).hexdigest()[:12]
        evidence_id = "E" + digest
        self._spans[evidence_id] = (context_source, *span)
        self.items[evidence_id] = Evidence(
            id=evidence_id,
            block_id=block.id,
            page=block.page,
            page_label=self.pages[block.page].label,
            section=block.section,
            quote=quote,
            bbox=block.bbox,
            match=match,
            source_pages=block.source_pages,
        )
        return evidence_id

    def contexts(self, evidence_ids: list[str]) -> list[dict]:
        output = []
        for evidence_id in evidence_ids:
            item = self.items.get(evidence_id)
            if item:
                block: Block = self.blocks[item.block_id]
                limit = 12000 if block.kind == "table" else 4000
                location = self._spans.get(evidence_id)
                if location is None:
                    # Also support registries populated from stored Evidence.
                    for source in ("text", "raw_text"):
                        span = quote_span(getattr(block, source), item.quote)
                        if span is not None:
                            location = (source, *span)
                            break
                if location is None:
                    continue
                source, position, end = location
                context = getattr(block, source)
                if len(context) > limit:
                    if end - position > limit:
                        # Pathological layout whitespace can make a short
                        # accepted quote span thousands of source characters.
                        # Compact only that span, preserving all quote content
                        # and a bounded amount of its original surroundings.
                        core = unicodedata.normalize("NFKC", context[position:end])
                        core = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", core)
                        core = re.sub(r"\s+", " ", core)
                        if len(core) > limit:
                            core = item.quote  # already verified; bound unusual combining sequences
                        budget = max(0, limit - len(core))
                        before = context[max(0, position - budget // 3) : position]
                        after = context[end : end + budget - len(before)]
                        context = before + core + after
                    else:
                        start = max(0, position - limit // 3)
                        start = max(start, end - limit)
                        context = context[start : start + limit]
                output.append(
                    {
                        "id": item.id,
                        "page": item.page,
                        "source_pages": item.source_pages or [item.page],
                        "section": item.section,
                        "quote": item.quote,
                        "context": context,
                    }
                )
        return output
