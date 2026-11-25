"""Resolve public paper identifiers to selectable sources, then download a chosen PDF.

Source patterns follow EasyRead's sources.py (MIT, LiangXiLin): arXiv, DOI,
publisher citation meta tags and direct PDFs. Discovery never runs a language model.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx

from .errors import InputError
from .storage import write_json

ARXIV = re.compile(r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z]{2})?/\d{7})(?:v\d+)?", re.I)
DOI = re.compile(r"10\.\d{4,9}/[^\s<>\"]+", re.I)
ATOM = {"a": "http://www.w3.org/2005/Atom"}


def _public_addresses(url: str) -> list[str]:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise InputError("请使用公开论文的 HTTP 或 HTTPS 链接。")
    try:
        addresses = socket.getaddrinfo(
            parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
        )
        resolved = list(dict.fromkeys(row[4][0] for row in addresses))
        if not resolved or any(not ipaddress.ip_address(ip).is_global or ipaddress.ip_address(ip).is_multicast for ip in resolved):
            raise InputError("网络导入只支持公开论文地址；本机文件请直接上传。")
    except (OSError, ValueError) as exc:
        raise InputError("无法连接这个论文地址，请检查网址。") from exc
    return resolved


def public_url(url: str) -> str:
    _public_addresses(url)
    return url


class PinnedPublicTransport(httpx.HTTPTransport):
    """Connect to the vetted IP while retaining the publisher's HTTP host and TLS identity."""

    def __init__(self):
        super().__init__(trust_env=False, limits=httpx.Limits(max_keepalive_connections=0))

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        addresses = _public_addresses(str(request.url))
        # IPv4 first on hosts with partial IPv6 connectivity.
        address = min(addresses, key=lambda ip: ipaddress.ip_address(ip).version)
        pinned = httpx.Request(
            request.method,
            request.url.copy_with(host=address),
            headers=request.headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": request.url.host},
        )
        return super().handle_request(pinned)


class CitationPage(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, list[str]] = {}
        self.title = ""
        self.in_title = False
        self.abstract = ""
        self.abstract_tags: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            name = (attrs.get("name") or attrs.get("property") or "").lower()
            if name and attrs.get("content"):
                self.meta.setdefault(name, []).append(attrs["content"].strip())
        if tag == "title":
            self.in_title = True
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            if self.abstract_tags or re.search(
                r"(?:^|[\s_-])abstract(?:$|[\s_-])",
                (attrs.get("id") or "") + " " + (attrs.get("class") or ""),
                re.I,
            ):
                self.abstract_tags.append(tag)

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if self.abstract_tags and tag == self.abstract_tags[-1]:
            self.abstract_tags.pop()

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        if self.abstract_tags:
            self.abstract += data

    def value(self, *names):
        return next((self.meta[n][0] for n in names if self.meta.get(n)), "")


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", value))).strip()


class PaperImporter:
    def __init__(self, cache_dir: Path, *, transport=None, pin_public=False):
        self.root = cache_dir / "paper-imports"
        self.transport = transport
        self.pin_public = pin_public

    def _fetch(self, url: str, *, max_bytes=3 * 1024 * 1024, pdf=False):
        # No user credentials/cookies accompany public-source requests. Validate redirects too.
        with httpx.Client(
            transport=PinnedPublicTransport() if self.pin_public else self.transport,
            timeout=20,
            headers={"User-Agent": "PaperLens/0.3 (public paper import)"},
        ) as client:
            for _ in range(6):
                if not self.pin_public:
                    public_url(url)
                with client.stream("GET", url) as response:
                    if response.is_redirect:
                        url = urljoin(url, response.headers.get("location", ""))
                        continue
                    response.raise_for_status()
                    kind = response.headers.get("content-type", "").lower()
                    data = bytearray()
                    for chunk in response.iter_bytes(65536):
                        data.extend(chunk)
                        if not pdf and ("application/pdf" in kind or data[:5] == b"%PDF-"):
                            return bytes(data[:1024]), url, "application/pdf"
                        if len(data) > max_bytes:
                            raise InputError("文件超过当前导入大小上限，请使用较小的 PDF。")
                    return bytes(data), url, kind
        raise InputError("论文链接跳转次数过多，请粘贴最终页面地址。")

    def _json(self, url: str) -> dict:
        return json.loads(self._fetch(url)[0])

    def _arxiv(self, ident: str = "", title: str = "") -> list[dict]:
        query = (
            "id_list=" + quote(ident, safe="")
            if ident
            else "search_query=" + quote('ti:"' + title.replace('"', "") + '"', safe="")
        )
        try:
            raw = self._fetch("https://export.arxiv.org/api/query?" + query + "&max_results=5")[0]
            feed = ET.fromstring(raw)
            rows = []
            for entry in feed.findall("a:entry", ATOM):
                link = entry.findtext("a:id", "", ATOM)
                if "/abs/" not in link:
                    continue
                ident_found = link.split("/abs/", 1)[1]
                rows.append(
                    {
                        "title": clean(entry.findtext("a:title", "", ATOM)),
                        "authors": [
                            clean(a.findtext("a:name", "", ATOM))
                            for a in entry.findall("a:author", ATOM)
                        ],
                        "year": entry.findtext("a:published", "", ATOM)[:4],
                        "venue": "arXiv",
                        "abstract": clean(entry.findtext("a:summary", "", ATOM)),
                        "url": "https://arxiv.org/abs/" + ident_found,
                        "pdf_url": "https://arxiv.org/pdf/" + ident_found,
                        "doi": "",
                    }
                )
            return rows
        except (httpx.HTTPError, ValueError, ET.ParseError, InputError):
            if ident:
                return [self._page("https://arxiv.org/abs/" + ident)]
            return []

    def _crossref(self, *, doi: str = "", title: str = "") -> list[dict]:
        url = (
            "https://api.crossref.org/works/" + quote(doi, safe="")
            if doi
            else "https://api.crossref.org/works?rows=5&query.bibliographic="
            + quote(title, safe="")
        )
        raw = self._json(url)["message"]
        rows = []
        for work in [raw] if doi else raw.get("items", []):
            name = next(iter(work.get("title", [])), "")
            if not name:
                continue
            date = next(
                (
                    work[k].get("date-parts", [[]])[0]
                    for k in ("published", "published-print", "published-online")
                    if work.get(k)
                ),
                [],
            )
            links = work.get("link", [])
            rows.append(
                {
                    "title": clean(name),
                    "authors": [
                        clean(" ".join(filter(None, [a.get("given"), a.get("family")])))
                        or a.get("name", "")
                        for a in work.get("author", [])
                    ],
                    "year": str(date[0]) if date else "",
                    "venue": next(iter(work.get("container-title", [])), ""),
                    "abstract": clean(work.get("abstract", "")),
                    "doi": work.get("DOI", ""),
                    "url": "https://doi.org/" + work["DOI"],
                    "pdf_url": next(
                        (x["URL"] for x in links if "pdf" in x.get("content-type", "").lower()), ""
                    ),
                }
            )
        return rows

    def _page(self, url: str) -> dict:
        raw, final, kind = self._fetch(url)
        if "application/pdf" in kind:
            return {
                "title": unquote(urlsplit(final).path.rsplit("/", 1)[-1]) or "PDF 论文",
                "url": final,
                "pdf_url": final,
            }
        page = CitationPage()
        page.feed(raw.decode("utf-8", errors="replace"))
        pdf = page.value("citation_pdf_url", "eprints.document_url")
        # OpenReview exposes a stable PDF endpoint for each forum identifier.
        if (
            not pdf
            and urlsplit(final).hostname in {"openreview.net", "www.openreview.net"}
            and "/forum?" in final
        ):
            pdf = final.replace("/forum?", "/pdf?", 1)
        return {
            "title": clean(page.value("citation_title", "dc.title", "og:title") or page.title)
            or "论文页面",
            "authors": page.meta.get("citation_author", page.meta.get("dc.creator", [])),
            "year": page.value("citation_publication_date", "citation_date", "dc.date")[:4],
            "venue": page.value("citation_journal_title", "citation_conference_title"),
            "abstract": re.sub(
                r"^Abstract\s*:?\s*",
                "",
                clean(page.value("citation_abstract", "dc.description") or page.abstract),
                flags=re.I,
            ),
            "doi": page.value("citation_doi", "dc.identifier.doi"),
            "url": final,
            "pdf_url": urljoin(final, pdf) if pdf else "",
        }

    def resolve(self, query: str) -> dict:
        query = query.strip()
        if not query or len(query) > 2000:
            raise InputError("请输入论文标题、DOI、arXiv 编号或论文网址。")
        warnings = []
        try:
            short = re.sub(r"^arxiv\s*:\s*", "", query, flags=re.I)
            link = urlsplit(query)
            arxiv_id = ARXIV.fullmatch(short)
            if link.hostname and any(
                link.hostname == host or link.hostname.endswith("." + host)
                for host in ("arxiv.org", "alphaxiv.org", "huggingface.co")
            ):
                arxiv_id = ARXIV.search(link.path)
            doi = DOI.fullmatch(
                re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)", "", query, flags=re.I)
            )
            if arxiv_id:
                rows = self._arxiv(ident=arxiv_id[0])
            elif doi:
                rows = self._crossref(doi=doi[0])
                if rows and not rows[0]["pdf_url"]:
                    try:
                        page = self._page(rows[0]["url"])
                        rows[0]["pdf_url"] = page["pdf_url"]
                        rows[0]["abstract"] = rows[0]["abstract"] or page.get("abstract", "")
                    except (httpx.HTTPError, InputError, ValueError):
                        pass
            elif link.scheme in {"http", "https"}:
                if link.path.lower().endswith(".pdf"):
                    public_url(query)
                    rows = [
                        {
                            "title": unquote(link.path.rsplit("/", 1)[-1]),
                            "url": query,
                            "pdf_url": query,
                        }
                    ]
                else:
                    rows = [self._page(query)]
            elif re.match(r"^(?:file|ftp|data|javascript|mailto):", query, re.I) or "://" in query:
                raise InputError("请上传本地 PDF，或粘贴公开的 HTTP/HTTPS 论文链接。")
            else:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    searches = [
                        pool.submit(self._arxiv, title=query),
                        pool.submit(self._crossref, title=query),
                    ]
                    rows = []
                    for future in searches:
                        try:
                            rows.extend(future.result())
                        except (httpx.HTTPError, InputError, ValueError, KeyError):
                            warnings.append("部分来源暂时无法连接。")
        except httpx.HTTPError as exc:
            raise InputError("论文来源暂时无法访问，请稍后重试，或下载 PDF 后上传。") from exc
        except (ValueError, KeyError, ET.ParseError) as exc:
            raise InputError("未能识别论文来源，请检查链接或换用标题检索。") from exc
        unique = {}
        for row in rows:
            key = re.sub(r"\W", "", row["title"]).casefold()
            if key not in unique or (row.get("pdf_url") and not unique[key].get("pdf_url")):
                unique[key] = row
        candidates = list(unique.values())[:8]
        for row in candidates:
            row["id"] = uuid.uuid4().hex
            write_json(self.root / (row["id"] + ".json"), row)
        return {
            "candidates": candidates,
            "message": "找到论文后，选择一项开始处理。"
            if candidates
            else "没有找到匹配论文，可换用 DOI、链接或上传 PDF。" + " ".join(warnings),
        }

    def download(self, candidate_id: str, destination: Path, max_bytes: int) -> dict:
        if not re.fullmatch(r"[a-f0-9]{32}", candidate_id):
            raise InputError("请先查找并选择论文。")
        try:
            candidate = json.loads(
                (self.root / (candidate_id + ".json")).read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise InputError("论文候选已失效，请重新查找。") from exc
        if not candidate.get("pdf_url"):
            raise InputError("未找到公开 PDF，请从论文页面获取文件后上传。")
        try:
            raw, _, _ = self._fetch(candidate["pdf_url"], max_bytes=max_bytes, pdf=True)
        except httpx.HTTPError as exc:
            raise InputError("PDF 下载失败，请从来源页面获取文件后上传。") from exc
        if b"%PDF-" not in raw[:1024]:
            raise InputError("来源返回的是网页或登录页，请下载实际 PDF 后上传。")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
        return candidate
