"""Small JSON transports with account routing and one shared request budget."""

from __future__ import annotations

import base64
import binascii
import json
import re
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from .concurrency_policy import execution_concurrency
from .config import Settings
from .control import checkpoint, task_stage
from .errors import ProviderError, ProviderOutcomeUnknown, ProviderRejected
from .models import Usage
from .reading_parallel import release_request_permit, try_request_permit
from .storage import write_json
from .telemetry import finish_request, record_request, start_request

# Shared by ordinary, fast, ultra, streaming and all concurrently running jobs.
# Each job keeps its separately measured stage ceiling; this is a total bound.
NETWORK_REQUEST_LIMIT = 32
_NETWORK_SLOTS = threading.BoundedSemaphore(NETWORK_REQUEST_LIMIT)
# Preserve the former aggregate ceiling for endpoints/models without a current
# verified ultra study. All such providers share this smaller pool, including streams.
_CONSERVATIVE_NETWORK_SLOTS = threading.BoundedSemaphore(6)


class RequestBudget:
    """Reserve attempts atomically, including retries across different models."""

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0
        self._lock = threading.Condition()
        self._reservations: dict[object, int] = {}

    def reserve(self, minimum: int = 1) -> ReservedBudget:
        """Keep a sibling's first attempt available until it finishes or uses it."""
        with self._lock:
            token = object()
            available = self.limit - self.used - sum(self._reservations.values())
            self._reservations[token] = min(max(0, minimum), max(0, available))
            return ReservedBudget(self, token)

    def take(self) -> None:
        self._take(None)

    def try_take(self) -> bool:
        return self._take(None, blocking=False)

    def _take(self, token: object | None, *, blocking: bool = True) -> bool:
        while True:
            checkpoint()
            with self._lock:
                if self.used >= self.limit:
                    raise ProviderError(
                        "已达到单篇论文的请求预算；已完成阶段已保留，可调整预算后继续。"
                    )
                others = sum(v for k, v in self._reservations.items() if k is not token)
                if self.used + others < self.limit:
                    self.used += 1
                    if self._reservations.get(token, 0):
                        self._reservations[token] -= 1
                    self._lock.notify_all()
                    return True
                if not blocking:
                    return False
                self._lock.wait(timeout=0.1)

    def wait_available(self, token: object | None = None) -> None:
        while True:
            checkpoint()
            with self._lock:
                others = sum(v for k, v in self._reservations.items() if k is not token)
                if self.used >= self.limit or self.used + others < self.limit:
                    return
                self._lock.wait(timeout=0.1)


class ReservedBudget:
    """A shared budget view; every attempt, including retries, counts globally."""

    def __init__(self, parent: RequestBudget, token: object):
        self.parent, self.token = parent, token

    @property
    def used(self) -> int:
        return self.parent.used

    @property
    def limit(self) -> int:
        return self.parent.limit

    def take(self) -> None:
        self.parent._take(self.token)

    def try_take(self) -> bool:
        return self.parent._take(self.token, blocking=False)

    def wait_available(self) -> None:
        self.parent.wait_available(self.token)

    def close(self) -> None:
        with self.parent._lock:
            self.parent._reservations.pop(self.token, None)
            self.parent._lock.notify_all()


def sum_usage(items: list[Usage]) -> Usage:
    active = [item for item in items if item.requests]
    data = {
        key: sum(getattr(item, key) for item in items)
        for key in ("requests", "prompt_tokens", "completion_tokens")
    }
    for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
        data[key] = (
            sum(getattr(item, key) for item in active)
            if all(getattr(item, key) is not None for item in active)
            else None
        )
    return Usage(**data)


class JsonProvider:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        budget: RequestBudget | None = None,
        responses_api: bool = False,
    ) -> None:
        settings.require_provider()
        self.settings = settings
        self.usage = Usage()
        self._budget = budget or RequestBudget(settings.llm_max_requests)
        self._lock = threading.RLock()
        self._cache_breakdown_complete = True
        self._events: list[dict] = []
        self._invocation_id = uuid.uuid4().hex
        self._sleeper = sleeper
        self._responses_api = responses_api
        self._conservative_slots = (
            None if execution_concurrency(settings, speed_mode="ultra") > 5
            else _CONSERVATIVE_NETWORK_SLOTS
        )
        key = settings.llm_api_key.get_secret_value()
        headers = {}
        if settings.llm_protocol == "anthropic":
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        elif key:
            headers = {"Authorization": f"Bearer {key}"}
        self._client = httpx.Client(
            base_url=settings.llm_base_url + "/",
            headers=headers,
            timeout=httpx.Timeout(settings.llm_timeout_seconds, connect=20),
            transport=transport,
            follow_redirects=False,
        )

    def __enter__(self) -> JsonProvider:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()
        write_json(
            self.settings.paperlens_cache_dir / "provider" / f"{self._invocation_id}.json",
            {
                "model": self.settings.llm_model,
                "account_id": self.settings.llm_account_id,
                "usage": self.usage.model_dump(),
                "events": self._events,
            },
        )

    def usage_record(self) -> dict:
        with self._lock:
            return {
                "model": self.settings.llm_model,
                "account_id": self.settings.llm_account_id,
                "provider_base_url": self.settings.llm_base_url,
                "usage": self.usage.model_dump(),
            }

    def _try_network_slot(self, slots=None) -> bool:
        if not try_request_permit():
            return False
        pool = slots if slots is not None else _NETWORK_SLOTS
        gate = self._conservative_slots
        if gate is not None and not gate.acquire(blocking=False):
            release_request_permit()
            return False
        if pool.acquire(blocking=False):
            return True
        if gate is not None:
            gate.release()
        release_request_permit()
        return False

    def _release_network_slot(self, slots=None) -> None:
        pool = slots if slots is not None else _NETWORK_SLOTS
        pool.release()
        if self._conservative_slots is not None:
            self._conservative_slots.release()
        release_request_permit()

    def list_models(self) -> list[str]:
        try:
            response = self._client.get("models")
        except httpx.HTTPError as exc:
            raise ProviderError("无法连接模型接口，请检查网络与服务地址。") from exc
        self._raise_status(response)
        try:
            return [item["id"] for item in response.json().get("data", [])]
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError("模型列表响应格式不兼容，请核对服务地址。") from exc

    def _raise_status(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        messages = {
            401: "API 鉴权失败，请检查所选账户的 API Key。",
            403: "模型接口拒绝访问，请检查账户权限。",
            402: "模型账户余额不足，请检查账户余额。",
            404: "模型或接口不存在，请检查模型名称与服务地址。",
            429: "模型接口限流；已达到本次重试上限，请稍后重试。",
        }
        try:
            error = response.json().get("error", {})
        except (ValueError, AttributeError):
            error = {}
        if not isinstance(error, dict):
            error = {}
        secret = self.settings.llm_api_key.get_secret_value()

        def clean(value, limit):
            text = value if isinstance(value, str) else ""
            if secret:
                text = text.replace(secret, "[已隐藏凭据]")
            text = re.sub(r"(?i)Bearer\s+\S+|sk-[A-Za-z0-9_-]+", "[已隐藏凭据]", text)
            return " ".join(text.split())[:limit]

        detail = {"http_status": response.status_code, "retryable": False,
                  "type": clean(error.get("type"), 80), "code": clean(error.get("code"), 80),
                  "param": clean(error.get("param"), 100), "message": clean(error.get("message"), 600)}
        message = messages.get(response.status_code, f"模型接口返回 HTTP {response.status_code}。")
        if detail["message"]:
            message += "服务说明：" + detail["message"]
        raise ProviderRejected(message, detail)

    def _record_usage(self, usage: dict) -> None:
        with self._lock:
            self.usage.prompt_tokens += int(usage.get("prompt_tokens", 0))
            self.usage.completion_tokens += int(usage.get("completion_tokens", 0))
            hit = usage.get("prompt_cache_hit_tokens")
            if hit is None:
                hit = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
            miss = usage.get("prompt_cache_miss_tokens")
            if miss is None and hit is not None:
                miss = int(usage.get("prompt_tokens", 0)) - int(hit)
            if hit is None or miss is None or min(int(hit), int(miss)) < 0:
                self._cache_breakdown_complete = False
                self.usage.prompt_cache_hit_tokens = None
                self.usage.prompt_cache_miss_tokens = None
            elif self._cache_breakdown_complete:
                self.usage.prompt_cache_hit_tokens = (
                    self.usage.prompt_cache_hit_tokens or 0
                ) + int(hit)
                self.usage.prompt_cache_miss_tokens = (
                    self.usage.prompt_cache_miss_tokens or 0
                ) + int(miss)

    def json(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 6000,
        reasoning_effort: str | None = None,
        images: list[str] | None = None,
        max_attempts: int | None = None,
        pdf_bytes: bytes | None = None,
    ) -> dict[str, Any]:
        responses_api = self._responses_api or pdf_bytes is not None
        if responses_api and (
            self.settings.llm_protocol != "openai"
            or urlsplit(self.settings.llm_base_url).hostname != "api.openai.com"
        ):
            raise ProviderError("所选账户未配置原生 PDF 接口，请使用原页图片读取。")
        if pdf_bytes is not None and (
            not isinstance(pdf_bytes, bytes)
            or b"%PDF-" not in pdf_bytes[:1024]
            or len(pdf_bytes) >= 50 * 1024 * 1024
        ):
            raise ProviderError("单批 PDF 必须是有效文件且小于 50 MB。")
        if images:
            if len(images) > 8:
                raise ProviderError("单次图片分析最多提交 8 张图片。")
            total_bytes = 0
            for image in images:
                if not re.fullmatch(r"data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=]+", image):
                    raise ProviderError(
                        "图片必须为本地生成的 PNG/JPEG/WebP 数据，不能使用外部链接。"
                    )
                try:
                    total_bytes += len(base64.b64decode(image.split(",", 1)[1], validate=True))
                except (ValueError, binascii.Error) as exc:
                    raise ProviderError("图片数据无效。") from exc
            if total_bytes > 16 * 1024 * 1024:
                raise ProviderError("本次图片超过应用请求大小预算。")
        max_tokens = min(max_tokens, self.settings.llm_max_output_tokens)
        system += "\n只返回一个合法 JSON 对象。"
        effort = self.settings.request_reasoning_effort(reasoning_effort)
        hostname = urlsplit(self.settings.llm_base_url).hostname
        options = {}
        if hostname in {"api.deepseek.com", "api.openai.com"} and effort != "default":
            options["reasoning_effort"] = effort
        anthropic = self.settings.llm_protocol == "anthropic"
        content: str | list[dict] = user
        if images:
            content = [{"type": "text", "text": user}]
            for image in images:
                if anthropic:
                    prefix, data = image.split(",", 1)
                    content.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": prefix[5:].split(";")[0],
                                "data": data,
                            },
                        }
                    )
                else:
                    content.append(
                        {"type": "image_url", "image_url": {"url": image, "detail": "high"}}
                    )
        messages = [{"role": "user", "content": content}]
        if not anthropic:
            messages.insert(0, {"role": "system", "content": system})
        last_problem = "未返回有效结果"
        attempts = self.settings.llm_max_retries + 1
        if max_attempts is not None:
            attempts = max(1, min(attempts, max_attempts))
        for attempt in range(attempts):
            checkpoint()
            payload = {"model": self.settings.llm_model, "messages": messages, "stream": False}
            if responses_api:
                inputs = [{"type": "input_text", "text": user}]
                # Responses JSON-mode validation checks input messages, not the
                # separate instructions field. Keep the data text unchanged.
                if "json" not in user.casefold():
                    inputs.append({"type": "input_text", "text": "请返回一个 JSON 对象。"})
                if pdf_bytes is not None:
                    inputs.append({
                        "type": "input_file", "filename": "pages.pdf",
                        "file_data": "data:application/pdf;base64,"
                        + base64.b64encode(pdf_bytes).decode("ascii"),
                        "detail": "high",
                    })
                inputs.extend({"type": "input_image", "image_url": image, "detail": "high"}
                              for image in images or [])
                payload = {
                    "model": self.settings.llm_model, "instructions": system,
                    "input": [{"role": "user", "content": inputs}],
                    "max_output_tokens": max_tokens, "store": False,
                    "text": {"format": {"type": "json_object"}},
                }
                if effort != "default":
                    payload["reasoning"] = {"effort": effort}
            elif anthropic:
                payload.update(system=system, max_tokens=max_tokens)
            else:
                payload.update(response_format={"type": "json_object"}, **options)
                payload[
                    "max_completion_tokens" if hostname == "api.openai.com" else "max_tokens"
                ] = max_tokens
            # Budget reservations may wait for a sibling. Never hold a network
            # slot (or a provider lock) during that wait: siblings need those slots.
            while True:
                while not self._try_network_slot():
                    checkpoint()
                    time.sleep(0.05)
                try:
                    checkpoint()
                    with self._lock:
                        if self.usage.requests >= self.settings.llm_max_requests:
                            raise ProviderError("已达到当前处理阶段的请求预算，已完成阶段仍保留。")
                        admitted = self._budget.try_take()
                        if admitted:
                            self.usage.requests += 1
                            event = {
                                "attempt": self.usage.requests,
                                "max_tokens": max_tokens,
                                "reasoning_effort": options.get("reasoning_effort", "provider_default"),
                                "api": "responses" if responses_api else "chat",
                            }
                except BaseException:
                    self._release_network_slot()
                    raise
                if admitted:
                    break
                self._release_network_slot()
                self._budget.wait_available()
            try:
                timing = start_request(self.settings)
                try:
                    response = (
                        self._responses_request(payload) if responses_api else self._client.post(
                            "messages" if anthropic else "chat/completions", json=payload
                        )
                    )
                except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                    last_problem, response = "模型网络连接失败", None
                    event.update(
                        outcome="connection_error", error_type=type(exc).__name__,
                        retryable=True, usage_unknown=False,
                    )
                except httpx.TimeoutException as exc:
                    last_problem, response = "模型请求超时", None
                    event.update(
                        outcome="timeout", error_type=type(exc).__name__,
                        retryable=False, usage_unknown=True,
                    )
                except httpx.HTTPError as exc:
                    last_problem, response = "模型网络连接失败", None
                    event.update(
                        outcome="network_error", error_type=type(exc).__name__,
                        retryable=False, usage_unknown=True,
                    )
                finally:
                    finish_request(timing)
                event["seconds"] = round(timing.seconds, 3)
            finally:
                self._release_network_slot()
            if response is not None:
                event["http_status"] = response.status_code
                if response.status_code == 408:
                    last_problem = "模型接口请求超时（HTTP 408）"
                    event.update(outcome="timeout", retryable=False, usage_unknown=True)
            with self._lock:
                self._events.append(event)
            if event.get("retryable") is False and event.get("usage_unknown"):
                # A read/write failure can follow submission of a paid request.
                # Without a terminal result, replaying it may duplicate model work.
                raise ProviderOutcomeUnknown(
                    f"{last_problem}；请求可能已提交，实际用量未知，"
                    "已停止自动重发并保留处理进度。"
                )
            if response is not None:
                if response.status_code not in {408, 429, 500, 502, 503, 504}:
                    try:
                        self._raise_status(response)
                    except ProviderRejected as exc:
                        event.update(outcome="rejected", retryable=False, usage_unknown=False,
                                     provider_error=exc.details)
                        raise
                if response.is_success:
                    try:
                        result = response.json()
                        usage = result.get("usage") or {}
                        if responses_api:
                            cached_tokens = (usage.get("input_tokens_details") or {}).get("cached_tokens")
                            usage = {
                                "prompt_tokens": int(usage.get("input_tokens", 0)),
                                "completion_tokens": int(usage.get("output_tokens", 0)),
                                "prompt_cache_hit_tokens": cached_tokens,
                            }
                            status = result.get("status")
                            finish = "stop" if status == "completed" else "length" if (
                                (result.get("incomplete_details") or {}).get("reason") == "max_output_tokens"
                            ) else "failed"
                            text = "".join(
                                part.get("text", "") for item in result.get("output", [])
                                for part in item.get("content", [])
                                if part.get("type") == "output_text"
                            ) if status == "completed" else ""
                            event["actual_model"] = result.get("model")
                        elif anthropic:
                            hit = int(usage.get("cache_read_input_tokens", 0))
                            miss = int(usage.get("input_tokens", 0)) + int(
                                usage.get("cache_creation_input_tokens", 0)
                            )
                            usage = {
                                "prompt_tokens": hit + miss,
                                "completion_tokens": int(usage.get("output_tokens", 0)),
                                "prompt_cache_hit_tokens": hit,
                                "prompt_cache_miss_tokens": miss,
                            }
                            finish = result.get("stop_reason")
                            text = "".join(
                                b.get("text", "")
                                for b in result.get("content", [])
                                if b.get("type") == "text"
                            )
                        else:
                            choice = result["choices"][0]
                            finish = choice.get("finish_reason")
                            text = choice["message"].get("content") or ""
                        self._record_usage(usage)
                        event.update(
                            finish_reason=finish,
                            prompt_tokens=int(usage.get("prompt_tokens", 0)),
                            completion_tokens=int(usage.get("completion_tokens", 0)),
                        )
                        if finish in {"length", "max_tokens"}:
                            last_problem = "模型输出被截断"
                            if max_tokens >= self.settings.llm_max_output_tokens:
                                break  # The same task at the same hard cap cannot recover.
                            max_tokens = min(max_tokens + 8000, self.settings.llm_max_output_tokens)
                        else:
                            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
                            parsed = json.loads(text)
                            if not isinstance(parsed, dict):
                                raise ValueError("Expected object")
                            record_request(
                                self.settings,
                                timing,
                                usage,
                                effective_reasoning_effort=event["reasoning_effort"],
                                retry_index=attempt,
                                task_stage=task_stage(),
                            )
                            return parsed
                    except (ValueError, KeyError, IndexError, TypeError):
                        last_problem = "模型返回空内容或不完整的 JSON"
                else:
                    last_problem = f"模型接口暂时不可用（HTTP {response.status_code}）"
            if attempt + 1 < attempts:
                self._sleeper(min(2**attempt, 8))
        if last_problem == "模型输出被截断":
            raise ProviderError(
                "模型输出被截断（已达到单次输出上限）；请在设置中提高单次输出上限后重试。"
            )
        retry_message = "自动重试后仍未恢复，" if attempts > 1 else ""
        raise ProviderError(f"{last_problem}；{retry_message}已保留处理进度。")

    def _responses_request(self, payload: dict) -> httpx.Response:
        """Consume terminal Responses events; a cut stream is an unknown outcome."""
        endpoint = "responses" if urlsplit(self.settings.llm_base_url).path.strip("/") else "v1/responses"
        with self._client.stream("POST", endpoint, json=payload | {"stream": True}) as response:
            if not response.is_success or "text/event-stream" not in response.headers.get("content-type", ""):
                response.read()
                return response
            for line in response.iter_lines():
                if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                    continue
                try:
                    event = json.loads(line[5:])
                    if event.get("type") in {"response.completed", "response.incomplete", "response.failed"}:
                        result = event["response"]
                        if not isinstance(result, dict):
                            raise ValueError("Missing terminal response")
                        return httpx.Response(200, json=result, request=response.request)
                    if event.get("type") == "error":
                        raise ValueError("Response stream error")
                except (ValueError, KeyError, TypeError, AttributeError) as exc:
                    raise httpx.RemoteProtocolError("模型流响应未提供可确认的终止结果。") from exc
        raise httpx.RemoteProtocolError("模型流在终止结果前断开。")


class ProviderPool:
    """Lazily open the selected accounts; never fall back to an unselected site."""

    def __init__(
        self,
        settings: Settings,
        *,
        budget: RequestBudget | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self.settings = settings
        self._budget = budget or RequestBudget(settings.llm_max_requests)
        self._providers: dict[tuple, JsonProvider] = {}
        self._lock = threading.RLock()
        self._transport = transport

    def for_role(self, role: str) -> JsonProvider:
        target = self.settings.for_role(role)
        identity = (
            target.llm_account_id,
            target.llm_base_url,
            target.llm_model,
            target.llm_protocol,
        )
        with self._lock:
            if identity not in self._providers:
                self._providers[identity] = JsonProvider(
                    target, budget=self._budget, transport=self._transport
                )
            return self._providers[identity]

    def json(self, *args, **kwargs):
        return self.for_role("main").json(*args, **kwargs)

    @property
    def usage(self) -> Usage:
        with self._lock:
            return sum_usage(
                [Usage.model_validate(p.usage_record()["usage"]) for p in self._providers.values()]
            )

    def usage_records(self) -> list[dict]:
        with self._lock:
            return [p.usage_record() for p in self._providers.values()]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        for provider in self._providers.values():
            provider.close()
