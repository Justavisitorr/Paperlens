"""Validated, hot-reloadable settings for the local workspace.

Each job takes an independent snapshot. The UI receives a form description, never
credentials. A single JSON rename commits both settings and a reference to an
immutable local secret file; a failed save cannot mix a new host with an old key.
Secret files are local plaintext (like .env), not encrypted credential storage.
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import SecretStr
from pydantic import ValidationError as PydanticValidationError

from .catalog import CatalogParseError, get_model, is_official_deepseek, load_catalog
from .config import Settings
from .errors import ConfigurationError
from .storage import write_json
from .web_policy import WorkspacePolicy


@dataclass(frozen=True)
class _Field:
    name: str
    label: str
    kind: str
    help: str = ""
    unit: str = ""
    options: tuple[tuple[str, str], ...] = ()

    @property
    def key(self) -> str:
        return self.name.upper()


_GROUPS = (
    (
        "model",
        "模型服务",
        "选择模型服务，供新任务使用。",
        (
            _Field(
                "llm_base_url",
                "服务地址",
                "text",
                "更换地址时，请同时更换密钥。",
            ),
            _Field("llm_model", "分析模型", "text", "选择用于分析论文的模型。"),
            _Field(
                "llm_api_key",
                "访问密钥",
                "secret",
                "仅存本机，留空保留原密钥。",
            ),
            _Field(
                "llm_reasoning_effort",
                "思考强度",
                "select",
                "想得越多，通常越慢、越费额度。",
                options=(
                    ("default", "服务默认"),
                    ("none", "不思考"),
                    ("low", "少想一点"),
                    ("medium", "标准思考"),
                    ("high", "多想一点"),
                    ("xhigh", "深入思考（xhigh）"),
                    ("max", "尽量多想"),
                ),
            ),
            _Field(
                "paperlens_vision_model",
                "看图模型",
                "text",
                "留空使用分析模型，需支持看图。",
            ),
            _Field(
                "paperlens_vision_allow_unverified",
                "确认看图",
                "boolean",
                "请先向服务商确认看图能力。",
            ),
        ),
    ),
    (
        "translation",
        "阅读整理",
        "AI 直读全部原页；按需启用审校与翻译。",
        (
            _Field("paperlens_math_enabled", "公式审校", "boolean", "对照原 PDF 核对复杂公式，按档位安排审校批次。"),
            _Field(
                "paperlens_text_review_enabled",
                "正文审校",
                "boolean",
                "对照原页修正乱码、断字和串行，在翻译与笔记前执行，计入总请求预算。",
            ),
            _Field(
                "paperlens_auto_translate_enabled",
                "自动翻译",
                "boolean",
                "英文译成中文，保留原文供对照。",
            ),
        ),
    ),
    (
        "auto_notes",
        "自动笔记",
        "三档共用，阅读时也可手动生成。",
        (
            _Field(
                "paperlens_auto_notes_enabled",
                "处理后写",
                "boolean",
                "使用阅读助手，可随时手动生成。",
            ),
            _Field(
                "paperlens_auto_notes_max_paragraphs",
                "笔记数量",
                "number",
                "每次最多为几处内容写笔记。",
                "段",
            ),
            _Field(
                "paperlens_auto_notes_context",
                "参考范围",
                "select",
                "范围越大，参考的内容越多。",
                options=(
                    ("nearby", "附近段落"),
                    ("section", "当前小节"),
                    ("paper", "全文相关"),
                ),
            ),
            _Field("paperlens_auto_notes_summary", "要点摘要", "boolean"),
            _Field("paperlens_auto_notes_explain", "解释概念", "boolean"),
            _Field("paperlens_auto_notes_connections", "前后联系", "boolean"),
            _Field("paperlens_auto_notes_questions", "延伸提问", "boolean"),
            _Field(
                "paperlens_auto_notes_check",
                "疑点核对",
                "boolean",
                "提示可疑之处，供对照原文。",
            ),
        ),
    ),
    (
        "limits",
        "固定上限",
        "限制花费与等待，三档共用。",
        (
            _Field(
                "paperlens_reading_output_tokens",
                "分析长度",
                "number",
                "内部逐段分析的回答长度。",
                "tokens",
            ),
            _Field(
                "llm_max_requests",
                "请求次数",
                "number",
                "每篇调用上限，含失败重试。",
                "次",
            ),
            _Field(
                "llm_max_output_tokens",
                "回答长度",
                "number",
                "模型单次回答的最长长度。",
                "tokens",
            ),
            _Field(
                "llm_max_input_chars",
                "分段长度",
                "number",
                "每次交给模型阅读的文字量。",
                "字符",
            ),
            _Field("llm_timeout_seconds", "等待时间", "number", unit="秒"),
            _Field("llm_max_retries", "重试次数", "number", unit="次"),
            _Field(
                "paperlens_chat_max_requests",
                "问答次数",
                "number",
                "一次提问最多调用几次模型。",
                "次",
            ),
        ),
    ),
    (
        "files",
        "文件读取",
        "设置文件大小与读取方式。",
        (
            _Field(
                "paperlens_reference_network",
                "公开书目查询",
                "boolean",
                "查询公开书目，匹配原文链接，并在展开引用树时获取下一级参考文献。",
            ),
            _Field("paperlens_max_pages", "页数上限", "number", unit="页"),
            _Field("paperlens_max_upload_mb", "文件大小", "number", unit="MB"),
            _Field(
                "paperlens_preview_pages",
                "原页图片",
                "number",
                "保存几页预览，设为零则不保存。",
                "页",
            ),
            _Field(
                "paperlens_max_parallel_jobs",
                "同时处理",
                "number",
                "同时处理越多，内存占用越大。",
                "篇",
            ),
        ),
    ),
    (
        "history",
        "最近任务",
        "仅清理记录，保留论文与笔记。",
        (
            _Field(
                "paperlens_job_history_limit",
                "保留条数",
                "number",
                "保留已结束任务，零为不自动清理。",
                "条",
            ),
        ),
    ),
    (
        "catalog",
        "参考价格",
        "联网获取公开资料，价格仅供参考。",
        (
            _Field(
                "paperlens_catalog_network",
                "联网更新",
                "boolean",
                "关闭后只使用本机目录。",
            ),
            _Field(
                "paperlens_catalog_auto_update",
                "自动更新",
                "boolean",
                "资料过期后自动联网更新。",
            ),
            _Field("paperlens_catalog_max_age_hours", "更新间隔", "number", unit="小时"),
        ),
    ),
)

_RETIRED_FIELDS = {'PAPERLENS_CODE_MODE', 'PAPERLENS_INLINE_MATH_MAX_REGIONS', 'PAPERLENS_MATH_DEVICE', 'PAPERLENS_MATH_MAX_REGIONS', 'PAPERLENS_MATH_TIMEOUT_SECONDS', 'PAPERLENS_PARSE_TIMEOUT_SECONDS', 'PAPERLENS_PARSER', 'PAPERLENS_READING_MODE', 'PAPERLENS_TRANSCRIPTION_MAX_REGIONS', 'PAPERLENS_VISION_ENABLED', 'PAPERLENS_VISION_MAX_PAGES'}
_FIELDS = {field.key: field for _, _, _, fields in _GROUPS for field in fields}
# Settings that became editable after the first saved-record format.
_ADDED_LATER = (
    "PAPERLENS_TEXT_REVIEW_ENABLED",
    "PAPERLENS_AUTO_TRANSLATE_ENABLED",
    "PAPERLENS_JOB_HISTORY_LIMIT",
    "PAPERLENS_REFERENCE_NETWORK",
    "PAPERLENS_READING_OUTPUT_TOKENS",
    "PAPERLENS_MATH_ENABLED",
    "PAPERLENS_AUTO_NOTES_ENABLED",
    "PAPERLENS_AUTO_NOTES_MAX_PARAGRAPHS",
    "PAPERLENS_AUTO_NOTES_CONTEXT",
    "PAPERLENS_AUTO_NOTES_SUMMARY",
    "PAPERLENS_AUTO_NOTES_EXPLAIN",
    "PAPERLENS_AUTO_NOTES_CONNECTIONS",
    "PAPERLENS_AUTO_NOTES_QUESTIONS",
    "PAPERLENS_AUTO_NOTES_CHECK",
)
_SECRET_NAME = re.compile(r"[0-9a-f]{32}\.env\Z")


def _bounds(field: _Field) -> dict[str, int | float]:
    result = {}
    for constraint in Settings.model_fields[field.name].metadata:
        for attr, key in (("ge", "min"), ("le", "max")):
            value = getattr(constraint, attr, None)
            if value is not None:
                result[key] = value
    return result


class RuntimeSettings:
    """Thread-safe workspace settings; paths cannot be changed through the API."""

    def __init__(self, settings: Settings, *, policy: WorkspacePolicy | None = None) -> None:
        self.policy = policy
        self._lock = threading.RLock()
        self._settings = settings.model_copy(deep=True)
        self._path = settings.paperlens_cache_dir / "ui-settings.json"
        self._secret_dir = settings.paperlens_cache_dir / "runtime-secrets"
        self._updated_at: str | None = None
        self._secret_name: str | None = None
        if self._path.exists():
            self._restore()

    def snapshot(self) -> Settings:
        with self._lock:
            return self._settings.model_copy(deep=True)

    def _catalog(self) -> Any:
        return load_catalog(
            self._settings.paperlens_cache_dir,
            allow_network=False,
            max_age_hours=self._settings.paperlens_catalog_max_age_hours,
        )

    def public(self) -> dict[str, Any]:
        """Describe a UI form using local metadata only, even if updates are allowed."""
        with self._lock:
            settings = self._settings
            official = is_official_deepseek(settings.llm_base_url)
            try:
                catalog = self._catalog()
            except (CatalogParseError, OSError, ValueError):
                catalog = None
            models = catalog.models if official and catalog else []
            selected = next((m for m in models if m.id == settings.llm_model), None)
            groups = []
            for group_id, title, description, specs in _GROUPS:
                fields = []
                for spec in specs:
                    value = getattr(settings, spec.name)
                    field: dict[str, Any] = {
                        "key": spec.key,
                        "label": spec.label,
                        "type": spec.kind,
                        "value": None if spec.kind == "secret" else value,
                    }
                    if spec.help:
                        field["help"] = spec.help
                    if spec.unit:
                        field["unit"] = spec.unit
                    if spec.kind == "secret":
                        field["configured"] = bool(value.get_secret_value().strip())
                    if spec.kind == "number":
                        field.update(_bounds(spec))
                        if self.policy and spec.name == "paperlens_max_upload_mb":
                            field["max"] = self.policy.upload_mb
                        if self.policy and spec.name == "paperlens_max_parallel_jobs":
                            field["max"] = self.policy.parallel_jobs
                        field["step"] = 1
                    if spec.options:
                        choices = list(spec.options)
                        if spec.name == "llm_reasoning_effort" and selected:
                            supported = selected.reasoning_efforts
                            if supported is not None:
                                choices = [
                                    item
                                    for item in choices
                                    if item[0]
                                    in (
                                        *supported,
                                        "none",
                                        "default",
                                        value,
                                    )
                                ]
                        field["options"] = [{"value": v, "label": label} for v, label in choices]
                    if spec.name in {"llm_model", "paperlens_vision_model"} and models:
                        choices = [
                            (model.id, model.name or model.id)
                            for model in models
                            if spec.name == "llm_model" or model.vision == "supported"
                        ]
                        if spec.name == "paperlens_vision_model":
                            choices.insert(0, ("", "跟随分析模型"))
                        if value not in {v for v, _ in choices}:
                            choices.append((value, f"{value}（当前设置）"))
                        field["type"] = "select"
                        field["options"] = [{"value": v, "label": label} for v, label in choices]
                    if spec.name == "llm_max_output_tokens" and selected:
                        if selected.max_output_tokens:
                            field["max"] = min(field["max"], selected.max_output_tokens)
                    fields.append(field)
                if group_id == "catalog":
                    if official and catalog:
                        state = "已过期，建议更新" if catalog.stale else "在有效期内"
                        description += f" 最近核验：{catalog.checked_at.date()}，{state}。"
                    else:
                        description += " 当前服务的能力和价格尚未核验，费用显示为未知。"
                groups.append(
                    {"id": group_id, "title": title, "description": description, "fields": fields}
                )
            public_catalog = (
                catalog.model_dump(mode="json")
                if official and catalog
                else {
                    "provider": "unknown",
                    "status": "unknown",
                    "stale": True,
                    "models": [],
                    "network_checked": False,
                    "warnings": ["provider_metadata_unavailable"],
                }
            )
            return {
                "groups": groups,
                "meta": {
                    "source": "本机设置" if self._updated_at else "启动配置",
                    "updated_at": self._updated_at,
                    "stale": False,
                },
                "catalog": public_catalog,
            }

    def _validated(self, values: dict[str, Any]) -> tuple[Settings | None, dict[str, str]]:
        data = self._settings.model_dump()
        errors = {}
        for key, value in values.items():
            spec = _FIELDS.get(key)
            if spec is None:
                errors[key] = "此设置不能从页面修改。"
                continue
            if spec.kind == "secret" and (
                value is None or value == "" or (isinstance(value, str) and not value.strip())
            ):
                continue
            if spec.kind in {"text", "select", "secret"}:
                if not isinstance(value, str):
                    errors[key] = "请输入文本。"
                    continue
                value = value.strip()
                limit = (
                    4096 if spec.kind == "secret" else 2048 if spec.name == "llm_base_url" else 200
                )
                if len(value) > limit or any(ord(char) < 32 or ord(char) == 127 for char in value):
                    errors[key] = "内容过长或包含不允许的控制字符。"
                    continue
                if spec.kind == "secret" and any(not 32 < ord(char) < 127 for char in value):
                    errors[key] = "密钥不能包含空白或非 ASCII 字符。"
                    continue
                if not value and spec.name not in {"paperlens_vision_model"}:
                    errors[key] = "此项不能为空。"
                    continue
                if spec.options and value not in {v for v, _ in spec.options}:
                    errors[key] = "请选择列表中的有效选项。"
                    continue
            elif spec.kind == "boolean" and type(value) is not bool:
                errors[key] = "请选择开启或关闭。"
                continue
            elif spec.kind == "number":
                if type(value) not in (int, float) or (
                    isinstance(value, float) and not math.isfinite(value)
                ):
                    errors[key] = "请输入有限的数字。"
                    continue
                if spec.name != "llm_timeout_seconds" and int(value) != value:
                    errors[key] = "请输入整数。"
                    continue
                bounds = _bounds(spec)
                if value < bounds["min"] or value > bounds["max"]:
                    errors[key] = f"请输入 {bounds['min']} 到 {bounds['max']} 之间的数值。"
                    continue
            data[spec.name] = value
        if errors:
            return None, errors
        try:
            candidate = type(self._settings)(_env_file=None, **data)
        except PydanticValidationError as exc:
            # Pydantic's usual message contains input values; never relay it.
            for item in exc.errors(include_input=False, include_context=False):
                key = str(item["loc"][0]).upper()
                errors[key] = (
                    "服务地址须为完整 HTTP(S) 地址，且不含凭据、查询参数或片段。"
                    if (key == "LLM_BASE_URL")
                    else "该值不符合此设置的要求。"
                )
            return None, errors
        if "PAPERLENS_VISION_ALLOW_UNVERIFIED" not in values and any(
            getattr(candidate, key) != getattr(self._settings, key)
            for key in ("llm_base_url", "llm_model", "paperlens_vision_model")
        ):
            candidate.paperlens_vision_allow_unverified = False
        if self.policy:
            try:
                self.policy.check_endpoint(candidate.llm_base_url)
            except ValueError as exc:
                errors["LLM_BASE_URL"] = str(exc)
            if candidate.paperlens_max_upload_mb > self.policy.upload_mb:
                errors["PAPERLENS_MAX_UPLOAD_MB"] = f"服务器每篇上限为 {self.policy.upload_mb} MB。"
            if candidate.paperlens_max_parallel_jobs > self.policy.parallel_jobs:
                errors["PAPERLENS_MAX_PARALLEL_JOBS"] = f"当前服务器每个账户最多并行 {self.policy.parallel_jobs} 篇。"
            if errors:
                return None, errors
        new_secret = values.get("LLM_API_KEY")
        if candidate.llm_base_url != self._settings.llm_base_url and not (
            isinstance(new_secret, str) and new_secret.strip()
        ):
            candidate.llm_api_key = SecretStr("")
        return candidate, {}

    def update(self, values: dict[str, Any]) -> dict[str, Any]:
        """Commit all supplied values together, returning frontend field errors."""
        with self._lock:
            if not isinstance(values, dict):
                return {"errors": {"LLM_MODEL": "设置提交格式无效，请刷新后重试。"}}
            candidate, errors = self._validated(values)
            if candidate is None:
                return {"errors": errors}
            if candidate == self._settings:
                return {"schema": self.public()}
            try:
                catalog = self._catalog()
            except (CatalogParseError, OSError, ValueError):
                catalog = None
            if catalog:
                info = get_model(catalog, candidate.llm_model, base_url=candidate.llm_base_url)
                if (
                    info.max_output_tokens
                    and candidate.llm_max_output_tokens > info.max_output_tokens
                ):
                    errors["LLM_MAX_OUTPUT_TOKENS"] = (
                        f"所选模型当前目录的输出上限为 {info.max_output_tokens} tokens。"
                    )
                if info.reasoning_efforts is not None and candidate.llm_reasoning_effort not in (
                    *info.reasoning_efforts,
                    "none",
                    "default",
                ):
                    errors["LLM_REASONING_EFFORT"] = "所选模型不支持此思考强度。"
                if True:
                    vision = get_model(
                        catalog,
                        candidate.paperlens_vision_model or candidate.llm_model,
                        base_url=candidate.llm_base_url,
                    )
                    if vision.vision == "unsupported":
                        errors["PAPERLENS_VISION_MODEL"] = (
                            "此模型不支持图片，请为 AI 直读选择支持图片的模型。"
                        )
            if errors:
                return {"errors": errors}
            try:
                self._commit(candidate)
            except (OSError, ValueError):
                keys = values.keys() or ("LLM_MODEL",)
                return {
                    "errors": {
                        key: "设置未保存，请检查本机目录权限和剩余空间后重试。" for key in keys
                    }
                }
            return {"schema": self.public()}

    def _secret_path(self, name: str) -> Path:
        if not isinstance(name, str) or not _SECRET_NAME.fullmatch(name):
            raise ValueError("Invalid local credential reference")
        if self._secret_dir.resolve() != self._path.parent.resolve() / "runtime-secrets":
            raise ValueError("Invalid local credential directory")
        path = self._secret_dir / name
        if path.is_symlink() or path.resolve().parent != self._secret_dir.resolve():
            raise ValueError("Invalid local credential path")
        return path

    def _commit(self, candidate: Settings) -> None:
        secret_name = f"{uuid.uuid4().hex}.env"
        self._secret_dir.mkdir(parents=True, exist_ok=True)
        secret_path = self._secret_path(secret_name)
        stored_key = candidate.llm_api_key.get_secret_value()
        if self.policy:
            stored_key = self.policy.seal(stored_key)
        payload = "LLM_API_KEY=" + json.dumps(stored_key) + "\n"
        updated_at = datetime.now(UTC).isoformat()
        record = {
            "version": 1,
            "updated_at": updated_at,
            "secret_file": secret_name,
            "values": {
                key: getattr(candidate, spec.name)
                for key, spec in _FIELDS.items()
                if spec.kind != "secret"
            },
        }
        committed = False
        try:
            descriptor = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            write_json(self._path, record)
            committed = True
        finally:
            if not committed:
                secret_path.unlink(missing_ok=True)
        # Readers see the new JSON only after its referenced secret exists.
        self._settings = candidate
        self._updated_at = updated_at
        self._secret_name = secret_name

    def _restore(self) -> None:
        try:
            record = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(record, dict) or record.get("version") != 1:
                raise ValueError("Invalid local settings record")
            updated = datetime.fromisoformat(record["updated_at"])
            if updated.tzinfo is None:
                raise ValueError("Missing settings timestamp timezone")
            values = record["values"]
            if not isinstance(values, dict) or "LLM_API_KEY" in values:
                raise ValueError("Invalid public settings record")
            values = {key: value for key, value in values.items() if key not in _RETIRED_FIELDS}
            # Initial round-one records predate this opt-in. Migrate only this
            # additive permission as false; missing host/model fields still fail.
            values = {"PAPERLENS_VISION_ALLOW_UNVERIFIED": False, **values}
            # Budget fields added after a record was written keep their startup value.
            values = {
                **{key: getattr(self._settings, _FIELDS[key].name) for key in _ADDED_LATER},
                **values,
            }
            # Persisted settings and credential always form one complete pair.
            if set(values) != {key for key, spec in _FIELDS.items() if spec.kind != "secret"}:
                raise ValueError("Incomplete settings record")
            path = self._secret_path(record["secret_file"])
            secrets = dotenv_values(
                stream=io.StringIO(path.read_text(encoding="utf-8")), interpolate=False
            )
            if set(secrets) != {"LLM_API_KEY"} or secrets["LLM_API_KEY"] is None:
                raise ValueError("Invalid credential file")
            stored_key = secrets["LLM_API_KEY"]
            if self.policy:
                stored_key = self.policy.unseal(stored_key)
            values["LLM_API_KEY"] = stored_key
            candidate, errors = self._validated(values)
            if candidate is None or errors:
                raise ValueError("Invalid persisted settings")
            # Empty persisted keys must override any startup .env key. A blank
            # form field means keep; a blank saved secret means explicitly none.
            candidate.llm_api_key = SecretStr(stored_key)
            self._settings = candidate
            self._updated_at = updated.isoformat()
            self._secret_name = record["secret_file"]
        except (OSError, ValueError, TypeError, KeyError):
            raise ConfigurationError(
                "本机保存的设置无法完整读取，请恢复 ui-settings.json 及其配套密钥文件后重试。"
            ) from None
