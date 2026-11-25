"""Page images → a typed, continuous reading edition, with source anchors beside it.

The design follows EasyRead 1.3.1 (MIT, Copyright 2026 LiangXiLin): ask the
model for paragraphs/math/tables/references, not a flat Markdown transcription.
Unlike its translation format, this edition keeps the source language, explicitly
separates end matter, and only consumes the assigned pages of each batch.
"""

from __future__ import annotations

import base64
import io
import re
import unicodedata
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal

import pypdfium2 as pdfium
from pydantic import BaseModel, Field, field_validator

from .catalog import CatalogParseError, get_model, load_catalog
from .config import Settings
from .control import PDF_LOCK
from .edition_anchors import content_anchors
from .edition_continuation import (
    join_continuation,
    normalize_reference,
    repair_word_breaks,
    same_page_column_continuation,
    wrap_numeric_powers,
)
from .edition_layout import anchor_row, matching_object
from .edition_pdf_words import restore_pdf_acronyms
from .formulas import strip_math_delimiters
from .models import Box, DashboardReport, ReadingItem, ReadingPart, TableCell, TableGrid
from .reading import overlap, section_level, utf16
from .reading_contacts import native_contact_content
from .reading_references import clean_references, reference_text_key
from .reading_visual_ownership import caption_image_box, claim_visual_sources, pdf_image_regions
from .storage import fingerprint

VERSION = "1.5"
BATCH_PAGES = 2
_INCOMPLETE_WARNING = "Structured edition incomplete on page "
class EditionBlock(BaseModel):
    kind: Literal[
        "paragraph",
        "heading",
        "formula",
        "algorithm",
        "code",
        "figure",
        "table",
        "reference",
        "information",
    ]
    page: int
    box: tuple[float, float, float, float] | None = None
    source_ids: list[str] = Field(default_factory=list)
    content: str = ""
    code_language: str = ""
    label: str = ""
    level: int = 1
    continuation: bool = False
    join: Literal["auto", "space", "none", "dehyphenate"] = "auto"
    reference_number: str = ""
    category: str = ""
    head: list[list[str]] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)

    @field_validator("head", mode="before")
    @classmethod
    def single_header_row(cls, value):
        return [value] if value and isinstance(value[0], str) else value


class EditionBatch(BaseModel):
    items: list[EditionBlock] = Field(min_length=1)


def page_image(pdf_path: Path, number: int, box: Box | None = None) -> str:
    """Reuse one image per page/batch; never hold PDFium objects during network calls."""
    with PDF_LOCK, pdfium.PdfDocument(pdf_path) as doc:
        page = doc[number - 1]
        bitmap = page.render(scale=3 if box else 2)
        try:
            with bitmap.to_pil() as image:
                if box:
                    image = image.crop(
                        (
                            int(box.x0 * image.width),
                            int(box.y0 * image.height),
                            int(box.x1 * image.width),
                            int(box.y1 * image.height),
                        )
                    )
                with image.convert("RGB") as rgb:
                    stream = io.BytesIO()
                    rgb.save(stream, "JPEG", quality=90)
                    return "data:image/jpeg;base64," + base64.b64encode(stream.getvalue()).decode()
        finally:
            bitmap.close()
            page.close()


def vision_ready(settings: Settings) -> bool:
    if settings.paperlens_vision_allow_unverified:
        return True
    try:
        catalog = load_catalog(settings.paperlens_cache_dir, allow_network=False)
        return (
            get_model(catalog, settings.llm_model, base_url=settings.llm_base_url).vision
            == "supported"
        )
    except CatalogParseError:
        return False


def _box(row: EditionBlock) -> Box | None:
    if row.box is None:
        return None
    x0, y0, x1, y1 = row.box
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        raise ValueError("正文区块范围不在原页内")
    return Box(x0=x0, y0=y0, x1=x1, y1=y1)


def _parts(report: DashboardReport, row: EditionBlock) -> list[ReadingPart]:
    """Exact substring offsets when available, geometric source anchors otherwise."""
    by_id = {b.id: b for b in report.document.blocks}
    candidates = [by_id[i] for i in row.source_ids if i in by_id and by_id[i].page == row.page]
    box = _box(row)
    if not candidates and box:
        candidates = [
            b for b in by_id.values() if b.page == row.page and overlap(b.bbox, box) > 0.5
        ]
    if box and row.kind in {"paragraph", "information", "heading", "reference"}:
        content, _ = _literal_characters(row.content)
        # A parser block may mix columns, or a model may omit one source ID.
        # Expand only with literal text evidence inside the returned region;
        # geometry alone is never sufficient to consume another paragraph.
        for block in by_id.values():
            if (
                block in candidates
                or block.page != row.page
                or block.kind not in {"text", "heading", "reference"}
                or overlap(block.bbox, box) <= 0
            ):
                continue
            literal, _ = _literal_characters(block.text)
            longest = SequenceMatcher(None, literal, content, autojunk=False).find_longest_match()
            symbolic = any(
                _literal_characters(token)[0] in content
                for token in re.findall(r"\{[A-Za-z][A-Za-z_0-9]{2,}\}", block.text)
            )
            if longest.size >= 32 or symbolic:
                candidates.append(block)
    parts = []
    for b in candidates:
        start = b.text.find(row.content) if row.content else -1
        left, right = (start, start + len(row.content)) if start >= 0 else (0, len(b.text))
        region = b.bbox
        if box and overlap(b.bbox, box) > 0:
            region = Box(
                x0=max(box.x0, b.bbox.x0),
                y0=max(box.y0, b.bbox.y0),
                x1=min(box.x1, b.bbox.x1),
                y1=min(box.y1, b.bbox.y1),
            )
        parts.append(
            ReadingPart(
                block_id=b.id,
                start=utf16(b.text[:left]),
                end=utf16(b.text[:right]),
                bbox=region,
                join=" " if parts else "",
            )
        )
    return parts


def _table(row: EditionBlock) -> TableGrid | None:
    rows = row.head + row.rows
    if not rows or not any(rows):
        return None
    return TableGrid(
        rows=len(rows),
        cols=max(map(len, rows)),
        cells=[
            TableCell(
                text=text,
                row_start=i,
                row_end=i + 1,
                col_start=j,
                col_end=j + 1,
                column_header=i < len(row.head),
            )
            for i, values in enumerate(rows)
            for j, text in enumerate(values)
        ],
    )


def validate_batch(
    raw: dict, pages: list[int], *, report: DashboardReport | None = None
) -> EditionBatch:
    batch = EditionBatch.model_validate(raw)
    present = {row.page for row in batch.items}
    if present != set(pages):
        raise ValueError("正文重排漏页或包含了只作上下文的页面")
    formulas: set[str] = set()
    originals = report.reading.items + report.reading.references if report and report.reading else []
    for index, row in enumerate(batch.items):
        try:
            _box(row)
        except ValueError:
            if report is None:
                raise
            # The model's auxiliary geometry may be inverted across columns.
            # Recover only a uniquely grounded same-page region, using the same
            # source matcher as apply_edition; never clamp or reorder coordinates.
            row = anchor_row(row, report, originals)
            _box(row)
            batch.items[index] = row
        if row.kind == "formula":
            row.content = strip_math_delimiters(row.content)
        if row.kind == "formula" and row.label:
            key = re.sub(r"\s", "", row.label)
            if key in formulas:
                raise ValueError("同批正文重复输出了带编号的公式")
            formulas.add(key)
    return batch


def _part_text(part: ReadingPart, blocks: dict) -> str:
    return (
        blocks[part.block_id]
        .text.encode("utf-16-le")[part.start * 2 : part.end * 2]
        .decode("utf-16-le")
    )


# TeX commands that only style or lay out their argument.
_TEX_LAYOUT = {
    "text", "textrm", "textit", "textbf", "mathrm", "mathbf", "mathit", "mathsf", "mathtt",
    "mathbb", "mathcal", "mathfrak", "boldsymbol", "bm", "operatorname", "displaystyle",
    "frac", "dfrac", "tfrac", "left", "right", "big", "Big", "bigl", "bigr", "Bigl", "Bigr",
    "hat", "tilde", "bar", "vec", "quad", "qquad", "limits", "dots", "ldots", "cdots", "cdot",
}
# TeX commands whose native PDF layer carries the Unicode glyph.
_TEX_SYMBOLS = {
    "rightarrow": "→", "to": "→", "leftarrow": "←", "Rightarrow": "⇒", "mapsto": "↦",
    "ge": "≥", "geq": "≥", "le": "≤", "leq": "≤", "neq": "≠", "ne": "≠",
    "approx": "≈", "sim": "∼", "propto": "∝", "equiv": "≡", "pm": "±", "mp": "∓",
    "times": "×", "div": "÷", "in": "∈", "notin": "∉", "subset": "⊂", "subseteq": "⊆",
    "cup": "∪", "cap": "∩", "forall": "∀", "exists": "∃", "infty": "∞",
    "sqrt": "√", "sum": "∑", "prod": "∏", "int": "∫", "partial": "∂", "nabla": "∇",
    "oplus": "⊕", "otimes": "⊗", "odot": "⊙", "circ": "∘", "ell": "ℓ",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "varepsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ",
    "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ",
    "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ",
    "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ",
    "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Phi": "Φ",
    "Psi": "Ψ", "Omega": "Ω",
}


def _literal_characters(text: str) -> tuple[str, list[int]]:
    """Compare text through spacing/inline-math changes, retaining source offsets."""
    # Formatting commands are not source words. Keep the original offsets so
    # an inline formula never changes the evidence's UTF-16 coordinate system.
    ignored: set[int] = set()
    replacements: dict[int, str] = {}
    # A braced TeX superscript is layout, including inline footnote markers.
    # The exponent's characters still have to match the native source exactly.
    ignored.update(match.start() for match in re.finditer(r"\^(?=\{)", text))
    # TeX also permits a single unbraced superscript token, e.g. $^7$.
    # Only recognize this inside math delimiters; a native caret remains evidence.
    for expression in re.finditer(r"(?<!\\)\$(?!\$)([^$\n]+)(?<!\\)\$", text):
        ignored.update(expression.start(1) + match.start()
                       for match in re.finditer(r"\^(?=[A-Za-z0-9])", expression[1]))
    for match in re.finditer(r"\\([A-Za-z]+)", text):
        command = match[1]
        if command in _TEX_LAYOUT:
            ignored.update(range(match.start(), match.end()))
        elif command in _TEX_SYMBOLS:
            replacements[match.start()] = _TEX_SYMBOLS[command]
            ignored.update(range(match.start() + 1, match.end()))
    # Compound words and PDF line-end word breaks vary between the native
    # layer and the image transcription; numeric and single-variable minus
    # signs remain significant. Text layers also space a dash between words
    # ("fair - just" for "fair – just") and split small caps ("C heck - L ist").
    for match in re.finditer(r"[A-Za-z]{2,} ?([-‐‑–—])(?=\s*[A-Za-z{]|\s*$)", text):
        ignored.add(match.start(1))
    characters, positions = [], []
    for index, character in enumerate(text):
        if index in ignored:
            continue
        for normalized in unicodedata.normalize(
            "NFKC", replacements.get(index, character)
        ).casefold():
            # PDF text layers space decimals as "0 . 6"; the edition writes 0.6.
            decimal = character in ".," and bool(
                re.search(r"\d *$", text[max(0, index - 2) : index])
                and re.match(r" *\d", text[index + 1 : index + 3])
            )
            if (
                normalized.isalnum()
                or normalized == "-"
                or decimal
                or (normalized != "$" and unicodedata.category(normalized).startswith("S"))
                or normalized == "/"
            ):
                characters.append(normalized)
                positions.append(index)
    return "".join(characters), positions


def _fragment_ranges(
    source: str, content: str, raw: str, positions: list[int]
) -> list[tuple[int, int, int, int]]:
    """Map independently anchored text without requiring PDF block order.

    Only exact runs consume source characters. Long passages can therefore
    replace their correct prefix/suffix even when a native block mixes two
    columns; an omitted word, sign or number is still retained.
    """
    if source in content:
        start = content.index(source)
        return [(0, len(source), start, start + len(source))]
    ranges = []
    for match in SequenceMatcher(None, source, content, autojunk=False).get_matching_blocks():
        if not match.size:
            continue
        start, end = match.a, match.a + match.size
        # A shared letter inside a different word is not an excerpt. In
        # particular, do not consume a neighboring symbol token's initial to
        # finish a PDF's split word "collec-" / "tion".
        while start < end and positions[start] and raw[positions[start] - 1].isalnum():
            start += 1
        while (
            start < end
            and positions[end - 1] + 1 < len(raw)
            and raw[positions[end - 1] + 1].isalnum()
        ):
            end -= 1
        if start == end:
            continue
        left, right = positions[start], positions[end - 1] + 1
        whole_words = (left == 0 or not raw[left - 1].isalnum()) and (
            right == len(raw) or not raw[right].isalnum()
        )
        symbolic = all(unicodedata.category(char).startswith("S") for char in source[start:end])
        if end - start >= 12 or (end - start >= 4 and whole_words) or symbolic:
            output_start = match.b + start - match.a
            ranges.append((start, end, output_start, output_start + end - start))
    return ranges


def _has_source_content(part: ReadingPart, blocks: dict) -> bool:
    source = blocks[part.block_id].text
    left = len(source.encode("utf-16-le")[: part.start * 2].decode("utf-16-le"))
    right = len(source.encode("utf-16-le")[: part.end * 2].decode("utf-16-le"))
    # Inspect the original context so an omitted decimal point remains meaningful.
    return any(left <= position < right for position in _literal_characters(source)[1])


def _covered_parts(
    item: ReadingItem, blocks: dict, owned_parts: list[ReadingPart] | None = None
) -> list[ReadingPart]:
    """Source IDs/boxes are anchors, not proof that an entire paragraph was returned.

    Only a literal passage (allowing spacing and inline math markup) consumes
    prose. An original typed object owns its exact regions independently of the
    caption/LaTeX/table text; a broad text-block ID provides no such ownership.
    Negative signs, decimals and other mathematical symbols remain significant.
    """
    if item.kind in {"figure", "table", "formula", "algorithm"}:
        if owned_parts is not None:
            return owned_parts
        kinds = {
            "figure": {"picture", "caption"},
            "table": {"table"},
            "formula": {"formula"},
            "algorithm": {"algorithm"},
        }
        typed = [p for p in item.parts if blocks[p.block_id].kind in kinds[item.kind]]
        if typed:
            for index, part in enumerate(typed):
                if blocks[part.block_id].kind == "caption" and part.start and item.label:
                    prefix = blocks[part.block_id].text.encode("utf-16-le")[: part.start * 2].decode("utf-16-le")
                    if _literal_characters(prefix)[0] == _literal_characters(item.label)[0]:
                        typed[index] = part.model_copy(update={"start": 0})
            return typed
    content, _ = _literal_characters(item.content or "")
    if not content:
        return []
    pieces = []
    position = 0
    for part in item.parts:
        source = _part_text(part, blocks)
        literal, positions = _literal_characters(source)
        pieces.append((position, position + len(literal), part, source, positions))
        position += len(literal)
    literal = "".join(_literal_characters(piece[3])[0] for piece in pieces)
    if not literal:
        return []
    if literal in content:
        ranges = [(0, len(literal))]
    elif content in literal:
        start = literal.index(content)
        ranges = [(start, start + len(content))]
    else:
        ranges = []
    if not ranges:
        # PDF glyph fragments can be emitted at the end of the line or in a
        # neighboring block. Accept a permutation only with overlapping source
        # geometry, long exact context, and precisely the same characters.
        matcher = SequenceMatcher(None, literal, content, autojunk=False)
        matches = matcher.get_matching_blocks()
        matched = sum(match.size for match in matches)
        deleted, inserted = set(), set()
        for operation, start, end, _, _ in matcher.get_opcodes():
            if operation in {"delete", "replace"}:
                deleted.update(
                    i for i, (lo, hi, *_) in enumerate(pieces) if lo < end and start < hi
                )
            if operation in {"insert", "replace"}:
                inserted.update(i for i, (lo, hi, *_) in enumerate(pieces) if lo <= start < hi)
        # Actual displacement must cross source-block boundaries. A normal
        # word change such as form/from is never a glyph-order repair.
        disordered = bool(deleted and inserted and deleted.isdisjoint(inserted)) and any(
            pieces[a][2].bbox
            and pieces[z][2].bbox
            and overlap(pieces[a][2].bbox, pieces[z][2].bbox) > 0
            for a in deleted
            for z in inserted
        )
        if (
            disordered
            and Counter(literal) == Counter(content)
            and matched >= len(literal) * 0.9
            and len(literal) - matched <= 16
        ):
            ranges = [(0, len(literal))]
        else:
            available = content
            pending = []
            # Reserve complete source excerpts before matching mixed blocks;
            # a shared word in an omitted passage must not steal the suffix of
            # another, fully returned source block.
            for piece in pieces:
                lo, _, _, source, _ = piece
                source_literal, _ = _literal_characters(source)
                if source_literal and source_literal in available:
                    output_start = available.index(source_literal)
                    output_end = output_start + len(source_literal)
                    ranges.append((lo, lo + len(source_literal)))
                    available = (
                        available[:output_start]
                        + "\0" * len(source_literal)
                        + available[output_end:]
                    )
                else:
                    pending.append(piece)
            for lo, _, _, source, positions in pending:
                matches = _fragment_ranges(
                    _literal_characters(source)[0], available, source, positions
                )
                for start, end, output_start, output_end in matches:
                    ranges.append((lo + start, lo + end))
                    # One returned occurrence cannot consume two distinct
                    # native occurrences, including repeated short fragments.
                    available = (
                        available[:output_start]
                        + "\0" * (output_end - output_start)
                        + available[output_end:]
                    )
    covered = []
    for lo, hi, part, source, positions in pieces:
        prefix_start = part.start
        if item.kind == "reference" and item.reference_number and part.start:
            prefix = (
                blocks[part.block_id].text.encode("utf-16-le")[: part.start * 2].decode("utf-16-le")
            )
            if re.fullmatch(rf"\s*\[?{re.escape(item.reference_number)}\]?[.\s]*", prefix):
                prefix_start = 0
        for start, end in ranges:
            a, z = max(lo, start), min(hi, end)
            if a >= z:
                continue
            if a == lo and z == hi:
                covered.append(part.model_copy(update={"start": prefix_start}))
                continue
            left, right = positions[a - lo], positions[z - lo - 1] + 1
            if (
                right < len(source)
                and source[right] in ".!?,;:"
                and right not in positions
                and (item.content or "").rstrip().endswith(source[right])
            ):
                right += 1
            covered.append(
                part.model_copy(
                    update={
                        "start": prefix_start if left == 0 else part.start + utf16(source[:left]),
                        "end": part.start + utf16(source[:right]),
                    }
                )
            )
    return covered


def _str_index(text: str, offset: int) -> int:
    return len(text.encode("utf-16-le")[: offset * 2].decode("utf-16-le"))


def _claim_formula_residue(
    content: str, anchors: list[ReadingPart], parts: list[ReadingPart], blocks: dict
) -> list[ReadingPart]:
    """Let returned TeX own the glyph soup the PDF layer extracted for it.

    A native "α = 0 . 6" or "W Q i ∈ R d model × d k" is rarely a literal
    match for $\\alpha = 0.6$ / $W_i^Q \\in \\mathbb{R}^{...}$ (script order,
    spacing, styling commands). A short gap that touches literally covered
    prose and whose characters all come from the row's own inline formulas
    is that rendering, not an omission. Each formula character is spent once,
    so the formulas cannot excuse a dropped sentence.
    """
    pool = Counter()
    for span in re.finditer(r"(?<!\\)\$(?!\$)([^$\n]+)(?<!\\)\$|\\\((.+?)\\\)", content):
        pool.update(_literal_characters(span[1] or span[2])[0])
    if not pool:
        return parts
    claimed = list(parts)
    for anchor in anchors:
        text = blocks[anchor.block_id].text
        intervals = sorted(
            (p.start, p.end)
            for p in parts
            if p.block_id == anchor.block_id and p.start < anchor.end and anchor.start < p.end
        )
        if not intervals:
            continue
        gaps, cursor = [], anchor.start
        for start, end in intervals:
            if start > cursor:
                gaps.append((cursor, start))
            cursor = max(cursor, end)
        if cursor < anchor.end:
            gaps.append((cursor, anchor.end))
        literal, positions = _literal_characters(text)
        for start, end in gaps:
            left, right = _str_index(text, start), _str_index(text, end)
            residue = "".join(c for c, i in zip(literal, positions, strict=True) if left <= i < right)
            need = Counter(residue)
            if not residue or len(residue) > 24 or need - pool:
                continue
            pool -= need
            claimed.append(anchor.model_copy(update={"start": start, "end": end}))
    if len(claimed) == len(parts):
        return parts
    rank: dict[str, int] = {}
    for index, anchor in enumerate(anchors):
        rank.setdefault(anchor.block_id, index)
    claimed.sort(key=lambda p: (rank.get(p.block_id, len(anchors)), p.block_id, p.start))
    merged: list[ReadingPart] = []
    for part in claimed:
        last = merged[-1] if merged else None
        if last and last.block_id == part.block_id and part.start <= last.end:
            merged[-1] = last.model_copy(update={"end": max(last.end, part.end)})
        else:
            merged.append(part)
    return merged


def _split_formula_parts(
    content: str, parts: list[ReadingPart], blocks: dict
) -> list[ReadingPart]:
    """Docling may cut one display equation into stacked formula blocks.

    Attention p.5 arrives as "MultiHead( Q,K,V ) = … where head = …" plus a
    sub-script row "i i i i )"; the edition anchors one of them. A touching
    formula block whose glyphs the returned TeX contains is the same equation
    and must not be printed a second time from the native layer.
    """
    pool = Counter(_literal_characters(content)[0])
    owned = [blocks[p.block_id] for p in parts]
    ids = {b.id for b in owned}
    claimed = []
    for block in blocks.values():
        if block.kind != "formula" or block.id in ids or block.page != owned[0].page:
            continue
        touching = any(
            min(block.bbox.x1, o.bbox.x1) > max(block.bbox.x0, o.bbox.x0)
            and max(block.bbox.y0, o.bbox.y0) - min(block.bbox.y1, o.bbox.y1) <= 0.012
            for o in owned
        )
        need = Counter(_literal_characters(block.text)[0])
        total = sum(need.values())
        if touching and total and sum((need & pool).values()) >= 0.8 * total:
            claimed.append(ReadingPart(block_id=block.id, start=0, end=utf16(block.text), bbox=block.bbox))
    return claimed


def _record_coverage(
    item: ReadingItem, covered: dict, blocks: dict, owned_parts: list[ReadingPart] | None = None
) -> None:
    anchors = item.parts
    parts = _covered_parts(item, blocks, owned_parts)
    if item.kind == "reference" and item.content:
        source = "".join(
            p.join + blocks[p.block_id].text.encode("utf-16-le")[p.start * 2:p.end * 2].decode("utf-16-le")
            for p in anchors
        )
        if reference_text_key(source) == reference_text_key(item.content):
            # A normalized dash or accent is already present in this same entry.
            # It must not reappear as a separate bibliography fallback.
            parts = [p.model_copy(update={"start": min(
                [p.start] + [q.start for q in parts
                             if q.block_id == p.block_id and q.start <= p.start < q.end]
            )}) for p in anchors]
    if item.kind not in {"figure", "table", "formula", "algorithm"}:
        parts = _claim_formula_residue(item.content or "", anchors, parts, blocks)
    elif item.kind == "formula" and parts:
        parts = parts + _split_formula_parts(item.content or "", parts, blocks)
    # Future retries must not mistake broad model anchors for ownership of
    # the omitted part of a source block, including mixed object/prose blocks.
    item.parts = parts
    for part in parts:
        covered[part.block_id].append((part.start, part.end))


def _remaining_parts(parts: list[ReadingPart], covered: dict, blocks: dict) -> list[ReadingPart]:
    """Subtract exact covered intervals; leave untouched-page parts unchanged."""
    remaining = []
    for part in parts:
        intervals = covered.get(part.block_id, [])
        if not intervals:
            remaining.append(part)
            continue
        ranges = [(part.start, part.end)]
        for start, end in intervals:
            ranges = [
                (left, right)
                for lo, hi in ranges
                for left, right in ((lo, min(hi, start)), (max(lo, end), hi))
                if left < right
            ]
        for left, right in ranges:
            fragment = part.model_copy(update={"start": left, "end": right})
            # A dropped period/newline is not a missing paragraph. Preserve all
            # substantive characters, including non-BMP source offsets.
            if _has_source_content(fragment, blocks):
                remaining.append(fragment)
    return remaining


def _retain_uncovered(
    original: ReadingItem, covered: dict, pages: set[int], blocks: dict
) -> ReadingItem | None:
    remaining = _remaining_parts(original.parts, covered, blocks)
    if remaining == original.parts and (remaining or original.page not in pages):
        return original
    if not remaining:
        return None
    source_pages = sorted({blocks[p.block_id].page for p in remaining})
    return original.model_copy(
        update={
            "id": "source-" + fingerprint([p.model_dump() for p in remaining])[:16],
            "parts": remaining,
            "page": source_pages[0],
            "bbox": remaining[0].bbox or blocks[remaining[0].block_id].bbox,
            "content": None,
            "transcription": None,
            "content_spans": [],
            "source_pages": source_pages,
            "result_ids": [],
            "visual_ids": [],
        }
    )


def _merge_retained(items: list[ReadingItem], retained: list[ReadingItem], blocks: dict) -> None:
    """Place source fallbacks beside their neighbors without reordering model rows."""
    for old in retained:
        key = min(
            ((blocks[p.block_id].order, p.start) for p in old.parts), default=(float("inf"), 0)
        )
        position = next(
            (
                index
                for index, item in enumerate(items)
                if item.page == old.page
                and item.parts
                and min((blocks[p.block_id].order, p.start) for p in item.parts) > key
            ),
            len(items),
        )
        items.insert(position, old)
    items.sort(key=lambda item: item.page)


def _ensure_unique_reading_ids(reading) -> None:
    """Distinguish repeated content by source location without dropping occurrences."""
    groups = [reading.items, reading.references, *reading.information_items.values()]
    seen_objects: set[int] = set()
    for group in groups:
        for index, item in enumerate(group):
            # A shared object cannot carry two occurrence IDs; retain both values.
            if id(item) in seen_objects:
                group[index] = item.model_copy(deep=True)
            else:
                seen_objects.add(id(item))
    items = [item for group in groups for item in group]
    last = {item.id: index for index, item in enumerate(items)}
    occupied = set(last)
    for index, item in enumerate(items):
        old_id = item.id
        if last[old_id] == index:
            continue
        source = {
            "page": item.page,
            "bbox": item.bbox.model_dump(mode="json") if item.bbox else None,
            "parts": [
                {"block_id": part.block_id, "start": part.start, "end": part.end}
                for part in item.parts
            ],
        }
        base = f"{old_id}@{fingerprint(source)[:12]}"
        candidate, suffix = base, 2
        while candidate in occupied:
            candidate = f"{base}-{suffix}"
            suffix += 1
        occupied.add(candidate)
        item.id = candidate


def apply_edition(
    report: DashboardReport, batches: list[EditionBatch], pdf_path: Path | None = None
) -> None:
    """Replace covered source passages; retain and flag omissions even on returned pages."""
    reading = report.reading
    if not reading or not batches:
        return
    pages = {r.page for batch in batches for r in batch.items}
    image_regions = pdf_image_regions(pdf_path, pages) if pdf_path and any(
        row.kind in {"figure", "table"} for batch in batches for row in batch.items
    ) else {}
    original_items = reading.items
    originals = original_items + reading.references
    by_block = {b.id: b for b in report.document.blocks}
    information: dict[str, list[ReadingItem]] = defaultdict(list)
    items, refs = [], []
    covered: dict[str, list[tuple[int, int]]] = defaultdict(list)
    seen_formulas: set[tuple[int, str]] = set()
    for batch in batches:
        for row in batch.items:
            if row.kind == "heading" and re.fullmatch(
                r"\s*(?:参考文献|references|bibliography)\s*[:：]?\s*", row.content, re.I
            ):
                # The reader owns the single, dedicated bibliography heading.
                continue
            row = anchor_row(row, report, originals)
            if row.kind == "information" and row.category == "authors":
                row = row.model_copy(update={"content": native_contact_content(
                    row.content,
                    [by_block[bid].text for bid in dict.fromkeys(row.source_ids)
                     if bid in by_block and by_block[bid].page == row.page],
                )})
            box = _box(row)
            parts = _parts(report, row)
            if not row.content.strip() and row.kind not in {"figure", "table"}:
                # Unreadable rows consume nothing. Retention below handles each
                # source fragment once, even when boxes overlap readable rows.
                continue
            ident = (
                "edition-"
                + fingerprint(
                    {"kind": row.kind, "page": row.page, "content": row.content, "label": row.label}
                    | ({"head": row.head, "rows": row.rows} if row.kind == "table" else {})
                )[:16]
            )
            item = ReadingItem(
                id=ident,
                kind="paragraph" if row.kind == "information" else row.kind,
                page=row.page,
                source_pages=[row.page],
                bbox=box,
                parts=parts,
                content=row.content.strip("\n") if row.kind == "code" else row.content.strip(),
                code_language=row.code_language if row.kind == "code" else "",
                label=row.label,
                level=max(1, min(3, row.level)),
                continuation=row.continuation,
                reference_number=row.reference_number.strip("[] "),
                table=_table(row) if row.kind == "table" else None,
            )
            if item.kind == "formula" and item.label:
                key = (item.page, item.label)
                if key in seen_formulas:
                    continue
                seen_formulas.add(key)
            if row.kind == "information":
                _record_coverage(item, covered, by_block)
                category = (
                    row.category
                    if row.category
                    in {"publication", "citation", "authors", "declarations", "footnotes", "titles"}
                    else "footnotes"
                )
                information[category].append(item)
                continue
            targets = refs if item.kind == "reference" else items
            if item.reference_number:
                item.label = f"[{item.reference_number}]"
                item.content = normalize_reference(item.content or "", item.reference_number)
            if item.kind == "heading":
                item.label = item.content or item.label
                item.level = section_level(item.label, item.level)
            if item.kind in {"paragraph", "reference", "heading", "code"}:
                _record_coverage(item, covered, by_block)
            # A paragraph can cross both columns and pages without losing page anchors.
            preceding_items = list(reversed(targets))
            # A floating figure at the top of a page does not break a sentence
            # continuing from the previous page. A heading or equation does.
            prev = next((i for i in preceding_items if i.kind not in {"figure", "table"}), None)
            same_block_continuation = bool(
                prev
                and item.kind == prev.kind == "paragraph"
                and prev.content
                and re.search(r"[,，]\s*$", prev.content)
                and {p.block_id for p in prev.parts} & {p.block_id for p in item.parts}
            )
            cross_column_continuation = bool(
                prev and targets[-1] is prev and same_page_column_continuation(prev, item, by_block)
            )
            if (
                (item.continuation or same_block_continuation or cross_column_continuation)
                and prev
                and item.kind in {"paragraph", "reference"}
            ):
                if (
                    prev.kind == item.kind
                    and item.page - prev.source_pages[-1] in {0, 1}
                    and (
                        not item.reference_number or item.reference_number == prev.reference_number
                    )
                    and prev.content is not None
                ):
                    prev.content = join_continuation(prev, item, by_block, join=row.join)
                    prev.parts.extend(item.parts)
                    prev.source_pages = sorted(set(prev.source_pages + item.source_pages))
                    continue
            if item.kind in {"figure", "table", "algorithm", "formula"}:
                owned_parts = None
                numbered_object = matching_object(row, originals)
                existing = [
                    old
                    for old in originals
                    if old.page == item.page
                    and old.kind == item.kind
                    and old.bbox
                    and box
                    and overlap(old.bbox, box) > 0.5
                ]
                if numbered_object or existing:
                    old = numbered_object or max(existing, key=lambda i: overlap(i.bbox, box))
                    owned_parts = old.parts
                    item.asset = old.asset
                    if numbered_object or old.asset:
                        # PDF image geometry is more precise than model-estimated
                        # rectangles, which can include the following heading.
                        item.bbox = old.bbox
                        item.parts = old.parts.copy()
                    if item.table is None and old.parts:
                        item.table = by_block[old.parts[0].block_id].table
                captions = {part.block_id for part in item.parts
                            if by_block[part.block_id].kind == "caption"}
                if item.kind == "figure" and len(captions) == 1:
                    caption = by_block[next(iter(captions))]
                    native, _ = _literal_characters(caption.text)
                    returned, _ = _literal_characters(item.label + " " + (item.content or ""))
                    if native and native in returned:
                        expanded = caption_image_box(item, caption, by_block, image_regions)
                        tables = [old for old in originals if old.kind == "table"
                                  and old.page == item.page and old.asset and old.bbox]
                        source_table = None
                        if expanded is None:
                            # Vector figures may be native tables rather than PDF
                            # raster images. Reuse only one precisely anchored,
                            # uniquely adjacent source crop; never invent bounds.
                            native_boxes = {item.page: [old.bbox for old in tables]}
                            exact = caption_image_box(item, caption, by_block, native_boxes)
                            matching = [old for old in tables if old.bbox == exact]
                            source_table = matching[0] if len(matching) == 1 else None
                        if source_table:
                            caption_parts = _covered_parts(item, by_block)
                            item.parts = caption_parts + source_table.parts.copy()
                            owned_parts = item.parts.copy()
                            item.bbox = box = source_table.bbox
                            item.asset = source_table.asset
                            item.table = source_table.table or next((
                                by_block[part.block_id].table for part in source_table.parts
                                if by_block[part.block_id].table
                            ), None)
                        elif expanded is not None and expanded != item.bbox:
                            item.bbox = box = expanded
                            item.asset = ""  # The old crop may show only the caption.
                if pdf_path and box and not item.asset:
                    item.asset = page_image(pdf_path, item.page, box)
                _record_coverage(item, covered, by_block, owned_parts)
            targets.append(item)
    # A rendered object also presents native objects and image text inside its
    # actual crop. Keep their evidence attached to that object rather than
    # rendering the same pixels or OCR a second time as a source fallback.
    claim_visual_sources(items, originals, by_block, covered, image_regions)
    # Preserve original result/evidence attachments exactly once, including page fallbacks.
    retained = [
        fallback
        for old in original_items
        if (fallback := _retain_uncovered(old, covered, pages, by_block)) is not None
    ]
    _merge_retained(items, retained, by_block)
    for old in original_items:
        if old.page not in pages or not (old.visual_ids or old.result_ids):
            continue
        candidates = [i for i in items if i.page == old.page]
        matching = [
            i
            for i in candidates
            if i.kind == old.kind and i.bbox and old.bbox and overlap(old.bbox, i.bbox) > 0.3
        ]
        target = next(iter(matching or candidates), None)
        if target:
            target.result_ids = list(dict.fromkeys(target.result_ids + old.result_ids))
            target.visual_ids = list(dict.fromkeys(target.visual_ids + old.visual_ids))
    retained_refs = [
        fallback
        for old in reading.references
        if (fallback := _retain_uncovered(old, covered, pages, by_block)) is not None
    ]
    valid_reference_ids = {item.id for item in clean_references(refs + retained_refs, by_block)}
    retained_refs = [item for item in retained_refs if item.id in valid_reference_ids]
    _merge_retained(refs, retained_refs, by_block)
    incomplete = {
        by_block[p.block_id].page
        for item in retained + retained_refs
        for p in item.parts
        if by_block[p.block_id].page in pages
    }
    for category, parts in reading.information_parts.items():
        remaining = _remaining_parts(parts, covered, by_block)
        incomplete.update(
            by_block[p.block_id].page for p in remaining if by_block[p.block_id].page in pages
        )
        reading.information_parts[category] = remaining
        reading.information[category] = list(dict.fromkeys(p.block_id for p in remaining))
    reading.items, reading.references = items, clean_references(refs, by_block)
    for category, previous in reading.information_items.items():
        for old in previous:
            fallback = _retain_uncovered(old, covered, pages, by_block)
            if fallback is not None:
                information[category].append(fallback)
                incomplete.update(
                    by_block[p.block_id].page
                    for p in fallback.parts
                    if by_block[p.block_id].page in pages
                )
    reading.information_items = dict(information)
    reading.structured_pages = sorted((pages | set(reading.structured_pages)) - incomplete)
    # Retried pages replace their own warning; unrelated warnings/pages survive.
    reading.warnings = [
        warning
        for warning in reading.warnings
        if warning not in {_INCOMPLETE_WARNING + str(page) for page in pages}
    ] + [_INCOMPLETE_WARNING + str(page) for page in sorted(incomplete)]
    for item in items + refs:
        if item.content is not None:
            source_parts = item.parts
            if item.kind == "reference" and item.reference_number:
                old = next(
                    (
                        i
                        for i in originals
                        if i.kind == "reference" and i.label.strip("[] ") == item.reference_number
                    ),
                    None,
                )
                if old:
                    source_parts = old.parts
            native = "".join(
                p.join
                + by_block[p.block_id]
                .text.encode("utf-16-le")[p.start * 2 : p.end * 2]
                .decode("utf-16-le")
                for p in source_parts
            )
            if item.kind != "code":
                item.content = repair_word_breaks(item.content, native)
            if item.kind == "paragraph":
                item.content = wrap_numeric_powers(item.content)
            item.content_spans = content_anchors(item, by_block)
    if pdf_path and restore_pdf_acronyms(
        [i for i in items if i.kind != "code"], pdf_path, by_block
    ):
        for item in items:
            if item.content is not None:
                item.content_spans = content_anchors(item, by_block)
    reading.version = "3"
    _ensure_unique_reading_ids(reading)
