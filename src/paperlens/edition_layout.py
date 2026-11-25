"""Deterministic source anchors for model-transcribed reading rows.

Numbers and source text select an existing region; model geometry never decides
between ambiguous candidates. This module does not rewrite transcription text.
"""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Block, DashboardReport, ReadingItem
    from .reading_edition import EditionBlock


def _compact(text: str) -> str:
    return "".join(
        c
        for c in unicodedata.normalize("NFKC", text).casefold()
        if unicodedata.category(c)[0] in {"L", "N"}
    )


def _number(text: str) -> str | None:
    match = re.fullmatch(r"\s*[（(]?\s*(\d+(?:\.\d+)*[a-z]?)\s*[)）]?\s*", text, re.I)
    return match[1].lower() if match else None


def _object_number(kind: str, text: str) -> str | None:
    names = {
        "figure": r"图|Figure|Fig\.?",
        "table": r"表|Table|Tab\.?",
        "algorithm": r"算法|Algorithm|Alg\.?",
    }
    match = re.match(rf"\s*(?:{names[kind]})\s*(\d+(?:\.\d+)*[a-z]?)", text, re.I)
    return match[1].lower() if match else None


def matching_object(row: EditionBlock, originals: list[ReadingItem]) -> ReadingItem | None:
    """Return the unique same-page, same-kind, same-number source object.

    Callers can reuse its exact parts, asset and table, rather than reconstructing
    those fields from whole-block IDs.
    """
    if row.kind not in {"algorithm", "figure", "table"}:
        return None
    number = _object_number(row.kind, row.label or row.content)
    if number is None:
        return None
    matches = [
        item
        for item in originals
        if item.page == row.page
        and item.kind == row.kind
        and _object_number(row.kind, item.label or item.content or "") == number
    ]
    return matches[0] if len(matches) == 1 else None


def _with_sources(row: EditionBlock, blocks: list[Block]) -> EditionBlock:
    if not blocks:
        return row
    return row.model_copy(
        update={
            "source_ids": list(dict.fromkeys(b.id for b in blocks)),
            "box": (
                min(b.bbox.x0 for b in blocks),
                min(b.bbox.y0 for b in blocks),
                max(b.bbox.x1 for b in blocks),
                max(b.bbox.y1 for b in blocks),
            ),
        }
    )


def _text_match(row: EditionBlock, blocks: list[Block]) -> Block | None:
    text = _compact(row.content)
    if not text:
        return None
    eligible = [b for b in blocks if b.kind in {"text", "heading", "reference", "caption"}]
    exact = [b for b in eligible if _compact(b.text) == text]
    if len(exact) == 1:
        return exact[0]
    if exact or len(text) < 12:
        return None
    # A literal excerpt may occupy only part of a native text block. Never choose
    # a merely nearby block, or a short common phrase appearing in two regions.
    matches = [b for b in eligible if text in _compact(b.text)]
    return matches[0] if len(matches) == 1 else None


def _paragraph_sources(
    row: EditionBlock, blocks: list[Block], originals: list[ReadingItem]
) -> list[Block]:
    """An existing paragraph can own several exact slices across two columns."""
    by_id = {b.id: b for b in blocks}
    matches: list[list[Block]] = []
    for item in originals:
        if item.kind != "paragraph" or item.page != row.page:
            continue
        parts = [p for p in item.parts if p.block_id in by_id]
        text = "".join(
            p.join
            + by_id[p.block_id]
            .text.encode("utf-16-le")[p.start * 2 : p.end * 2]
            .decode("utf-16-le")
            for p in parts
        )
        if parts and _compact(text) == _compact(row.content):
            matches.append([by_id[p.block_id] for p in parts])
    return matches[0] if len(matches) == 1 else []


def _biography_ids(blocks: list[Block]) -> set[str]:
    headings = [
        b
        for b in blocks
        if re.fullmatch(
            r"\s*(?:作者简介|Author biographies|About the authors)\s*[:：]?\s*", b.text, re.I
        )
    ]
    owned: set[str] = set()
    for heading in headings:
        following = sorted(
            (
                b
                for b in blocks
                if b.page == heading.page
                and b.bbox.x0 >= heading.bbox.x0 - 0.04
                and b.bbox.y0 >= heading.bbox.y0
            ),
            key=lambda b: b.order,
        )
        for block in following:
            if block.id != heading.id and block.kind == "heading":
                break
            if block.kind in {"text", "heading", "picture"}:
                owned.add(block.id)
    return owned


def anchor_row(
    row: EditionBlock, report: DashboardReport, originals: list[ReadingItem]
) -> EditionBlock:
    """Copy a row with a uniquely supported source region; leave ambiguity alone."""
    blocks = [b for b in report.document.blocks if b.page == row.page]
    by_id = {b.id: b for b in blocks}
    if row.kind == "formula":
        number = _number(row.label)
        candidates = [b for b in blocks if b.kind == "formula"]
        if number:
            matches = [
                b
                for b in candidates
                if re.search(rf"[（(]\s*{re.escape(number)}\s*[)）]\s*$", b.text, re.I)
            ]
        else:
            # Match a literal named quantity, e.g. RMSE or sparsity, before '='.
            prefix = re.sub(r"\\(?:mathrm|operatorname|text)\{([^{}]+)\}", r"\1", row.content)
            name = re.match(r"\s*([A-Za-z][A-Za-z ]{1,40})\s*=", prefix)
            matches = [
                b for b in candidates if name and _compact(b.text).startswith(_compact(name[1]))
            ]
        return _with_sources(row, matches) if len(matches) == 1 else row
    old = matching_object(row, originals)
    if old:
        row = _with_sources(row, [by_id[p.block_id] for p in old.parts if p.block_id in by_id])
        if old.bbox:
            row = row.model_copy(
                update={"box": (old.bbox.x0, old.bbox.y0, old.bbox.x1, old.bbox.y1)}
            )
        return row
    if row.kind not in {"heading", "paragraph", "reference", "information"}:
        return row
    block = _text_match(row, blocks)
    if not block:
        sources = (
            _paragraph_sources(row, blocks, originals)
            if row.kind in {"paragraph", "heading"}
            else []
        )
        if not sources:
            return row
        block = sources[0]
    else:
        sources = [block]
    row = _with_sources(row, sources)
    if block.id in _biography_ids(blocks):
        return row.model_copy(update={"kind": "information", "category": "authors", "label": ""})
    if (
        row.kind == "heading"
        and block.kind == "text"
        and len(_compact(row.content)) >= 25
        and re.search(r"[。.!?！？]\s*$", row.content)
    ):
        # The first sentence on a page can finish the preceding page's paragraph.
        body = sorted((b for b in blocks if b.kind in {"text", "heading"}), key=lambda b: b.order)
        previous = sorted(
            (
                b
                for b in report.document.blocks
                if b.page == row.page - 1 and b.kind in {"text", "heading"}
            ),
            key=lambda b: b.order,
        )
        continuation = bool(
            body
            and body[0].id == block.id
            and previous
            and previous[-1].kind == "text"
            and not re.search(r"[。.!?！？]\s*$", previous[-1].text)
        )
        row = row.model_copy(
            update={
                "kind": "paragraph",
                "label": "",
                "level": 1,
                "continuation": row.continuation or continuation,
            }
        )
    return row
