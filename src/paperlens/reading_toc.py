"""Hide source-PDF contents lists, without changing evidence or physical pages."""

from __future__ import annotations

import re
from collections import defaultdict

from .models import DashboardReport, PaperDocument, ReadingDocument, ReadingItem, ReadingPart

_LEADER = r"(?:[.．·…][ \t]*){3,}"
_PAGE = r"(?:\d{1,4}|[ivxlcdmIVXLCDM]{1,8})(?![\w.])"
_NUMBER = r"(?:\d+(?:\.\d+)*\.?|[A-Z](?:\.\d+)*\.?)"
_PLAIN_PARENT = re.compile(
    rf"^[ \t]*(?P<number>{_NUMBER})[ \t]+[^\n\r]{{1,120}}?[ \t]+{_PAGE}[ \t]*$"
)
_NUMBERED = re.compile(rf"(?<!\S){_NUMBER}[ \t]+[^\n\r]{{1,160}}?{_LEADER}[ \t]*{_PAGE}")
_UNNUMBERED = re.compile(
    rf"^[ \t]*[A-Za-z\u3400-\u9fff][^\n\r]{{1,120}}?{_LEADER}[ \t]*{_PAGE}[ \t]*$", re.M
)
_HEADING = re.compile(
    r"^[ \t]*(?:table[ \t]+of[ \t]+contents|contents|目[ \t]*录)[ \t]*[:：]?(?:[ \t]*$|(?=[ \t]+\d+(?:\.\d+)*\.?[ \t]))",
    re.I | re.M,
)


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def toc_ranges(document: PaperDocument) -> dict[str, list[tuple[int, int]]]:
    """Require a contents heading/continuation or a dense numbered leader list.

    Only positively identified rows/headings are excluded. A page designation
    never makes its other paragraphs expendable. Offsets address original text.
    """
    heading_keys = {
        re.sub(r"\s+", " ", block.text).strip().casefold()
        for block in document.blocks
        if block.kind == "heading"
    }
    pages: dict[int, list] = defaultdict(list)
    for block in document.blocks:
        if block.kind not in {"text", "heading", "table"}:
            continue
        numbered = list(_NUMBERED.finditer(block.text))
        rows = numbered + [
            m
            for m in _UNNUMBERED.finditer(block.text)
            if not any(n.start() <= m.start() < n.end() for n in numbered)
        ]
        headings = list(_HEADING.finditer(block.text))
        pages[block.page].append((block, rows, headings, numbered))
    found: dict[str, list[tuple[int, int]]] = {}
    previous = -2
    for page, entries in sorted(pages.items()):
        rows = [m for _, matches, _, _ in entries for m in matches]
        numbered = [m for _, _, _, matches in entries for m in matches]
        has_heading = any(
            headings or block.docling_label == "table_of_contents"
            for block, _, headings, _ in entries
        )
        # A heading-less continuation needs multiple rows. A standalone list
        # needs three hierarchical entries corroborated by real section titles.
        hierarchical = [m for m in numbered if re.match(r"\d+\.\d+\s", m.group())]
        corroborated = sum(
            re.sub(r"\s+", " ", re.split(_LEADER, m.group(), maxsplit=1)[0]).strip().casefold()
            in heading_keys
            for m in hierarchical
        )
        confirmed = bool(rows) and (
            has_heading
            or (previous == page - 1 and len(rows) >= 2)
            or (len(hierarchical) >= 3 and corroborated >= 2)
        )
        if not confirmed:
            continue
        previous = page
        for index, (block, matches, headings, _) in enumerate(entries):
            parent = _PLAIN_PARENT.fullmatch(block.text)
            if parent and index + 1 < len(entries):
                children = entries[index + 1][3]
                prefix = parent.group("number").rstrip(".") + "."
                # A TOC chapter can omit dot leaders. Require its immediately
                # following subentries to carry that chapter's number prefix.
                if children and all(m.group().startswith(prefix) for m in children):
                    matches = [*matches, parent]
            spans = sorted(
                {
                    (_utf16(block.text[: m.start()]), _utf16(block.text[: m.end()]))
                    for m in [*matches, *headings]
                }
            )
            if spans:
                found[block.id] = spans
    return found


def filter_source_toc(report: DashboardReport, reading: ReadingDocument | None = None) -> None:
    """Upgrade an in-memory reading projection; saved PDF/source/cache stays intact.

    Changed fragments retain IDs and UTF-16 source anchors. Their old combined
    translations are hidden; translating the surviving text remains opt-in.
    """
    ranges = toc_ranges(report.document)
    if not ranges:
        return
    reading = reading if reading is not None else report.reading
    if reading is None:
        # Legacy portable reports without a projection use the same local path.
        from .reading import build_reading

        report.reading = build_reading(report)
        return
    blocks = {block.id: block for block in report.document.blocks}

    def subtract(parts: list[ReadingPart]) -> list[ReadingPart]:
        kept = []
        for part in parts:
            if part.block_id not in ranges:
                kept.append(part)
                continue
            intervals = [(part.start, part.end)]
            for start, end in ranges[part.block_id]:
                intervals = [
                    (left, right)
                    for lo, hi in intervals
                    for left, right in ((lo, min(hi, start)), (max(lo, end), hi))
                    if left < right
                ]
            raw = blocks[part.block_id].text.encode("utf-16-le")
            for start, end in intervals:
                text = raw[start * 2 : end * 2].decode("utf-16-le")
                if not text.strip():
                    continue
                left = start + _utf16(text[: len(text) - len(text.lstrip())])
                right = end - _utf16(text[len(text.rstrip()) :])
                kept.append(
                    part.model_copy(
                        update={
                            "start": left,
                            "end": right,
                            "join": part.join if not kept else "\n",
                        }
                    )
                )
        return kept

    def project(items: list[ReadingItem]) -> list[ReadingItem]:
        out = []
        for item in items:
            parts = subtract(item.parts)
            if parts == item.parts:
                out.append(item)
                continue
            report.translations.pop(item.id, None)
            if not parts:
                continue
            # Mixed source/TOC items fall back only to the retained source spans;
            # an old semantic transcription or table must not reintroduce TOC rows.
            item.parts = parts
            item.content = None
            item.transcription = None
            item.content_spans = []
            item.table = None
            item.kind = "paragraph"
            item.label = ""
            item.asset = ""
            out.append(item)
        return out

    reading.items = project(reading.items)
    reading.references = project(reading.references)
    for category, items in reading.information_items.items():
        reading.information_items[category] = project(items)
    for category, ids in reading.information.items():
        parts = reading.information_parts.get(category)
        if parts is None:
            parts = [ReadingPart(block_id=bid, end=_utf16(blocks[bid].text)) for bid in ids]
        parts = subtract(parts)
        reading.information_parts[category] = parts
        reading.information[category] = list(dict.fromkeys(p.block_id for p in parts))
    reading.excluded_toc_parts = [
        ReadingPart(block_id=bid, start=start, end=end)
        for bid, spans in ranges.items()
        for start, end in spans
    ]
    complete = [
        bid
        for bid in ranges
        if not subtract([ReadingPart(block_id=bid, end=_utf16(blocks[bid].text))])
    ]
    reading.hidden_ids = sorted(set(reading.hidden_ids) | set(complete))
