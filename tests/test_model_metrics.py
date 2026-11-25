"""Public metadata is cached, exact, uncredentialed, and never a paid model call."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Lock

import httpx
import pytest

from paperlens import model_metrics as metrics
from paperlens.config import ModelRoute, Settings

MODEL = "deepseek/deepseek-v3.2"
BASE = "https://openrouter.ai/api/v1"


def settings_for(tmp_path, **changes):
    return Settings(
        _env_file=None,
        llm_api_key="never-send-this-key",
        llm_model=MODEL,
        llm_base_url=BASE,
        paperlens_cache_dir=tmp_path,
        paperlens_catalog_network=True,
        **changes,
    )


def endpoint(**changes):
    return {
        "provider_name": "Provider A",
        "status": 0,
        "pricing": {
            "prompt": "0.000001",
            "completion": "0.000002",
            "input_cache_read": "0.0000002",
        },
        "throughput_last_30m": {"p50": 50, "p90": 30},
        "latency_last_30m": {"p50": 2, "p90": 4},
        **changes,
    }


def install_client(monkeypatch, rows=None, *, handler=None):
    real_client = httpx.Client
    requests, options = [], []

    def respond(request):
        requests.append(request)
        if handler:
            return handler(request)
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json={"data": [{"id": MODEL}]})
        return httpx.Response(200, json={"data": {"id": MODEL, "endpoints": rows or [endpoint()]}})

    def client(**kwargs):
        options.append(kwargs)
        return real_client(**kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(metrics.httpx, "Client", client)
    return requests, options


@pytest.fixture(autouse=True)
def clear_memory():
    metrics._memory.clear()
    yield
    metrics._memory.clear()


def test_metadata_uses_only_uncredentialed_gets_and_deduplicates_roles(tmp_path, monkeypatch):
    requests, options = install_client(monkeypatch)
    settings = settings_for(tmp_path)
    loaded = metrics.load_model_metrics(settings)
    assert list(loaded) == [(BASE, MODEL)]
    assert loaded[(BASE, MODEL)].openrouter_route is True
    assert len(requests) == 2
    for request in requests:
        assert request.method == "GET" and not request.content
        assert request.url.host == "openrouter.ai"
        assert "authorization" not in request.headers and "x-api-key" not in request.headers
        assert "never-send" not in str(request.url) + str(request.headers)
    assert options[0]["follow_redirects"] is False
    assert options[0]["trust_env"] is False
    assert options[0]["timeout"].read <= 5
    assert "never-send" not in "".join(p.read_text() for p in tmp_path.rglob("*.json"))
    assert metrics.load_model_metrics(settings) == loaded
    assert len(requests) == 2


def test_network_off_reads_only_fresh_disk_cache_without_constructing_client(tmp_path, monkeypatch):
    install_client(monkeypatch)
    settings = settings_for(tmp_path)
    first = metrics.load_model_metrics(settings)
    metrics._memory.clear()
    monkeypatch.setattr(metrics.httpx, "Client", lambda **_: pytest.fail("Network disabled"))
    offline = settings.model_copy(update={"paperlens_catalog_network": False})
    assert metrics.load_model_metrics(offline) == first
    for path in tmp_path.rglob("*.json"):
        data = json.loads(path.read_text())
        data["fetched_at"] -= 901
        path.write_text(json.dumps(data))
    assert metrics.load_model_metrics(offline) == {}


def test_no_network_without_permission_or_for_ambiguous_alias(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics.httpx, "Client", lambda **_: pytest.fail("Unexpected network"))
    settings = settings_for(tmp_path)
    assert (
        metrics.load_model_metrics(settings.model_copy(update={"paperlens_catalog_network": False}))
        == {}
    )
    assert (
        metrics.load_model_metrics(settings.model_copy(update={"llm_model": "deepseek-flash"}))
        == {}
    )


def test_literal_model_matching_does_not_use_name_or_canonical_alias(tmp_path, monkeypatch):
    def handler(request):
        assert request.url.path == "/api/v1/models"
        return httpx.Response(
            200, json={"data": [{"id": "deepseek/other", "name": MODEL, "canonical_slug": MODEL}]}
        )

    requests, _ = install_client(monkeypatch, handler=handler)
    assert metrics.load_model_metrics(settings_for(tmp_path)) == {}
    assert len(requests) == 1


def test_endpoint_identity_must_match_even_after_exact_listing(tmp_path, monkeypatch):
    def handler(request):
        data = (
            [{"id": MODEL}]
            if request.url.path.endswith("/models")
            else {"id": "deepseek/other", "endpoints": [endpoint()]}
        )
        return httpx.Response(200, json={"data": data})

    install_client(monkeypatch, handler=handler)
    assert metrics.load_model_metrics(settings_for(tmp_path)) == {}


def test_metadata_failure_is_negatively_cached_and_cannot_fail_an_estimate(tmp_path, monkeypatch):
    requests, _ = install_client(monkeypatch, handler=lambda _: httpx.Response(503))
    settings = settings_for(tmp_path)
    assert metrics.load_model_metrics(settings) == {}
    assert metrics.load_model_metrics(settings) == {}
    assert len(requests) == 1


def test_null_or_invalid_speed_stays_unknown(tmp_path, monkeypatch):
    rows = [
        endpoint(throughput_last_30m=None, latency_last_30m=None),
        endpoint(throughput_last_30m=0),
        endpoint(throughput_last_30m="NaN"),
        endpoint(throughput_last_30m=-5),
        endpoint(status=-2, throughput_last_30m=1000),
    ]
    install_client(monkeypatch, rows)
    result = metrics.load_model_metrics(settings_for(tmp_path))[(BASE, MODEL)]
    assert metrics.request_seconds(result, 10000, 2000) is None
    assert metrics.usage_cost(result, 1000, 2000)["status"] == "range"


def test_throughput_uses_output_tokens_and_latency_once_with_reference_bounds(
    tmp_path, monkeypatch
):
    install_client(monkeypatch)
    result = metrics.load_model_metrics(settings_for(tmp_path))[(BASE, MODEL)]
    assert metrics.request_seconds(result, 1000, 1000) == 22
    assert metrics.request_seconds(result, 100000, 1000) == 22
    reference = replace(result, endpoint="https://other.example/v1", openrouter_route=False)
    assert metrics.request_seconds(reference, 1000, 1000) == 33
    assert metrics.request_seconds(reference, 1000, 1000, "high") == 66
    assert metrics.usage_cost(reference, 1000, 1000)["status"] == "unknown"


def test_prices_are_usd_per_token_include_requests_and_skip_disabled_providers(
    tmp_path, monkeypatch
):
    rows = [
        endpoint(),
        endpoint(
            provider_name="B",
            pricing={"prompt": "0.000002", "completion": "0.000004", "request": "0.005"},
        ),
        endpoint(status=-2, pricing={"prompt": "100", "completion": "100"}),
    ]
    install_client(monkeypatch, rows)
    result = metrics.load_model_metrics(settings_for(tmp_path))[(BASE, MODEL)]
    cost = metrics.usage_cost(result, 1000, 2000, requests=2)
    assert cost["status"] == "range"
    assert cost["off_peak"] == pytest.approx([0.0042, 0.02])
    assert cost["peak"] == cost["off_peak"]
    assert cost["source"].endswith("/endpoints")


def test_non_token_fees_are_only_a_known_subtotal(tmp_path, monkeypatch):
    rows = [endpoint(pricing={"prompt": "0.000001", "completion": "0.000002", "image": "0.01"})]
    install_client(monkeypatch, rows)
    result = metrics.load_model_metrics(settings_for(tmp_path))[(BASE, MODEL)]
    cost = metrics.usage_cost(result, 1000, 2000)
    assert cost["status"] == "partial" and cost["unpriced_models"] == [MODEL]
    empty = replace(result, endpoints=())
    assert metrics.usage_cost(empty, 1000, 2000)["status"] == "unknown"


@pytest.mark.parametrize("pricing", [{}, {"prompt": "0.000001"}, {"completion": "0.000002"}])
def test_unpriced_alternative_provider_is_not_an_additive_known_subtotal(
    tmp_path, monkeypatch, pricing
):
    install_client(monkeypatch, [endpoint(), endpoint(provider_name="B", pricing=pricing)])
    result = metrics.load_model_metrics(settings_for(tmp_path))[(BASE, MODEL)]
    cost = metrics.usage_cost(result, 1000, 2000)
    assert cost == {"status": "unknown", "reason": "routable_provider_price_unknown"}
    assert "off_peak" not in cost and "peak" not in cost


def test_all_five_role_models_fetch_in_parallel_and_repeated_load_is_single_flight(
    tmp_path, monkeypatch
):
    models = [f"author/model-{i}" for i in range(5)]
    barrier = Barrier(5)
    lock = Lock()
    active = peak = 0

    def handler(request):
        nonlocal active, peak
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": model} for model in models]})
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=3)
        model = "/".join(request.url.path.split("/")[-3:-1])
        with lock:
            active -= 1
        return httpx.Response(200, json={"data": {"id": model, "endpoints": [endpoint()]}})

    requests, _ = install_client(monkeypatch, handler=handler)
    routes = {
        role: ModelRoute(account_id=str(i), model=model, base_url=BASE)
        for i, (role, model) in enumerate(
            zip(("main", "reading", "verification", "vision", "chat"), models, strict=True)
        )
    }
    settings = settings_for(tmp_path, llm_routes=routes)
    with ThreadPoolExecutor(max_workers=2) as pool:
        loaded = list(pool.map(lambda _: metrics.load_model_metrics(settings), range(2)))
    assert loaded[0] == loaded[1] and len(loaded[0]) == 5
    assert peak == 5 and len(requests) == 6


def test_same_exact_model_on_other_service_gets_speed_reference_but_not_openrouter_price(
    tmp_path, monkeypatch
):
    requests, _ = install_client(monkeypatch)
    route = ModelRoute(account_id="private", model=MODEL, base_url="https://other.example/v1")
    settings = settings_for(tmp_path, llm_routes={"reading": route})
    loaded = metrics.load_model_metrics(settings)
    assert len(requests) == 2
    assert loaded[(BASE, MODEL)].openrouter_route is True
    reference = loaded[(route.base_url, MODEL)]
    assert reference.openrouter_route is False
    assert metrics.usage_cost(reference, 1000, 1000)["status"] == "unknown"
    assert reference.source_url.startswith("https://openrouter.ai/")
