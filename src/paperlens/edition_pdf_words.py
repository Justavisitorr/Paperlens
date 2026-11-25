"""Read rare acronym hyphen artifacts from original PDF regions during rebuilding."""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

import pypdfium2 as pdfium

from .control import PDF_LOCK
from .edition_continuation import repair_pdf_acronyms
from .models import Block, ReadingItem


def restore_pdf_acronyms(items: list[ReadingItem], pdf_path: Path, blocks: dict[str, Block]) -> int:
    """Repair only semantic paragraphs; source blocks and evidence never change."""
    regions = defaultdict(list)
    candidates = {}
    for item in items:
        if item.kind != "paragraph" or not re.search(r"\b[A-Z0-9]{4,8}\b", item.content or ""):
            continue
        candidates[item.id] = item
        for part in item.parts:
            block = blocks.get(part.block_id)
            if block:
                regions[block.page].append((item.id, part.bbox or block.bbox))
        if not item.parts and item.bbox:
            regions[item.page].append((item.id, item.bbox))
    if not regions:
        return 0
    source = defaultdict(list)
    try:
        with PDF_LOCK, pdfium.PdfDocument(pdf_path) as doc:
            for number, page_regions in regions.items():
                page = doc[number - 1]
                textpage = page.get_textpage()
                try:
                    width, height = page.get_size()
                    seen = set()
                    for ident, box in page_regions:
                        key = (ident, box.x0, box.y0, box.x1, box.y1)
                        if key in seen:
                            continue
                        seen.add(key)
                        source[ident].append(
                            textpage.get_text_bounded(
                                left=box.x0 * width,
                                bottom=(1 - box.y1) * height,
                                right=box.x1 * width,
                                top=(1 - box.y0) * height,
                            )
                        )
                finally:
                    textpage.close()
                    page.close()
    except (OSError, ValueError, pdfium.PdfiumError):
        return 0
    changed = 0
    for ident, pieces in source.items():
        item = candidates[ident]
        repaired = repair_pdf_acronyms(item.content or "", "\n".join(pieces))
        if repaired != item.content:
            item.content = repaired
            changed += 1
    return changed
