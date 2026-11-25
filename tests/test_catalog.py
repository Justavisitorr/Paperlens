"""Catalog provenance and failure behavior without trusting live web changes."""

from datetime import timedelta

import httpx
import pytest

from paperlens.catalog import estimate_usage_cost, get_model, load_catalog, parse_model_list
from paperlens.storage import write_json


def test_network_denied_stays_offline_even_when_refresh_requested(tmp_path):
    def forbidden(request):
        raise AssertionError("Network permission must be checked first")

    result = load_catalog(
        tmp_path, allow_network=False, force_refresh=True, transport=httpx.MockTransport(forbidden)
    )
    assert not result.network_checked
    assert "network_update_not_authorized" in result.warnings
    assert result.sources and all(
        s.url.startswith("https://api-docs.deepseek.com/") for s in result.sources
    )


def test_failed_update_preserves_last_verified_snapshot(tmp_path):
    original = load_catalog(tmp_path)
    path = tmp_path / "catalog" / "deepseek.json"
    write_json(path, original.model_dump(mode="json"))
    before = path.read_bytes()

    def unavailable(request):
        assert "authorization" not in request.headers
        return httpx.Response(503, text="provider down")

    state = load_catalog(
        tmp_path, allow_network=True, force_refresh=True, transport=httpx.MockTransport(unavailable)
    )
    assert state.stale and state.network_checked
    assert path.read_bytes() == before
    assert any(w.startswith("refresh_failed:") for w in state.warnings)


def test_expiry_and_unknown_provider_do_not_borrow_official_capabilities(tmp_path):
    catalog = load_catalog(tmp_path)
    expired = load_catalog(tmp_path, now=catalog.checked_at + timedelta(days=30))
    assert expired.stale
    assert (
        get_model(catalog, "deepseek-flash", base_url="https://proxy.example").vision == "unknown"
    )
    result = estimate_usage_cost(
        {"prompt_tokens": 100, "completion_tokens": 50},
        "deepseek-flash",
        catalog,
        base_url="https://proxy.example",
    )
    assert result["status"] == "unknown"


def test_cache_breakdown_unknown_is_range_not_zero(tmp_path):
    catalog = load_catalog(tmp_path)
    usage = {"prompt_tokens": 1000000, "completion_tokens": 1000000}
    result = estimate_usage_cost(usage, "deepseek-flash", catalog)
    assert result["status"] == "range"
    assert all(v["max_usd"] > v["min_usd"] > 0 for v in result["scenarios"].values())
    usage.update(prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=1000000)
    result = estimate_usage_cost(usage, "deepseek-flash", catalog)
    assert result["status"] == "estimated"
    assert all(v["estimated_usd"] > 0 for v in result["scenarios"].values())


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": None, "completion_tokens": 1},
        {"prompt_tokens": -1, "completion_tokens": 1},
        {
            "prompt_tokens": 10,
            "completion_tokens": 1,
            "prompt_cache_hit_tokens": 5,
            "prompt_cache_miss_tokens": 6,
        },
    ],
)
def test_invalid_or_missing_totals_stay_unknown(tmp_path, usage):
    assert (
        estimate_usage_cost(usage, "deepseek-flash", load_catalog(tmp_path))["status"] == "unknown"
    )


def test_models_list_does_not_imply_pricing_or_vision():
    models = parse_model_list({"data": [{"id": "custom-model"}]}, base_url="https://custom.example")
    assert len(models) == 1 and models[0].vision == "unknown" and not models[0].prices
