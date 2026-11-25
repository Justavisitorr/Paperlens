"""Display-formula crops fitted to the glyphs PDFium actually paints.

Layout models give formula boxes that can clip a leading glyph (``θ`` of
``θ^{t+1}``), cut a fraction's numerator, or take in half a line of the next
paragraph. The painted glyph boxes decide instead: glyphs centred in the box
are the formula's, clipped glyphs and same-row neighbours join it unless they
belong to another block, and the crop never reaches into those other blocks.
"""

from __future__ import annotations

import statistics

from .models import Box

Rect = tuple[float, float, float, float]  # x0, top, x1, bottom in points, top-left origin


def page_glyphs(page) -> list[Rect]:
    """Painted glyph boxes of a page in displayed top-left coordinates."""
    if page.get_rotation():
        return []
    crop = page.get_cropbox()
    textpage = page.get_textpage()
    try:
        boxes = []
        for index in range(textpage.count_chars()):
            if not textpage.get_text_range(index, 1).strip():
                continue
            left, bottom, right, top = textpage.get_charbox(index)
            if right > left and top > bottom:
                boxes.append((left - crop[0], crop[3] - top, right - crop[0], crop[3] - bottom))
        return boxes
    finally:
        textpage.close()


def _inside(x: float, y: float, rect: Rect) -> bool:
    return rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]


def _overlaps(a: Rect, b: Rect) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def formula_box(
    glyphs: list[Rect], bbox: Box, others: list[Box], width: float, height: float
) -> Box:
    """The formula's own glyphs plus padding, or the original box when unsure."""
    box = (bbox.x0 * width, bbox.y0 * height, bbox.x1 * width, bbox.y1 * height)
    foreign = [(b.x0 * width, b.y0 * height, b.x1 * width, b.y1 * height) for b in others]

    def centre(g: Rect) -> tuple[float, float]:
        return (g[0] + g[2]) / 2, (g[1] + g[3]) / 2

    def theirs(g: Rect) -> bool:
        return any(_inside(*centre(g), rect) for rect in foreign)

    own = [g for g in glyphs if _inside(*centre(g), box)]
    if not own:
        return bbox
    size = statistics.median(g[3] - g[1] for g in own)
    # A block of one or two glyphs beside the formula is a parsing residue, such as the
    # leading θ split off "θ^{t+1} = ...", not another paragraph.
    foreign = [r for r in foreign if sum(_inside(*centre(g), r) for g in glyphs) > 2]
    rest = [g for g in glyphs if g not in own]
    while True:
        x0 = min(g[0] for g in own)
        x1 = max(g[2] for g in own)
        top = min(g[1] for g in own)
        bottom = max(g[3] for g in own)
        joined = [
            g
            for g in rest
            if not theirs(g)
            and (
                _overlaps(g, box)
                # A neighbour on the formula's rows that the layout box clipped away.
                or (
                    g[1] < bottom
                    and top < g[3]
                    and (abs(g[0] - x1) < size * 0.8 or abs(x0 - g[2]) < size * 0.8)
                )
            )
        ]
        if not joined:
            break
        own += joined
        rest = [g for g in rest if g not in joined]
    pad_x, pad_y = size * 0.2, size * 0.3
    left, right = x0 - pad_x, x1 + pad_x
    upper, lower = top - pad_y, bottom + pad_y
    # Path-drawn parts (fraction bars, radicals) sit inside the glyph span or the
    # padding; the padding stops short of any other block's glyphs.
    for g in rest:
        if g[0] < right and left < g[2]:
            if g[3] <= top:
                upper = max(upper, (g[3] + top) / 2)
            elif g[1] >= bottom:
                lower = min(lower, (g[1] + bottom) / 2)
    return Box(
        x0=max(0.0, left / width),
        y0=max(0.0, upper / height),
        x1=min(1.0, right / width),
        y1=min(1.0, lower / height),
    )
