"""Attach source regions already presented inside a rendered reading object."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pdfplumber

from .edition_layout import _object_number
from .models import Block, Box, ReadingItem, ReadingPart
from .parsing.pdf_utils import _display_page
from .reading import overlap, utf16


def pdf_image_regions(path: Path, pages: set[int]) -> dict[int, list[Box]]:
    """Read actual embedded-image bounds in the displayed PDF coordinate frame."""
    regions: dict[int, list[Box]] = defaultdict(list)
    if not path.is_file():
        return regions
    try:
        with pdfplumber.open(path) as pdf:
            for number in sorted(pages):
                page = pdf.pages[number - 1]
                try:
                    displayed = _display_page(page)
                    x, y = displayed.bbox[:2]
                    for image in displayed.images:
                        try:
                            box = Box(
                                x0=(image["x0"] - x) / displayed.width,
                                y0=(image["top"] - y) / displayed.height,
                                x1=(image["x1"] - x) / displayed.width,
                                y1=(image["bottom"] - y) / displayed.height,
                            )
                        except (KeyError, TypeError, ValueError, ZeroDivisionError):
                            # Never clamp a malformed/out-of-page image into
                            # apparent ownership of otherwise sound prose.
                            continue
                        # Whole-page scans also contain ordinary prose. They do
                        # not establish ownership by an individual figure.
                        area = (box.x1 - box.x0) * (box.y1 - box.y0)
                        if 0 < area < 0.65:
                            regions[number].append(box)
                finally:
                    page.close()
    except Exception:
        # Optional metadata cannot invalidate an already rendered PDF crop.
        # Without reliable image evidence, retain the original source fallback.
        return {}
    return regions



def caption_image_box(
    item: ReadingItem, caption: Block, blocks: dict[str, Block], regions: dict[int, list[Box]]
) -> Box | None:
    """Select exact image bounds for a uniquely anchored numbered caption."""
    if not item.bbox or caption.page != item.page:
        return None
    number = _object_number("figure", item.label)
    if number is None or _object_number("figure", caption.text) != number:
        return None
    if overlap(caption.bbox, item.bbox) < .98:
        return None

    def adjacent(image: Box, box: Box) -> bool:
        width = min(image.x1 - image.x0, box.x1 - box.x0)
        widest = max(image.x1 - image.x0, box.x1 - box.x0)
        shared = max(0, min(image.x1, box.x1) - max(image.x0, box.x0))
        gap = max(box.y0 - image.y1, image.y0 - box.y1)
        area = (image.x1 - image.x0) * (image.y1 - image.y0)
        return (area >= .01 and widest > 0 and width / widest >= .65
                and shared / width >= .9 and 0 <= gap <= .03)

    candidates = []
    for image in regions.get(item.page, []):
        if not adjacent(image, caption.bbox):
            continue
        captions = [block for block in blocks.values()
                    if block.page == item.page and block.kind == "caption"
                    and adjacent(image, block.bbox)]
        if len(captions) != 1 or captions[0].id != caption.id:
            continue
        region = Box(x0=min(image.x0, caption.bbox.x0), y0=min(image.y0, caption.bbox.y0),
                     x1=max(image.x1, caption.bbox.x1), y1=max(image.y1, caption.bbox.y1))
        # Accept an exact caption anchor or an already supported image/caption
        # crop. A broad model rectangle supplies no additional ownership.
        if overlap(item.bbox, caption.bbox) < .8 and (
            overlap(item.bbox, region) < .95 or overlap(region, item.bbox) < .95
        ):
            continue
        # Reject interposed prose, neighboring captions and mixed source blocks.
        if any(block.page == item.page and block.id != caption.id and block.text.strip()
               and overlap(block.bbox, region) > .2
               and overlap(block.bbox, image) < .98
               for block in blocks.values()):
            continue
        # Render the image itself. The literal caption is presented separately,
        # avoiding a second copy of its printed text inside the crop.
        candidates.append(image)
    return candidates[0] if len(candidates) == 1 else None


def claim_visual_sources(
    items: list[ReadingItem],
    originals: list[ReadingItem],
    blocks: dict[str, Block],
    covered: dict[str, list[tuple[int, int]]],
    image_regions: dict[int, list[Box]],
) -> None:
    """One displayed crop owns contained source objects and actual image text.

    A model rectangle alone never owns prose. Text must additionally be inside
    an embedded PDF image; partially overlapping or mixed image/prose blocks
    remain separate. Native visual objects already have independent geometry.
    """
    for item in items:
        if item.kind not in {"figure", "table"} or not item.asset or not item.bbox:
            continue
        parts = []
        for old in originals:
            if (
                old.page == item.page
                and old.kind in {"figure", "table"}
                and old.bbox
                and overlap(old.bbox, item.bbox) >= 0.98
            ):
                parts.extend(old.parts)
                item.result_ids = list(dict.fromkeys(item.result_ids + old.result_ids))
                item.visual_ids = list(dict.fromkeys(item.visual_ids + old.visual_ids))
        regions = image_regions.get(item.page, [])
        for block in blocks.values():
            if (
                block.page == item.page
                and block.kind == "text"
                and overlap(block.bbox, item.bbox) >= 0.98
                and any(overlap(block.bbox, region) >= 0.98 for region in regions)
            ):
                parts.append(ReadingPart(block_id=block.id, end=utf16(block.text), bbox=block.bbox))
        for part in parts:
            if part not in item.parts:
                item.parts.append(part)
            span = (part.start, part.end)
            if span not in covered[part.block_id]:
                covered[part.block_id].append(span)
    # Several structured sub-tables can legitimately share one native region.
    # Preserve each grid/caption and its own ID, but show identical pixels once.
    shown: list[ReadingItem] = []
    visuals = [item for item in items if item.kind in {"figure", "table"} and item.asset and item.bbox]
    # A captioned source figure supplies the shared image for structured views.
    for item in sorted(visuals, key=lambda value: (
        value.kind != "figure", -(value.bbox.x1-value.bbox.x0)*(value.bbox.y1-value.bbox.y0)
    )):
        redundant = any(
            old.page == item.page and (
                (old.bbox == item.bbox and old.asset == item.asset)
                or (overlap(item.bbox, old.bbox) >= .98 and any(
                    a.block_id == b.block_id
                    and blocks[a.block_id].kind in {"table", "picture"}
                    and max(a.start, b.start) < min(a.end, b.end)
                    for a in item.parts for b in old.parts
                ))
            ) for old in shown
        )
        if redundant:
            item.asset = ""
        else:
            shown.append(item)
