"""Resolve bibliography destinations during an explicit build, never while reading.

Crossref search results are candidates, not answers: a destination needs a complete
title, the first author and the cited publication year to agree. No LLM is used.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote, unquote, urlsplit

import httpx

if TYPE_CHECKING:
    from .models import DashboardReport, ReadingItem

RESOLVER_VERSION = "2"
CROSSREF_URL = "https://api.crossref.org/works"
TIMEOUT_SECONDS = 8
MAX_REQUESTS = 20
MAX_CONCURRENCY = 3
CACHE_DAYS = 30
_DOI = re.compile(r"10\.\d{4,9}/[^\s<>\"'{}\\]+", re.I)
_URL = re.compile(r"https?://[^\s<>\"'{}\\]+", re.I)
_ARXIV = re.compile(
    r"\barxiv\s*:\s*(\d{2}(?:0[1-9]|1[0-2])\.\d{4,5}(?:v\d+)?"
    r"|[a-z][a-z0-9.-]*/\d{7}(?:v\d+)?)(?!\w|\.\d)",
    re.I,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _words(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", html.unescape(re.sub(r"<[^>]+>", "", text)))
    return re.findall(r"[^\W_]+", text.casefold())


def _trim_destination(value: str) -> str:
    value = value.rstrip(".,;:!?。；，")
    for opening, closing in (("(", ")"), ("[", "]")):
        while value.endswith(closing) and value.count(closing) > value.count(opening):
            value = value[:-1]
    return value


def _doi_url(doi: str) -> str:
    return "https://doi.org/" + quote(_trim_destination(doi), safe="/()[]:;_-")


def _safe_url(value: str) -> bool:
    """Accept only complete HTTP destinations; never make credentials clickable."""
    if re.search(r"[\s<>\"'{}\\\x00-\x1f\x7f]", value) or value.endswith(("...", "…")):
        return False
    try:
        url = urlsplit(value)
        valid = bool(
            url.scheme.lower() in {"http", "https"}
            and url.hostname
            and not url.username
            and not url.password
            and url.port != 0
        )
        if valid and url.hostname.lower() in {"doi.org", "dx.doi.org"}:
            path = unquote(url.path.lstrip("/"))
            valid = bool(_DOI.fullmatch(path) and not path.endswith("/"))
        return valid
    except ValueError:
        return False


def _possibly_wrapped_plain_link(text: str, end: int, raw: str) -> bool:
    return bool(not re.search(r"[.,;!?)\]]$", raw) and re.match(r"\r?\n[ \t]*\S", text[end:]))


def _direct_dois(text: str) -> list[str]:
    # A DOI is case-insensitive. An identifier printed in both a DOI field and
    # a URL must be one candidate, even when their letter case differs.
    results: dict[str, str] = {}

    def mask_other_url(match: re.Match) -> str:
        value = _trim_destination(match.group())
        try:
            if _safe_url(value) and urlsplit(value).hostname.lower() in {"doi.org", "dx.doi.org"}:
                return match.group()
        except ValueError:
            pass
        return " " * len(match.group())

    text = _URL.sub(mask_other_url, text)
    for match in _DOI.finditer(text):
        raw = match.group()
        if (re.search(r"(?:\.{3}|…)[)\].,;!?]*$", raw)
                or _possibly_wrapped_plain_link(text, match.end(), raw)
                or (match.end() < len(text) and text[match.end()] == "\\")):
            continue
        value = _trim_destination(unquote(raw))
        if not _DOI.fullmatch(value) or value.endswith("/") or re.search(r"%(?![0-9a-f]{2})", raw, re.I):
            continue
        results.setdefault(value.casefold(), value)
    return list(results.values())


def _normalize_tex_urls(text: str) -> str:
    # A closed TeX URL field determines the continuation boundary; plain text
    # split at a line break is not joined because its missing characters are unknown.
    return re.sub(
        r"\\(?:url|href)\s*\{[^{}]*\}",
        lambda match: re.sub(r"\r?\n[ \t]*", "", match.group()),
        text,
        flags=re.I,
    )


def _is_search_url(value: str) -> bool:
    try:
        url = urlsplit(value)
        host = (url.hostname or "").lower()
        return (
            host in {"search.crossref.org", "scholar.google.com"}
            or (host == "google.com" or host.endswith(".google.com")) and url.path == "/search"
            or host in {"arxiv.org", "www.arxiv.org"} and url.path.startswith("/search")
        )
    except ValueError:
        return False


def _reference_text(item: ReadingItem, blocks: dict) -> str:
    if item.content and item.content.strip():
        return item.content.strip()
    return _source_text(item, blocks)


def _source_text(item: ReadingItem, blocks: dict) -> str:
    # Reading offsets are UTF-16, including for supplementary-plane characters.
    return "".join(
        part.join
        + blocks[part.block_id]
        .text.encode("utf-16-le")[part.start * 2 : part.end * 2]
        .decode("utf-16-le")
        for part in item.parts
        if part.block_id in blocks
    ).strip()


def _citation(text: str) -> dict:
    text = re.sub(r"^\s*(?:\[\d+\]|\d+[.)])\s*", "", text)
    years = re.findall(r"\b(?:18|19|20)\d{2}\b", text)
    # Useful for audit and search; acceptance independently checks the complete
    # title in the citation so author initials cannot become a guessed title.
    before_type = re.split(r"\[[JMCRDBNP](?:/[^\]]+)?\]", text, maxsplit=1, flags=re.I)
    title = ""
    if len(before_type) > 1:
        title = re.split(r"[.。]\s*", before_type[0])[-1].strip()
    else:
        parts = re.split(r"[.。]\s+", text)
        for part in parts[1:]:
            if len(_words(part)) >= 2 and re.search(r"[a-z\u4e00-\u9fff]", part):
                title = part.strip()
                break
    return {"text": text, "title": title, "year": int(years[-1]) if years else None}


def _title_position(title: str, citation: str) -> int | None:
    words = _words(title)
    if len("".join(words)) < 6:
        return None
    # A result whose title is merely a prefix of the cited title is unsafe.
    pattern = r"(?<!\w)" + r"[\W_]*".join(re.escape(word) for word in words)
    pattern += r"\s*(?:\[[JMCRDBNP](?:/[^\]]+)?\]|[.。]|$)"
    citation = html.unescape(re.sub(r"<[^>]+>", "", citation))
    match = re.search(pattern, unicodedata.normalize("NFKC", citation), re.I)
    return match.start() if match else None


def _first_author_matches(family: str, prefix: str) -> bool:
    first = re.split(r"[,;，；]|\band\b|\bet\s+al\b", prefix, maxsplit=1, flags=re.I)[0]
    words, surname = _words(first), _words(family)
    if not surname:
        return False
    # Surname-first citations, and initials-first citations (J. K. Smith).
    for offset in range(min(3, len(words))):
        if words[offset : offset + len(surname)] == surname:
            return all(len(word) == 1 for word in words[:offset])
    return False


def _publication_years(work: dict) -> set[int]:
    years = set()
    for key in ("published", "published-print", "published-online", "issued"):
        dates = work.get(key, {}).get("date-parts", [])
        if dates and dates[0] and isinstance(dates[0][0], int):
            years.add(dates[0][0])
    return years


def _match(work: dict, citation: dict) -> dict | None:
    doi = work.get("DOI", "")
    if not isinstance(doi, str) or not _DOI.fullmatch(doi):
        return None
    authors = work.get("author", [])
    if not authors or citation["year"] not in _publication_years(work):
        return None
    for title in work.get("title", []):
        if not isinstance(title, str):
            continue
        if not citation["title"] or _words(title) != _words(citation["title"]):
            continue
        position = _title_position(title, citation["text"])
        family = authors[0].get("family", "")
        if position is None or not _first_author_matches(family, citation["text"][:position]):
            continue
        # Page metadata, when both sides provide a range, also distinguishes
        # chapters/editions which otherwise have identical bibliographic fields.
        pages = re.search(r"\b(\d+)\s*[-–—]\s*(\d+)\s*[.。]?\s*$", citation["text"])
        if pages and work.get("page"):
            if _words(work["page"]) != list(pages.groups()):
                continue
        return {
            "href": _doi_url(doi),
            "matched_title": title,
            "matched_first_author": family,
            "matched_year": citation["year"],
            "matched_type": work.get("type", ""),
            "matched_pages": work.get("page", ""),
        }
    return None


def _lookup(client: httpx.Client, citation: dict) -> dict:
    result = {"state": "unmatched", "href": "", "checked_at": _now(), "source_urls": []}
    try:
        response = client.get(
            CROSSREF_URL,
            params={"query.bibliographic": citation["text"], "rows": 10},
            timeout=TIMEOUT_SECONDS,
        )
        result["source_urls"] = [str(response.request.url)]
        response.raise_for_status()
        matches = {}
        for work in response.json()["message"]["items"]:
            match = _match(work, citation)
            if match:
                matches[match["href"]] = match
        if len(matches) == 1:
            match = next(iter(matches.values()))
            result.update(match, state="resolved")
            result["source_urls"].append(match["href"])
        elif matches:
            result["state"] = "ambiguous"
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, IndexError):
        result["state"] = "network_error"
    return result


def _cached(path: Path, content_hash: str) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        result = payload["result"]
        if payload["version"] != RESOLVER_VERSION or payload["content_hash"] != content_hash:
            return None
        if datetime.now(UTC) - datetime.fromisoformat(result["checked_at"]) > timedelta(
            days=CACHE_DAYS
        ):
            return None
        if result["state"] not in {"resolved", "unmatched", "ambiguous"}:
            return None
        if result["state"] == "resolved" and not re.fullmatch(
            r"https://doi\.org/10\.\d{4,9}/[^\s<>]+", result["href"], re.I
        ):
            return None
        if result["state"] != "resolved" and result["href"]:
            return None
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return None


def resolve_references(
    report: DashboardReport,
    cache_dir: str | Path,
    allow_network: bool = True,
    max_requests: int = MAX_REQUESTS,
    *,
    client: httpx.Client | None = None,
) -> dict:
    """Assign verified destinations and return a serializable provenance audit.

    Call only from explicit generation/rebuild. Offline mode may reuse previously
    verified cache entries but never sends requests. Existing hrefs are preserved.
    Failed lookups remain empty and do not interrupt generation.
    """
    audit = {"version": RESOLVER_VERSION, "checked_at": _now(), "requests": 0, "entries": []}
    if report.reading is None:
        return audit
    blocks = {block.id: block for block in report.document.blocks}
    cache_dir = Path(cache_dir) / "reference-links"
    pending: dict[str, list[tuple]] = {}
    for item in report.reading.references:
        text = _reference_text(item, blocks)
        entry = {
            "reference_id": item.id,
            "state": "unmatched",
            "href": item.href,
            "checked_at": "",
            "source_urls": [],
            "cached": False,
        }
        audit["entries"].append(entry)
        if item.reference_number:
            entry["reference_number"] = item.reference_number
        if item.href:
            entry.update(state="existing_search" if _is_search_url(item.href) else "existing",
                         source_urls=[item.href])
            continue
        # Semantic items can share broad source evidence with neighbouring
        # references. Only this item's own text may supply a destination.
        text = _normalize_tex_urls(text)
        dois = _direct_dois(text)
        urls = list(dict.fromkeys(
            url for match in _URL.finditer(text)
            if not re.search(r"(?:\.{3}|…)[)\].,;!?]*$", match.group())
            and not _possibly_wrapped_plain_link(text, match.end(), match.group())
            and (match.end() == len(text) or text[match.end()] != "\\")
            and _safe_url(url := _trim_destination(match.group()))
        ))
        direct_urls = [url for url in urls if not _is_search_url(url)]
        arxiv = list(dict.fromkeys(match.group(1) for match in _ARXIV.finditer(text)))
        if len(dois) > 1 or (not dois and len(direct_urls) > 1) or (
            not dois and not direct_urls and len(arxiv) > 1
        ):
            entry["state"] = "ambiguous"
            continue
        if dois or direct_urls or arxiv:
            item.href = (_doi_url(dois[0]) if dois else direct_urls[0] if direct_urls
                         else "https://arxiv.org/abs/" + arxiv[0])
            entry.update(state="direct", href=item.href, source_urls=[item.href], checked_at=_now())
            continue
        if urls:
            item.href = urls[0]
            entry.update(state="search", href=item.href, source_urls=[item.href], checked_at=_now())
            continue
        citation = _citation(text)
        entry.update(title=citation["title"], year=citation["year"])
        content_hash = hashlib.sha256((RESOLVER_VERSION + "\n" + text).encode()).hexdigest()
        path = cache_dir / (content_hash + ".json")
        cached = _cached(path, content_hash)
        if cached:
            entry.update(cached, cached=True)
            item.href = cached["href"]
        elif not citation["year"] or not citation["title"] or not text:
            entry["state"] = "insufficient_metadata"
        elif not allow_network:
            entry["state"] = "offline"
        else:
            pending.setdefault(content_hash, []).append((item, entry, citation, path))

    own_client = client is None
    if pending:
        client = client or httpx.Client(headers={"User-Agent": "PaperLens/0.3 (reference lookup)"})
        try:
            jobs = list(pending.items())
            allowed = jobs[: max(0, min(max_requests, MAX_REQUESTS))]
            for _, references in jobs[len(allowed) :]:
                for _, entry, _, _ in references:
                    entry["state"] = "request_limit"
            audit["requests"] = len(allowed)
            with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as pool:
                results = pool.map(lambda job: _lookup(client, job[1][0][2]), allowed)
                for (content_hash, references), result in zip(allowed, results, strict=True):
                    for item, entry, _, _ in references:
                        entry.update(result)
                        if result["state"] == "resolved":
                            item.href = result["href"]
                    if result["state"] != "network_error":
                        try:
                            cache_dir.mkdir(parents=True, exist_ok=True)
                            references[0][3].write_text(
                                json.dumps(
                                    {
                                        "version": RESOLVER_VERSION,
                                        "content_hash": content_hash,
                                        "result": result,
                                    },
                                    ensure_ascii=False,
                                ),
                                encoding="utf-8",
                            )
                        except OSError:
                            pass
        finally:
            if own_client:
                client.close()
    return audit
