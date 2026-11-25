"""Bounded, cancellable streaming transport; shares account audit and budget with JSON."""

from __future__ import annotations

import asyncio
import json
import re
import time
from urllib.parse import urlsplit

import httpx

from .control import checkpoint, task_stage
from .errors import ProviderError
from .provider import _NETWORK_SLOTS, JsonProvider
from .telemetry import finish_request, record_request, start_request


def answer_prefix(raw: str) -> str | None:
    """Decode only the first answer string, including incomplete JSON escapes."""
    match = re.match(r'\s*\{\s*"answer"\s*:\s*"', raw)
    if not match:
        return None
    content = raw[match.end() :]
    escaped = False
    end = len(content)
    for i, char in enumerate(content):
        if not escaped and char == '"':
            end = i
            break
        if not escaped and char == "\\":
            escaped = True
        else:
            escaped = False
    content = content[:end]
    # An unfinished escape belongs to the next network chunk.
    tail = re.search(r"(?<!\\)(?:\\\\)*\\(?:u[0-9a-fA-F]{0,3})?$", content)
    if tail:
        # Keep paired backslashes; remove only the final incomplete escape.
        trailing = re.search(r"\\(?:u[0-9a-fA-F]{0,3})?$", content)
        content = content[: trailing.start()]
    try:
        text = json.loads('"' + content + '"')
        # JSON may finish the high surrogate before the low surrogate arrives.
        if text and 0xD800 <= ord(text[-1]) <= 0xDBFF:
            text = text[:-1]
        return text
    except ValueError:
        return None


async def stream_json(
    provider: JsonProvider,
    system: str,
    user: str,
    *,
    max_tokens=3200,
    transport: httpx.AsyncBaseTransport | None = None,
):
    """One attempt only: never silently replay a partially delivered paid request."""
    settings = provider.settings
    if len(user) > settings.llm_max_input_chars:
        raise ProviderError("问题与引用超过当前输入预算。")
    anthropic = settings.llm_protocol == "anthropic"
    hostname = urlsplit(settings.llm_base_url).hostname
    limit = min(max_tokens, settings.llm_max_output_tokens)
    payload = {
        "model": settings.llm_model,
        "stream": True,
        "messages": [{"role": "user", "content": user}],
    }
    system += "\n只返回一个合法 JSON 对象，answer 必须是第一个字段。"
    if anthropic:
        payload.update(system=system, max_tokens=limit)
    else:
        payload["messages"].insert(0, {"role": "system", "content": system})
        payload.update(
            response_format={"type": "json_object"}, stream_options={"include_usage": True}
        )
        payload["max_completion_tokens" if hostname == "api.openai.com" else "max_tokens"] = limit
        if (
            hostname in {"api.openai.com", "api.deepseek.com"}
            and settings.llm_reasoning_effort != "default"
        ):
            payload["reasoning_effort"] = settings.request_reasoning_effort()
    event = {
        "attempt": 0,
        "max_tokens": limit,
        "stream": True,
        "reasoning_effort": payload.get("reasoning_effort", "provider_default"),
    }
    started = time.monotonic()
    acquired = False
    timing = None
    usage, finish = {}, None
    try:
        while True:
            while not provider._try_network_slot(_NETWORK_SLOTS):
                checkpoint()
                await asyncio.sleep(0.05)
            acquired = True
            checkpoint()
            with provider._lock:
                if provider.usage.requests >= settings.llm_max_requests:
                    raise ProviderError("已达到当前处理阶段的请求预算，已完成阶段仍保留。")
                admitted = provider._budget.try_take()
                if admitted:
                    provider.usage.requests += 1
            if admitted:
                break
            provider._release_network_slot(_NETWORK_SLOTS)
            acquired = False
            # A sibling can own the last reserved attempt. Never await it while
            # holding either network pool, and never block the async event loop.
            await asyncio.sleep(0.05)
        event["attempt"] = provider.usage.requests
        event["outcome"] = "cancelled"
        timing = start_request(settings)
        headers = dict(provider._client.headers)
        ended, size = False, 0
        async with httpx.AsyncClient(
            base_url=settings.llm_base_url + "/",
            headers=headers,
            timeout=httpx.Timeout(settings.llm_timeout_seconds, connect=20),
            transport=transport,
            follow_redirects=False,
        ) as client:
            async with client.stream(
                "POST", "messages" if anthropic else "chat/completions", json=payload
            ) as response:
                event["http_status"] = response.status_code
                if not response.is_success:
                    await response.aread()
                provider._raise_status(response)
                async for line in response.aiter_lines():
                    if len(line) > 200_000:
                        raise ProviderError("模型流响应超过大小上限。")
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        ended = True
                        break
                    try:
                        item = json.loads(data)
                        if item.get("error") or item.get("type") == "error":
                            raise ProviderError("模型在生成回答时中断，请重新尝试。")
                        chunk = ""
                        if anthropic:
                            if item.get("type") == "message_start":
                                usage.update(item.get("message", {}).get("usage") or {})
                            elif item.get("type") == "message_delta":
                                usage.update(item.get("usage") or {})
                                finish = item.get("delta", {}).get("stop_reason")
                            elif item.get("type") == "content_block_delta":
                                delta = item.get("delta") or {}
                                if delta.get("type") == "text_delta":
                                    chunk = delta.get("text") or ""
                            elif item.get("type") == "message_stop":
                                ended = True
                        else:
                            if item.get("usage"):
                                usage = item["usage"]
                            choices = item.get("choices") or []
                            if choices:
                                chunk = choices[0].get("delta", {}).get("content") or ""
                                finish = choices[0].get("finish_reason") or finish
                        if not isinstance(chunk, str):
                            raise ValueError()
                    except (ValueError, TypeError, AttributeError, IndexError) as exc:
                        raise ProviderError("模型流响应格式不兼容。") from exc
                    size += len(chunk)
                    if size > 60_000:
                        raise ProviderError("回答超过大小上限，请缩小提问范围。")
                    if chunk:
                        yield chunk
        if finish in {"length", "max_tokens"}:
            raise ProviderError(
                f"模型已连接，但回答达到输出上限（{limit:,} tokens）而被截断。"
                "请在设置 → 论文处理中提高“回答长度”，或缩小提问范围后重试。"
            )
        if not ended or finish not in {"stop", "end_turn", "stop_sequence"}:
            raise ProviderError("模型回答被截断或未正常结束，请重新尝试。")
        event["outcome"] = "complete"
    except httpx.HTTPError as exc:
        event["outcome"] = "network_error"
        raise ProviderError("模型连接中断，请重新尝试。") from exc
    except ProviderError as exc:
        event["outcome"] = "error"
        event["error"] = str(exc)
        raise
    finally:
        if timing is not None:
            finish_request(timing)
        if anthropic:
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
        provider._record_usage(usage)
        event.update(
            finish_reason=finish, usage_reported=bool(usage), **provider.usage.model_dump()
        )
        event["seconds"] = round(time.monotonic() - started, 3)
        provider._events.append(event)
        if timing is not None and event.get("outcome") == "complete":
            record_request(
                settings,
                timing,
                usage,
                effective_reasoning_effort=event["reasoning_effort"],
                stream=True,
                task_stage=task_stage(),
            )
        if acquired:
            provider._release_network_slot(_NETWORK_SLOTS)
