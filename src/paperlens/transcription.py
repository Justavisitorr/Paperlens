"""Rendering saved transcription fragments and source regions; no generation route."""
from __future__ import annotations

import base64
import io
import re
from pathlib import Path

import pypdfium2 as pdfium

from .control import PDF_LOCK
from .formulas import compile_formula
from .models import DashboardReport, ReadingItem, ReadingTranscription, TranscriptionSegment


def parse_transcription(raw: dict) -> ReadingTranscription | None:
    if raw.get("status") == "unreadable":
        return None
    pieces = raw.get("segments")
    if isinstance(raw.get("lines"), list):
        pieces = []
        for index, line in enumerate(raw["lines"]):
            if not isinstance(line, list):
                raise ValueError("Invalid algorithm line")
            if index:
                pieces.append({"text": "\n"})
            pieces.extend(line)
    if raw.get("status") != "recognized" or not isinstance(pieces, list):
        raise ValueError("Invalid transcription response")
    segments = []
    for item in pieces:
        if not isinstance(item, dict):
            raise ValueError("Invalid transcription segment")
        text, latex = item.get("text"), item.get("latex")
        if not text and not latex:
            continue
        if bool(text) == bool(latex):
            raise ValueError("Each transcription segment must be text or mathematics")
        if latex:
            if re.search(r"[\u3400-\u9fff]", re.sub(r"\\text\{[^}]*\}", "", latex)):
                raise ValueError("Prose was placed in a mathematics segment")
            formula = compile_formula(latex, source="vision")
            if formula.status != "renderable":
                raise ValueError("Unrenderable transcribed formula")
            segments.append(TranscriptionSegment(formula=formula))
        else:
            segments.append(TranscriptionSegment(text=text))
    if not segments:
        return None
    return ReadingTranscription(
        segments=segments, source="vision", warnings=raw.get("warnings", [])
    )

def region_images(pdf_path: Path, report: DashboardReport, item: ReadingItem) -> list[str]:
    blocks = {b.id: b for b in report.document.blocks}
    boxes = (
        [(item.page, item.bbox)]
        if item.kind == "algorithm" and item.bbox
        else [(blocks[p.block_id].page, p.bbox or blocks[p.block_id].bbox) for p in item.parts]
    )
    if len(boxes) > 8:
        raise ValueError("Region spans more than eight crops")
    images = []
    with PDF_LOCK, pdfium.PdfDocument(str(pdf_path)) as pdf:
        for number, box in boxes:
            page = pdf[number - 1]
            width, height = page.get_size()
            bitmap = page.render(scale=2.5)
            with bitmap.to_pil() as original:
                with original.crop(
                    (
                        max(0, int(box.x0 * width * 2.5) - 1),
                        max(0, int(box.y0 * height * 2.5) - 1),
                        min(original.width, int(box.x1 * width * 2.5) + 2),
                        min(original.height, int(box.y1 * height * 2.5) + 2),
                    )
                ) as crop:
                    stream = io.BytesIO()
                    crop.convert("RGB").save(stream, format="JPEG", quality=94)
                    images.append(
                        "data:image/jpeg;base64," + base64.b64encode(stream.getvalue()).decode()
                    )
            bitmap.close()
            page.close()
    return images
