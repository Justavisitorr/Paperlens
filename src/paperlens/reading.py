"""Deterministic reading projection over immutable source blocks.

The projection owns presentation, never replaces quoted evidence. Each slice uses
UTF-16 offsets so browser selections and existing notes remain reproducible.
"""

from __future__ import annotations

import base64
import hashlib
import io
import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

from .models import (
    Block,
    Box,
    DashboardReport,
    PaperDocument,
    ReadingDocument,
    ReadingItem,
    ReadingPart,
    SourcePDF,
    TableCell,
    TableGrid,
)
from .reading_code import code_language, code_text, is_code
from .reading_ownership import project_sources
from .reading_references import LABEL, repair_reference_boundaries
from .reading_toc import filter_source_toc


def utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def section_level(text: str, fallback: int = 1) -> int:
    """Explicit section numbers take precedence over inferred heading levels."""
    number = re.match(r"^(?:\d+|[A-Z])((?:\.\d+)*)\.?\s+", text.strip())
    return max(1, min(3, 1 + number.group(1).count(".") if number else fallback))


def part(block: Block, start: int = 0, end: int | None = None, join: str = "") -> ReadingPart:
    return ReadingPart(
        block_id=block.id, start=utf16(block.text[:start]), end=utf16(block.text[:end]), join=join
    )


def overlap(a: Box, b: Box) -> float:
    area = max(0, a.x1 - a.x0) * max(0, a.y1 - a.y0)
    return (
        max(0, min(a.x1, b.x1) - max(a.x0, b.x0)) * max(0, min(a.y1, b.y1) - max(a.y0, b.y0))
    ) / max(area, 1e-9)


def union(blocks: list[Block]) -> Box:
    return Box(
        x0=min(b.bbox.x0 for b in blocks),
        y0=min(b.bbox.y0 for b in blocks),
        x1=max(b.bbox.x1 for b in blocks),
        y1=max(b.bbox.y1 for b in blocks),
    )


def markdown_table(block: Block) -> TableGrid | None:
    rows = [
        line.strip().strip("|").split("|")
        for line in block.raw_text.splitlines()
        if line.strip().startswith("|") and not re.fullmatch(r"[| :\-]+", line.strip())
    ]
    if len(rows) < 2 or len({len(r) for r in rows}) != 1:
        return None
    return TableGrid(
        rows=len(rows),
        cols=len(rows[0]),
        cells=[
            TableCell(
                text=text.strip(),
                row_start=r,
                row_end=r + 1,
                col_start=c,
                col_end=c + 1,
                column_header=r == 0,
            )
            for r, row in enumerate(rows)
            for c, text in enumerate(row)
        ],
    )


def enrich_layout(report: DashboardReport, layout: PaperDocument) -> None:
    """Import geometry/objects into old reports without changing quoted text or IDs."""
    if report.document.sha256 != layout.sha256:
        raise ValueError("Layout and report must describe the same PDF")
    for old, new in zip(report.document.pages, layout.pages, strict=True):
        old.label, old.rotation = new.label, new.rotation
    for b in report.document.blocks:
        candidates = [
            n
            for n in layout.blocks
            if n.page == b.page
            and n.kind == b.kind
            and overlap(b.bbox, n.bbox) > 0.8
            and overlap(n.bbox, b.bbox) > 0.8
        ]
        if candidates:
            n = max(candidates, key=lambda n: overlap(b.bbox, n.bbox))
            b.table = n.table
            b.docling_label, b.heading_level = n.docling_label, n.heading_level
    ids = {b.id for b in report.document.blocks}
    for n in layout.blocks:
        if n.kind in {"picture", "header", "footer"} and n.id not in ids:
            added = n.model_copy(deep=True)
            on_page = [
                b
                for b in report.document.blocks
                if b.page == n.page and b.kind not in {"picture", "header", "footer"}
            ]
            if on_page:
                nearest = min(
                    on_page, key=lambda b: abs(b.bbox.y0 - n.bbox.y0) + abs(b.bbox.x0 - n.bbox.x0)
                )
                added.order = nearest.order
            report.document.blocks.append(added)
    for e in report.evidence:
        e.page_label = report.document.pages[e.page - 1].label


def build_reading(report: DashboardReport, pdf_path: Path | None = None) -> ReadingDocument:
    original = sorted(report.document.blocks, key=lambda b: (b.page, b.order))
    blocks, source_parts, hidden = project_sources(report.document, pdf_path)
    by_id = {b.id: b for b in [*original, *blocks]}

    def source_part(b: Block, start=0, end=None, join="") -> ReadingPart:
        result = part(b, start, end, join)
        if b.id in source_parts:
            base = source_parts[b.id]
            result.block_id = base.block_id
            result.start += base.start
            result.end += base.start
            result.bbox = base.bbox
        return result

    information: dict[str, list[str]] = defaultdict(list)
    categories: dict[str, str] = {}
    in_authors = False
    declaration_section = False
    front = True
    previous_category = ""
    previous: Block | None = None
    for b in blocks:
        text = b.text.strip()
        compact = re.sub(r"\s+", "", text)
        category = ""
        if b.kind in {"header", "footer"}:
            hidden.add(b.id)
            continue
        if b.kind == "heading" and (
            re.match(r"^(?:\d+(?:\.\d+)*\.?|[A-Z](?:\.\d+)*\.)\s+", text)
            or re.match(
                r"^(Introduction|Methods?|Results?|Discussion|Conclusions?|References|Bibliography|Appendix|Appendices|引言|方法|结果|结论|参考文献|附录)\b",
                text,
                re.I,
            )
        ):
            front = False
            in_authors = False
        if b.kind == "heading":
            declaration_section = bool(
                re.match(
                    r"^(Funding|Acknowledg|Conflict of interest|致谢|利益冲突|作者贡献)", text, re.I
                )
            )
        if re.match(r"^(作者简介|作者信息|Author biographies)[:：]?", text, re.I):
            in_authors = True
        if in_authors:
            category = "authors"
        elif declaration_section or re.match(
            r"^(基金项目|资助|Funding|Acknowledg|Conflict of interest|利益冲突|收稿日期|修回日期)",
            text,
            re.I,
        ):
            category = "declarations"
        elif b.docling_label == "footnote":
            category = "footnotes"
        elif re.search(r"(?:E-?mail|电子邮[箱件]|通讯作者)\s*[:：]", text, re.I):
            category = "authors"
        elif front and re.match(
            r"^(DOI\b|doi\.org|https?://doi\.org|中图分类号|文章编号|文献标志码|网络出版地址)",
            text,
            re.I,
        ):
            category = "publication"
        elif front and re.match(
            r"^(中文引用格式|英文引用格式|引用格式|Cite this|Citation:)", text, re.I
        ):
            category = "citation"
        elif front and b.docling_label == "title":
            category = "titles"
        elif (
            front
            and b.kind == "heading"
            and len(text) < 180
            and not re.match(
                r"^(摘要|Abstract|Introduction|引言|Keywords|关键词|References|Bibliography|参考文献|Methods?|Results?|Discussion|Conclusions?|Background|Materials|Appendix|Appendices|背景|方法|结论|附录)",
                text,
                re.I,
            )
        ):
            category = "titles"
        elif (
            front
            and previous_category == "publication"
            and previous
            and previous.page == b.page
            and abs(previous.bbox.y0 - b.bbox.y0) < 0.015
            and re.match(r"^[:：]?\s*https?://", text)
        ):
            category = "publication"
        elif (
            front
            and previous_category in {"titles", "authors"}
            and previous is not None
            and previous.page == b.page
            and -0.015 <= b.bbox.y0 - previous.bbox.y1 < 0.07
            and b.bbox.y0 < 0.7
            and len(text) < 240
            and not re.match(r"^(摘要|Abstract|关键词|Keywords)", compact, re.I)
        ):
            category = "authors"
        if category:
            categories[b.id] = category
            information[category].append(b.id)
        previous_category, previous = category, b
    # Citation blocks may be split or delivered slightly out of order on a cover page.
    citation_blocks = [by_id[i] for i in information["citation"]]
    for b in blocks:
        if b.id in categories or b.kind != "text":
            continue
        if any(c.page == b.page and overlap(b.bbox, c.bbox) > 0.6 for c in citation_blocks):
            categories[b.id] = "citation"
            information["citation"].append(b.id)

    pictures = [b for b in blocks if b.kind == "picture" and b.id not in categories]
    captions = [b for b in blocks if b.kind == "caption"]
    claimed_captions: set[str] = set()
    objects: list[tuple[float, ReadingItem]] = []
    figure_groups: dict[tuple[str, ...], ReadingItem] = {}
    for b in pictures:
        # Parser caption references occasionally point to the previous figure.
        # Their physical relation must agree with the object before grouping.
        cs = [
            by_id[i]
            for i in b.caption_ids
            if i in by_id
            and by_id[i].kind == "caption"
            and by_id[i].page == b.page
            and -0.015 <= by_id[i].bbox.y0 - b.bbox.y1 < 0.065
            and min(by_id[i].bbox.x1, b.bbox.x1) > max(by_id[i].bbox.x0, b.bbox.x0)
        ]
        if not cs:
            cs = [
                c
                for c in captions
                if c.page == b.page
                and -0.015 <= c.bbox.y0 - b.bbox.y1 < 0.055
                and min(c.bbox.x1, b.bbox.x1) > max(c.bbox.x0, b.bbox.x0)
            ]
        if not cs:
            following = [
                p
                for p in pictures
                if p.page == b.page
                and 0 <= p.bbox.y0 - b.bbox.y1 < 0.03
                and abs(p.bbox.x0 - b.bbox.x0) < 0.025
                and abs(p.bbox.x1 - b.bbox.x1) < 0.025
            ]
            for below in following:
                cs = [
                    by_id[i] for i in below.caption_ids if i in by_id and by_id[i].kind == "caption"
                ]
                if cs:
                    break
        if cs:
            # Include adjacent bilingual captions so both panels share one key.
            primary = min(cs, key=lambda c: c.bbox.y0)
            cs = [
                c
                for c in captions
                if c.page == b.page
                and -0.002 <= c.bbox.y0 - primary.bbox.y0 < 0.035
                and min(c.bbox.x1, primary.bbox.x1) > max(c.bbox.x0, primary.bbox.x0)
                and re.match(r"^(?:图|Fig(?:ure)?\.?)[\s\d]", c.text, re.I)
            ] or cs
            cs.sort(key=lambda c: (c.bbox.y0, c.bbox.x0))
        # A composite figure has one caption and can contain multiple picture regions.
        claimed_captions.update(c.id for c in cs)
        same_page = [
            x for x in blocks if x.page == b.page and x.kind not in {"picture", "header", "footer"}
        ]
        anchor = (
            min(cs, key=lambda c: c.order)
            if cs
            else min(same_page, key=lambda x: abs(x.bbox.y0 - b.bbox.y0), default=b)
        )
        item = ReadingItem(
            id="object-" + b.id,
            kind="figure",
            page=b.page,
            bbox=b.bbox,
            label=" / ".join(c.text for c in cs) or b.text or "原文图片",
            parts=[source_part(b), *[source_part(c) for c in cs]],
        )
        key = tuple(c.id for c in cs) or (b.id,)
        if key in figure_groups:
            existing = figure_groups[key]
            existing.parts.append(source_part(b))
            existing.bbox = union(
                [by_id[p.block_id] for p in existing.parts if by_id[p.block_id].kind == "picture"]
            )
        else:
            figure_groups[key] = item
            objects.append((anchor.order - 0.1, item))
        for n in same_page:
            if n.kind not in {"caption", "heading"} and overlap(n.bbox, b.bbox) > 0.9:
                hidden.add(n.id)
    # The semantic figure includes subplot descriptions outside the plot frame.
    # Stop at its caption; adjacent article paragraphs are never swallowed.
    for item in figure_groups.values():
        caption_blocks = [
            by_id[p.block_id] for p in item.parts if by_id[p.block_id].kind == "caption"
        ]
        picture_blocks = [
            by_id[p.block_id] for p in item.parts if by_id[p.block_id].kind == "picture"
        ]
        if not picture_blocks:
            continue
        box = union(picture_blocks)
        below = [c.bbox.y0 for c in caption_blocks if c.bbox.y0 >= box.y1 - 0.015]
        bottom = min(below) if below else box.y1
        region = box.model_copy(update={"y1": max(box.y1, min(bottom, box.y1 + 0.06))})
        owned = [
            n
            for n in blocks
            if n.page == item.page
            and n.kind == "text"
            and n.id not in categories
            and overlap(n.bbox, region) > 0.85
        ]
        hidden.update(n.id for n in owned)
        item.bbox = union([*picture_blocks, *owned])
    hidden.update(claimed_captions)
    # Keep algorithms as original layout, instead of presenting broken formulas/steps.
    algorithm_ids: set[str] = set()
    for i, b in enumerate(blocks):
        if (
            b.id in categories
            or not re.match(r"^(?:算法|Algorithm)\s*\d+", b.text, re.I)
            or b.kind not in {"heading", "algorithm"}
        ):
            continue
        group = [b]
        for n in blocks[i + 1 :]:
            if n.kind in {"header", "footer", "picture"}:
                continue
            if n.page != b.page or n.kind == "heading" or n.bbox.x0 > b.bbox.x1 + 0.08:
                break
            group.append(n)
        algorithm_ids.update(n.id for n in group)
        objects.append(
            (
                b.order,
                ReadingItem(
                    id="algorithm-" + b.id,
                    kind="algorithm",
                    label=b.text,
                    page=b.page,
                    bbox=union(group),
                    parts=[source_part(n) for n in group],
                ),
            )
        )

    items: list[tuple[float, ReadingItem]] = list(objects)
    reference_mode = False
    previous_block: Block | None = None
    last_item: ReadingItem | None = None
    native = None
    code_layout: dict = {}
    codes_enabled = bool(
        report.run.processing_limits.get("code_blocks", report.run.processing_profile == "detailed")
    )
    if pdf_path:
        import pdfplumber

        native = pdfplumber.open(pdf_path)

    def native_hyphen(b: Block) -> bool:
        if native is None:
            return False
        p = native.pages[b.page - 1]
        bb = b.bbox
        text = (
            p.crop(
                (
                    max(0, bb.x0 * p.width - 2),
                    max(0, bb.y0 * p.height - 2),
                    min(p.width, bb.x1 * p.width + 2),
                    min(p.height, bb.y1 * p.height + 2),
                )
            ).extract_text()
            or ""
        )
        return bool(re.search(r"[A-Za-z]-\s*$", text))

    try:
        for b in blocks:
            if b.id in hidden or b.id in categories or b.id in algorithm_ids or b.kind == "picture":
                continue
            text = b.text.strip()
            if not text:
                continue
            if b.kind == "text" and re.fullmatch(r"[<>:;=?@\s]{3,}", text):
                # Pieces of a tall brace or bracket (TeX extension font) beside a formula.
                if any(
                    n.page == b.page
                    and n.kind == "formula"
                    and n.bbox.y0 < b.bbox.y1
                    and b.bbox.y0 < n.bbox.y1
                    and -0.03 < n.bbox.x0 - b.bbox.x1 < 0.03
                    for n in blocks
                ):
                    hidden.add(b.id)
                    continue
            # A bulleted list item ("· Batch size: 16, 32") is not a section title.
            listing = is_code(b)
            if listing:
                # A listing cannot be a bibliography entry, even if a heading was missed.
                reference_mode = False
            if b.kind == "heading" and not listing and not re.match(r"^[·•▪◦‣]", text):
                reference_mode = bool(
                    re.match(r"^(参考文献|References|Bibliography)\s*[:：]?$", text, re.I)
                )
                level = section_level(text, b.heading_level or 1)
                if reference_mode:
                    hidden.add(b.id)
                    previous_block, last_item = None, None
                    continue
                item = ReadingItem(
                    id="reading-" + b.id,
                    kind="heading",
                    label=text,
                    level=min(3, level),
                    parts=[source_part(b)],
                    page=b.page,
                )

            else:
                kind = (
                    ("code" if codes_enabled else "algorithm")
                    if listing
                    else "reference"
                    if reference_mode or b.kind == "reference"
                    else (b.kind if b.kind in {"table", "formula", "algorithm"} else "paragraph")
                )
                item = ReadingItem(
                    id="reading-" + b.id,
                    kind=kind,
                    parts=[source_part(b)],
                    page=b.page,
                    bbox=b.bbox,
                )
                if kind == "code":
                    if not last_item or last_item.kind != "code" or b.page - last_item.page > 1:
                        code_layout.clear()
                    item.content = code_text(
                        b, native.pages[b.page - 1] if native else None, code_layout
                    )
                    item.code_language = code_language(item.content)
                if kind == "formula":
                    # Do not let crop padding include the next prose baseline.
                    following = [
                        n.bbox.y0
                        for n in blocks
                        if n.page == b.page
                        and n.kind == "text"
                        and n.id not in hidden
                        and n.id not in categories
                        and b.bbox.y0 < n.bbox.y0 <= b.bbox.y1 + 0.01
                        and min(n.bbox.x1, b.bbox.x1) > max(n.bbox.x0, b.bbox.x0)
                    ]
                    if following:
                        item.bbox = b.bbox.model_copy(
                            update={"y1": max(b.bbox.y0, min(following) - 0.001)}
                        )
                if b.kind == "table" and b.table is None:
                    b.table = markdown_table(b)
                marker = re.match(rf"^\s*\[({LABEL})\]\s*", text) if kind == "reference" else None
                trailing = re.search(r"\s*\[(\d+)\]\s*$", text) if kind == "reference" else None
                if marker or trailing:
                    m = marker or trailing
                    item.label = "[" + m.group(1) + "]"
                    item.parts = [
                        source_part(b, m.end() if marker else 0, None if marker else m.start())
                    ]
                continuation = (
                    previous_block
                    and last_item
                    and kind in {"paragraph", "reference"}
                    and last_item.kind == kind
                    and not item.label
                    and b.page - previous_block.page in {0, 1}
                )
                if continuation:
                    a = previous_block
                    cross = b.page > a.page or (
                        a.bbox.x1 <= b.bbox.x0 + 0.05 and b.bbox.y0 < a.bbox.y0
                    )
                    short_gap = (
                        b.page == a.page
                        and abs(a.bbox.x0 - b.bbox.x0) < 0.08
                        and 0 <= b.bbox.y0 - a.bbox.y1 < 0.025
                    )
                    tail = re.sub(r"\s*\[\d+\]\s*$", "", a.text).rstrip()
                    can_join = (cross or (reference_mode and short_gap)) and (
                        reference_mode
                        or (a.section == b.section and not re.search(r"[。！？.!?;；:：]$", tail))
                    )
                    if (
                        reference_mode
                        and re.search(r"[.!?。]$", tail)
                        and re.match(r"[A-Z\u3400-\u9fff]", text)
                    ):
                        can_join = False
                    if can_join:
                        hyphen = tail.endswith("-") or native_hyphen(a)
                        if tail.endswith("-"):
                            last_item.parts[-1].end -= 1
                        cjk = bool(
                            re.search(r"[\u3400-\u9fff]$", tail)
                            or re.match(r"[\u3400-\u9fff]", text)
                        )
                        item.parts[0].join = "" if hyphen or cjk else " "
                        last_item.parts.extend(item.parts)
                        previous_block = b
                        continue
            items.append((b.order, item))
            last_item, previous_block = item, b
    finally:
        if native:
            native.close()
    ordered = [i for _, i in sorted(items, key=lambda pair: (pair[1].page, pair[0]))]
    # Listing captions can follow the code in parser order but sit above it on paper.
    code_captions: set[str] = set()
    for item in ordered:
        if item.kind != "code" or not item.bbox:
            continue
        candidates = [
            b
            for b in blocks
            if b.page == item.page
            and re.match(r"^(?:Listing\s+\d+|代码\s*\d+)", b.text, re.I)
            and 0 <= item.bbox.y0 - b.bbox.y1 < 0.045
        ]
        if candidates:
            caption = max(candidates, key=lambda b: b.bbox.y1)
            item.label = caption.text
            code_captions.add(caption.id)
    ordered = [
        i for i in ordered if not (i.parts and all(p.block_id in code_captions for p in i.parts))
    ]
    # Table captions belong to the table, including legacy reports that had only Markdown.
    table_captions: set[str] = set()
    for item in ordered:
        if item.kind != "table" or item.bbox is None:
            continue

        def number(text: str) -> str | None:
            found = re.match(r"^\s*(?:表|Table)\s*(\d+)", text, re.I)
            return found.group(1) if found else None

        free = [
            c
            for c in captions
            if c.page == item.page and c.id not in table_captions and number(c.text)
        ]
        # Parsers often prefix a table's own caption to its text: the number decides.
        own = number(by_id[item.parts[0].block_id].text)
        matches = [c for c in free if own and number(c.text) == own]
        if not matches:
            matches = [
                c
                for c in free
                if min(c.bbox.x1, item.bbox.x1) > max(c.bbox.x0, item.bbox.x0)
                and (
                    0 <= item.bbox.y0 - c.bbox.y1 < 0.04
                    # Captions set below the table, as in ACL-style papers.
                    or (not own and 0 <= c.bbox.y0 - item.bbox.y1 < 0.04)
                )
            ]
        if matches:
            caption = min(
                matches,
                key=lambda c: min(abs(item.bbox.y0 - c.bbox.y1), abs(c.bbox.y0 - item.bbox.y1)),
            )
            # A bilingual pair ("表 1 实验数据" over "Table 1 Experimental datasets") is one caption.
            pair = [
                c
                for c in free
                if c is caption
                or (
                    number(c.text) == number(caption.text)
                    and min(abs(c.bbox.y0 - caption.bbox.y1), abs(caption.bbox.y0 - c.bbox.y1))
                    < 0.02
                )
            ]
            pair.sort(key=lambda c: c.bbox.y0)
            item.label = " / ".join(c.text for c in pair)
            item.parts.extend(source_part(c) for c in pair)
            table_captions.update(c.id for c in pair)
    ordered = [
        item
        for item in ordered
        if item.kind != "paragraph"
        or not item.parts
        or item.parts[0].block_id not in table_captions
    ]
    # Attach each quantitative record exactly once, preferring its original object/evidence.
    for v in report.visuals:
        candidates = [
            i for i in ordered if i.kind in {"figure", "table", "formula"} and i.page == v.page
        ]
        exact = [i for i in candidates if set(v.source_block_ids) & {p.block_id for p in i.parts}]
        region = [i for i in candidates if i.bbox and v.bbox and overlap(i.bbox, v.bbox) > 0.5]
        target = next(iter(exact or region), None)
        if target:
            target.visual_ids.append(v.id)
    evidence = {e.id: e for e in report.evidence}
    for result in report.quantitative_results:
        targets = [i for i in ordered if result.visual_id in i.visual_ids]
        if not targets:
            source_ids = {evidence[e].block_id for e in result.evidence_ids if e in evidence}
            targets = [i for i in ordered if source_ids & {p.block_id for p in i.parts}]
        if not targets:
            targets = [
                i
                for i in ordered
                if i.page == result.page
                and i.kind in {"figure", "table"}
                and result.figure_label
                and result.figure_label in i.label
            ]
        if not targets:
            targets = [i for i in ordered if i.page == result.page and i.kind == "paragraph"]
        if targets:
            targets[0].result_ids.append(result.id)
        else:
            # Keep unanchored results visible, with their actual page and no guessed figure.
            item = ReadingItem(
                id="results-" + result.id,
                kind="paragraph",
                page=result.page,
                label="本页定量记录（未定位到具体图表）",
                result_ids=[result.id],
            )
            slot = next((j for j, i in enumerate(ordered) if i.page > result.page), len(ordered))
            ordered.insert(slot, item)
    references = [i for i in ordered if i.kind == "reference"]
    ordered = [i for i in ordered if i.kind != "reference"]
    for item in references:
        text = "".join(
            p.join
            + by_id[p.block_id]
            .text.encode("utf-16-le")[p.start * 2 : p.end * 2]
            .decode("utf-16-le")
            for p in item.parts
        )
        doi = re.search(r"10\.\d{4,9}/[^\s<>]+", text, re.I)
        url = re.search(r"https?://[^\s<>]+", text, re.I)
        if doi:
            item.href = "https://doi.org/" + quote(doi.group().rstrip(".,;。"), safe="/()[]:;_-")
        elif url:
            item.href = url.group().rstrip(".,;。")
    version = hashlib.sha256("\n".join(b.id + ":" + b.text for b in original).encode()).hexdigest()[
        :20
    ]
    reading = ReadingDocument(
        source_version=version,
        items=ordered,
        references=references,
        information={
            category: list(dict.fromkeys(source_part(by_id[bid]).block_id for bid in ids))
            for category, ids in information.items()
        },
        information_parts={
            category: [source_part(by_id[bid]) for bid in ids]
            for category, ids in information.items()
        },
        hidden_ids=sorted(hidden),
    )
    filter_source_toc(report, reading)
    repair_reference_boundaries(report, reading)
    return reading


def _fitted_formula(report, item, page, cache, width, height):
    """The display formula's painted glyphs rather than the layout box."""
    from .object_crop import formula_box, page_glyphs

    try:
        if item.page not in cache:
            cache[item.page] = page_glyphs(page)
        own = {part.block_id for part in item.parts}
        others = [
            block.bbox
            for block in report.document.blocks
            if block.page == item.page and block.id not in own
        ]
        return formula_box(cache[item.page], item.bbox, others, width, height)
    except Exception:  # A damaged text layer keeps the layout box.
        return item.bbox


def prepare_reading(report: DashboardReport, pdf_path: Path | None = None) -> None:
    pdf_bytes = pdf_path.read_bytes() if pdf_path is not None else None
    if pdf_bytes is not None and hashlib.sha256(pdf_bytes).hexdigest() != report.document.sha256:
        raise ValueError("Source PDF hash does not match the report")
    previous = report.reading
    report.reading = build_reading(report, pdf_path)
    if previous and previous.source_version == report.reading.source_version:
        transcriptions = {i.id: i for i in previous.items if i.transcription}
        for item in report.reading.items:
            old = transcriptions.get(item.id)
            if old and old.parts == item.parts and old.bbox == item.bbox:
                item.transcription = old.transcription
    if pdf_path is None:
        return
    import pypdfium2 as pdfium

    report.source_pdf = SourcePDF(
        data_url="data:application/pdf;base64," + base64.b64encode(pdf_bytes).decode(),
        sha256=report.document.sha256,
    )
    glyph_cache: dict[int, list] = {}
    with pdfium.PdfDocument(pdf_bytes) as pdf:
        for item in report.reading.items:
            if item.kind not in {"figure", "table", "formula", "algorithm"} or item.bbox is None:
                continue
            page = pdf[item.page - 1]
            width, height = page.get_size()
            b = item.bbox
            if item.kind == "formula":
                b = _fitted_formula(report, item, page, glyph_cache, width, height)
                item.bbox = b
                if len(item.parts) == 1:
                    item.parts[0].bbox = b
            margin = 0.0005 if item.kind == "formula" else 0.004
            bitmap = page.render(
                scale=min(2.3, 1500 / max(1, width * (b.x1 - b.x0))),
                crop=(
                    max(0, b.x0 - margin) * width,
                    max(0, 1 - b.y1 - margin) * height,
                    max(0, 1 - b.x1 - margin) * width,
                    max(0, b.y0 - margin) * height,
                ),
            )
            image = bitmap.to_pil()
            buffer = io.BytesIO()
            image.save(buffer, format="WEBP", quality=90)
            item.asset = "data:image/webp;base64," + base64.b64encode(buffer.getvalue()).decode()
            image.close()
            bitmap.close()
            page.close()
