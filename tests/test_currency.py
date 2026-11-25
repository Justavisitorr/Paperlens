"""Reference rates are dated, cached, optional-network, and separate from USD costs."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from paperlens import currency, server
from paperlens.config import Settings
from paperlens.storage import write_json

NOW = datetime(2026, 10, 19, 3, tzinfo=UTC)


@pytest.fixture(autouse=True)
def reset_cache(monkeypatch):
    currency._cache.clear()
    currency._retry_after.clear()
    monkeypatch.setattr(currency, "_now", lambda: NOW)
    yield
    currency._cache.clear()
    currency._retry_after.clear()


def settings_for(tmp_path, *, network=False):
    return Settings(_env_file=None, llm_api_key="never-send-api-key",
                    paperlens_cache_dir=tmp_path / "cache",
                    paperlens_output_dir=tmp_path / "outputs",
                    paperlens_catalog_network=network)


def rates():
    return json.loads(currency._BUNDLE.read_text(encoding="utf-8"))["rates"]


def rows():
    return [{"base": "USD", "quote": code, "rate": rate, "date": "2026-10-16"}
            for code, rate in rates().items() if code != "USD"]


def install_client(monkeypatch, *, handler=None):
    real_client = httpx.Client
    requests, options = [], []

    def respond(request):
        requests.append(request)
        return handler(request) if handler else httpx.Response(200, json=rows())

    def factory(**kwargs):
        options.append(kwargs)
        return real_client(**kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(currency.httpx, "Client", factory)
    return requests, options


def cache_path(settings):
    return settings.paperlens_cache_dir / "currency/exchange-rates.json"


def write_cache(settings, *, checked=NOW - timedelta(days=2), date="2026-10-15"):
    record = {"base": "USD", "date": date, "rates": rates(), "checked_at": checked.isoformat()}
    record["rates"]["CNY"] = 6.5
    write_json(cache_path(settings), record)
    return record


def test_network_off_cold_start_uses_explicitly_dated_verified_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(currency.httpx, "Client", lambda **_: pytest.fail("Network disabled"))
    result = currency.load_exchange_rates(settings_for(tmp_path))
    assert result["base"] == "USD" and result["rates"]["USD"] == 1
    assert result["rates"]["CNY"] == 6.6862
    assert result["date"] == "2026-10-16" and result["checked_at"] is None
    assert result["stale"] and result["snapshot"]
    assert "European Central Bank" in result["source"]
    assert result["source_url"].startswith("https://api.frankfurter.dev/")
    assert {row["code"] for row in result["currencies"]} == set(result["rates"])
    assert not cache_path(settings_for(tmp_path)).exists()


def test_anonymous_fixed_host_get_refreshes_daily_cache_and_never_sends_keys(tmp_path, monkeypatch):
    requests, options = install_client(monkeypatch)
    settings = settings_for(tmp_path, network=True)
    first = currency.load_exchange_rates(settings)
    assert not first["stale"] and not first["snapshot"]
    assert first["checked_at"] == NOW.isoformat()
    assert first["date"] == "2026-10-16"  # Friday is still the latest on Monday morning.
    assert currency.load_exchange_rates(settings) == first and len(requests) == 1
    [request] = requests
    assert request.method == "GET" and not request.content
    assert request.url.host == "api.frankfurter.dev"
    assert request.url.params["base"] == "USD"
    assert "authorization" not in request.headers and "x-api-key" not in request.headers
    assert options[0]["timeout"].read <= 5 and options[0]["timeout"].connect <= 5
    assert options[0]["follow_redirects"] is False and options[0]["trust_env"] is False
    assert "never-send-api-key" not in cache_path(settings).read_text()
    currency._cache.clear()
    offline = settings.model_copy(update={"paperlens_catalog_network": False})
    assert currency.load_exchange_rates(offline) == first and len(requests) == 1
    monkeypatch.setattr(currency, "_now", lambda: NOW + timedelta(days=1))
    currency.load_exchange_rates(settings)
    assert len(requests) == 2


def test_failed_refresh_keeps_last_cache_and_backs_off_repeated_gets(tmp_path, monkeypatch):
    requests, _ = install_client(monkeypatch, handler=lambda _: httpx.Response(503))
    settings = settings_for(tmp_path, network=True)
    record = write_cache(settings)
    first = currency.load_exchange_rates(settings)
    assert first["stale"] and not first["snapshot"]
    assert first["rates"]["CNY"] == 6.5 and first["date"] == record["date"]
    assert currency.load_exchange_rates(settings) == first and len(requests) == 1
    assert json.loads(cache_path(settings).read_text()) == record


def test_network_off_uses_stale_cache_before_bundled_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(currency.httpx, "Client", lambda **_: pytest.fail("Network disabled"))
    settings = settings_for(tmp_path)
    record = write_cache(settings)
    result = currency.load_exchange_rates(settings)
    assert result["stale"] and not result["snapshot"]
    assert result["rates"] == record["rates"] and result["date"] == record["date"]


@pytest.mark.parametrize("fault", ["missing", "mixed_dates", "wrong_base", "negative", "nan", "bool"])
def test_invalid_upstream_rates_cannot_replace_the_last_valid_cache(tmp_path, monkeypatch, fault):
    body = rows()
    if fault == "missing":
        body.pop()
    elif fault == "mixed_dates":
        body[0]["date"] = "2026-10-15"
    elif fault == "wrong_base":
        body[0]["base"] = "EUR"
    else:
        body[0]["rate"] = {"negative": -2, "nan": "NaN", "bool": True}[fault]
    install_client(monkeypatch, handler=lambda _: httpx.Response(200, json=body))
    settings = settings_for(tmp_path, network=True)
    record = write_cache(settings)
    result = currency.load_exchange_rates(settings)
    assert result["stale"] and result["rates"] == record["rates"]
    assert json.loads(cache_path(settings).read_text()) == record


def test_cold_network_failure_and_malformed_disk_cache_return_dated_snapshot(tmp_path, monkeypatch):
    install_client(monkeypatch, handler=lambda _: httpx.Response(200, content=b"not json"))
    settings = settings_for(tmp_path, network=True)
    path = cache_path(settings)
    path.parent.mkdir(parents=True)
    path.write_text('{"base":"EUR","rates":{"USD":1}}')
    result = currency.load_exchange_rates(settings)
    assert result["snapshot"] and result["stale"] and result["date"] == "2026-10-16"
    assert result["rates"]["CNY"] > 1


def test_concurrent_requests_refresh_once_and_preserve_all_common_currencies(tmp_path, monkeypatch):
    requests, _ = install_client(monkeypatch)
    settings = settings_for(tmp_path, network=True)
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda _: currency.load_exchange_rates(settings), range(5)))
    assert len(requests) == 1 and all(result == results[0] for result in results)
    assert {"USD", "CNY", "EUR", "GBP", "JPY", "HKD"} <= results[0]["rates"].keys()


def test_api_exposes_reference_rates_and_supported_currency_names(tmp_path):
    with TestClient(server.create_app(settings_for(tmp_path))) as client:
        response = client.get("/api/exchange-rates")
        assert response.status_code == 200
        result = response.json()
        assert result["base"] == "USD" and result["rates"]["CNY"] == 6.6862
        assert result["snapshot"] and result["stale"]
        assert "never-send-api-key" not in response.text
        supported = client.get("/api/currencies").json()["currencies"]
        assert {"code": "CNY", "name": "人民币"} in supported


@pytest.mark.asyncio
async def test_slow_exchange_refresh_does_not_block_the_async_server(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def slow(settings):
        entered.set()
        assert release.wait(3)
        return {"base": "USD", "rates": {"USD": 1}, "stale": True}

    monkeypatch.setattr(currency, "load_exchange_rates", slow)
    transport = httpx.ASGITransport(app=server.create_app(settings_for(tmp_path)))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        pending = asyncio.create_task(client.get("/api/exchange-rates"))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            other = await asyncio.wait_for(client.get("/api/currencies"), timeout=1)
            assert other.status_code == 200 and not pending.done()
        finally:
            release.set()
        assert (await pending).status_code == 200
