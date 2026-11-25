import json

import httpx
import pytest
from pydantic import SecretStr

from paperlens.config import Settings
from paperlens.connections import ConnectionStore


def settings(tmp_path):
    return Settings(
        _env_file=None, llm_api_key=SecretStr("env-key"), paperlens_output_dir=tmp_path / "outputs"
    )


def test_multiple_keys_persist_without_public_secrets(tmp_path):
    base = settings(tmp_path)
    store = ConnectionStore(base)
    first = store.save_account(
        {"provider_id": "deepseek", "name": "个人", "api_key": "personal-key"}
    )
    second = store.save_account({"provider_id": "deepseek", "name": "团队", "api_key": "team-key"})
    assert first["id"] != second["id"]
    assert first["base_url"] == "https://api.deepseek.com"
    assert "personal-key" not in json.dumps(store.public())
    assert "env-key" not in json.dumps(store.public())
    reopened = ConnectionStore(base)
    assert len(reopened.public()["accounts"]) == 3
    assert reopened.path.parent == tmp_path / ".paperlens"
    assert [a["name"] for a in reopened.public()["accounts"][1:]] == ["个人", "团队"]


def test_roles_inherit_main_and_use_separate_keys(tmp_path):
    base = settings(tmp_path)
    store = ConnectionStore(base)
    main = store.save_account({"provider_id": "deepseek", "api_key": "main-key"})
    chat = store.save_account({"provider_id": "anthropic", "api_key": "chat-key"})
    store.set_routing(
        {
            "main": {"account_id": main["id"], "model": "main-model"},
            "chat": {"account_id": chat["id"], "model": "chat-model"},
        }
    )
    resolved = store.resolve(base)
    assert resolved.for_role("reading").llm_api_key.get_secret_value() == "main-key"
    assert resolved.for_role("chat").llm_api_key.get_secret_value() == "chat-key"
    assert resolved.for_role("chat").llm_protocol == "anthropic"
    assert resolved.llm_model == "main-model"
    assert "main-key" not in json.dumps(resolved.public_configuration())
    store.set_routing({"chat": None})
    assert store.resolve(base).for_role("chat").llm_model == "main-model"


def test_switch_address_clears_key_and_model_cache(tmp_path):
    store = ConnectionStore(settings(tmp_path))
    account = store.save_account(
        {"provider_id": "custom", "base_url": "https://one.test/v1", "api_key": "secret"}
    )
    account = store.save_account({"api_key": "", "name": "renamed"}, account["id"])
    assert store._account(account["id"])["api_key"] == "secret"
    account = store.save_account({"base_url": "https://two.test/v1"}, account["id"])
    assert store._account(account["id"])["api_key"] == ""
    assert account["models"]["status"] == "empty"


def test_conversation_assignment_migrates_then_can_be_changed_independently(tmp_path):
    base = settings(tmp_path)
    store = ConnectionStore(base)
    store.set_routing({"chat": {"account_id": "env", "model": "old-chat"}})
    saved = json.loads(store.path.read_text(encoding="utf-8"))
    del saved["routing"]["conversation"]
    store.path.write_text(json.dumps(saved), encoding="utf-8")
    reopened = ConnectionStore(base)
    assert reopened.public()["routing"]["conversation"] == reopened.public()["routing"]["chat"]
    assert reopened.resolve(base).for_role("conversation").llm_model == "old-chat"
    reopened.set_routing({"conversation": {"account_id": "env", "model": "new-conversation"}})
    reopened = ConnectionStore(base)
    assert reopened.resolve(base).for_role("conversation").llm_model == "new-conversation"
    assert reopened.resolve(base).for_role("chat").llm_model == "old-chat"
    reopened.set_routing({"conversation": None})
    assert reopened.resolve(base).for_role("conversation").llm_model == base.llm_model


def test_refresh_models_auth_and_capabilities(tmp_path):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "image-reader",
                        "name": "Reader",
                        "architecture": {"input_modalities": ["text", "image"]},
                    },
                    {"id": "plain", "architecture": {"input_modalities": ["text"]}},
                    {"id": "unknown"},
                ]
            },
        )

    base = settings(tmp_path)
    store = ConnectionStore(base, transport=httpx.MockTransport(respond))
    account = store.save_account({"provider_id": "openrouter", "api_key": "router-key"})
    models = store.refresh_models(account["id"])
    assert str(requests[0].url) == "https://openrouter.ai/api/v1/models"
    assert requests[0].headers["authorization"] == "Bearer router-key"
    assert models["status"] == "live"
    assert [row["vision"] for row in models["items"]] == ["supported", "unsupported", "unknown"]
    store.set_routing({"vision": {"account_id": account["id"], "model": "image-reader"}})
    assert store.resolve(base).for_role("vision").paperlens_vision_allow_unverified
    with pytest.raises(ValueError, match="不支持图片"):
        store.set_routing({"vision": {"account_id": account["id"], "model": "plain"}})
    reopened = ConnectionStore(base)
    assert reopened.public()["accounts"][1]["models"]["status"] == "cached"


def test_anthropic_model_pages(tmp_path):
    requests = []

    def respond(request):
        requests.append(request)
        page = len(requests)
        return httpx.Response(
            200,
            json={
                "data": [{"id": f"claude-{page}", "display_name": f"Claude {page}"}],
                "has_more": page == 1,
                "last_id": "claude-1",
            },
        )

    store = ConnectionStore(settings(tmp_path), transport=httpx.MockTransport(respond))
    account = store.save_account({"provider_id": "anthropic", "api_key": "anthropic-key"})
    assert len(store.refresh_models(account["id"])["items"]) == 2
    assert requests[0].headers["x-api-key"] == "anthropic-key"
    assert "authorization" not in requests[0].headers
    assert requests[1].url.params["after_id"] == "claude-1"
    assert requests[0].url.path == "/v1/models"


def test_refresh_failure_keeps_previous_models(tmp_path):
    count = 0

    def respond(request):
        nonlocal count
        count += 1
        return (
            httpx.Response(200, json={"data": [{"id": "real-model"}]})
            if count == 1
            else httpx.Response(401)
        )

    store = ConnectionStore(settings(tmp_path), transport=httpx.MockTransport(respond))
    assert store.refresh_models("env")["status"] == "live"
    failed = store.refresh_models("env")
    assert failed["status"] == "cached"
    assert failed["items"][0]["id"] == "real-model"
    assert "401" in failed["error"]
    assert "env-key" not in json.dumps(failed)


def test_env_readonly_and_bound_account_cannot_delete(tmp_path):
    store = ConnectionStore(settings(tmp_path))
    with pytest.raises(ValueError, match="只读"):
        store.save_account({}, "env")
    with pytest.raises(ValueError, match="只读"):
        store.delete_account("env")
    account = store.save_account({"provider_id": "ollama"})
    store.set_routing({"main": {"account_id": account["id"], "model": "local-model"}})
    with pytest.raises(ValueError, match="模型分配"):
        store.delete_account(account["id"])
    store.set_routing({"main": {"account_id": "env", "model": "deepseek-flash"}})
    store.delete_account(account["id"])
    assert len(store.public()["accounts"]) == 1


def test_restore_keeps_submitted_model_and_endpoint(tmp_path):
    base = settings(tmp_path)
    store = ConnectionStore(base)
    account = store.save_account({"provider_id": "openrouter", "api_key": "key-a"})
    store.set_routing({"main": {"account_id": account["id"], "model": "submitted"}})
    submitted = store.resolve(base).public_configuration()
    store.set_routing({"main": {"account_id": "env", "model": "new-model"}})
    assert store.restore(base, submitted).llm_model == "submitted"
    assert store.restore(base, submitted).llm_api_key.get_secret_value() == "key-a"
    store.save_account({"base_url": "https://different.test/v1"}, account["id"])
    with pytest.raises(ValueError, match="地址已改变"):
        store.restore(base, submitted)


def test_env_resolution_uses_current_base_and_legacy_guard(tmp_path):
    base = settings(tmp_path)
    store = ConnectionStore(base)
    changed = base.model_copy(
        update={"llm_api_key": SecretStr("updated-key"), "llm_model": "updated-model"}
    )
    assert store.resolve(changed).llm_model == "updated-model"
    assert store.resolve(changed).llm_api_key.get_secret_value() == "updated-key"
    with pytest.raises(ValueError, match="地址已改变"):
        store.restore(changed, {"llm_base_url": "https://previous.test/v1", "llm_model": "old"})


def test_local_endpoint_without_key_and_manual_model(tmp_path):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(404)

    base = settings(tmp_path)
    store = ConnectionStore(base, transport=httpx.MockTransport(respond))
    account = store.save_account({"provider_id": "ollama"})
    models = store.refresh_models(account["id"])
    assert models["status"] == "failed"
    assert "authorization" not in seen[0].headers
    store.set_routing({"main": {"account_id": account["id"], "model": "my-model"}})
    store.resolve(base).require_provider()
    assert store.resolve(base).llm_model == "my-model"


def test_env_endpoint_change_discards_old_discovery(tmp_path):
    base = settings(tmp_path)
    store = ConnectionStore(
        base,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"data": [{"id": "previous"}]})
        ),
    )
    store.refresh_models("env")
    changed = base.model_copy(update={"llm_base_url": "https://changed.test/v1"})
    assert ConnectionStore(changed).public()["accounts"][0]["models"]["items"] == []


def test_live_discovery_does_not_depend_on_local_catalog(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("local metadata unavailable")

    monkeypatch.setattr("paperlens.catalog.load_catalog", unavailable)
    store = ConnectionStore(
        settings(tmp_path),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": [{"id": "new-model"}]})
        ),
    )
    models = store.refresh_models("env")
    assert models["status"] == "live"
    assert models["items"] == [{"id": "new-model", "name": "new-model", "vision": "unknown"}]


def test_legacy_vision_model_until_web_routing_is_saved(tmp_path):
    base = settings(tmp_path).model_copy(
        update={"paperlens_vision_model": "env-vision", "paperlens_vision_allow_unverified": True}
    )
    store = ConnectionStore(base)
    assert store.resolve(base).for_role("vision").llm_model == "env-vision"
    assert store.resolve(base).for_role("vision").paperlens_vision_allow_unverified
    store.set_routing({"vision": None})
    assert store.resolve(base).for_role("vision").llm_model == base.llm_model


def test_example_key_does_not_count_as_a_connected_account(tmp_path):
    base = settings(tmp_path).model_copy(update={"llm_api_key": SecretStr("replace_with_your_key")})
    store = ConnectionStore(
        base, transport=httpx.MockTransport(lambda request: pytest.fail("placeholder sent"))
    )
    assert not store.public()["accounts"][0]["configured"]
    assert store.refresh_models("env")["status"] == "failed"
