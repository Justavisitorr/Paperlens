"""Daily public reference exchange rates for displaying USD-denominated costs.

Source and API contract: https://frankfurter.dev/#rates
Only the fixed ECB provider route is requested, without credentials or documents.
An offline bundle has a visible observation date and is always marked stale.
"""

from __future__ import annotations

import json
import math
import threading
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from .config import Settings
from .storage import write_json

_CURRENCIES = {
    "CNY": "人民币", "USD": "美元", "EUR": "欧元", "GBP": "英镑", "JPY": "日元",
    "HKD": "港币", "AUD": "澳大利亚元", "CAD": "加拿大元", "CHF": "瑞士法郎",
    "SGD": "新加坡元", "KRW": "韩元",
}
_URL = ("https://api.frankfurter.dev/v2/providers/ecb/rates?base=USD&quotes="
        "CNY,EUR,GBP,JPY,HKD,AUD,CAD,CHF,SGD,KRW")
_SOURCE = "Frankfurter / European Central Bank"
_BUNDLE = Path(__file__).with_name("assets") / "exchange-rates.json"
_MAX_BYTES = 32_000
_TTL_SECONDS = 24 * 60 * 60
_lock = threading.Lock()
_cache: dict[str, dict] = {}
_retry_after: dict[str, float] = {}


def _now() -> datetime:
    return datetime.now(UTC)


def currencies() -> list[dict[str, str]]:
    return [{"code": code, "name": name} for code, name in _CURRENCIES.items()]


def _validated(data: dict) -> dict:
    if not isinstance(data, dict) or data.get("base") != "USD":
        raise ValueError("Unexpected reference-rate base")
    observed = date.fromisoformat(data["date"])
    if observed < date(1999, 1, 1) or observed > _now().date() + timedelta(days=1):
        raise ValueError("Unexpected reference-rate date")
    values = data["rates"]
    if not isinstance(values, dict) or set(values) != set(_CURRENCIES):
        raise ValueError("Incomplete reference-rate currencies")
    rates = {}
    for code, value in values.items():
        if isinstance(value, bool):
            raise ValueError("Invalid reference rate")
        rate = float(value)
        if not math.isfinite(rate) or not 0 < rate < 1_000_000_000:
            raise ValueError("Invalid reference rate")
        rates[code] = rate
    if rates["USD"] != 1:
        raise ValueError("Invalid identity conversion")
    return {"base": "USD", "date": observed.isoformat(), "rates": rates}


def _read(path: Path) -> dict | None:
    try:
        if path.stat().st_size > _MAX_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return None
        data = _validated(value)
        checked = datetime.fromisoformat(value["checked_at"])
        if checked.tzinfo is None or checked > _now() + timedelta(minutes=5):
            return None
        return {**data, "checked_at": checked.isoformat()}
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return None


def _fetch() -> dict:
    with httpx.Client(timeout=httpx.Timeout(5, connect=2), follow_redirects=False,
                      trust_env=False) as client:
        with client.stream("GET", _URL) as response:
            response.raise_for_status()
            chunks, size = [], 0
            started = time.monotonic()
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > _MAX_BYTES or time.monotonic() - started > 5:
                    raise ValueError("Reference-rate response exceeds limits")
                chunks.append(chunk)
            rows = json.loads(b"".join(chunks))
    if not isinstance(rows, list) or len(rows) != len(_CURRENCIES) - 1:
        raise ValueError("Unexpected reference-rate response")
    rates, dates = {"USD": 1.0}, set()
    for row in rows:
        if not isinstance(row, dict) or row.get("base") != "USD":
            raise ValueError("Unexpected reference-rate base")
        code = row.get("quote")
        if code not in _CURRENCIES or code in rates:
            raise ValueError("Unexpected reference-rate currency")
        dates.add(row["date"])
        rates[code] = row["rate"]
    if len(dates) != 1:
        raise ValueError("Mixed reference-rate dates")
    return {**_validated({"base": "USD", "date": dates.pop(), "rates": rates}),
            "checked_at": _now().isoformat()}


def _result(data: dict, *, stale: bool, snapshot: bool) -> dict:
    # Weekends and ECB holidays do not make a successfully refreshed latest
    # reference rate stale; its actual observation date remains visible.
    old_observation = (_now().date() - date.fromisoformat(data["date"])).days > 7
    return {**data, "source": _SOURCE, "source_url": _URL,
            "stale": stale or old_observation, "snapshot": snapshot,
            "currencies": currencies()}


def load_exchange_rates(settings: Settings) -> dict:
    """Reuse daily rates; on failure use the last cache, then the dated bundle.

    ``paperlens_catalog_network=False`` never opens a network client. Failed
    refreshes are backed off for five minutes, including concurrent callers.
    No background or inference requests are made by this service.
    """
    path = settings.paperlens_cache_dir / "currency" / "exchange-rates.json"
    key = str(path.resolve())
    with _lock:
        cached = _cache.get(key) or _read(path)
        if cached:
            age = (_now() - datetime.fromisoformat(cached["checked_at"])).total_seconds()
            if 0 <= age < _TTL_SECONDS:
                return _result(cached, stale=False, snapshot=False)
        if settings.paperlens_catalog_network and time.monotonic() >= _retry_after.get(key, 0):
            try:
                fresh = _fetch()
            except (httpx.HTTPError, ValueError, KeyError, TypeError, OverflowError):
                _retry_after[key] = time.monotonic() + 300
            else:
                _cache[key] = fresh
                _retry_after.pop(key, None)
                while len(_cache) > 32:
                    del _cache[next(iter(_cache))]
                try:
                    write_json(path, fresh)
                except OSError:
                    pass
                return _result(fresh, stale=False, snapshot=False)
        if cached:
            return _result(cached, stale=True, snapshot=False)
        # This is an observed public snapshot, never a fabricated live rate.
        try:
            bundled = _validated(json.loads(_BUNDLE.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, TypeError):
            return {"base": "USD", "rates": {"USD": 1.0}, "date": None,
                    "checked_at": None, "source": _SOURCE, "source_url": _URL,
                    "stale": True, "snapshot": True, "currencies": currencies()}
        return _result({**bundled, "checked_at": None}, stale=True, snapshot=True)
