"""Small source-only catalogue records, independent of generated analysis titles."""

from __future__ import annotations

import re

from .models import DashboardReport


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _author_names(text: str) -> list[str]:
    """A catalogue byline must not turn an affiliation/email into a person."""
    names = re.split(r"[（(\n]", text)[0]
    if len(names) > 240:
        return []
    columns = re.split(r"(?:\t| {3,})+", names.strip())
    if len(columns) > 1:
        parsed = [_author_names(column) for column in columns]
        # Accept layout columns only when every entry is already a valid,
        # multiword Latin name. A split first/last name must fall back unchanged.
        if all(
            len(group) == 1
            and sum(bool(re.search(r"[A-Za-z]", word)) for word in group[0].split()) >= 2
            for group in parsed
        ):
            return [group[0] for group in parsed]
    # Some typeset author rows separate names only with affiliation markers.
    names = re.sub(
        r"(?<=[A-Za-z\u3400-\u9fff])[0-9¹²³⁴⁵⁶⁷⁸⁹⁰]+(?=\s+[A-Z\u3400-\u9fff])", ",", names
    )
    authors = []
    for value in re.split(r"[,，、;；]|\s+(?:and|&)\s+", names):
        value = re.sub(r"^\s*[0-9¹²³⁴⁵⁶⁷⁸⁹⁰]+\s*", "", value)
        if re.search(
            r"@|https?://|\b(?:university|institute|research|laboratory|department|college|school|academy|hospital)\b|大学|学院|研究所|实验室|医院",
            value,
            re.I,
        ):
            break
        name = re.sub(r"[\s0-9¹²³⁴⁵⁶⁷⁸⁹⁰*†‡]+$", "", _plain(value))
        if name:
            authors.append(name)
    return authors


def bibliography(report: DashboardReport) -> dict:
    reading = report.reading
    by_id = {block.id: block for block in report.document.blocks}

    def info(category: str) -> list[str]:
        if not reading:
            return []
        semantic = [i.content for i in reading.information_items.get(category, []) if i.content]
        return semantic or [
            by_id[i].text for i in reading.information.get(category, []) if i in by_id
        ]

    titles = info("titles")
    original = titles[0] if titles else report.document.title
    if not original or re.fullmatch(r"[a-f0-9]{32,64}|paper|document", original, re.I):
        original = re.sub(r"\.pdf$", "", report.document.source_name, flags=re.I) or "未命名论文"
    authors = []
    for text in info("authors"):
        if re.match(r"作者简介|通信作者|通讯作者|Author biographies|About the authors", text, re.I):
            continue
        names = _author_names(text)
        if names:
            authors = names
            break
    publication = " ".join(info("publication") + info("citation"))
    date = re.search(r"\b((?:19|20)\d{2})\b", publication)
    doi = re.search(r"10\.\d{4,9}/[^\s<>\"，。]+", publication, re.I)
    url = re.search(r"https?://[^\s<>\"，。]+", publication)
    venue = re.search(r"\[[A-Z]\]\s*[.。]?\s*([^,，]+)[,，]\s*(?:19|20)\d{2}", publication)
    abstract = ""
    if reading:
        for index, item in enumerate(reading.items[:-1]):
            if item.kind == "heading" and re.fullmatch(
                r"摘\s*要|Abstract", (item.content or "").strip(), re.I
            ):
                following = reading.items[index + 1]
                if following.kind == "paragraph":
                    abstract = following.content
                    break
    texts = (
        [i.content for i in reading.items if i.kind == "paragraph" and i.content] if reading else []
    )
    texts += [b.text for b in report.document.blocks if b.page <= 2 and b.kind == "text"]
    for text in texts:
        if abstract:
            break
        match = re.match(r"\s*(?:摘\s*要|Abstract)\s*[:：]?\s*(.+)", text, re.I | re.S)
        if match:
            abstract = re.split(r"关键词\s*[:：]|Keywords\s*:", match[1], maxsplit=1, flags=re.I)[0]
            break
    doi_text = doi[0].rstrip(".,;)") if doi else ""
    return {
        "title": _plain(original),
        "original_title": _plain(original),
        "alternate_title": _plain(titles[1]) if len(titles) > 1 else "",
        "authors": authors,
        "year": date[1] if date else "",
        "venue": _plain(venue[1]) if venue else "",
        "abstract": _plain(abstract)[:6000],
        "doi": doi_text,
        "source_url": "https://doi.org/" + doi_text
        if doi_text
        else url[0].rstrip(".,;")
        if url
        else "",
        "reading_source_version": reading.source_version if reading else "",
        "reading_item_ids": [
            "article-opening",
            *[i.id for i in reading.items],
            "article-references",
            *[i.id for i in reading.references],
            "article-information",
        ]
        if reading
        else [],
    }
