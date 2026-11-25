"""Conservative local chart ownership and bounded PDF-native crops.

All boxes describe the displayed (already rotated) page, normalized from its
top-left corner. This module neither creates visual IDs nor guesses quadrants.
Caption proximity and containment deliberately mirror reading.py's geometry,
without importing that presentation module or its heavier dependencies.
"""

from __future__ import annotations

import base64
import io
import math
from collections.abc import Sequence
from pathlib import Path

import pypdfium2 as pdfium

from .control import PDF_LOCK, checkpoint
from .errors import InputError
from .models import Block, Box, PaperDocument, VisualEvidence

MAX_IMAGES = 8
MAX_IMAGE_BYTES = 16 * 1024 * 1024  # Includes base64 and the data-URL prefix.
MAX_PIXELS = 12_000_000
MAX_LONG_EDGE = 4096


def _union(boxes: Sequence[Box]) -> Box:
    return Box(
        x0=min(b.x0 for b in boxes), y0=min(b.y0 for b in boxes),
        x1=max(b.x1 for b in boxes), y1=max(b.y1 for b in boxes),
    )


def _valid(box: Box | None) -> bool:
    return box is not None and box.x1 > box.x0 and box.y1 > box.y0


def _contained(inner: Box, outer: Box) -> float:
    area = (inner.x1 - inner.x0) * (inner.y1 - inner.y0)
    if area <= 0:
        return 0
    return (
        max(0, min(inner.x1, outer.x1) - max(inner.x0, outer.x0))
        * max(0, min(inner.y1, outer.y1) - max(inner.y0, outer.y0)) / area
    )


def _caption_distance(block: Block, caption: Block) -> float | None:
    if block.page != caption.page:
        return None
    if min(block.bbox.x1, caption.bbox.x1) <= max(block.bbox.x0, caption.bbox.x0):
        return None
    if block.kind == "table":
        gap = min(abs(caption.bbox.y0 - block.bbox.y1), abs(block.bbox.y0 - caption.bbox.y1))
        return gap if gap < 0.08 else None
    gap = caption.bbox.y0 - block.bbox.y1
    # Same tolerance as the reading ownership rule. A reference alone is not proof.
    return max(0, gap) if -0.015 <= gap < 0.065 else None


def attach_chart_regions(
    document: PaperDocument, visuals: Sequence[VisualEvidence],
) -> dict[str, list[Box]]:
    """Fill trusted chart boxes in place; return actual picture-panel boxes.

    Only pictures/tables physically related to a source caption can establish a
    region. Missing geometry stays None. Nearby short axis/legend labels may
    expand it, but captions and article paragraphs are excluded from the crop.
    """
    by_id = {b.id: b for b in document.blocks}
    charts = [v for v in visuals if v.kind in {"figure", "table"}]
    owners: dict[str, list[Block]] = {v.id: [] for v in charts}
    captions = {
        v.id: [by_id[i] for i in v.source_block_ids
               if i in by_id and by_id[i].page == v.page
               and by_id[i].kind in {"caption", "text", "heading"}]
        for v in charts
    }
    blocks = [b for b in document.blocks if b.kind in {"picture", "table"} and _valid(b.bbox)]
    assigned: dict[str, str] = {}
    for block in blocks:
        kind = "table" if block.kind == "table" else "figure"
        options: list[tuple[int, float, str]] = []
        for visual in charts:
            if visual.page != block.page or visual.kind != kind:
                continue
            if block.id in visual.source_block_ids:
                options.append((-1, 0, visual.id))
                continue
            for caption in captions[visual.id]:
                distance = _caption_distance(block, caption)
                if distance is not None:
                    options.append((0 if caption.id in block.caption_ids else 1, distance, visual.id))
        if options:
            _, _, owner = min(options)
            owners[owner].append(block)
            assigned[block.id] = owner

    # Stacked panels are allowed only when their edges align and a neighboring
    # picture already belongs to a physical caption. Do not bridge another figure.
    changed = True
    while changed:
        changed = False
        for block in blocks:
            if block.kind != "picture" or block.id in assigned:
                continue
            following = [
                other for other in blocks if other.kind == "picture"
                and other.id in assigned and other.page == block.page
                and 0 <= other.bbox.y0 - block.bbox.y1 < 0.03
                and abs(other.bbox.x0 - block.bbox.x0) < 0.025
                and abs(other.bbox.x1 - block.bbox.x1) < 0.025
            ]
            if not following:
                continue
            below = min(following, key=lambda b: b.bbox.y0)
            # A different caption in this gap is a hard ownership boundary.
            if any(c.page == block.page and block.bbox.y1 <= c.bbox.y0 < below.bbox.y0
                   for cs in captions.values() for c in cs):
                continue
            owner = assigned[below.id]
            assigned[block.id] = owner
            owners[owner].append(block)
            changed = True

    panels: dict[str, list[Box]] = {}
    for visual in charts:
        owned = owners[visual.id]
        if not owned:
            continue
        box = _union([b.bbox for b in owned])
        following_captions = [c.bbox.y0 for c in captions[visual.id] if c.bbox.y0 >= box.y1]
        bottom = min(following_captions, default=box.y1 + 0.04)
        # This small halo retains standalone ticks, axis titles, and legends.
        # Ownership requires mostly contained short text; it never absorbs prose.
        halo = Box(x0=max(0, box.x0 - 0.04), x1=min(1, box.x1 + 0.04),
                   y0=max(0, box.y0 - 0.025), y1=min(1, bottom, box.y1 + 0.04))
        labels = [b for b in document.blocks if b.page == visual.page and b.kind == "text"
                  and b.id not in visual.source_block_ids and 0 < len(b.text.strip()) <= 100
                  and len(b.text.splitlines()) <= 3 and _contained(b.bbox, halo) > 0.85]
        visual.bbox = _union([box, *[b.bbox for b in labels]])
        visual.source_block_ids = list(dict.fromkeys(
            [*visual.source_block_ids, *[b.id for b in owned], *[b.id for b in labels]]
        ))
        if visual.kind == "figure" and len(owned) > 1:
            # Actual detector boxes, never a mechanical grid. The parent crop
            # remains the authoritative place for axes outside these panel boxes.
            panels[visual.id] = [b.bbox for b in sorted(owned, key=lambda b: (b.bbox.y0, b.bbox.x0))]
    return panels


def native_chart_text(document: PaperDocument, visual: VisualEvidence) -> str:
    """Native labels strictly inside the located chart, for value checks only."""
    if visual.kind not in {"figure", "table"} or not _valid(visual.bbox):
        return ""
    lines: list[str] = []
    for block in sorted(document.blocks, key=lambda b: b.order):
        if (block.page != visual.page or block.kind not in {"text", "table"}
                or block.text_source != "pdf_text"):
            continue
        if block.kind == "table" and block.table:
            lines.extend(c.text for c in block.table.cells
                         if _contained(c.bbox or block.bbox, visual.bbox) >= 1 - 1e-8)
        elif _contained(block.bbox, visual.bbox) >= 1 - 1e-8 and block.text.strip():
            lines.append(block.text.strip())
    return "\n".join(dict.fromkeys(lines))[:12000]


def _padded(box: Box, amount: float) -> Box:
    return Box(x0=max(0, box.x0 - amount), y0=max(0, box.y0 - amount),
               x1=min(1, box.x1 + amount), y1=min(1, box.y1 + amount))


def _encode(bitmap, byte_budget: int) -> tuple[str, int, int]:
    """Bound the complete encoded payload, including base64 expansion."""
    with bitmap.to_pil() as original, original.convert("RGB") as rgb:
        image = rgb
        resized = None
        try:
            for quality in (90, 80, 65, 50):
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=quality, optimize=True)
                url = "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")
                if len(url) <= byte_budget:
                    return url, image.width, image.height
            while max(image.size) > 128:
                new_size = tuple(max(1, int(d * 0.7)) for d in image.size)
                next_image = image.resize(new_size)
                if resized is not None:
                    resized.close()
                resized = image = next_image
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=65, optimize=True)
                url = "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")
                if len(url) <= byte_budget:
                    return url, image.width, image.height
            raise InputError("图片裁剪超出本次图片输入的大小限制。")
        finally:
            if resized is not None:
                resized.close()


def render_visual_inputs(
    pdf_path: Path | str, number: int, candidates: Sequence[VisualEvidence], *,
    crop_long_edge: int, max_crops: int, include_panels: bool = False,
    panels: dict[str, list[Box]] | None = None,
) -> tuple[list[str], list[dict]]:
    """Render one page context and bounded, independent high-resolution crops.

    Formula slots (up to two on a mixed page) are reserved before optional
    panels. PDFium's crop uses displayed coordinates after intrinsic rotation:
    (left, bottom, right, top), measured as margins in PDF canvas units. Passing
    the margins to render allocates only the cropped bitmap, not a huge page.
    Returned bbox is always in ORIGINAL PAGE normalized coordinates, never in
    the local crop's coordinates; all provider calls belong outside PDF_LOCK.
    """
    slots = max(0, min(MAX_IMAGES - 1, int(max_crops)))
    long_edge = max(128, min(MAX_LONG_EDGE, int(crop_long_edge)))
    page_candidates = [v for v in candidates if v.page == number and _valid(v.bbox)]
    charts = [v for v in page_candidates if v.kind in {"figure", "table"}]
    formulas = [v for v in page_candidates if v.kind == "formula"]
    formula_slots = min(len(formulas), max(0, slots - (1 if charts else 0)), 2 if charts else slots)
    selected_charts = charts[:slots - formula_slots]
    selected = selected_charts + formulas[:slots - len(selected_charts)]
    # If formulas did not fill the crop budget, every complete chart still wins
    # over a panel. Panels may only reference their already selected parent.
    specs = [(v.id, "formula_crop" if v.kind == "formula" else "chart_crop",
              _padded(v.bbox, 0.006 if v.kind == "formula" else 0.012), None)
             for v in selected]
    if include_panels and panels:
        for visual in selected:
            for panel_index, box in enumerate(panels.get(visual.id, []), 1):
                if len(specs) >= slots:
                    break
                # A panel must remain within its trusted parent's region.
                if _valid(box) and _contained(box, visual.bbox) >= 1 - 1e-8:
                    specs.append((visual.id, "panel_crop", _padded(box, 0.012), panel_index))
    full = Box(x0=0, y0=0, x1=1, y1=1)
    specs.insert(0, (None, "page_context", full, None))
    images: list[str] = []
    image_order: list[dict] = []
    with PDF_LOCK, pdfium.PdfDocument(pdf_path) as pdf:
        if number < 1 or number > len(pdf):
            raise InputError("图片分析页码超出 PDF 页数。")
        page = pdf[number - 1]
        try:
            width, height = page.get_size()
            for visual_id, role, box, panel_index in specs:
                checkpoint()
                crop_width, crop_height = width * (box.x1 - box.x0), height * (box.y1 - box.y0)
                missing_region = any(v.kind != "formula" and not _valid(v.bbox) for v in candidates)
                context_edge = max(2400, long_edge) if missing_region else min(long_edge, 1800)
                target = context_edge if role == "page_context" else long_edge
                # PDFium rounds the full canvas and margins separately. Leave
                # two pixels per edge so rounding cannot exceed the hard limits.
                scale = min((target - 2) / max(crop_width, crop_height),
                            math.sqrt((MAX_PIXELS - 2 * MAX_LONG_EDGE) / (crop_width * crop_height)),
                            1_000_000 / max(width, height))
                margins = (box.x0 * width, (1 - box.y1) * height,
                           (1 - box.x1) * width, box.y0 * height)
                bitmap = page.render(scale=scale, crop=margins)
                try:
                    # Divide the budget fairly: no early image consumes all of it.
                    remaining = MAX_IMAGE_BYTES - sum(len(data) for data in images)
                    url, pixels_w, pixels_h = _encode(bitmap, remaining // (len(specs) - len(images)))
                    images.append(url)
                    meta = {"visual_id": visual_id, "role": role, "page": number,
                            "bbox": box.model_dump(), "width": pixels_w, "height": pixels_h}
                    if panel_index is not None:
                        meta["panel_index"] = panel_index
                        meta["parent_visual_id"] = visual_id
                    image_order.append(meta)
                finally:
                    bitmap.close()
        finally:
            page.close()
    return images, image_order
