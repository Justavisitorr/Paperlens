"""Source-preserving repairs for reports produced before word-level ownership.

The parser repairs new documents. This projection repairs saved reports without
changing the text, identity or geometry addressed by evidence and annotations.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from pathlib import Path

from .models import Block, PaperDocument, ReadingPart


def normalized_offsets(text: str) -> tuple[str, list[int]]:
    chars, offsets = [], []
    for index, char in enumerate(text):
        for value in unicodedata.normalize("NFKC", char):
            if not value.isspace():
                chars.append(value)
                offsets.append(index)
    return "".join(chars), offsets


def side_margin(block: Block) -> bool:
    """A text strip wholly beside the type area: arXiv's rotated stamp, line numbers.

    The vertical stamp is extracted one reversed glyph run at a time
    ("3202", "gu", "A" …); body text never fits inside the outer 8%.
    """
    return block.kind in {"text", "heading"} and (block.bbox.x1 < 0.08 or block.bbox.x0 > 0.92)


NUMBERED_HEADING = re.compile(r"^(?:\d+(?:\.\d+)*\.?|[A-Z](?:\.\d+)+\.?|[IVX]+\.)\s+\S")
NAMED_SECTION = re.compile(
    r"^(?:abstract|introduction|references|bibliography|acknowledge?ments?|appendix|"
    r"conclusions?|related work|about the authors?|biograph|"
    r"摘要|引言|参考文献|致谢|附录|结论|作者)(?![A-Za-z])",
    re.I,
)


def figure_titles(blocks: list[Block]) -> set[str]:
    """Panel titles drawn above a picture that the layout model calls headings.

    Attention p.4 "Scaled Dot-Product Attention" and p.14 "Input-Input Layer5"
    are figure lettering, not sections. Only papers whose sections are
    numbered qualify, and a standard unnumbered section name never does.
    """
    headings = [b for b in blocks if b.kind == "heading"]
    if sum(bool(NUMBERED_HEADING.match(b.text.strip())) for b in headings) < 3:
        return set()
    pictures = [b for b in blocks if b.kind == "picture"]
    titles = set()
    for heading in headings:
        text = heading.text.strip()
        if NUMBERED_HEADING.match(text) or NAMED_SECTION.match(text) or text[-1:] in ":：":
            continue
        width = heading.bbox.x1 - heading.bbox.x0
        for picture in pictures:
            shared = min(heading.bbox.x1, picture.bbox.x1) - max(heading.bbox.x0, picture.bbox.x0)
            if (
                picture.page == heading.page
                and width > 0
                and shared >= 0.5 * width
                and 0 <= picture.bbox.y0 - heading.bbox.y1 <= 0.07
            ):
                titles.add(heading.id)
                break
    return titles


def project_sources(
    document: PaperDocument, pdf_path: Path | None
) -> tuple[list[Block], dict[str, ReadingPart], set[str]]:
    original = sorted(document.blocks, key=lambda block: (block.page, block.order))
    lettering = figure_titles(original)
    margin_pages: dict[str, set[int]] = defaultdict(set)
    for block in original:
        if block.bbox.y1 < 0.105 and len(block.text) < 120:
            token = re.sub(r"[\W\d_]+", "", block.text)
            if len(token) >= 4:
                margin_pages[token].add(block.page)
    original = [
        block.model_copy(update={"kind": "header"})
        if (
            block.bbox.y1 < 0.105
            and (
                len(margin_pages.get(re.sub(r"[\W\d_]+", "", block.text), set())) >= 2
                or re.fullmatch(r"[·•\s]*\d+[·•\s]*|第\s*\d+\s*[卷期]", block.text)
            )
        )
        or side_margin(block)
        or block.id in lettering
        else block
        for block in original
    ]
    # Saved pre-v2 reports are projected from their preserved blocks.
    # Opening a paper never runs a retired PDF parser or OCR repair pipeline.
    return original, {}, set()
