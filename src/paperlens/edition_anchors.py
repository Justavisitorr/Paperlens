"""Keep literal passages of a reconstructed edition selectable in source coordinates."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from .models import Block, ReadingContentSpan, ReadingItem, ReadingPart
from .reading import utf16

# Formula renderings and complete citation tokens belong to the rich-text renderer.
_RICH = re.compile(r"\$\$[\s\S]*?\$\$|\$[^$\n]+\$|\[\d+(?:\s*[,，–—-]\s*\d+)*\]")


def content_anchors(item: ReadingItem, blocks: dict[str, Block]) -> list[ReadingContentSpan]:
    if not item.content or item.kind not in {"paragraph", "heading", "algorithm"}:
        return []
    pieces, ranges = [], []
    position = 0
    for part in item.parts:
        block = blocks.get(part.block_id)
        if block is None:
            continue
        text = block.text.encode("utf-16-le")[part.start * 2 : part.end * 2].decode("utf-16-le")
        pieces.append(text)
        ranges.append((position, position + len(text), text, part))
        position += len(text)
    source = "".join(pieces)
    excluded = [m.span() for m in _RICH.finditer(item.content)]
    spans = []
    for match in SequenceMatcher(None, source, item.content, autojunk=False).get_matching_blocks():
        if match.size < 4:
            continue
        for left, right, text, part in ranges:
            a, b = max(left, match.a), min(right, match.a + match.size)
            if b - a < 4:
                continue
            start, end = match.b + a - match.a, match.b + b - match.a
            allowed = [(start, end)]
            for x, y in excluded:
                allowed = [
                    (lo_part, hi_part)
                    for lo, hi in allowed
                    for lo_part, hi_part in ((lo, min(hi, x)), (max(lo, y), hi))
                    if hi_part - lo_part >= 4
                ]
            for lo, hi in allowed:
                offset = a - left + lo - start
                spans.append(
                    ReadingContentSpan(
                        start=utf16(item.content[:lo]),
                        end=utf16(item.content[:hi]),
                        source=ReadingPart(
                            block_id=part.block_id,
                            start=part.start + utf16(text[:offset]),
                            end=part.start + utf16(text[: offset + hi - lo]),
                            bbox=part.bbox,
                        ),
                    )
                )
    return sorted(spans, key=lambda span: span.start)
