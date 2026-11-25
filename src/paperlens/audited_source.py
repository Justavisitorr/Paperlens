"""Freeze the reviewed reading into the exact source consumed by analysis.

The native transcript is saved separately by the coordinator. In this document,
both text and raw_text contain the reviewed version: evidence matching must not
fall back to a rejected pre-review number, negation or formula.
"""

from __future__ import annotations

from .edition_anchors import content_anchors
from .models import Block, DashboardReport, ReadingItem, ReadingPart, TableGrid
from .reading import utf16
from .storage import fingerprint

VERSION = "audited-reading-v1"


def _part_text(part: ReadingPart, blocks: dict[str, Block]) -> str:
    block = blocks[part.block_id]
    return block.text.encode("utf-16-le")[part.start * 2:part.end * 2].decode("utf-16-le")


def _table_text(table: TableGrid) -> str:
    rows = [[] for _ in range(table.rows)]
    for cell in table.cells:
        if 0 <= cell.row_start < table.rows and 0 <= cell.col_start < table.cols:
            row = rows[cell.row_start]
            row.extend([""] * max(0, cell.col_start + 1 - len(row)))
            row[cell.col_start] = cell.text
    return "\n".join(" | ".join(row) for row in rows)


def _item_text(item: ReadingItem, blocks: dict[str, Block]) -> str:
    text = item.content
    table = item.table if item.kind == "table" else None
    if text is None:
        text = item.label if table and item.label else "".join(
            part.join + _part_text(part, blocks) for part in item.parts
        )
        if table:
            # A fallback source block can already contain its serialized cells.
            # Remove only a known, exact grid suffix, including the old grid if
            # cells were corrected. Preserve the caption separately for readers
            # before parts are re-anchored to the complete analysis block.
            grids = [blocks[part.block_id].table for part in item.parts] + [table]
            for grid in grids:
                cells = _table_text(grid) if grid else ""
                if cells and text == cells:
                    text = ""
                elif cells and text.endswith("\n" + cells):
                    text = text[:-(len(cells) + 1)]
            item.content = text
    return text + "\n" + _table_text(table) if table else text


def canonicalize_reading(report: DashboardReport) -> dict:
    """Re-anchor the reading and expose only its final content to downstream work.

    Unchanged, whole native blocks retain their IDs. Changed or merged content
    gets a deterministic new ID, so an annotation against an earlier transcript
    can never silently select different characters. Cross-page text is kept
    intact and attributed to all source pages rather than heuristically split.
    """
    if not report.reading:
        return {"version": VERSION, "status": "not_applicable", "blocks": [], "removed_source_ids": []}
    reading = report.reading
    originals = {block.id: block for block in report.document.blocks}
    canonical: dict[str, Block] = {}
    mappings = []
    touched = set()
    groups = [reading.items, reading.references, *reading.information_items.values()]
    for items in groups:
        for item in items:
            old_parts = item.parts
            ids = list(dict.fromkeys(part.block_id for part in old_parts))
            if any(ident not in originals for ident in ids):
                raise ValueError("Reviewed text references an unknown original block")
            sources = [originals[ident] for ident in ids]
            pages = sorted({item.page, *item.source_pages,
                            *(page for b in sources for page in (b.source_pages or [b.page]))})
            if not set(pages) <= {p.number for p in report.document.pages}:
                raise ValueError("Reviewed text references a page outside the original PDF")
            text = _item_text(item, originals)
            kind = {"paragraph": "text", "figure": "picture", "code": "algorithm"}.get(item.kind, item.kind)
            original = sources[0] if sources else None
            unchanged = bool(
                original and len(old_parts) == 1 and old_parts[0].start == 0
                and old_parts[0].end == utf16(original.text)
                and text == original.text and kind == original.kind
                and (item.table is None or item.table == original.table)
                and pages == [original.page]
            )
            if unchanged:
                block = original.model_copy(deep=True)
            else:
                identity = {"version": VERSION, "item": item.id, "kind": kind, "text": text,
                            "sources": [p.model_dump(mode="json") for p in old_parts], "pages": pages}
                ident = "reviewed-" + fingerprint(identity)[:24]
                block = Block(
                    id=ident, page=pages[0], text=text, raw_text=text,
                    bbox=original.bbox if original else item.bbox or {"x0": 0, "y0": 0, "x1": 1, "y1": 1},
                    order=min((b.order for b in sources), default=len(originals) + len(canonical)),
                    section=original.section if original else "", kind=kind,
                    table=item.table.model_copy(deep=True) if item.table else None,
                    source_ref="ai-direct:unverified-position", text_source="ocr",
                    source_pages=pages if len(pages) > 1 else [], source_block_ids=ids,
                    text_warnings=["审校后的阅读正文；原始转录及来源块映射保存在处理记录中。"],
                )
            # Even an unchanged block must not offer stale raw text as evidence.
            block.raw_text = text
            canonical[block.id] = block
            touched.update(ids)
            mappings.append({"item_id": item.id, "block_id": block.id, "source_block_ids": ids,
                             "source_parts": [p.model_dump(mode="json") for p in old_parts],
                             "source_pages": pages, "changed": not unchanged})
            item.parts = [ReadingPart(block_id=block.id, start=0, end=utf16(text), bbox=block.bbox)]
            item.source_pages = pages if len(pages) > 1 else item.source_pages
            item.content_spans = content_anchors(item, {block.id: block})

    # Older/incomplete projections may retain publication metadata solely as
    # source parts. Keep those exact blocks; never resurrect removed body items.
    for parts in reading.information_parts.values():
        for part in parts:
            if part.block_id in originals and part.block_id not in touched:
                canonical[part.block_id] = originals[part.block_id].model_copy(deep=True)
                canonical[part.block_id].raw_text = canonical[part.block_id].text
                touched.add(part.block_id)
    for ids in reading.information.values():
        for ident in ids:
            if ident in originals and ident not in touched:
                canonical[ident] = originals[ident].model_copy(deep=True)
                canonical[ident].raw_text = canonical[ident].text
                touched.add(ident)
    # Explicit metadata source lists only describe retained raw fallback blocks.
    reading.information = {name: [i for i in ids if i in canonical] for name, ids in reading.information.items()}
    reading.information_parts = {
        name: [p for p in parts if p.block_id in canonical]
        for name, parts in reading.information_parts.items()
    }
    reading.hidden_ids = [ident for ident in reading.hidden_ids if ident in canonical]
    reading.excluded_toc_parts = [part for part in reading.excluded_toc_parts if part.block_id in canonical]
    report.document.blocks = sorted(canonical.values(), key=lambda block: block.order)
    for page in report.document.pages:
        page.text_chars = sum(len(block.text) for block in report.document.blocks if block.page == page.number)
    reading.source_version = fingerprint({
        "version": VERSION, "pdf": report.document.sha256,
        "blocks": [block.model_dump(mode="json") for block in report.document.blocks],
    })
    report.quality.text_chars = sum(len(block.text) for block in report.document.blocks)
    return {"version": VERSION, "status": "completed", "source_version": reading.source_version,
            "original_document": "original-document.json", "original_reading": "original-reading.json",
            "blocks": mappings, "removed_source_ids": sorted(set(originals) - touched)}
