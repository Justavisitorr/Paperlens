"""Saved provider accounts, live model discovery, and per-task model assignments."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from threading import RLock
from uuid import uuid4

import httpx
from pydantic import SecretStr

from .config import ModelRoute, Settings
from .storage import write_json
from .web_policy import WorkspacePolicy

ROLES = [
    {"id": "main", "label": "主模型", "description": "整理论文结论并生成分析报告"},
    {"id": "reading", "label": "论文阅读", "description": "分段阅读与证据提取，默认使用主模型"},
    {
        "id": "verification",
        "label": "内容核查",
        "description": "核查术语、定义和前后矛盾，默认使用主模型",
    },
    {"id": "vision", "label": "图片与公式", "description": "阅读图表和公式，需要支持图片输入"},
    {
        "id": "chat",
        "label": "阅读问答",
        "description": "选词解释、侧边问答、翻译和自动笔记，默认使用主模型",
    },
    {
        "id": "conversation",
        "label": "对话",
        "description": "对话页面的单篇与多篇论文问答，默认使用主模型",
    },
]
ROLE_IDS = {item["id"] for item in ROLES}


def _empty_models() -> dict:
    return {"items": [], "status": "empty", "fetched_at": None}


def _vision(item: dict) -> str:
    """Use explicit API metadata only; a model's name is not a capability proof."""
    architecture = item.get("architecture") or {}
    inputs = architecture.get("input_modalities", item.get("input_modalities"))
    if isinstance(inputs, list):
        return "supported" if "image" in inputs else "unsupported"
    capabilities = item.get("capabilities") or {}
    if isinstance(capabilities, dict):
        supported = capabilities.get("vision", capabilities.get("supports_vision"))
        if isinstance(supported, bool):
            return "supported" if supported else "unsupported"
        if isinstance(supported, dict) and isinstance(supported.get("supported"), bool):
            return "supported" if supported["supported"] else "unsupported"
    return "unknown"


class ConnectionStore:
    """One atomic local file; public responses never contain API credentials."""

    def __init__(self, settings: Settings, root: Path | None = None, *, transport=None, policy: WorkspacePolicy | None = None):
        self.settings = settings.model_copy(deep=True)
        self.root = root or settings.paperlens_output_dir.parent / ".paperlens"
        self.path = self.root / "connections.json"
        self.transport = transport
        self.policy = policy
        self._lock = RLock()
        self.providers = json.loads(
            files("paperlens").joinpath("assets/service-presets.json").read_text(encoding="utf-8")
        )["providers"]
        self._data = {
            "version": 1,
            "accounts": [],
            "routing": None,
            "env_models": _empty_models(),
            "env_endpoint": [settings.llm_base_url, settings.llm_protocol],
        }
        if self.path.exists():
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
            if self.policy:
                for account in self._data["accounts"]:
                    account["api_key"] = self.policy.unseal(account["api_key"])
        endpoint = [settings.llm_base_url, settings.llm_protocol]
        if self._data.get("env_endpoint") != endpoint:
            self._data["env_models"] = _empty_models()
            self._data["env_endpoint"] = endpoint
        for account in [*self._data["accounts"], {"models": self._data["env_models"]}]:
            models = account["models"]
            if models["status"] == "live":
                models["status"] = "cached"

    def update_environment(self, base: Settings) -> None:
        """Keep the legacy runtime/.env connection in sync after UI parameter saves."""
        with self._lock:
            changed = (
                self.settings.llm_base_url,
                self.settings.llm_protocol,
                self.settings.llm_api_key,
            ) != (base.llm_base_url, base.llm_protocol, base.llm_api_key)
            if changed:
                data = deepcopy(self._data)
                data["env_models"] = _empty_models()
                self._commit(data)
            self.settings = base.model_copy(deep=True)

    def _commit(self, data: dict) -> None:
        persisted = data
        if self.policy:
            persisted = deepcopy(data)
            for account in persisted["accounts"]:
                account["api_key"] = self.policy.seal(account["api_key"])
        write_json(self.path, persisted)
        self._data = data

    def _env(self, base: Settings | None = None) -> dict:
        base = base or self.settings
        provider = next(
            (
                p["id"]
                for p in self.providers
                if p["base_url"].rstrip("/") == base.llm_base_url.rstrip("/")
            ),
            "custom",
        )
        return {
            "id": "env",
            "name": ".env / 环境变量",
            "provider_id": provider,
            "base_url": base.llm_base_url,
            "api_key": base.llm_api_key.get_secret_value(),
            "protocol": base.llm_protocol,
            "requires_key": base.llm_requires_key,
            "readonly": True,
            "models": deepcopy(self._data["env_models"]),
        }

    def _account(self, account_id: str, base: Settings | None = None) -> dict:
        if account_id == "env":
            return self._env(base)
        for account in self._data["accounts"]:
            if account["id"] == account_id:
                return deepcopy(account)
        raise ValueError("模型账户不存在，请重新选择账户。")

    @staticmethod
    def _public_account(account: dict) -> dict:
        result = {key: value for key, value in account.items() if key != "api_key"}
        key = account.get("api_key", "")
        result["configured"] = (bool(key) and key != "replace_with_your_key") or not account[
            "requires_key"
        ]
        return deepcopy(result)

    def _routing(self, base: Settings | None = None) -> dict:
        if self._data["routing"] is not None:
            routing = deepcopy(self._data["routing"])
            # Preserve the model used by conversations before their slot was split out.
            routing.setdefault("conversation", deepcopy(routing.get("chat")))
            return routing
        return {
            role: (
                {"account_id": "env", "model": (base or self.settings).llm_model}
                if role == "main"
                else None
            )
            for role in ROLE_IDS
        }

    def public(self) -> dict:
        with self._lock:
            return {
                "providers": deepcopy(self.providers),
                "roles": deepcopy(ROLES),
                "accounts": [
                    self._public_account(a) for a in [self._env(), *self._data["accounts"]]
                ],
                "routing": self._routing(),
            }

    def save_account(self, payload: dict, account_id: str | None = None) -> dict:
        with self._lock:
            if account_id == "env":
                raise ValueError("环境变量账户只读；请新增网页账户。")
            previous = self._account(account_id) if account_id else None
            provider_id = payload.get(
                "provider_id", (previous or {}).get("provider_id", "deepseek")
            )
            preset = next((p for p in self.providers if p["id"] == provider_id), None)
            if preset is None:
                raise ValueError("请选择已有服务商，或选择自定义服务。")
            same_provider = previous and previous["provider_id"] == provider_id
            base_url = payload.get("base_url")
            if not base_url:
                base_url = previous["base_url"] if same_provider else preset["base_url"]
            base_url = Settings.valid_base_url(base_url)
            if self.policy:
                self.policy.check_endpoint(base_url)
            protocol = payload.get("protocol") or (
                previous["protocol"] if same_provider else preset["protocol"]
            )
            if protocol not in {"openai", "anthropic"}:
                raise ValueError("目前支持 OpenAI 兼容或 Anthropic 协议。")
            same_endpoint = previous and (previous["base_url"], previous["protocol"]) == (
                base_url,
                protocol,
            )
            key = str(payload.get("api_key") or "").strip()
            if not key and same_endpoint:
                key = previous["api_key"]
            name = str(
                payload.get("name") or (previous or {}).get("name") or preset["name"]
            ).strip()
            account = {
                "id": account_id or uuid4().hex,
                "name": name[:120],
                "provider_id": provider_id,
                "base_url": base_url,
                "protocol": protocol,
                "api_key": key,
                "requires_key": preset["requires_key"],
                "readonly": False,
                "models": previous["models"]
                if same_endpoint and key == previous["api_key"]
                else _empty_models(),
            }
            data = deepcopy(self._data)
            data["accounts"] = [a for a in data["accounts"] if a["id"] != account["id"]] + [account]
            self._commit(data)
            return self._public_account(account)

    def delete_account(self, account_id: str) -> None:
        with self._lock:
            if account_id == "env":
                raise ValueError("环境变量账户只读。")
            self._account(account_id)
            if any(
                route and route["account_id"] == account_id for route in self._routing().values()
            ):
                raise ValueError("该账户仍用于模型分配，请先切换相关模型。")
            data = deepcopy(self._data)
            data["accounts"] = [a for a in data["accounts"] if a["id"] != account_id]
            self._commit(data)

    def set_routing(self, mapping: dict) -> dict:
        with self._lock:
            unknown = set(mapping) - ROLE_IDS
            if unknown:
                raise ValueError("存在未知模型用途。")
            routing = self._routing()
            for role, route in mapping.items():
                if route is None and role != "main":
                    routing[role] = None
                    continue
                if not isinstance(route, dict) or not str(route.get("model", "")).strip():
                    raise ValueError("主模型和指定的辅助模型需要模型名称。")
                account = self._account(route.get("account_id", ""))
                model = str(route["model"]).strip()
                item = next((m for m in account["models"]["items"] if m["id"] == model), {})
                confirmed = bool(route.get("vision_confirmed", False))
                if role == "vision" and item.get("vision") == "unsupported":
                    raise ValueError("提供方声明该模型不支持图片，请选择其他视觉模型。")
                routing[role] = {
                    "account_id": account["id"],
                    "model": model,
                    "vision_confirmed": confirmed or item.get("vision") == "supported",
                }
            data = deepcopy(self._data)
            data["routing"] = routing
            self._commit(data)
            return deepcopy(routing)

    def refresh_models(self, account_id: str) -> dict:
        with self._lock:
            account = self._account(account_id)
        if self.policy:
            self.policy.check_endpoint(account["base_url"])
        previous = account["models"]
        try:
            if not self._public_account(account)["configured"]:
                raise ValueError("请先保存此账户的 API Key。")
            headers = {"Accept": "application/json"}
            if account["protocol"] == "anthropic":
                headers.update({"x-api-key": account["api_key"], "anthropic-version": "2023-06-01"})
            elif account["api_key"]:
                headers["Authorization"] = f"Bearer {account['api_key']}"
            url = account["base_url"].rstrip("/") + "/models"
            params = {"limit": 100} if account["protocol"] == "anthropic" else {}
            items = {}
            with httpx.Client(
                timeout=20, follow_redirects=False, transport=self.transport
            ) as client:
                for _ in range(20):
                    response = client.get(url, headers=headers, params=params)
                    response.raise_for_status()
                    body = response.json()
                    if not isinstance(body, dict):
                        raise ValueError("服务未返回兼容的模型列表，可手动填写模型名称。")
                    rows = body.get("data")
                    if not isinstance(rows, list):
                        raise ValueError("服务未返回兼容的模型列表，可手动填写模型名称。")
                    for item in rows:
                        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                            continue
                        model_id = item["id"]
                        items[model_id] = {
                            "id": model_id,
                            "name": str(item.get("name") or item.get("display_name") or model_id),
                            "vision": _vision(item),
                        }
                    if not body.get("has_more"):
                        break
                    last_id = body.get("last_id") or (rows[-1].get("id") if rows else None)
                    if not last_id or params.get("after_id", params.get("after")) == last_id:
                        raise ValueError("模型分页响应不完整，请重试。")
                    params["after_id" if account["protocol"] == "anthropic" else "after"] = last_id
                else:
                    raise ValueError("模型列表超过分页上限，已保留上次结果，可手动填写模型名称。")
            from .catalog import get_model, load_catalog

            # Capabilities can supplement a live list, but must not gate discovery.
            try:
                catalog = load_catalog(self.settings.paperlens_cache_dir, allow_network=False)
                for item in items.values():
                    info = get_model(catalog, item["id"], base_url=account["base_url"])
                    if info is not None and item["vision"] == "unknown":
                        item["vision"] = info.vision
            except (OSError, ValueError):
                pass
            models = {
                "items": list(items.values()),
                "status": "live",
                "fetched_at": datetime.now(UTC).isoformat(),
            }
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            if isinstance(exc, httpx.HTTPStatusError):
                code = exc.response.status_code
                reason = {
                    401: "密钥无效或已失效",
                    403: "账户没有权限",
                    404: "此服务未提供模型列表接口",
                    429: "请求过于频繁",
                }.get(code, "服务暂时不可用")
                error = f"拉取模型失败（HTTP {code}）：{reason}。可重试或手动填写模型名称。"
            elif isinstance(exc, httpx.HTTPError):
                error = "暂时无法连接模型服务，请检查网络和服务地址。"
            else:
                error = str(exc) if isinstance(exc, ValueError) else "模型列表格式不兼容。"
            models = {
                **previous,
                "status": "cached" if previous["items"] else "failed",
                "error": error,
            }
        with self._lock:
            current = self._account(account_id)
            if (current["base_url"], current["protocol"], current["api_key"]) != (
                account["base_url"],
                account["protocol"],
                account["api_key"],
            ):
                raise ValueError("账户已更新，请重新拉取模型。")
            data = deepcopy(self._data)
            if account_id == "env":
                data["env_models"] = models
            else:
                next(a for a in data["accounts"] if a["id"] == account_id)["models"] = models
            self._commit(data)
        return deepcopy(models)

    def _route(self, binding: dict, base: Settings) -> ModelRoute:
        account = self._account(binding["account_id"], base)
        if self.policy:
            self.policy.check_endpoint(account["base_url"])
        return ModelRoute(
            account_id=account["id"],
            model=binding["model"],
            base_url=account["base_url"],
            api_key=SecretStr(account["api_key"]),
            protocol=account["protocol"],
            requires_key=account["requires_key"],
            vision_confirmed=binding.get("vision_confirmed", False),
        )

    def resolve(self, base: Settings) -> Settings:
        with self._lock:
            routing = self._routing(base)
            main = self._route(routing["main"], base)
            routes = {
                role: self._route(binding, base) if binding else main.model_copy(deep=True)
                for role, binding in routing.items()
            }
            if self._data["routing"] is None:
                routes["vision"] = main.model_copy(
                    update={
                        "model": base.paperlens_vision_model or main.model,
                        "vision_confirmed": base.paperlens_vision_allow_unverified,
                    }
                )
            return base.model_copy(
                deep=True,
                update={
                    "llm_routes": routes,
                    "llm_base_url": main.base_url,
                    "llm_model": main.model,
                    "llm_api_key": main.api_key,
                    "llm_protocol": main.protocol,
                    "llm_requires_key": main.requires_key,
                    "llm_account_id": main.account_id,
                },
            )

    def restore(self, base: Settings, configuration: dict) -> Settings:
        """Restore a submitted snapshot without changing its saved account endpoints."""
        with self._lock:
            raw_routes = configuration.get("llm_routes") or {}
            if not raw_routes:
                raw_routes = {
                    "main": {
                        "account_id": configuration.get("llm_account_id", "env"),
                        "model": configuration.get("llm_model", base.llm_model),
                        "base_url": configuration.get("llm_base_url", base.llm_base_url),
                        "protocol": configuration.get("llm_protocol", "openai"),
                    }
                }
            routes = {}
            for role, saved in raw_routes.items():
                account = self._account(saved["account_id"], base)
                if (account["base_url"], account["protocol"]) != (
                    saved["base_url"],
                    saved.get("protocol", "openai"),
                ):
                    raise ValueError("任务使用的模型账户地址已改变，请重新提交论文。")
                routes[role] = self._route(saved, base)
            main = routes["main"]
            values = {
                **base.model_dump(),
                **configuration,
                "llm_routes": routes,
                "llm_api_key": main.api_key,
                "llm_account_id": main.account_id,
                "llm_base_url": main.base_url,
                "llm_model": main.model,
                "llm_protocol": main.protocol,
                "llm_requires_key": main.requires_key,
            }
            return type(base).model_validate(values)
