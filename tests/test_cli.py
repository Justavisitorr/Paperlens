"""CLI contract tests substitute parsing/provider work; no real API costs."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from paperlens import cli, exporting, pipeline, provider
from paperlens.config import Settings
from paperlens.errors import InputError
from paperlens.models import Block, Box, Page, PaperDocument

runner = CliRunner()


@pytest.fixture
def settings(tmp_path: Path, monkeypatch) -> Settings:
    config = Settings(
        _env_file=None,
        llm_api_key="private-test-key",
        llm_base_url="https://private-test-provider.example",
        llm_model="private-test-model",
        paperlens_output_dir=tmp_path / "outputs",
        paperlens_vision_allow_unverified=True, paperlens_cache_dir=tmp_path / "cache",
    )
    monkeypatch.setattr(cli, "Settings", lambda: config)
    return config


def document(*, partial: bool = False) -> PaperDocument:
    return PaperDocument(
        title="测试论文",
        source_name="输入.pdf",
        sha256="a" * 64,
        parser="pdfplumber",
        pages=[
            Page(number=1, width=600, height=800, text_chars=4),
            Page(number=2, width=600, height=800, status="image_only" if partial else "blank"),
        ],
        blocks=[
            Block(
                id="b1",
                page=1,
                text="测试正文",
                raw_text="测试正文",
                order=0,
                bbox=Box(x0=0.1, y0=0.1, x1=0.9, y1=0.2),
            )
        ],
    )


def test_help_exposes_all_entry_points():
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for command in ("analyze", "doctor", "serve"):
        assert command in result.output


@pytest.mark.parametrize("status,exit_code", [("complete", 0), ("partial", 2)])
def test_analyze_uses_common_pipeline_and_honors_partial(
    monkeypatch, settings: Settings, status: str, exit_code: int
):
    captured = {}

    def fake(path, config, *, progress):
        captured.update(path=path, config=config)
        progress("parsing", 0.2, "正文提取")
        return SimpleNamespace(
            output_dir=config.paperlens_output_dir,
            report_path=config.paperlens_output_dir / "dashboard.html",
            report=SimpleNamespace(run=SimpleNamespace(status=status)),
        )

    monkeypatch.setattr(pipeline, "run_pipeline", fake)
    output = settings.paperlens_output_dir / "自定义结果"
    result = runner.invoke(
        cli.app,
        ["analyze", "有 空格的论文.pdf", "--output-dir", str(output)],
    )
    assert result.exit_code == exit_code, result.output
    assert captured["path"] == Path("有 空格的论文.pdf")
    assert captured["config"].paperlens_reading_mode == "ai_direct"
    assert captured["config"].paperlens_output_dir == output
    assert "dashboard.html" in result.output


def test_analyze_errors_do_not_show_credentials(monkeypatch, settings: Settings):
    def fail(*args, **kwargs):
        raise InputError(f"无法读取文件 {settings.llm_api_key.get_secret_value()}")

    monkeypatch.setattr(pipeline, "run_pipeline", fail)
    result = runner.invoke(cli.app, ["analyze", "invalid.pdf"])
    assert result.exit_code == 1
    assert "无法读取文件" in result.output
    assert settings.llm_api_key.get_secret_value() not in result.output




def test_doctor_configuration_output_contains_only_booleans(monkeypatch, settings: Settings):
    monkeypatch.setattr(exporting, "get_dashboard_template", lambda: "__PAPERLENS_DATA__")
    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data and all(type(value) is bool for value in data.values())
    assert data["api_checked"] is False
    for private in (
        settings.llm_api_key.get_secret_value(),
        settings.llm_model,
        settings.llm_base_url,
    ):
        assert private not in result.output


def test_doctor_api_is_small_json_request_without_reasoning(monkeypatch, settings: Settings):
    options = {}

    class FakeProvider:
        def __init__(self, config):
            assert config.llm_api_key == settings.llm_api_key
            assert config.llm_model == settings.llm_model

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def json(self, system, user, **kwargs):
            options.update(kwargs)
            return {"ok": True}

    monkeypatch.setattr(provider, "JsonProvider", FakeProvider)
    monkeypatch.setattr(exporting, "get_dashboard_template", lambda: "__PAPERLENS_DATA__")
    result = runner.invoke(cli.app, ["doctor", "--api"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["api_ok"] is True
    assert options == {"max_tokens": 64, "reasoning_effort": "none"}


def test_missing_assets_are_reported_by_doctor(monkeypatch, settings: Settings):
    def missing():
        raise FileNotFoundError("private filesystem path")

    monkeypatch.setattr(exporting, "get_dashboard_template", missing)
    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["dashboard_ready"] is False
    assert "private filesystem path" not in result.output


def test_serve_defaults_to_loopback_and_one_process(monkeypatch, settings: Settings):
    import uvicorn

    captured = {}
    monkeypatch.setattr(uvicorn, "run", lambda application, **kwargs: captured.update(kwargs))
    result = runner.invoke(cli.app, ["serve"])
    assert result.exit_code == 0, result.output
    assert captured["host"] == "127.0.0.1"
    assert captured["workers"] == 1
    assert captured["access_log"] is False
