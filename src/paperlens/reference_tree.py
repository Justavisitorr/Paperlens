"""Read public outbound citations on demand. Never infer or crawl citation edges."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import httpx

from .reference_links import (
    _ARXIV,
    _DOI,
    _citation,
    _reference_text,
    _title_position,
    _trim_destination,
    _words,
)
from .storage import write_json

API = "https://api.openalex.org"
PAGE_SIZE = 20
CACHE_VERSION = 1
SUMMARY_FIELDS = "id,doi,title,publication_year,referenced_works_count"
WORK_FIELDS = SUMMARY_FIELDS + ",referenced_works"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def _now() -> str:
    return datetime.now(UTC).isoformat()


def work_id(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return (
        value.rsplit("/", 1)[-1]
        if re.fullmatch(r"(?:https://openalex\.org/)?W[1-9][0-9]{0,15}", value)
        else ""
    )


def _node(data: dict) -> dict | None:
    identity = work_id(data.get("id"))
    if not identity:
        return None
    doi = data.get("doi") or ""
    doi = (
        doi
        if isinstance(doi, str)
        and re.fullmatch(r"https://doi\.org/10\.\d{4,9}/[^\s<>\"']+", doi, re.I)
        else ""
    )
    year = data.get("publication_year")
    count = data.get("referenced_works_count")
    return {
        "id": identity,
        "title": str(data.get("title") or identity)[:1200],
        "year": year if isinstance(year, int) and 1000 <= year <= 3000 else None,
        "url": doi or f"https://openalex.org/{identity}",
        "reference_count": count if isinstance(count, int) and count >= 0 else None,
    }


def _identifiers(text: str, href: str) -> list[str]:
    """Only interpret identifiers; never fetch a link printed in a document."""
    values: list[str] = []
    for source in (text, href):
        for match in _DOI.finditer(unquote(source)):
            doi = _trim_destination(match.group()).lower()
            if len(doi) <= 300:
                values.append("doi:" + doi)
        arxiv = [match.group(1) for match in _ARXIV.finditer(source)]
        try:
            parsed = urlsplit(source)
            if parsed.hostname in {"arxiv.org", "www.arxiv.org"}:
                match = re.fullmatch(r"/(?:abs|pdf)/(.+?)(?:\.pdf)?/?", parsed.path)
                if match:
                    candidate = _ARXIV.fullmatch("arxiv:" + match[1])
                    if candidate:
                        arxiv.append(candidate[1])
        except ValueError:
            pass
        for identifier in arxiv:
            values.append("doi:10.48550/arxiv." + re.sub(r"v\d+$", "", identifier.lower()))
    return list(dict.fromkeys(values))


def _exact_citation(work: dict, citation: dict) -> bool:
    """A title search is a candidate list, not proof that it is the cited work."""
    position = _title_position(work.get("title") or "", citation["text"])
    if position is None:
        return False
    if work.get("publication_year") != citation["year"]:
        return False
    authors = work.get("authorships") or []
    if not authors:
        return False
    name = authors[0].get("author", {}).get("display_name") or ""
    words = _words(name)
    if not words:
        return False
    # Match the leading citation author, allowing initials or a full given name.
    prefix = re.split(
        r"[,;，；]|\band\b|\bet\s+al\b", citation["text"][:position], maxsplit=1, flags=re.I
    )[0]
    prefix_words = _words(prefix)
    surname = words[-1]
    if surname not in prefix_words[:5]:
        return False
    preceding = prefix_words[: prefix_words.index(surname)]
    return all(len(word) == 1 or word in words[:-1] for word in preceding)


class LookupFailure(Exception):
    def __init__(self, state: str, message: str):
        self.state, self.message = state, message
        super().__init__(message)


class ReferenceTreeService:
    """Per-workspace cache and coalesced bounded requests, with no background crawl."""

    def __init__(
        self,
        cache_dir: Path | Callable[[], Path],
        *,
        client: httpx.Client | None = None,
        allow_network: Callable[[], bool] = lambda: True,
    ):
        self.cache_dir = cache_dir
        self.client = client
        self.allow_network = allow_network
        self._locks = [threading.Lock() for _ in range(32)]
        self._requests = threading.BoundedSemaphore(3)

    def _path(self, key: str) -> Path:
        root = self.cache_dir() if callable(self.cache_dir) else self.cache_dir
        digest = hashlib.sha256(key.encode()).hexdigest()
        return Path(root) / "reference-tree" / (digest + ".json")

    def _cached(self, key: str):
        try:
            payload = json.loads(self._path(key).read_text(encoding="utf-8"))
            if payload["version"] != CACHE_VERSION:
                return None
            if datetime.now(UTC) - datetime.fromisoformat(payload["checked_at"]) > timedelta(
                days=7
            ):
                return None
            return payload
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _get(self, path: str, params: dict) -> dict:
        # The only network destination is a fixed public API. Redirects are disabled.
        if not self.allow_network():
            raise LookupFailure(
                "disabled", "公开书目查询已关闭。可在设置中开启后查看新的引用关系。"
            )

        def request(client: httpx.Client):
            try:
                with client.stream(
                    "GET", API + path, params=params, timeout=10, follow_redirects=False
                ) as response:
                    if response.status_code == 404:
                        raise LookupFailure(
                            "not_found", "OpenAlex 尚未收录这篇文献，或记录已移除。"
                        )
                    if response.status_code in {401, 402, 403, 409, 429}:
                        raise LookupFailure(
                            "rate_limited", "公开书目服务暂时限流或匿名额度不足，请稍后重试。"
                        )
                    response.raise_for_status()
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            raise LookupFailure("error", "书目服务返回的数据过大，请稍后重试。")
                        chunks.append(chunk)
                    value = json.loads(b"".join(chunks))
                    if not isinstance(value, dict):
                        raise ValueError("invalid response")
                    return value
            except (httpx.HTTPError, ValueError) as exc:
                raise LookupFailure("error", "暂时无法读取公开书目，请重试。") from exc

        with self._requests:
            if self.client is not None:
                return request(self.client)
            with httpx.Client(headers={"User-Agent": "PaperLens/1.0 (citation tree)"}) as client:
                return request(client)

    def _load(
        self, key: str, fetch: Callable[[], dict], *, refresh=False
    ) -> tuple[dict, bool, str]:
        cached = None if refresh else self._cached(key)
        if cached is not None:
            return cached["data"], True, cached["checked_at"]
        lock = self._locks[int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % len(self._locks)]
        with lock:
            cached = None if refresh else self._cached(key)
            if cached is not None:
                return cached["data"], True, cached["checked_at"]
            data = fetch()
            checked_at = _now()
            try:
                write_json(
                    self._path(key),
                    {"version": CACHE_VERSION, "checked_at": checked_at, "data": data},
                )
            except OSError:
                pass  # A read-only cache must not hide a successful public lookup.
            return data, False, checked_at

    def _resolve(self, text: str, href: str) -> dict:
        identifiers = _identifiers(text, href)
        if len(identifiers) > 1:
            raise LookupFailure("ambiguous", "这条引用含多个不同文献标识，暂不能确定树的起点。")
        if identifiers:
            identity = identifiers[0].removeprefix("doi:")
            return self._get(
                "/works/https://doi.org/" + quote(identity, safe=""), {"select": WORK_FIELDS}
            )
        citation = _citation(text)
        if not citation["year"] or len(_words(citation["text"])) < 4:
            raise LookupFailure(
                "unresolved", "这条引用缺少可核实的 DOI、arXiv 编号，或完整标题、作者和年份。"
            )
        result = self._get(
            "/works",
            {
                # Initials make heuristic title splitting unreliable. Search only the
                # public citation, then check its full title, first author and year.
                "search": citation["text"][:500],
                "filter": f"publication_year:{citation['year']}",
                "per_page": 5,
                "select": WORK_FIELDS + ",authorships",
            },
        )
        matches = {
            work_id(work.get("id")): work
            for work in result.get("results", [])
            if isinstance(work, dict)
            and work_id(work.get("id"))
            and _exact_citation(work, citation)
        }
        if len(matches) != 1:
            raise LookupFailure(
                "ambiguous" if matches else "unresolved",
                "未能唯一核实这条引用的标题、首位作者和年份；请从原文链接核对。",
            )
        return next(iter(matches.values()))

    def branch(
        self, text: str, href: str = "", *, node: str = "", offset: int = 0, refresh=False
    ) -> dict:
        if node and not work_id(node):
            raise ValueError("文献标识无效。")
        if offset < 0 or offset > 10000 or offset % PAGE_SIZE:
            raise ValueError("引用分页位置无效。")
        result = {
            "state": "available",
            "message": "",
            "node": None,
            "references": [],
            "offset": offset,
            "next_offset": None,
            "total": None,
            "cached": False,
            "checked_at": "",
            "source": "OpenAlex",
            "source_url": "",
        }
        try:
            key = "work:" + node if node else "root:" + text + "\n" + href
            data, cached, checked_at = self._load(
                key,
                lambda: (
                    self._get("/works/" + node, {"select": WORK_FIELDS})
                    if node
                    else self._resolve(text, href)
                ),
                refresh=refresh,
            )
            current = _node(data)
            if current is None:
                raise LookupFailure("error", "书目服务返回了无法识别的文献记录，请重试。")
            references = data.get("referenced_works") or []
            if not isinstance(references, list):
                raise LookupFailure("error", "书目服务返回了无法识别的引用记录，请重试。")
            refs = list(dict.fromkeys(work_id(value) for value in references if work_id(value)))
            result.update(
                node=current,
                total=len(refs),
                cached=cached,
                checked_at=checked_at,
                source_url=API + "/works/" + current["id"],
            )
            if not refs:
                result.update(
                    state="unavailable",
                    message="OpenAlex 未提供这篇文献的参考文献记录；这不代表原论文没有引用。",
                )
                return result
            selected = refs[offset : offset + PAGE_SIZE]
            if selected:
                response, summaries_cached, _ = self._load(
                    "summaries:" + "|".join(selected),
                    lambda: self._get(
                        "/works",
                        {
                            "filter": "openalex_id:" + "|".join(selected),
                            "per_page": PAGE_SIZE,
                            "select": SUMMARY_FIELDS,
                        },
                    ),
                    refresh=refresh,
                )
                summaries = {
                    entry["id"]: entry
                    for work in response.get("results", [])
                    if isinstance(work, dict) and (entry := _node(work))
                }
                # Preserve every provider-reported edge even when a target record was deleted.
                result["references"] = [
                    summaries.get(identity)
                    or {
                        "id": identity,
                        "title": "文献信息暂缺 · " + identity,
                        "year": None,
                        "url": f"https://openalex.org/{identity}",
                        "reference_count": None,
                    }
                    for identity in selected
                ]
                result["cached"] = cached and summaries_cached
            if offset + PAGE_SIZE < len(refs):
                result["next_offset"] = offset + PAGE_SIZE
            return result
        except LookupFailure as exc:
            result.update(state=exc.state, message=exc.message)
            return result


def reference_text(report, item) -> str:
    return _reference_text(item, {block.id: block for block in report.document.blocks})
