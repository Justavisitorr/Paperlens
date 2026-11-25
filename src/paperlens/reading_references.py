"""Repair explicit bibliography labels without changing source blocks or body prose."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import quote

from .models import DashboardReport, ReadingDocument, ReadingItem, ReadingPart

LABEL = r"(?:\d{1,5}|[A-Z][A-Za-z0-9]{0,23}\+?)"
LEADING = re.compile(r"^[ \t]*\[(" + LABEL + r")\][ \t]*")
LINES = re.compile(r"(?m)^[ \t]*\[(" + LABEL + r")\][ \t]*")


def _text_part(text: str, part: ReadingPart) -> str:
    return text.encode("utf-16-le")[part.start * 2 : part.end * 2].decode("utf-16-le")


def _units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def reference_text_key(text: str) -> str:
    """Compare typography without discarding numbers, punctuation or words."""
    text = unicodedata.normalize("NFKD", text).translate(str.maketrans({
        "–": "-", "—": "-", "−": "-", "‐": "-", "‑": "-",
        "‘": "'", "’": "'", "“": '"', "”": '"',
    }))
    return "".join(
        c for c in text if not c.isspace() and not unicodedata.category(c).startswith("M")
    )


def clean_references(items: list[ReadingItem], blocks: dict) -> list[ReadingItem]:
    """Remove empty entries and proven residues of an already corrected reference."""
    corrected = []
    for item in items:
        if item.content is None or not item.parts:
            continue
        bounds: dict[str, ReadingPart] = {}
        for part in item.parts:
            if part.block_id in bounds:
                old = bounds[part.block_id]
                old.start, old.end = min(old.start, part.start), max(old.end, part.end)
            else:
                bounds[part.block_id] = part.model_copy(deep=True)
        source = "".join(
            _text_part(blocks[p.block_id].text, p)
            for p in bounds.values() if p.block_id in blocks
        )
        if reference_text_key(source) == reference_text_key(item.content):
            corrected.append((item, bounds))

    output = []
    for item in items:
        text = item.content
        if text is None:
            if item.transcription:
                text = "".join(
                    s.formula.latex if s.formula else s.text for s in item.transcription.segments
                )
            else:
                text = "".join(
                    p.join + _text_part(blocks[p.block_id].text, p)
                    for p in item.parts if p.block_id in blocks
                )
        body = LEADING.sub("", text)
        if not any(c.isalnum() for c in reference_text_key(body)):
            continue
        if (
            item.id.startswith("source-") and item.content is None
            and not item.transcription and item.parts
        ):
            label = item.reference_number or item.label
            duplicate = any(
                (not label or label == (other.reference_number or other.label))
                and all(
                    p.block_id in bounds
                    and bounds[p.block_id].start <= p.start < p.end <= bounds[p.block_id].end
                    for p in item.parts
                )
                for other, bounds in corrected
            )
            if duplicate:
                continue
        output.append(item)
    return output


def repair_reference_boundaries(
    report: DashboardReport, reading: ReadingDocument | None = None
) -> int:
    """Use line/part-start labels only inside existing bibliography entries.

    Keep the original ID for the first slice; additional IDs derive from that ID
    and exact label/offset, so reloads are idempotent. No network or model work.
    """
    reading = reading if reading is not None else report.reading
    if reading is None:
        return 0
    blocks = {b.id: b for b in report.document.blocks}
    output: list[ReadingItem] = []
    changed = 0
    for item in reading.references:
        source = ""
        spans = []
        for part in item.parts:
            block = blocks.get(part.block_id)
            if not block:
                continue
            source += part.join
            start = len(source)
            text = _text_part(block.text, part)
            source += text
            spans.append((start, len(source), text, part))
        text = item.content if item.content is not None else source
        markers = {m.start(): m for m in LINES.finditer(text)}
        if item.content is None:
            for start, _, piece, _ in spans:
                m = LEADING.match(piece)
                if m:
                    # Match on the complete string to retain absolute offsets.
                    markers[start] = LINES.match(text, start) or re.compile(
                        r"[ \t]*\[(" + LABEL + r")\][ \t]*"
                    ).match(text, start)
        found = sorted((m for m in markers.values() if m), key=lambda m: m.start())
        if not found:
            label = (item.reference_number or item.label).strip("[] ")
            if re.fullmatch(LABEL, label):
                item.reference_number = label
                item.label = f"[{label}]"
            output.append(item)
            continue
        # A label at the beginning supplies a missing label; later labels split a lump.
        segments = []
        if text[: found[0].start()].strip():
            segments.append(
                (0, found[0].start(), (item.reference_number or item.label).strip("[] "))
            )
        for index, marker in enumerate(found):
            end = found[index + 1].start() if index + 1 < len(found) else len(text)
            segments.append((marker.end(), end, marker.group(1)))
        for index, (start, end, label) in enumerate(segments):
            child = item.model_copy(deep=True)
            if index:
                suffix = hashlib.sha256(f"{item.id}:{label}:{start}".encode()).hexdigest()[:12]
                child.id = f"{item.id}-ref-{suffix}"
            child.label = f"[{label}]" if label else item.label
            child.reference_number = label
            if item.content is None:
                child.parts = []
                for low, high, piece, part in spans:
                    a, b = max(start, low), min(end, high)
                    if a < b:
                        copied = part.model_copy(deep=True)
                        copied.start = part.start + _units(piece[: a - low])
                        copied.end = part.start + _units(piece[: b - low])
                        copied.join = part.join if child.parts else ""
                        child.parts.append(copied)
                if child.parts:
                    child.page = blocks[child.parts[0].block_id].page
                    child.source_pages = list(
                        dict.fromkeys(blocks[p.block_id].page for p in child.parts)
                    )
            else:
                # Semantic editions may not have exact source offsets. Preserve the
                # supplied anchors rather than inventing a text-to-PDF mapping.
                child.content = text[start:end]
                child.content_spans = []
            chunk = text[start:end]
            doi = re.search(r"10\.\d{4,9}/[^\s<>]+", chunk, re.I)
            url = re.search(r"https?://[^\s<>]+", chunk, re.I)
            if doi:
                child.href = "https://doi.org/" + quote(
                    doi.group().rstrip(".,;。"), safe="/()[]:;_-"
                )
            elif url:
                child.href = url.group().rstrip(".,;。")
            elif len(segments) > 1:
                # A link attached to a formerly merged item cannot be assigned to
                # every child. Retain it only when the old explicit label agrees.
                old_label = (item.reference_number or item.label).strip("[] ")
                child.href = item.href if label and label == old_label else ""
            output.append(child)
        changed += 1
    reading.references = clean_references(output, blocks)
    changed += len(output) - len(reading.references)
    return changed
