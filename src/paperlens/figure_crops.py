"""Render native-reading figures from PDF geometry, keeping model boxes as hints."""

from __future__ import annotations

import base64
import io
import math
import re
from collections import defaultdict
from contextlib import closing
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

from .control import PDF_LOCK, checkpoint
from .models import Box, DashboardReport, FigureCrop, ReadingItem

VERSION = "pdf-geometry-v1"
FULL_PAGE = Box(x0=0, y0=0, x1=1, y1=1)


def _area(box: Box) -> float:
    return (box.x1 - box.x0) * (box.y1 - box.y0)


def _intersection(a: Box, b: Box) -> float:
    return max(0, min(a.x1, b.x1) - max(a.x0, b.x0)) * max(0, min(a.y1, b.y1) - max(a.y0, b.y0))


def _display_box(obj, bitmap, transform) -> Box | None:
    left, bottom, right, top = obj.get_bounds()
    points = [transform.to_bitmap(x, y) for x in (left, right) for y in (bottom, top)]
    x0, x1 = min(p[0] for p in points) / bitmap.width, max(p[0] for p in points) / bitmap.width
    y0, y1 = min(p[1] for p in points) / bitmap.height, max(p[1] for p in points) / bitmap.height
    if 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1:
        return Box(x0=x0, y0=y0, x1=x1, y1=y1)
    return None


def _graphics(page, bitmap) -> tuple[list[Box], list[Box]]:
    """Top-level forms preserve complete subplots, including their axes and labels."""
    transform = bitmap.get_posconv(page)
    regions = []
    kinds = [
        pdfium.raw.FPDF_PAGEOBJ_IMAGE,
        pdfium.raw.FPDF_PAGEOBJ_FORM,
        pdfium.raw.FPDF_PAGEOBJ_PATH,
    ]
    for obj in page.get_objects(filter=kinds, max_depth=1):
        box = _display_box(obj, bitmap, transform)
        # Backgrounds and whole-page scans do not locate an individual figure.
        if box is None:
            continue
        if box.x1 - box.x0 >= 0.08 and box.y1 - box.y0 >= 0.025 and 0.005 <= _area(box) <= 0.7:
            regions.append(box)
    labels = []
    # LaTeX often places (a)/(b) outside the imported subplot objects.
    with closing(page.get_textpage()) as textpage:
        for obj in page.get_objects(
            filter=[pdfium.raw.FPDF_PAGEOBJ_TEXT], max_depth=1, textpage=textpage
        ):
            box = _display_box(obj, bitmap, transform)
            if (
                box
                and box.x1 - box.x0 < 0.08
                and re.fullmatch(r"\(?[a-zA-Z0-9]{1,3}\)", obj.extract().strip())
            ):
                labels.append(box)
    return regions, labels


def _regions(
    items: list[ReadingItem], graphics: list[Box], labels: list[Box], width: float, height: float
):
    owned: dict[str, list[Box]] = defaultdict(list)
    ambiguous = set()
    for graphic in graphics:
        matches = sorted(
            (
                (_intersection(graphic, item.bbox) / _area(graphic), item.id)
                for item in items
                if item.bbox
            ),
            reverse=True,
        )
        if not matches or matches[0][0] < 0.5:
            continue
        if len(matches) > 1 and matches[1][0] >= 0.5 and matches[0][0] - matches[1][0] < 0.15:
            ambiguous.update(ident for score, ident in matches if score >= 0.5)
            continue
        owned[matches[0][1]].append(graphic)
    crops = {}
    for item in items:
        boxes = owned[item.id]
        if boxes and item.id not in ambiguous:
            box = Box(
                x0=min(b.x0 for b in boxes),
                y0=min(b.y0 for b in boxes),
                x1=max(b.x1 for b in boxes),
                y1=max(b.y1 for b in boxes),
            )
            # A small decorative shape is not evidence for the complete figure.
            if _area(box) >= 0.25 * _area(item.bbox):
                nearby = [
                    label
                    for label in labels
                    if label.x0 >= box.x0 - 0.01
                    and label.x1 <= box.x1 + 0.01
                    and label.y0 >= box.y0 - 0.02
                    and label.y1 <= box.y1 + 0.04
                ]
                if nearby:
                    box = Box(
                        x0=min(box.x0, *(b.x0 for b in nearby)),
                        y0=min(box.y0, *(b.y0 for b in nearby)),
                        x1=max(box.x1, *(b.x1 for b in nearby)),
                        y1=max(box.y1, *(b.y1 for b in nearby)),
                    )
                pad_x, pad_y = 4 / width, 4 / height
                crops[item.id] = FigureCrop(
                    version=VERSION,
                    source="pdf_objects",
                    box=Box(
                        x0=max(0, box.x0 - pad_x),
                        y0=max(0, box.y0 - pad_y),
                        x1=min(1, box.x1 + pad_x),
                        y1=min(1, box.y1 + pad_y),
                    ),
                )
                continue
        crops[item.id] = FigureCrop(version=VERSION, source="page", box=FULL_PAGE)
    return crops


def _encode(image: Image.Image, box: Box) -> str:
    bounds = (
        math.floor(box.x0 * image.width),
        math.floor(box.y0 * image.height),
        math.ceil(box.x1 * image.width),
        math.ceil(box.y1 * image.height),
    )
    with image.crop(bounds) as crop, crop.convert("RGB") as rgb:
        stream = io.BytesIO()
        rgb.save(stream, "JPEG", quality=90)
        return "data:image/jpeg;base64," + base64.b64encode(stream.getvalue()).decode("ascii")


def render_native_figures(report: DashboardReport, pdf: Path) -> list[dict]:
    """Change only figure assets/crop metadata; preserve text, IDs and evidence boxes."""
    if report.document.parser not in {"openai-pdf", "model-page-images"} or not report.reading:
        return []
    pages: dict[int, list[ReadingItem]] = defaultdict(list)
    for item in report.reading.items:
        if item.kind == "figure":
            pages[item.page].append(item)
    audit = []
    if not pages:
        return audit
    with PDF_LOCK, pdfium.PdfDocument(pdf) as doc:
        for number, items in pages.items():
            pending = [
                item
                for item in items
                if not item.asset or not item.asset_crop or item.asset_crop.version != VERSION
            ]
            if not pending:
                continue
            checkpoint()
            page = doc[number - 1]
            try:
                width, height = page.get_size()
                bitmap = page.render(scale=min(3, 2600 / max(width, height)))
                try:
                    try:
                        graphics, labels = _graphics(page, bitmap)
                    except pdfium.PdfiumError:
                        graphics, labels = [], []
                    regions = _regions(items, graphics, labels, width, height)
                    with bitmap.to_pil() as image:
                        for item in pending:
                            crop = regions[item.id]
                            item.asset = _encode(image, crop.box)
                            item.asset_crop = crop
                            audit.append(
                                {
                                    "id": item.id,
                                    "page": number,
                                    "label": item.label,
                                    **crop.model_dump(mode="json"),
                                }
                            )
                finally:
                    bitmap.close()
            finally:
                page.close()
    return audit
