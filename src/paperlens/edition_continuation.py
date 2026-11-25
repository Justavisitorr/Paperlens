"""Join reading continuations while keeping source-supported word boundaries."""

from __future__ import annotations

import re
from typing import Literal

from .models import Block, ReadingItem

Join = Literal["auto", "space", "none", "dehyphenate"]
_HYPHENS = "-‐‑"
_WORD = re.compile(r"[A-Za-z0-9]+(?:[" + _HYPHENS + r"][A-Za-z0-9]+)*")
_PROTECTED = re.compile(r"\$\$[\s\S]*?\$\$|\$[^$\n]+\$|https?://[^\s<>]+|\b10\.\d{4,9}/[^\s<>]+")


def repair_word_breaks(content: str, source_text: str) -> str:
    """Repair explicit line breaks, without trusting a missing source hyphen.

    Extractors may flatten both line breaks and legitimate compound hyphens.
    A whole source token therefore cannot disprove a model's internal hyphen.
    Missing hyphens are restored only for exact uppercase source abbreviations.
    """
    forms: dict[str, dict[str, str]] = {}
    for match in _WORD.finditer(source_text):
        word = match[0]
        key = word.translate(str.maketrans("", "", _HYPHENS)).casefold()
        forms.setdefault(key, {})[word.casefold()] = word

    def repair(match: re.Match) -> str:
        word = match[0]
        plain = word.translate(str.maketrans("", "", _HYPHENS))
        variants = forms.get(plain.casefold(), {})
        if len(variants) != 1 or not re.search(r"[A-Za-z]", word):
            return word
        source_word = next(iter(variants.values()))
        if word != plain:
            return word
        if re.fullmatch(r"[A-Z0-9]+(?:[" + _HYPHENS + r"][A-Z0-9]+)+", source_word):
            if re.fullmatch(r"[A-Z0-9]+", word):
                return source_word
        return word

    def join_line(match: re.Match) -> str:
        plain = match[1] + match[2]
        variants = forms.get(plain.casefold(), {})
        if len(variants) == 1 and plain.casefold() in variants and re.search(r"[A-Za-z]", plain):
            return plain
        return match[0]

    def repair_segment(text: str) -> str:
        text = re.sub(
            r"([A-Za-z0-9]+)[" + _HYPHENS + r"][ \t]*\r?\n[ \t]*([A-Za-z0-9]+)",
            join_line,
            text,
        )
        return _WORD.sub(repair, text)

    pieces, start = [], 0
    for protected in _PROTECTED.finditer(content):
        pieces.extend((repair_segment(content[start : protected.start()]), protected[0]))
        start = protected.end()
    pieces.append(repair_segment(content[start:]))
    return "".join(pieces)


def normalize_reference(text: str, number: str) -> str:
    """Remove only this reference's redundant leading marker, never inline cites."""
    if not number:
        return text
    return re.sub(r"^\s*\[" + re.escape(number) + r"\]\s*", "", text, count=1)


def same_page_column_continuation(
    previous: ReadingItem, current: ReadingItem, blocks: dict[str, Block]
) -> bool:
    """Recognize a comma-ended source paragraph crossing adjacent page columns."""
    if previous.kind != "paragraph" or current.kind != "paragraph":
        return False
    if not re.search(r"[,，]\s*$", previous.content or ""):
        return False
    if not previous.parts or not current.parts:
        return False
    left = blocks.get(previous.parts[-1].block_id)
    right = blocks.get(current.parts[0].block_id)
    if left is None or right is None or left.page != right.page:
        return False
    if not re.search(r"[,，]\s*$", left.text):
        return False
    if not (
        left.order < right.order
        and left.bbox.x1 < right.bbox.x0
        and (left.bbox.x0 + left.bbox.x1) / 2 < 0.5
        and (right.bbox.x0 + right.bbox.x1) / 2 > 0.5
        and right.bbox.y0 <= left.bbox.y1
    ):
        return False
    return not any(
        block.page == left.page
        and left.order < block.order < right.order
        and block.kind in {"heading", "formula", "algorithm"}
        for block in blocks.values()
    )


def repair_pdf_acronyms(content: str, pdf_text: str) -> str:
    """Recover a short compound acronym from PDFium's special hyphen character.

    Require an intact, unambiguous spelling in the same source region as well.
    PDFium's range extraction exposes U+FFFE; bounded extraction exposes U+0002
    for the same glyph. Neither character implies a hyphen in ordinary prose.
    """
    token = r"(?<![A-Za-z0-9])([A-Z0-9]{1,4})[-‐‑]([A-Z0-9]{1,4})(?![A-Za-z0-9])"
    forms: dict[str, set[str]] = {}
    for match in re.finditer(token, pdf_text):
        if re.search(r"[A-Z]", match[0]):
            forms.setdefault(match[1] + match[2], set()).add(match[0])
    repairs = {}
    damaged = r"(?<![A-Za-z0-9])([A-Z0-9]{1,4})[\ufffe\x02]([A-Z0-9]{1,4})(?![A-Za-z0-9])"
    for match in re.finditer(damaged, pdf_text):
        plain = match[1] + match[2]
        candidates = forms.get(plain, set())
        if len(candidates) != 1 or re.search(
            r"(?<![A-Za-z0-9])" + re.escape(plain) + r"(?![A-Za-z0-9])", pdf_text
        ):
            continue
        repairs[plain] = next(iter(candidates))
    if not repairs:
        return content

    def repair(segment: str) -> str:
        return _WORD.sub(lambda match: repairs.get(match[0], match[0]), segment)

    pieces, start = [], 0
    for protected in _PROTECTED.finditer(content):
        pieces.extend((repair(content[start : protected.start()]), protected[0]))
        start = protected.end()
    return "".join(pieces) + repair(content[start:])


def wrap_numeric_powers(text: str) -> str:
    """Typeset an unambiguous bare numeric TeX power without changing its value."""
    pieces, start = [], 0

    def wrap(segment: str) -> str:
        return re.sub(r"(?<![\w$])(\d+(?:\.\d+)?\^\{[+-]?\d+\})(?!\w)", r"$\1$", segment)

    for protected in _PROTECTED.finditer(text):
        pieces.extend((wrap(text[start : protected.start()]), protected[0]))
        start = protected.end()
    return "".join(pieces) + wrap(text[start:])


def _restore_source_tail(
    left: str, right: str, previous: ReadingItem, blocks: dict[str, Block]
) -> str:
    """Undo model word completion only when the source confirms the shorter tail.

    Both the preceding words and the next fragment must agree. Broad evidence
    ending at a neighbouring reference consequently cannot correct this item.
    """
    if not previous.parts:
        return left
    part = previous.parts[-1]
    block = blocks.get(part.block_id)
    if block is None:
        return left
    try:
        source = block.text.encode("utf-16-le")[part.start * 2 : part.end * 2].decode("utf-16-le")
    except UnicodeError:
        return left
    source = source.rstrip()
    if previous.kind == "reference" and previous.reference_number:
        source = re.sub(r"\s*\[" + re.escape(previous.reference_number) + r"\]\s*$", "", source)
    original = re.search(r"([A-Za-z]{3,})([" + _HYPHENS + r"]?)$", source)
    projected = re.search(r"([A-Za-z]{3,})([" + _HYPHENS + r"]?)$", left)
    following = re.match(r"[A-Za-z]+", right)
    if not original or not projected or not following:
        return left
    short, long = original[1], projected[1]
    if not long.casefold().startswith(short.casefold()) or len(long) <= len(short):
        return left
    if not (original[2] or projected[2]):
        return left
    added = long[len(short) :]
    if not following[0].casefold().startswith(added.casefold()):
        return left
    source_context = re.findall(r"[A-Za-z]+", source[: original.start()])[-2:]
    reading_context = re.findall(r"[A-Za-z]+", left[: projected.start()])[-2:]
    if len(source_context) < 2 or [s.casefold() for s in source_context] != [
        s.casefold() for s in reading_context
    ]:
        return left
    # Retain an explicit split marker for the caller's selected join policy.
    return left[: projected.start()] + long[: len(short)] + (projected[2] or original[2])


def join_continuation(
    previous: ReadingItem,
    current: ReadingItem,
    blocks: dict[str, Block],
    *,
    join: Join = "auto",
) -> str:
    """Return joined semantic text; the caller retains parts and page provenance."""
    left, right = previous.content or "", current.content or ""
    if previous.kind == "reference":
        left = normalize_reference(left, previous.reference_number)
    if current.kind == "reference":
        right = normalize_reference(right, current.reference_number or previous.reference_number)
    left, right = left.rstrip(), right.lstrip()
    projected_left = left
    left = _restore_source_tail(left, right, previous, blocks)
    source_confirmed_split = left != projected_left
    if not left or not right:
        return left + right
    if join == "space":
        return left + " " + right
    if join == "none":
        if source_confirmed_split and left.endswith(tuple(_HYPHENS)):
            left = left[:-1]
        return left + right
    if join == "auto" and left.endswith(tuple(_HYPHENS)) and not source_confirmed_split:
        tail, head = re.search(r"([A-Za-z0-9]+)[-‐‑]$", left), re.match(r"[A-Za-z0-9]+", right)
        if tail and head:
            compound = (tail[1] + "-" + head[0]).casefold()
            hyphens = str.maketrans({char: "-" for char in _HYPHENS})
            if any(
                word[0].translate(hyphens).casefold() == compound
                for block in blocks.values()
                for word in _WORD.finditer(block.text)
            ):
                return left + right
    if join == "dehyphenate" or left.endswith(tuple(_HYPHENS)):
        return (left[:-1] if left.endswith(tuple(_HYPHENS)) else left) + right
    if re.search(r"[\u3400-\u9fff]$", left) or re.match(r"[\u3400-\u9fff]", right):
        return left + right
    return left + " " + right
