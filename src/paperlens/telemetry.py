"""Bounded local timing samples, without prompts, responses or credentials."""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from .config import Settings
from .control import concurrency_limit

_lock = threading.Lock()
_active: list[RequestTiming] = []
_MAX_BYTES = 1024 * 1024


@dataclass(eq=False)
class RequestTiming:
    endpoint: str
    started: float
    concurrency: int
    seconds: float = 0
    accelerated: bool = False


def start_request(settings: Settings) -> RequestTiming:
    timing = RequestTiming(settings.llm_base_url.rstrip("/"), time.perf_counter(), 1,
                           accelerated=concurrency_limit() > 2)
    with _lock:
        peers = [item for item in _active if item.endpoint == timing.endpoint]
        peak = max(timing.concurrency, len(peers) + 1)
        timing.concurrency = peak
        for item in peers:
            item.concurrency = max(item.concurrency, peak)
        _active.append(timing)
    return timing


def finish_request(timing: RequestTiming) -> None:
    timing.seconds = time.perf_counter() - timing.started
    with _lock:
        if timing in _active:
            _active.remove(timing)


def record_request(
    settings: Settings,
    timing: RequestTiming,
    usage: dict,
    *,
    effective_reasoning_effort: str,
    stream: bool = False,
    retry_index: int = 0,
    task_stage: str = "unknown",
) -> None:
    """Best effort only: a measurement failure must never fail a paid result."""
    try:
        inputs = int(usage.get("prompt_tokens") or 0)
        outputs = int(usage.get("completion_tokens") or 0)
        if inputs < 0 or outputs <= 0 or not math.isfinite(timing.seconds) or timing.seconds <= 0:
            return
        sample = {
            "recorded_at": datetime.now(UTC).isoformat(),
            "endpoint": timing.endpoint,
            "model": settings.llm_model,
            "protocol": settings.llm_protocol,
            "reasoning_effort": settings.llm_reasoning_effort,
            "effective_reasoning_effort": effective_reasoning_effort,
            "input_tokens": inputs,
            "output_tokens": outputs,
            "duration_seconds": round(timing.seconds, 6),
            "concurrency": timing.concurrency,
            "accelerated": timing.accelerated,
            "stream": stream,
            "retry_index": retry_index,
            "task_stage": task_stage[:80],
            "success": True,
            "status": "complete",
        }
        details = usage.get("completion_tokens_details") or {}
        details = details if isinstance(details, dict) else {}
        reasoning = usage.get("reasoning_tokens", details.get("reasoning_tokens"))
        if reasoning is not None and 0 <= int(reasoning) <= outputs:
            sample["reasoning_tokens"] = int(reasoning)
            sample["visible_output_tokens"] = outputs - int(reasoning)
        path = settings.paperlens_cache_dir / "telemetry" / "llm.jsonl"
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > _MAX_BYTES:
                with path.open("rb") as handle:
                    handle.seek(-_MAX_BYTES // 2, 2)
                    handle.readline()  # Drop the partial first record.
                    tail = handle.read()
                temporary = path.with_suffix(".tmp")
                try:
                    temporary.write_bytes(tail)
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(sample, ensure_ascii=False) + "\n")
    except (OSError, ValueError, TypeError, OverflowError):
        return
