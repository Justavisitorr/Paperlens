"""Workspace settings contract, credential isolation and atomic persistence."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import SecretStr

from paperlens import runtime
from paperlens.config import Settings
from paperlens.errors import ConfigurationError
from paperlens.runtime import RuntimeSettings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        llm_api_key="secret-test-initial",
        paperlens_output_dir=tmp_path / "outputs",
        paperlens_cache_dir=tmp_path / "cache",
    )


def fields(schema: dict) -> dict:
    return {field["key"]: field for group in schema["groups"] for field in group["fields"]}


def test_shipped_environment_template_loads_valid_settings():
    template = Path(__file__).resolve().parents[1] / ".env.example"
    assert Settings(_env_file=template).llm_max_requests == 500


def test_environment_request_budget_and_saved_override_survive_restart(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LLM_MAX_REQUESTS", raising=False)
    (tmp_path / ".env").write_text("LLM_MAX_REQUESTS=500\n", encoding="utf-8")
    startup = Settings()
    assert startup.llm_max_requests == 500
    manager = RuntimeSettings(startup)
    assert fields(manager.public())["LLM_MAX_REQUESTS"]["max"] == 1000
    assert "schema" in manager.update({"LLM_MAX_REQUESTS": 750})
    assert RuntimeSettings(Settings()).snapshot().llm_max_requests == 750
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "LLM_MAX_REQUESTS=500\n"


def test_public_schema_never_returns_key_or_uses_network(settings, monkeypatch):
    settings.paperlens_catalog_network = True
    settings.paperlens_catalog_auto_update = True
    original = runtime.load_catalog
    calls = []

    def local_only(*args, **kwargs):
        calls.append(kwargs)
        assert kwargs["allow_network"] is False
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime, "load_catalog", local_only)
    manager = RuntimeSettings(settings)
    schema = manager.public()
    view = fields(schema)
    assert schema["meta"]["stale"] is False
    assert "secret-test-initial" not in json.dumps(schema)
    assert view["LLM_API_KEY"]["value"] is None
    assert view["LLM_API_KEY"]["configured"] is True
    assert view["LLM_MODEL"]["type"] == "select"
    assert settings.llm_model in {v["value"] for v in view["LLM_MODEL"]["options"]}
    assert view["PAPERLENS_MAX_PARALLEL_JOBS"]["max"] == 4
    assert view["LLM_MAX_OUTPUT_TOKENS"]["max"] <= 1_000_000
    assert "PAPERLENS_OUTPUT_DIR" not in view
    assert "PAPERLENS_CACHE_DIR" not in view
    assert calls
    assert not list(settings.paperlens_cache_dir.glob("ui-settings.json"))


def test_snapshot_freezes_existing_jobs_and_cannot_mutate_manager(settings):
    manager = RuntimeSettings(settings)
    first_job = manager.snapshot()
    settings.llm_model = "caller-changed"
    result = manager.update({
        "LLM_MAX_INPUT_CHARS": 24000,
        "PAPERLENS_MAX_PARALLEL_JOBS": 3,
        "PAPERLENS_TEXT_REVIEW_ENABLED": False,
    })
    assert "schema" in result
    assert first_job.llm_max_input_chars == 18000
    assert first_job.paperlens_text_review_enabled is True
    second_job = manager.snapshot()
    assert second_job.llm_max_input_chars == 24000
    assert second_job.paperlens_max_parallel_jobs == 3
    assert second_job.paperlens_text_review_enabled is False
    second_job.llm_max_input_chars = 9000
    assert manager.snapshot().llm_max_input_chars == 24000
    assert manager.snapshot().llm_model != "caller-changed"


def test_all_editable_fields_round_trip_without_overriding_paths(settings, monkeypatch):
    manager = RuntimeSettings(settings)
    changes = {
        "LLM_BASE_URL": "https://models.example.test/v1/",
        "LLM_MODEL": "our-model",
        "LLM_API_KEY": "test-new-key",
        "LLM_REASONING_EFFORT": "default",
        "LLM_MAX_INPUT_CHARS": 32000,
        "LLM_MAX_OUTPUT_TOKENS": 16384,
        "LLM_MAX_REQUESTS": 140,
        "LLM_TIMEOUT_SECONDS": 240.5,
        "LLM_MAX_RETRIES": 3,
        "PAPERLENS_MAX_PAGES": 100,
        "PAPERLENS_MAX_UPLOAD_MB": 80,
        "PAPERLENS_PREVIEW_PAGES": 20,
        "PAPERLENS_MAX_PARALLEL_JOBS": 2,
        "PAPERLENS_JOB_HISTORY_LIMIT": 12,
        "PAPERLENS_VISION_MODEL": "our-vision",
        "PAPERLENS_TEXT_REVIEW_ENABLED": False,
        "PAPERLENS_CHAT_MAX_REQUESTS": 10,
        "PAPERLENS_CATALOG_NETWORK": True,
        "PAPERLENS_CATALOG_AUTO_UPDATE": True,
        "PAPERLENS_CATALOG_MAX_AGE_HOURS": 48,
    }
    assert "schema" in manager.update(changes)
    record = json.loads((settings.paperlens_cache_dir / "ui-settings.json").read_text())
    assert "LLM_API_KEY" not in record["values"]
    assert "test-new-key" not in json.dumps(record)
    assert not (settings.paperlens_cache_dir / ".env").exists()
    monkeypatch.setenv("LLM_MODEL", "changed-startup-env")
    restored = RuntimeSettings(settings)
    after = restored.snapshot()
    assert after.llm_base_url == "https://models.example.test/v1"
    assert after.llm_api_key.get_secret_value() == "test-new-key"
    assert after.paperlens_cache_dir == settings.paperlens_cache_dir
    assert after.paperlens_output_dir == settings.paperlens_output_dir
    assert restored.public()["meta"]["updated_at"]
    assert restored.public()["meta"]["stale"] is False
    for key, value in changes.items():
        if key not in {"LLM_BASE_URL", "LLM_API_KEY"}:
            assert getattr(after, key.lower()) == value


@pytest.mark.parametrize("blank", ["", "  ", None])
def test_blank_secret_keeps_key_for_same_host_and_removes_it_on_host_change(settings, blank):
    manager = RuntimeSettings(settings)
    assert "schema" in manager.update({"LLM_API_KEY": blank, "LLM_MAX_REQUESTS": 119})
    assert manager.snapshot().llm_api_key == settings.llm_api_key
    assert "schema" in manager.update(
        {"LLM_BASE_URL": "https://other.example.test", "LLM_API_KEY": blank}
    )
    assert manager.snapshot().llm_api_key.get_secret_value() == ""
    restarted = RuntimeSettings(settings)
    assert restarted.snapshot().llm_api_key.get_secret_value() == ""
    assert fields(restarted.public())["LLM_API_KEY"]["configured"] is False


def test_custom_provider_does_not_inherit_official_prices_or_model_limit(settings):
    manager = RuntimeSettings(settings)
    result = manager.update(
        {"LLM_BASE_URL": "http://127.0.0.1:1234/v1", "LLM_MAX_OUTPUT_TOKENS": 900000}
    )
    assert "schema" in result
    schema = result["schema"]
    assert fields(schema)["LLM_MODEL"]["type"] == "text"
    assert fields(schema)["PAPERLENS_VISION_MODEL"]["type"] == "text"
    assert schema["catalog"]["status"] == "unknown"
    assert schema["catalog"]["models"] == []
    assert schema["meta"]["stale"] is False


def test_catalog_preserves_unlisted_current_model(settings):
    settings.llm_model = "deepseek-current-unlisted"
    manager = RuntimeSettings(settings)
    options = fields(manager.public())["LLM_MODEL"]["options"]
    assert any(item["value"] == settings.llm_model for item in options)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("LLM_BASE_URL", "https://user:password@example.test"),
        ("LLM_BASE_URL", "https://example.test?token=private"),
        ("LLM_BASE_URL", "file:///etc/config"),
        ("LLM_MODEL", ""),
        ("LLM_MODEL", "bad\nvalue"),
        ("LLM_MODEL", 42),
        ("LLM_API_KEY", "new\nLLM_BASE_URL=https://example.test"),
        ("LLM_API_KEY", "key with space"),
        ("LLM_API_KEY", False),
        ("LLM_REASONING_EFFORT", "extreme"),
        ("PAPERLENS_PARSER", "uninstalled-parser"),
        ("PAPERLENS_MATH_DEVICE", "unknown-device"),
        ("PAPERLENS_MAX_PARALLEL_JOBS", 0),
        ("PAPERLENS_MAX_PARALLEL_JOBS", 5),
        ("PAPERLENS_MAX_PARALLEL_JOBS", 1.5),
        ("LLM_MAX_REQUESTS", True),
        ("LLM_MAX_REQUESTS", "20"),
        ("LLM_MAX_REQUESTS", 0),
        ("LLM_MAX_REQUESTS", 1001),
        ("LLM_MAX_REQUESTS", 10**500),
        ("LLM_TIMEOUT_SECONDS", float("nan")),
        ("LLM_TIMEOUT_SECONDS", float("inf")),
        ("PAPERLENS_VISION_ENABLED", "false"),
        ("PAPERLENS_MAX_PAGES", 2001),
        ("PAPERLENS_CATALOG_MAX_AGE_HOURS", 0),
        ("PAPERLENS_OUTPUT_DIR", "elsewhere"),
        ("PAPERLENS_CACHE_DIR", "elsewhere"),
        ("__dict__", {}),
    ],
)
def test_invalid_patch_is_rejected_as_a_whole(settings, key, value):
    manager = RuntimeSettings(settings)
    original = manager.snapshot()
    result = manager.update({"LLM_TIMEOUT_SECONDS": 250, key: value})
    assert key in result["errors"]
    assert manager.snapshot() == original
    assert not (settings.paperlens_cache_dir / "ui-settings.json").exists()
    assert "secret-test-initial" not in json.dumps(result)


def test_official_model_limits_and_vision_are_validated(settings):
    manager = RuntimeSettings(settings)
    result = manager.update({"LLM_MAX_OUTPUT_TOKENS": 900000})
    assert "LLM_MAX_OUTPUT_TOKENS" in result["errors"]
    result = manager.update({"LLM_MODEL": "deepseek-v4-pro"})
    assert "PAPERLENS_VISION_MODEL" in result["errors"]
    assert "schema" in manager.update(
        {"LLM_MODEL": "deepseek-v4-pro", "PAPERLENS_VISION_MODEL": "deepseek-flash"}
    )


def test_failed_commit_preserves_runtime_and_disk_pair(settings, monkeypatch):
    manager = RuntimeSettings(settings)
    assert "schema" in manager.update({"LLM_MAX_REQUESTS": 119})
    before = manager.snapshot()
    config_path = settings.paperlens_cache_dir / "ui-settings.json"
    previous_bytes = config_path.read_bytes()
    files_before = set(settings.paperlens_cache_dir.rglob("*.env"))

    def fail(*args, **kwargs):
        raise OSError("pretend disk error including secret-test-new-key")

    monkeypatch.setattr(runtime, "write_json", fail)
    result = manager.update(
        {"LLM_BASE_URL": "https://new.example.test", "LLM_API_KEY": "secret-test-new-key"}
    )
    assert "errors" in result
    assert "secret-test-new-key" not in json.dumps(result)
    assert manager.snapshot() == before
    assert config_path.read_bytes() == previous_bytes
    assert set(settings.paperlens_cache_dir.rglob("*.env")) == files_before
    assert RuntimeSettings(settings).snapshot() == before


def test_secret_characters_are_not_dotenv_interpolated(settings):
    manager = RuntimeSettings(settings)
    key = "fake-${HOME}-quote\"-slash\\-single'"
    assert "schema" in manager.update({"LLM_API_KEY": key})
    assert RuntimeSettings(settings).snapshot().llm_api_key.get_secret_value() == key


@pytest.mark.parametrize(
    "corruption", ["traversal", "absolute", "missing_secret", "bad_json", "incomplete"]
)
def test_corrupt_persistence_fails_closed_without_fallback_to_startup_secret(settings, corruption):
    manager = RuntimeSettings(settings)
    assert "schema" in manager.update({"LLM_MAX_REQUESTS": 119})
    config_path = settings.paperlens_cache_dir / "ui-settings.json"
    record = json.loads(config_path.read_text())
    if corruption == "traversal":
        record["secret_file"] = "../outside.env"
    elif corruption == "absolute":
        record["secret_file"] = str(settings.paperlens_cache_dir / "outside.env")
    elif corruption == "missing_secret":
        (settings.paperlens_cache_dir / "runtime-secrets" / record["secret_file"]).unlink()
    elif corruption == "incomplete":
        del record["values"]["LLM_BASE_URL"]
    config_path.write_text("not json" if corruption == "bad_json" else json.dumps(record))
    with pytest.raises(ConfigurationError) as exc:
        RuntimeSettings(settings)
    assert "secret-test-initial" not in str(exc.value)


def test_parallel_updates_keep_all_committed_fields(settings):
    manager = RuntimeSettings(settings)
    with ThreadPoolExecutor(max_workers=2) as pool:
        updates = list(
            pool.map(
                manager.update,
                [
                    {"LLM_MAX_REQUESTS": 118},
                    {"LLM_MAX_INPUT_CHARS": 26000},
                ],
            )
        )
    assert all("schema" in item for item in updates)
    restored = RuntimeSettings(settings).snapshot()
    assert restored.llm_max_requests == 118
    assert restored.llm_max_input_chars == 26000


def test_missing_catalog_still_provides_usable_text_fields_and_hot_settings(settings, monkeypatch):
    def missing(*args, **kwargs):
        raise runtime.CatalogParseError("bad bundled metadata")

    monkeypatch.setattr(runtime, "load_catalog", missing)
    manager = RuntimeSettings(settings)
    assert fields(manager.public())["LLM_MODEL"]["type"] == "text"
    assert "schema" in manager.update({"LLM_MAX_REQUESTS": 100})
    assert manager.public()["catalog"]["stale"] is True
    assert manager.public()["meta"]["stale"] is False


def test_invalid_top_level_request_returns_field_error(settings):
    result = RuntimeSettings(settings).update(["not-a-map"])
    assert "errors" in result


def test_startup_settings_secret_is_never_modified(settings):
    manager = RuntimeSettings(settings)
    assert "schema" in manager.update({"LLM_API_KEY": "replacement"})
    assert settings.llm_api_key == SecretStr("secret-test-initial")


def test_changing_model_requires_new_custom_vision_confirmation(tmp_path):
    runtime = RuntimeSettings(
        Settings(_env_file=None, llm_api_key="test", paperlens_cache_dir=tmp_path)
    )
    assert "errors" not in runtime.update(
        {
            "LLM_BASE_URL": "https://custom.example",
            "LLM_MODEL": "vision-v1",
            "PAPERLENS_VISION_ALLOW_UNVERIFIED": True,
        }
    )
    assert runtime.snapshot().paperlens_vision_allow_unverified
    assert "errors" not in runtime.update({"LLM_MODEL": "vision-v2"})
    assert not runtime.snapshot().paperlens_vision_allow_unverified


def test_older_settings_default_new_vision_permission_to_false(settings):
    first = RuntimeSettings(settings)
    first.update({"LLM_MAX_INPUT_CHARS": 8000})
    path = settings.paperlens_cache_dir / "ui-settings.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["values"].pop("PAPERLENS_VISION_ALLOW_UNVERIFIED")
    path.write_text(json.dumps(data), encoding="utf-8")
    restored = RuntimeSettings(settings).snapshot()
    assert restored.llm_max_input_chars == 8000
    assert not restored.paperlens_vision_allow_unverified


@pytest.mark.parametrize("key", runtime._ADDED_LATER)
def test_settings_saved_before_a_budget_field_existed_still_load(settings, key):
    first = RuntimeSettings(settings)
    first.update({"LLM_MAX_INPUT_CHARS": 8000})
    path = settings.paperlens_cache_dir / "ui-settings.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["values"].pop(key)
    path.write_text(json.dumps(data), encoding="utf-8")
    restored = RuntimeSettings(settings).snapshot()
    assert restored.llm_max_input_chars == 8000
    name = runtime._FIELDS[key].name
    assert getattr(restored, name) == getattr(settings, name)


def test_every_budget_field_added_after_round_one_is_migrated():
    # A field missing here makes every older ui-settings.json fail to load at startup.
    assert "PAPERLENS_TEXT_REVIEW_ENABLED" in runtime._ADDED_LATER
    assert set(runtime._ADDED_LATER) <= set(runtime._FIELDS)
