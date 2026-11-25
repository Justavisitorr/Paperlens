"""Routing, real transport shapes, and the shared concurrency request ceiling."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

import httpx
from fastapi.testclient import TestClient

from paperlens import server
from paperlens.config import ModelRoute, Settings
from paperlens.errors import ProviderError
from paperlens.provider import JsonProvider, ProviderPool, RequestBudget


def reply():
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3},
        },
    )


def test_shared_budget_counts_parallel_attempts_across_accounts(tmp_path):
    settings = Settings(
        _env_file=None,
        llm_api_key="main-test",
        llm_base_url="https://a.example/v1",
        paperlens_cache_dir=tmp_path,
    )
    settings.llm_routes = {
        "verification": ModelRoute(
            account_id="review",
            model="judge",
            base_url="https://b.example/v1",
            api_key="review-test",
        )
    }
    seen, lock = [], Lock()

    def transport(req):
        with lock:
            seen.append((req.url.host, req.headers["authorization"]))
        time.sleep(0.04)
        return reply()

    with ProviderPool(
        settings, budget=RequestBudget(3), transport=httpx.MockTransport(transport)
    ) as pool:

        def call(role):
            try:
                return pool.for_role(role).json("review", "{}")
            except ProviderError:
                return None

        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(call, ["main", "verification"] * 3))
        assert sum(value is not None for value in results) == 3
        assert pool.usage.requests == len(seen) == 3
        assert all(
            key == ("Bearer main-test" if host == "a.example" else "Bearer review-test")
            for host, key in seen
        )
        assert len(pool.usage_records()) == 2


def test_anthropic_native_messages_and_usage(tmp_path):
    settings = Settings(
        _env_file=None,
        llm_protocol="anthropic",
        llm_base_url="https://api.anthropic.com/v1",
        llm_api_key="test-only",
        llm_model="selected-model",
        paperlens_cache_dir=tmp_path,
    )

    def transport(req):
        assert req.url.path == "/v1/messages"
        assert req.headers["x-api-key"] == "test-only" and "authorization" not in req.headers
        payload = json.loads(req.content)
        assert payload["system"] and payload["messages"][0]["role"] == "user"
        assert "response_format" not in payload
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": '{"ok":true}'}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 8},
            },
        )

    with JsonProvider(settings, transport=httpx.MockTransport(transport)) as provider:
        assert provider.json("test", "{}") == {"ok": True}
        assert provider.usage.prompt_tokens == 18 and provider.usage.prompt_cache_hit_tokens == 8


def test_local_endpoint_can_run_without_key(tmp_path):
    settings = Settings(
        _env_file=None,
        llm_requires_key=False,
        llm_api_key="",
        llm_base_url="http://127.0.0.1:11434/v1",
        paperlens_cache_dir=tmp_path,
    )

    def transport(req):
        assert "authorization" not in req.headers
        return reply()

    with JsonProvider(settings, transport=httpx.MockTransport(transport)) as provider:
        assert provider.json("test", "{}") == {"ok": True}


def test_account_api_roundtrip_and_acceleration_confirmation(tmp_path, monkeypatch):
    import threading

    entered, release = threading.Event(), threading.Event()
    settings = Settings(
        _env_file=None,
        llm_api_key="env-test",
        paperlens_cache_dir=tmp_path / "cache",
        paperlens_output_dir=tmp_path / "outputs",
    )

    def run(*args, **kwargs):
        entered.set()
        release.wait(4)
        raise ProviderError("controlled stop")

    monkeypatch.setattr(server, "run_pipeline", run)
    with TestClient(server.create_app(settings)) as client:
        try:
            created = client.post(
                "/api/connections/accounts",
                json={"provider_id": "deepseek", "name": "账户一", "api_key": "private-test"},
            )
            assert created.status_code == 201
            account = created.json()["account"]
            assert "private-test" not in created.text
            route = {"main": {"account_id": account["id"], "model": "deepseek-flash"}, "chat": None}
            assert (
                client.put("/api/connections/routing", json={"routing": route}).status_code == 200
            )
            job = client.post("/api/jobs", files={"file": ("x.pdf", b"%PDF-test")}, data={"unlimited_confirmed": "true"}).json()["id"]
            assert entered.wait(2)
            assert client.post(f"/api/jobs/{job}/accelerate").status_code == 409
            boosted = client.post(f"/api/jobs/{job}/accelerate", json={"confirmed": True})
            assert boosted.status_code == 200 and boosted.json()["acceleration"]["concurrency"] == 3
            assert "accelerate" not in boosted.json()["actions"]
            snapshot = (tmp_path / "cache/jobs" / job / "job.json").read_text(encoding="utf-8")
            assert "private-test" not in snapshot and "env-test" not in snapshot
        finally:
            release.set()
    with TestClient(server.create_app(settings)) as client:
        saved = client.get("/api/connections").json()
        assert saved["routing"]["main"]["account_id"] == account["id"]
        assert next(a for a in saved["accounts"] if a["id"] == account["id"])["configured"]
