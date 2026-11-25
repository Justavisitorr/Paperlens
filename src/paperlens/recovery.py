"""Explain failed stages from existing audits, without resubmitting model work."""

import json
import re
from pathlib import Path

from .models import StageFailure

LABELS = {
    "native_reading": "原 PDF 直读",
    "math_review": "公式审校",
    "text_review": "正文审校",
    "auto_notes": "自动笔记",
    "translation": "正文翻译",
    "analysis": "论文分析",
    "validation": "报告页面验证",
    "parsing": "PDF 解析",
    "verification": "论据核查",
}


def failure_detail(stage, model, row, item_pages=None):
    detail = row.get("provider_error") or {}
    message = str(
        row.get("reason") or row.get("message") or row.get("error") or "本步骤尚未完成。"
    )[:1400]
    code = row.get("error_code", "provider_error")
    match = re.search(r"HTTP\s+(\d{3})", message)
    status = detail.get("http_status") or (int(match[1]) if match else None)
    retryable = status not in {400, 401, 402, 403, 404, 405, 413, 422}
    guidance = "可补处理本步骤；已成功的批次会复用。"
    if status in {401, 403, 404}:
        guidance = "先检查本步骤所用模型、账户密钥和权限，再继续处理。"
    elif status == 402:
        guidance = "先检查所选模型账户的余额，再继续处理。"
    elif status in {400, 405, 413, 422}:
        guidance = "服务拒绝了请求格式或参数；先修正程序或模型设置，原样重试通常无效。"
    elif status == 429:
        guidance = "模型服务限流；稍后补处理，或降低并行数。"
    elif code == "provider_outcome_unknown":
        guidance = "等待连接恢复后再补处理；已提交的请求可能已计费，程序不会自动重复发送。"
    elif row.get("input_budget") or code == "input_budget_exceeded":
        guidance = "减少输入，或在预算弹窗中确认仅本次提高上限后继续。"
        retryable = False
    ids = row.get("ids") or ([row["item_id"]] if row.get("item_id") else [])
    pages = set(row.get("pages") or [])
    pages.update((item_pages or {})[ident] for ident in ids if ident in (item_pages or {}))
    return StageFailure(
        stage=stage,
        label=LABELS.get(stage, stage or "处理阶段"),
        model=model,
        pages=sorted(p for p in pages if type(p) is int and p > 0),
        items=len(ids),
        code=code,
        message=message,
        guidance=guidance,
        retryable=retryable,
        http_status=status,
        parameter=str(detail.get("param") or "")[:100],
    )


def stage_failures(directory: Path, model="", routes=None, report=None) -> list[StageFailure]:
    def read(name):
        path = directory / name
        try:
            if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
                return {}
            value = json.loads(path.read_text("utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    reading = read("reading-verification.json")
    stages = {
        "native_reading": read("native-reading-verification.json"),
        "math_review": reading.get("math_review", {}),
        "text_review": read("text-review.json") or reading.get("text_review", {}),
        "auto_notes": read("auto-notes-verification.json"),
        "translation": read("translation-verification.json"),
    }
    item_pages = (
        {item.id: item.page for item in report.reading.items} if report and report.reading else {}
    )
    grouped = {}
    routes = routes or {}
    for stage, audit in stages.items():
        role = "chat" if stage in {"auto_notes", "translation"} else "vision"
        routed = (
            audit.get("model")
            or (routes.get(role) or routes.get("main") or {}).get("model")
            or model
        )
        rows = [b for b in audit.get("batches", []) if b.get("status") in {"unavailable", "failed"}]
        rows.extend(audit.get("errors", []))
        if (
            not rows
            and audit.get("error_code")
            and audit.get("status") in {"partial", "unavailable"}
        ):
            rows = [audit]
        if not rows and audit.get("error"):
            rows = [audit]
        if stage == "translation" and audit.get("remaining") and not rows:
            rows = [
                {
                    "message": "；".join(audit.get("warnings", [])) or "部分英文段落未完成翻译。",
                    "ids": audit["remaining"],
                    "input_budget": audit.get("input_budget"),
                }
            ]
        for row in rows:
            failure = failure_detail(stage, routed, row, item_pages)
            key = (stage, routed, failure.code, failure.message)
            if key in grouped:
                previous = grouped[key]
                previous.pages = sorted(set(previous.pages) | set(failure.pages))
                previous.batches += 1
                previous.items += failure.items
            else:
                grouped[key] = failure
    return list(grouped.values())


def attach_failures(report, directory):
    if report.run.status == "partial" and not report.run.failures:
        report.run.failures = stage_failures(
            directory, report.run.model, report.run.model_routing, report
        )
    return report
