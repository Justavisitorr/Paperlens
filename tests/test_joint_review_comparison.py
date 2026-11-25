"""Accounting checks for the isolated live experiment, not model quality tests."""
import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "joint_comparison", Path(__file__).parents[1] / "tools/compare_joint_review.py"
)
comparison = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(comparison)


def receipt(**changes):
    response = {
        "model": "gpt-6.1-terra", "service_tier": "default",
        "prompt_cache_retention": "24h",
        "usage": {"input_tokens": 10000, "output_tokens": 2000,
                  "input_tokens_details": {"cached_tokens": 2000, "cache_write_tokens": 7000},
                  "output_tokens_details": {"reasoning_tokens": 1500}},
    }
    response.update(changes)
    return {"response": response}


def test_cache_write_total_price_and_reasoning_not_double_counted():
    result = comparison.cost(receipt())
    assert result["usd"] == pytest.approx(0.01985)
    assert result["reasoning_tokens"] == 1500
    assert result["ordinary_input_tokens"] == 1000
    assert result["cache_ttl"] == "30m"  # Separate from the legacy retention field.


def test_explicit_long_cache_ttl():
    assert comparison.cost(receipt(prompt_cache_options={"ttl": "12h"}))["usd"] == pytest.approx(0.0286)


@pytest.mark.parametrize("value", [{}, {"response": {"error": "timeout"}}, receipt(service_tier="priority")])
def test_unknown_usage_or_tier_not_reported_as_zero(value):
    assert comparison.cost(value)["usd"] is None


def test_long_context_multiplier():
    value = receipt(usage={"input_tokens": 300000, "output_tokens": 1000})
    assert comparison.cost(value)["usd"] == pytest.approx(0.6075)


def test_actual_mixed_cache_write_lifetimes_override_default():
    value = receipt()
    value["response"]["usage"]["input_tokens_details"]["cache_write_tokens_details"] = {
        "ttl_30m_tokens": 3000, "ttl_12h_tokens": 4000,
    }
    assert comparison.cost(value)["usd"] == pytest.approx(0.02485)


def test_known_tokens_retained_when_price_unknown(tmp_path):
    value = receipt(service_tier="unpriced")
    comparison.write(tmp_path / "requests/request-0001.json", value)
    summary = comparison.summarize(tmp_path)
    assert summary["tokens"]["input_tokens"] == 10000
    assert summary["unknown_usage_requests"] == 0
    assert summary["unpriced_requests"] == 1
