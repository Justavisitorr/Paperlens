"""Command-line boundaries for AI analysis and the local web app."""

from __future__ import annotations

import importlib.util
import json
import math
import os
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError as SettingsValidationError

from .config import Settings
from .errors import PaperLensError, ProviderError

app = typer.Typer(
    help="PaperLens：输入 PDF，生成带原文证据的离线论文阅读看板。",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)




def _settings(output_dir: Path | None = None) -> Settings:
    try:
        settings = Settings()
    except (SettingsValidationError, OSError) as exc:
        typer.echo("配置读取失败，请核对 .env 中的字段格式和文件权限。", err=True)
        raise typer.Exit(1) from exc
    updates = {}
    if output_dir is not None:
        updates["paperlens_output_dir"] = output_dir
    return settings.model_copy(update=updates)


def _public_error(exc: Exception, settings: Settings) -> str:
    if not isinstance(exc, PaperLensError):
        return "操作未完成，请检查文件权限、依赖与配置后重试。"
    message = str(exc)
    secret = settings.llm_api_key.get_secret_value()
    return message.replace(secret, "[已隐藏]") if secret else message


def _pipeline_progress():
    previous: tuple[str, int] | None = None

    def progress(stage: str, value: float, message: str) -> None:
        nonlocal previous
        fraction = min(1.0, max(0.0, value)) if math.isfinite(value) else 0.0
        current = (stage, int(fraction * 20))
        if current != previous:
            typer.echo(f"[{round(fraction * 100):3d}%] {message}", err=True)
            previous = current

    return progress


@app.command()
def analyze(
    pdf_path: Annotated[Path, typer.Argument(help="待分析的 PDF 文件路径。")],
    output_dir: Annotated[Path | None, typer.Option("--output-dir", help="结果保存目录。")] = None,
) -> None:
    """一键提取、分析、校验导图并生成离线 HTML，无需补充提示词。"""
    settings = _settings(output_dir)
    try:
        from .pipeline import run_pipeline

        result = run_pipeline(pdf_path, settings, progress=_pipeline_progress())
    except Exception as exc:
        typer.echo(_public_error(exc, settings), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"看板：{result.report_path.resolve()}")
    typer.echo(f"处理结果：{result.output_dir.resolve()}")
    if result.report.run.status == "partial":
        typer.echo("已生成部分结果，请先查看看板中的质量提示；本次未视为完整成功。", err=True)
        raise typer.Exit(2)




def _writable(path: Path) -> bool:
    candidate = path.resolve()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate.is_dir() and os.access(candidate, os.W_OK)


@app.command()
def doctor(
    check_api: Annotated[
        bool, typer.Option("--api", help="额外执行一次极小的真实 JSON 模型请求。")
    ] = False,
) -> None:
    """检查依赖与配置；配置只输出布尔状态，不输出密钥、地址或模型值。"""
    settings = _settings()
    from .reading_edition import vision_ready

    try:
        from .exporting import get_dashboard_template

        dashboard_ready = "__PAPERLENS_DATA__" in get_dashboard_template()
    except (ImportError, OSError, PaperLensError):
        dashboard_ready = False
    checks = {
        "api_key_configured": bool(settings.llm_api_key.get_secret_value().strip())
        and settings.llm_api_key.get_secret_value().strip() != "replace_with_your_key",
        "model_configured": bool(settings.llm_model.strip()),
        "base_url_valid": bool(settings.llm_base_url),
        "pdf_renderer_ready": importlib.util.find_spec("pypdfium2") is not None,
        "vision_model_confirmed": vision_ready(settings.for_role("vision")),
        "dashboard_ready": dashboard_ready,
        "output_directory_writable": _writable(settings.paperlens_output_dir),
        "cache_directory_writable": _writable(settings.paperlens_cache_dir),
        "api_checked": check_api,
        "api_ok": False,
    }
    error: str | None = None
    if check_api:
        try:
            from .provider import JsonProvider

            with JsonProvider(settings) as provider:
                reply = provider.json(
                    "这是软件接口的 JSON 连通性检查。",
                    '返回 JSON：{"ok": true}',
                    max_tokens=64,
                    reasoning_effort="none",
                )
            if reply.get("ok") is not True:
                raise ProviderError("模型已响应，但未通过 JSON 格式连通性检查。")
            checks["api_ok"] = True
        except Exception as exc:
            error = _public_error(exc, settings)
    typer.echo(json.dumps(checks, ensure_ascii=False, indent=2))
    if error:
        typer.echo(error, err=True)
    required = [value for key, value in checks.items() if key not in {"api_checked", "api_ok"}]
    if not all(required) or (check_api and not checks["api_ok"]):
        raise typer.Exit(1)


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="监听地址，默认只允许本机访问。")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535, help="本地服务端口。")] = 8000,
) -> None:
    """启动本地上传页面；上传 PDF 后自动排队处理。"""
    settings = _settings()
    try:
        import uvicorn

        from .server import create_app

        typer.echo(f"请在浏览器打开 http://{host}:{port}")
        uvicorn.run(create_app(settings), host=host, port=port, workers=1, access_log=False)
    except Exception as exc:
        typer.echo(_public_error(exc, settings), err=True)
        raise typer.Exit(1) from exc
