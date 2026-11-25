"""Refreshable provider facts, kept separate from user settings and application budgets.

Only explicitly permitted refreshes use the network. An unrecognized document
layout is rejected as a whole; the last validated snapshot remains available.
Nothing here reads API credentials or changes the selected model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .storage import write_json

PRICING_URL = "https://api-docs.deepseek.com/quick_start/pricing/"
MODELS_URL = "https://api-docs.deepseek.com/api/list-models/"
VISION_URL = "https://api-docs.deepseek.com/guides/vision/"
OFFICIAL_BASE_URL = "https://api.deepseek.com"
SNAPSHOT_PATH = Path(__file__).with_name("assets") / "provider-catalog.json"
Capability = Literal["supported", "unsupported", "unknown"]


class CatalogRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TokenPrices(CatalogRecord):
    """USD per one million tokens; periods are independently sourced."""

    input_cache_hit: Decimal = Field(ge=0, le=100000)
    input_cache_miss: Decimal = Field(ge=0, le=100000)
    output: Decimal = Field(ge=0, le=100000)


class ModelInfo(CatalogRecord):
    id: str
    name: str = ""
    provider: str = "unknown"
    context_window_tokens: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    vision: Capability = "unknown"
    json_output: Capability = "unknown"
    input_modalities: list[str] | None = None
    reasoning_efforts: list[str] | None = None
    prices: dict[Literal["off_peak", "peak"], TokenPrices] = Field(default_factory=dict)
    sources: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_limits(self) -> ModelInfo:
        if (
            self.context_window_tokens
            and self.max_output_tokens
            and self.max_output_tokens > self.context_window_tokens
        ):
            raise ValueError("Output limit exceeds context window")
        return self


class SourceRecord(CatalogRecord):
    url: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CatalogSnapshot(CatalogRecord):
    schema_version: Literal[1] = 1
    provider: Literal["deepseek"] = "deepseek"
    base_url: Literal["https://api.deepseek.com"] = OFFICIAL_BASE_URL
    currency: Literal["USD"] = "USD"
    checked_at: datetime
    models: list[ModelInfo]
    sources: list[SourceRecord]

    @model_validator(mode="after")
    def validate_snapshot(self) -> CatalogSnapshot:
        if self.checked_at.tzinfo is None:
            raise ValueError("checked_at must have a timezone")
        identifiers = [model.id for model in self.models]
        if not identifiers or len(identifiers) != len(set(identifiers)):
            raise ValueError("Snapshot requires unique models")
        if any(model.provider != self.provider for model in self.models):
            raise ValueError("Model and catalog providers differ")
        if {source.url for source in self.sources} != {PRICING_URL, MODELS_URL, VISION_URL}:
            raise ValueError("Snapshot requires all official sources")
        return self


class CatalogState(CatalogSnapshot):
    status: Literal["live", "cache", "bundled", "stale"]
    stale: bool
    expires_at: datetime
    network_checked: bool = False
    warnings: list[str] = Field(default_factory=list)


class CatalogParseError(ValueError):
    """Official pages no longer match the supported, validated structure."""


def is_official_deepseek(base_url: str) -> bool:
    try:
        parsed = urlsplit(base_url)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "api.deepseek.com"
            and parsed.port in (None, 443)
            and not parsed.username
            and not parsed.password
            and parsed.path.rstrip("/") in ("", "/v1")
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False


class _Page(HTMLParser):
    """Collect actual HTML tables/code; never use a page-wide money regex."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[tuple[str, int, int]]]] = []
        self.codes: list[str] = []
        self._table: list[list[tuple[str, int, int]]] | None = None
        self._row: list[tuple[str, int, int]] | None = None
        self._cell: list[str] | None = None
        self._span = (1, 1)
        self._code: list[str] | None = None
        self._sup = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            if self._table is not None:
                raise CatalogParseError("Nested tables are not supported")
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            values = dict(attrs)
            self._span = (int(values.get("rowspan") or 1), int(values.get("colspan") or 1))
            if not all(1 <= size <= 100 for size in self._span):
                raise CatalogParseError("Invalid table span")
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")
        elif tag == "sup":
            self._sup += 1
        elif tag == "code":
            self._code = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None and not self._sup:
            self._cell.append(data)
        if self._code is not None:
            self._code.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            text = " ".join("".join(self._cell).split())
            self._row.append((text, *self._span))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None
        elif tag == "sup":
            self._sup = max(0, self._sup - 1)
        elif tag == "code" and self._code is not None:
            self.codes.append("".join(self._code))
            self._code = None


def _expand(rows: list[list[tuple[str, int, int]]]) -> list[list[str]]:
    cells: dict[tuple[int, int], str] = {}
    for y, row in enumerate(rows):
        x = 0
        for value, height, width in row:
            while (y, x) in cells:
                x += 1
            for dy in range(height):
                for dx in range(width):
                    position = (y + dy, x + dx)
                    if position in cells:
                        raise CatalogParseError("Overlapping table cells")
                    cells[position] = value
            x += width
    width = max((x for _, x in cells), default=-1) + 1
    if any(y >= len(rows) for y, _ in cells):
        raise CatalogParseError("Table span extends beyond rows")
    return [[cells.get((y, x), "") for x in range(width)] for y in range(len(rows))]


def _capability(value: str) -> Capability:
    if value.strip() == "✓":
        return "supported"
    if value.strip().lower() == "not supported":
        return "unsupported"
    return "unknown"


def parse_model_list(data: dict[str, Any], *, base_url: str) -> list[ModelInfo]:
    """Parse /models metadata without inferring capabilities from a model name.

    The caller obtains this JSON with its own authenticated provider transport.
    Missing optional metadata stays unknown. Price matching is a separate step.
    """
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise CatalogParseError("Invalid models response")
    result: list[ModelInfo] = []
    seen: set[str] = set()
    for item in data["data"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise CatalogParseError("Invalid model identifier")
        model_id = item["id"]
        if not model_id or model_id in seen:
            raise CatalogParseError("Empty or duplicate model identifier")
        seen.add(model_id)
        modalities = item.get("input_modalities")
        if modalities is not None and (
            not isinstance(modalities, list)
            or not modalities
            or any(not isinstance(value, str) for value in modalities)
        ):
            raise CatalogParseError("Invalid model modalities")
        limits = {}
        for key in ("context_window", "max_output_tokens"):
            value = item.get(key)
            if value is not None and (type(value) is not int or value <= 0):
                raise CatalogParseError("Invalid token limit")
            limits[key] = value
        effort = item.get("effort")
        efforts = effort.get("supported_levels") if isinstance(effort, dict) else None
        if efforts is not None and (
            not isinstance(efforts, list) or any(not isinstance(v, str) for v in efforts)
        ):
            raise CatalogParseError("Invalid effort metadata")
        result.append(
            ModelInfo(
                id=model_id,
                name=item.get("name") or model_id,
                provider="deepseek" if is_official_deepseek(base_url) else "custom",
                context_window_tokens=limits["context_window"],
                max_output_tokens=limits["max_output_tokens"],
                input_modalities=modalities,
                reasoning_efforts=efforts,
                vision=(
                    "unknown"
                    if modalities is None
                    else "supported"
                    if "image" in modalities
                    else "unsupported"
                ),
            )
        )
    return result


def parse_official_pages(pages: dict[str, str], *, checked_at: datetime) -> CatalogSnapshot:
    if set(pages) != {PRICING_URL, MODELS_URL, VISION_URL}:
        raise CatalogParseError("Required source missing")
    parser = _Page()
    parser.feed(pages[PRICING_URL])
    tables = [_expand(table) for table in parser.tables]
    candidates = [table for table in tables if table and table[0][:3] == ["MODEL"] * 3]
    if len(candidates) != 1:
        raise CatalogParseError("Expected one model pricing table")
    table = candidates[0]
    model_ids = table[0][3:]
    if not model_ids or any(not re.fullmatch(r"deepseek-[a-z0-9.-]+", v) for v in model_ids):
        raise CatalogParseError("Unsupported model columns")
    if len(set(model_ids)) != len(model_ids):
        raise CatalogParseError("Duplicate model columns")
    # Parse model metadata from the official JSON example, not guesses such as
    # 1M = one million characters. The current API describes exact token limits.
    metadata_parser = _Page()
    metadata_parser.feed(pages[MODELS_URL])
    metadata: dict[str, ModelInfo] = {}
    for code in metadata_parser.codes:
        try:
            data = json.loads(code)
        except ValueError:
            continue
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            continue
        if not any(isinstance(v, dict) and v.get("id") in model_ids for v in data["data"]):
            continue
        for model in parse_model_list(data, base_url=OFFICIAL_BASE_URL):
            if model.id in metadata:
                raise CatalogParseError("Duplicate documented model metadata")
            metadata[model.id] = model
    prices: dict[str, dict[str, dict[str, Decimal]]] = {v: {} for v in model_ids}
    models = [metadata.get(v, ModelInfo(id=v, name=v, provider="deepseek")) for v in model_ids]
    labels = {
        "1M INPUT TOKENS (CACHE HIT)": "input_cache_hit",
        "1M INPUT TOKENS (CACHE MISS)": "input_cache_miss",
        "1M OUTPUT TOKENS": "output",
    }
    for row in table[1:]:
        if len(row) != len(model_ids) + 3:
            raise CatalogParseError("Pricing columns changed")
        for index, model in enumerate(models):
            value = row[index + 3]
            if row[0] == "PRICING":
                if row[1] not in labels or row[2] not in ("OFF-PEAK", "PEAK"):
                    raise CatalogParseError("Pricing units or periods changed")
                if not re.fullmatch(r"\$\d+(?:\.\d+)?", value):
                    raise CatalogParseError("USD price cell not recognized")
                period = row[2].lower().replace("-", "_")
                target = prices[model.id].setdefault(period, {})
                key = labels[row[1]]
                if key in target:
                    raise CatalogParseError("Duplicate price field")
                target[key] = Decimal(value[1:])
            elif row[0] == "MODEL VERSION":
                model.name = value
            elif row[0] == "FEATURES" and row[1] == "Vision":
                capability = _capability(value)
                if model.vision != "unknown" and capability != model.vision:
                    raise CatalogParseError("Model docs and pricing disagree on vision")
                model.vision = capability
            elif row[0] == "FEATURES" and row[1] == "Json Output":
                model.json_output = _capability(value)
    # The vision guide independently identifies the supported model in its
    # introduction. Fail closed if it disappears instead of trusting old facts.
    vision_model_names = re.findall(r"<code>\s*(deepseek-[a-z0-9.-]+)\s*</code>", pages[VISION_URL])
    for model in models:
        if set(prices[model.id]) != {"off_peak", "peak"}:
            raise CatalogParseError("Incomplete price periods")
        model.prices = {key: TokenPrices(**value) for key, value in prices[model.id].items()}
        if model.vision == "supported" and model.id not in vision_model_names:
            raise CatalogParseError("Vision model absent from guide")
        model.sources = [PRICING_URL, MODELS_URL, VISION_URL]
    return CatalogSnapshot(
        checked_at=checked_at,
        models=models,
        sources=[
            SourceRecord(url=url, sha256=hashlib.sha256(html.encode()).hexdigest())
            for url, html in pages.items()
        ],
    )


def _fetch_pages(client: httpx.Client) -> dict[str, str]:
    pages = {}
    for url in (PRICING_URL, MODELS_URL, VISION_URL):
        # Never follow a source-page redirect to another provider or accept an
        # unbounded response. These public requests contain no credentials.
        with client.stream("GET", url) as response:
            response.raise_for_status()
            if "text/html" not in response.headers.get("content-type", "").lower():
                raise CatalogParseError("Official source is not HTML")
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > 2_000_000:
                    raise CatalogParseError("Official source exceeds size limit")
            pages[url] = content.decode("utf-8")
    return pages


def load_catalog(
    cache_dir: Path,
    *,
    allow_network: bool = False,
    force_refresh: bool = False,
    max_age_hours: float = 168,
    now: datetime | None = None,
    transport: httpx.BaseTransport | None = None,
) -> CatalogState:
    """Read validated cache/snapshot; optionally refresh if requested or expired.

    ``force_refresh`` does not override ``allow_network``. A refresh failure
    preserves the previous file and marks the returned data stale. Call from a
    worker thread when used by an async web route (network work is bounded).
    """
    if not 0 < max_age_hours <= 8760:
        raise ValueError("max_age_hours must be in (0, 8760]")
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("now must have a timezone")
    cache_path = cache_dir / "catalog" / "deepseek.json"
    warnings = []
    snapshot = None
    origin: Literal["cache", "bundled"] = "bundled"
    for path, location in ((cache_path, "cache"), (SNAPSHOT_PATH, "bundled")):
        try:
            snapshot = CatalogSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
            if snapshot.checked_at > current + timedelta(minutes=5):
                raise ValueError("Catalog timestamp is in the future")
            origin = location  # type: ignore[assignment]
            break
        except (OSError, ValueError):
            if path.exists():
                warnings.append(f"invalid_{location}_catalog")
    if snapshot is None:
        raise CatalogParseError("No valid provider catalog is available")
    expires = snapshot.checked_at + timedelta(hours=max_age_hours)
    expired = current >= expires
    network_checked = False
    failed = False
    if allow_network and (force_refresh or expired or origin == "bundled"):
        network_checked = True
        try:
            with httpx.Client(
                timeout=httpx.Timeout(20, connect=10),
                follow_redirects=False,
                transport=transport,
                headers={"User-Agent": "PaperLens-provider-catalog/1"},
            ) as client:
                pages = _fetch_pages(client)
            snapshot = parse_official_pages(pages, checked_at=current)
            write_json(cache_path, snapshot.model_dump(mode="json"))
            return CatalogState(
                **snapshot.model_dump(),
                status="live",
                stale=False,
                expires_at=current + timedelta(hours=max_age_hours),
                network_checked=True,
                warnings=warnings,
            )
        except (httpx.HTTPError, OSError, ValueError) as exc:
            # Do not relay response bodies, URLs, proxy strings or secrets.
            failed = True
            warnings.append(f"refresh_failed:{type(exc).__name__}")
    elif force_refresh and not allow_network:
        warnings.append("network_update_not_authorized")
    if expired:
        warnings.append("catalog_expired")
    warnings.append("prices_are_estimates_not_billing")
    return CatalogState(
        **snapshot.model_dump(),
        status="stale" if expired or failed else origin,
        stale=expired or failed,
        expires_at=expires,
        network_checked=network_checked,
        warnings=warnings,
    )


def get_model(
    catalog: CatalogSnapshot,
    model_id: str,
    *,
    base_url: str = OFFICIAL_BASE_URL,
) -> ModelInfo:
    """Unknown IDs and custom endpoints never silently inherit official prices."""
    if is_official_deepseek(base_url):
        for model in catalog.models:
            if model.id == model_id:
                return model.model_copy(deep=True)
    return ModelInfo(id=model_id, name=model_id)


def estimate_usage_cost(
    usage: dict[str, Any],
    model_id: str,
    catalog: CatalogSnapshot,
    *,
    base_url: str = OFFICIAL_BASE_URL,
) -> dict[str, Any]:
    """Return explicit period scenarios, not a guessed historical invoice.

    No timestamps/holiday calendar are inferred. Unknown cache breakdown gives
    an input-cache range; absent/invalid total tokens gives unknown cost.
    """
    info = get_model(catalog, model_id, base_url=base_url)
    metadata: dict[str, Any] = {
        "price_checked_at": catalog.checked_at.isoformat(),
        "price_sources": info.sources,
        "currency": catalog.currency,
        "catalog_stale": getattr(catalog, "stale", True),
        "historical_price_verified": False,
    }
    if not info.prices:
        return {**metadata, "status": "unknown", "reason": "model_or_provider_price_unknown"}
    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if any(type(value) is not int or value < 0 for value in (prompt, completion)):
        return {**metadata, "status": "unknown", "reason": "token_totals_missing_or_invalid"}
    hit = usage.get("prompt_cache_hit_tokens")
    miss = usage.get("prompt_cache_miss_tokens")
    if hit is None:
        detail = usage.get("prompt_tokens_details")
        if isinstance(detail, dict):
            hit = detail.get("cached_tokens")
    for value in (hit, miss):
        if value is not None and (type(value) is not int or not 0 <= value <= prompt):
            return {**metadata, "status": "unknown", "reason": "cache_tokens_invalid"}
    if hit is not None and miss is not None and hit + miss != prompt:
        return {**metadata, "status": "unknown", "reason": "cache_tokens_do_not_sum_to_input"}
    known_cache = hit is not None or miss is not None
    if known_cache:
        hit = prompt - miss if hit is None else hit
        miss = prompt - hit if miss is None else miss
    scenarios = {}
    for period, prices in info.prices.items():

        def amount(hit_tokens: int, miss_tokens: int, prices=prices) -> float:
            value = (
                hit_tokens * prices.input_cache_hit
                + miss_tokens * prices.input_cache_miss
                + completion * prices.output
            ) / Decimal(1_000_000)
            return float(value.quantize(Decimal("0.00000001")))

        if known_cache:
            scenarios[period] = {"estimated_usd": amount(hit, miss)}
        else:
            values = [amount(prompt, 0), amount(0, prompt)]
            scenarios[period] = {"min_usd": min(values), "max_usd": max(values)}
    return {
        **metadata,
        "status": "estimated" if known_cache else "range",
        "cache_breakdown": "known" if known_cache else "unknown",
        "billing_period": "unknown",
        "scenarios": scenarios,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    parser.add_argument(
        "--refresh", action="store_true", help="Allow official HTTPS metadata update"
    )
    parser.add_argument("--max-age-hours", type=float, default=168)
    args = parser.parse_args()
    state = load_catalog(
        args.cache_dir,
        allow_network=args.refresh,
        force_refresh=args.refresh,
        max_age_hours=args.max_age_hours,
    )
    print(state.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
