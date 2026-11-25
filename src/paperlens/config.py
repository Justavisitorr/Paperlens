"""Configuration is loaded at the boundary, never embedded in report data."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import ConfigurationError


class ModelRoute(BaseModel):
    """A resolved account binding; secrets never belong in public reports."""

    account_id: str
    model: str
    base_url: str
    api_key: SecretStr = SecretStr("")
    protocol: Literal["openai", "anthropic"] = "openai"
    requires_key: bool = True
    vision_confirmed: bool = False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8-sig",
        extra="ignore",
    )

    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-flash"
    llm_protocol: Literal["openai", "anthropic"] = "openai"
    llm_requires_key: bool = True
    llm_account_id: str = "env"
    llm_routes: dict[str, ModelRoute] = Field(default_factory=dict)
    llm_timeout_seconds: float = Field(default=180, ge=5, le=1800)
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    llm_max_requests: int = Field(default=120, ge=1, le=1000)
    llm_max_input_chars: int = Field(default=18000, ge=2000, le=1000000)
    llm_max_output_tokens: int = Field(default=48000, ge=512, le=1000000)
    llm_reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max", "default"] = "low"
    paperlens_reading_mode: Literal["ai_direct"] = "ai_direct"
    paperlens_processing_profile: Literal["economy", "standard", "detailed"] = "standard"
    paperlens_reading_output_tokens: int = Field(default=8000, ge=512, le=1000000)
    paperlens_max_pages: int = Field(default=300, ge=1, le=2000)
    paperlens_output_dir: Path = Path("outputs")
    paperlens_cache_dir: Path = Path(".cache/paperlens")
    paperlens_max_upload_mb: int = Field(default=100, ge=1, le=1000)
    paperlens_preview_pages: int = Field(default=40, ge=0, le=500)
    paperlens_browser_channel: str = "chrome"
    paperlens_max_parallel_jobs: int = Field(default=1, ge=1, le=4)
    paperlens_job_history_limit: int = Field(default=30, ge=0, le=1000)
    paperlens_vision_model: str = ""
    paperlens_vision_allow_unverified: bool = False
    paperlens_math_enabled: bool = True
    paperlens_text_review_enabled: bool = True
    paperlens_chat_max_requests: int = Field(default=8, ge=1, le=40)
    paperlens_auto_translate_enabled: bool = False
    paperlens_auto_notes_enabled: bool = False
    paperlens_auto_notes_max_paragraphs: int = Field(default=6, ge=1, le=30)
    paperlens_auto_notes_context: Literal["nearby", "section", "paper"] = "section"
    paperlens_auto_notes_summary: bool = True
    paperlens_auto_notes_explain: bool = True
    paperlens_auto_notes_connections: bool = False
    paperlens_auto_notes_questions: bool = False
    paperlens_auto_notes_check: bool = False
    paperlens_catalog_network: bool = False
    paperlens_reference_network: bool = True
    paperlens_catalog_auto_update: bool = False
    paperlens_catalog_max_age_hours: int = Field(default=168, ge=1, le=2160)

    @field_validator("paperlens_reading_mode", mode="before")
    @classmethod
    def migrate_reading_mode(cls, value):
        # Saved settings and paused job snapshots from <=1.1 use classic.
        # They now enter the single current route; historical reports stay readable.
        return "ai_direct" if value in {"classic", "ai_direct"} else value

    @field_validator("llm_base_url")
    @classmethod
    def valid_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        parts = urlsplit(value)
        if parts.scheme not in {"https", "http"} or not parts.hostname:
            raise ValueError("LLM_BASE_URL 必须为完整的 HTTP(S) 地址")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("LLM_BASE_URL 不应包含鉴权信息、查询参数或片段")
        return value

    def require_provider(self) -> None:
        key = self.llm_api_key.get_secret_value().strip()
        if self.llm_requires_key and (not key or key == "replace_with_your_key"):
            raise ConfigurationError("请在网页设置中添加模型账户，或在 .env 中配置 LLM_API_KEY。")
        if not self.llm_model.strip():
            raise ConfigurationError("请配置 LLM_MODEL 为验收指定的模型名称。")

    def for_role(self, role: str) -> Settings:
        route = self.llm_routes.get(role) or self.llm_routes.get("main")
        if route is None:
            return self.model_copy(deep=True, update={"llm_routes": {}})
        return self.model_copy(
            deep=True,
            update={
                "llm_base_url": route.base_url,
                "llm_model": route.model,
                "llm_api_key": route.api_key,
                "llm_protocol": route.protocol,
                "llm_requires_key": route.requires_key,
                "llm_account_id": route.account_id,
                "llm_routes": {},
                "paperlens_vision_allow_unverified": route.vision_confirmed
                if role == "vision"
                else self.paperlens_vision_allow_unverified,
            },
        )

    def request_reasoning_effort(self, stage_hint: str | None = None) -> str:
        """An explicit OpenAI experiment setting applies throughout the run."""
        if urlsplit(self.llm_base_url).hostname == "api.openai.com":
            effort = self.llm_reasoning_effort
            if effort == "none" and self.llm_model in {"gpt-6-astra", "gpt-6.1-sol"}:
                raise ConfigurationError("所选模型需要 low 或更高的思考强度，请在设置中调整。")
            return effort
        return stage_hint or self.llm_reasoning_effort

    def public_configuration(self) -> dict:
        data = self.model_dump(
            mode="json",
            exclude={
                "llm_api_key",
                "llm_routes",
                "paperlens_output_dir",
                "paperlens_cache_dir",
            },
        )
        data["llm_routes"] = {
            role: route.model_dump(mode="json", exclude={"api_key"})
            for role, route in self.llm_routes.items()
        }
        return data

    def prepare_local_paths(self) -> None:
        self.paperlens_cache_dir.mkdir(parents=True, exist_ok=True)
        self.paperlens_output_dir.mkdir(parents=True, exist_ok=True)
        root = self.paperlens_cache_dir.resolve().parent
        for name, relative in {
            "PLAYWRIGHT_BROWSERS_PATH": "browsers",
            "XDG_CACHE_HOME": "xdg",
        }.items():
            os.environ.setdefault(name, str(root / relative))

    def public_summary(self) -> dict[str, str | bool]:
        return {
            "base_url": self.llm_base_url,
            "model": self.llm_model,
            "key_configured": bool(self.llm_api_key.get_secret_value()),
            "parser": "ai-direct",
        }
