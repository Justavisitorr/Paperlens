"""Public OpenRouter metadata for an exact model, never a paid inference probe.

Prices apply only to OpenRouter routes. Other services may use the same exact
model's public speed as a deliberately wide reference, never as local evidence.
The 30-minute provider observations can be absent; missing values stay unknown.

Sources:
https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties
https://openrouter.ai/docs/api/api-reference/endpoints/list-all-endpoints-for-a-model
https://openrouter.ai/docs/guides/best-practices/latency-and-performance
"""

from __future__ import annotations

import json
import math
import re
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from .config import Settings
from .storage import fingerprint, write_json

_ROOT = "https://openrouter.ai/api/v1/models"
_TTL_SECONDS = 15 * 60
_TIMEOUT = httpx.Timeout(5, connect=2, pool=2)
_MAX_BYTES = 8_000_000
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:~-]*")
_load_lock = threading.Lock()
_memory_lock = threading.Lock()
_memory: dict[tuple[str, str], tuple[float, dict | None]] = {}


@dataclass(frozen=True)
class EndpointMetrics:
    provider: str
    prompt_usd: float | None
    completion_usd: float | None
    cache_read_usd: float | None
    request_usd: float
    throughput: float | None
    latency: float | None
    extra_charges: bool = False


@dataclass(frozen=True)
class ModelMetrics:
    model: str
    endpoint: str
    checked_at: str
    source_url: str
    openrouter_route: bool
    endpoints: tuple[EndpointMetrics, ...]


def _number(value, *, positive=False) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and (result > 0 if positive else result >= 0) else None


def _observation(value, *, positive=False) -> float | None:
    # Current API can return either a scalar or a percentile record.
    if isinstance(value, dict):
        value = value.get("p50")
    return _number(value, positive=positive)


def _openrouter_route(endpoint: str) -> bool:
    parsed = urlsplit(endpoint)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "openrouter.ai"
        and parsed.path.rstrip("/") == "/api/v1"
        and parsed.port in (None, 443)
    )


def _get(url: str, root: Path, client: httpx.Client | None) -> tuple[dict | None, str]:
    """The caller holds the single-flight lock; only allowlisted public URLs enter."""
    key = (str(root.resolve()), url)
    now = time.time()
    with _memory_lock:
        cached = _memory.get(key)
    if cached is None:
        try:
            value = json.loads((root / f"{fingerprint(url)}.json").read_text(encoding="utf-8"))
            if value.get("url") == url:
                cached = (float(value["fetched_at"]), value.get("payload"))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
    if cached and -300 <= now - cached[0] <= _TTL_SECONDS:
        payload = cached[1] if isinstance(cached[1], dict) else None
        return payload, datetime.fromtimestamp(cached[0], UTC).isoformat()
    if client is None:
        return None, datetime.now(UTC).isoformat()
    payload = None
    try:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            chunks, size = [], 0
            started = time.monotonic()
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > _MAX_BYTES or time.monotonic() - started > 6:
                    raise ValueError("Public model metadata exceeds size limit")
                chunks.append(chunk)
            decoded = json.loads(b"".join(chunks))
            if isinstance(decoded, dict):
                payload = decoded
    except (httpx.HTTPError, ValueError):
        pass
    # Cache failures too, avoiding repeated network requests on every estimate.
    with _memory_lock:
        _memory[key] = (now, payload)
        while len(_memory) > 32:
            del _memory[next(iter(_memory))]
    try:
        write_json(
            root / f"{fingerprint(url)}.json",
            {
                "url": url,
                "fetched_at": now,
                "payload": payload,
            },
        )
    except OSError:
        pass
    return payload, datetime.fromtimestamp(now, UTC).isoformat()


def _parse(row: dict) -> EndpointMetrics | None:
    if not isinstance(row, dict) or row.get("status", 0) != 0:
        return None
    pricing = row.get("pricing")
    pricing = pricing if isinstance(pricing, dict) else {}
    extra = _number(pricing.get("request", 0)) is None or any(
        (_number(value) or 0) > 0
        for key, value in pricing.items()
        if key not in {"prompt", "completion", "input_cache_read", "request", "discount"}
    )
    return EndpointMetrics(
        provider=str(row.get("provider_name") or ""),
        prompt_usd=_number(pricing.get("prompt")),
        completion_usd=_number(pricing.get("completion")),
        cache_read_usd=_number(pricing.get("input_cache_read")),
        request_usd=_number(pricing.get("request", 0)) or 0,
        throughput=_observation(row.get("throughput_last_30m"), positive=True),
        latency=_observation(row.get("latency_last_30m")),
        extra_charges=extra,
    )


def load_model_metrics(settings: Settings) -> dict[tuple[str, str], ModelMetrics]:
    """Load up to five role models; network-off reads only a fresh public cache.

    Matching requires the literal model ID in /models and in /endpoints. A model
    such as ``deepseek-flash`` is not guessed to mean an OpenRouter model. Neither
    user credentials nor document text is read or transmitted by this client.
    """
    routes = {}
    for role in ("main", "reading", "verification", "vision", "chat"):
        resolved = settings.for_role(role)
        model = (
            settings.paperlens_vision_model
            if role == "vision"
            and "vision" not in settings.llm_routes
            and settings.paperlens_vision_model
            else resolved.llm_model
        )
        if _MODEL_ID.fullmatch(model):
            routes[(resolved.llm_base_url.rstrip("/"), model)] = None
    models = list(dict.fromkeys(model for _, model in routes))[:5]
    if not models:
        return {}
    root = settings.paperlens_cache_dir / "model_metrics"
    # A single client has no user auth headers and never follows redirects.
    with _load_lock:
        client = (
            httpx.Client(timeout=_TIMEOUT, follow_redirects=False, trust_env=False)
            if (settings.paperlens_catalog_network)
            else None
        )
        try:
            listing, _ = _get(_ROOT, root, client)
            listed = listing.get("data") if isinstance(listing, dict) else None
            exact = (
                {
                    row["id"]
                    for row in listed
                    if isinstance(row, dict) and isinstance(row.get("id"), str)
                }
                if (isinstance(listed, list))
                else set()
            )
            wanted = [model for model in models if model in exact]

            def fetch(model):
                url = _ROOT + "/" + quote(model, safe="/:") + "/endpoints"
                response, checked = _get(url, root, client)
                data = response.get("data") if isinstance(response, dict) else None
                if not isinstance(data, dict) or data.get("id") != model:
                    return model, None
                rows = data.get("endpoints")
                if not isinstance(rows, list):
                    return model, None
                endpoints = tuple(item for row in rows if (item := _parse(row)) is not None)
                return model, (checked, url, endpoints)

            with ThreadPoolExecutor(max_workers=5, thread_name_prefix="model-metadata") as pool:
                loaded = dict(pool.map(fetch, wanted))
        finally:
            if client is not None:
                client.close()
    result = {}
    for endpoint, model in routes:
        item = loaded.get(model)
        if item is not None:
            checked, url, endpoints = item
            result[(endpoint, model)] = ModelMetrics(
                model,
                endpoint,
                checked,
                url,
                _openrouter_route(endpoint),
                endpoints,
            )
    return result


def request_seconds(
    metrics: ModelMetrics, input_tokens: float, output_tokens: float, point: str = "typical"
) -> float | None:
    """One request: measured time-to-first-token plus output / observed tokens/s.

    The observed latency already includes input processing; input tokens must not
    be converted to output tokens or divided by output throughput. This fallback
    has no local concurrency or reasoning calibration, so retain a wide envelope.
    """
    if min(input_tokens, output_tokens) < 0:
        return None
    values = [
        row.latency + output_tokens / row.throughput
        for row in metrics.endpoints
        if row.throughput is not None and row.latency is not None
    ]
    if not values:
        return None
    if metrics.openrouter_route:
        return {
            "low": min(values) * 0.7,
            "typical": statistics.median(values),
            "high": max(values) * 1.8,
        }[point]
    return {
        "low": min(values) * 0.5,
        "typical": statistics.median(values) * 1.5,
        "high": max(values) * 3,
    }[point]


def usage_cost(
    metrics: ModelMetrics, prompt: float, completion: float, requests: float = 0
) -> dict:
    """USD bounds across available providers and unknown cache hits on OpenRouter.

    Additional non-token unit charges make this a known subtotal, never a total.
    The configured direct provider retains its own catalog pricing instead.
    """
    if not metrics.openrouter_route:
        return {"status": "unknown", "reason": "openrouter_price_not_actual_provider"}
    if min(prompt, completion, requests) < 0:
        return {"status": "unknown", "reason": "invalid_token_usage"}
    if any(row.prompt_usd is None or row.completion_usd is None for row in metrics.endpoints):
        # These are alternative routes, not additive pieces of one invoice. A
        # priced provider is no known subtotal if the request may use another.
        return {"status": "unknown", "reason": "routable_provider_price_unknown"}
    values = []
    partial = False
    for row in metrics.endpoints:
        cached = row.cache_read_usd if row.cache_read_usd is not None else row.prompt_usd
        fixed = completion * row.completion_usd + requests * row.request_usd
        values.append(
            (prompt * min(cached, row.prompt_usd) + fixed, prompt * row.prompt_usd + fixed)
        )
        partial |= row.extra_charges
    if not values:
        return {"status": "unknown", "reason": "model_or_provider_price_unknown"}
    pair = [round(min(a for a, _ in values), 8), round(max(b for _, b in values), 8)]
    return {
        "status": "partial" if partial else "range",
        "off_peak": pair,
        "peak": pair[:],
        **({"unpriced_models": [metrics.model]} if partial else {}),
        "source": metrics.source_url,
        "checked_at": metrics.checked_at,
    }
