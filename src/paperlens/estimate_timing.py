"""Calibrate request service time with recent, comparable API measurements."""

from __future__ import annotations

import json
import math
import statistics
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from .estimate_workload import Work
from .model_metrics import ModelMetrics
from .model_metrics import request_seconds as public_request_seconds


def history_samples(cache_dir: Path, *, as_of: datetime | None = None) -> list[dict]:
    """Read a bounded tail, never aggregate report durations or historical pages."""
    path = Path(cache_dir) / "telemetry" / "llm.jsonl"
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - 2_000_000))
            if size > 2_000_000:
                handle.readline()
            lines = handle.read(2_000_000).splitlines()[-4000:]
    except OSError:
        return []
    now = as_of or datetime.now(UTC)
    samples = []
    for line in lines:
        try:
            row = json.loads(line)
            recorded = datetime.fromisoformat(row["recorded_at"].replace("Z", "+00:00"))
            if (
                recorded.tzinfo is None
                or not now - timedelta(days=30) <= recorded <= now
                or row.get("execution_mode") == "codex_session"
                or row.get("success") is not True
                or row.get("status") != "complete"
                or row.get("stream", False)
                or not 1 <= int(row.get("concurrency", 1)) <= 16
            ):
                continue
            values = [float(row[k]) for k in ("input_tokens", "output_tokens", "duration_seconds")]
            if not all(math.isfinite(v) for v in values):
                continue
            if values[0] < 0 or values[1] <= 0 or not 0 < values[2] <= 3600:
                continue
            if not all(isinstance(row.get(k), str) and row[k] for k in ("endpoint", "model", "protocol")):
                continue
            samples.append(row)
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
            continue
    return sorted(samples, key=lambda row: row["recorded_at"])


def service_seconds(inputs: float, outputs: float) -> float:
    # Explicit cold-start assumption, not a claimed speed of any named model.
    return 3 + inputs / 1000 + outputs / 35


def comparable_samples(work: Work, samples: list[dict]) -> list[dict]:
    """Same actual endpoint/model/protocol/effort, newest first-class identity."""
    settings = work.settings
    effective = work.effort if (
        urlsplit(settings.llm_base_url).hostname in {"api.deepseek.com", "api.openai.com"}
        and work.effort != "default"
    ) else "provider_default"
    return [s for s in samples if (
        s["endpoint"].rstrip("/") == settings.llm_base_url.rstrip("/")
        and s["model"] == settings.llm_model
        and s["protocol"] == settings.llm_protocol
        and s.get("effective_reasoning_effort", s.get("reasoning_effort")) == effective
    )][-120:]


def calibrate_usage(work: Work, samples: list[dict], point: str) -> None:
    """Calibrate hidden reasoning, retaining demand's independent content basis.

    API completion totals already include reasoning; do not add those tokens a
    second time. A missing reasoning breakdown is unknown rather than zero.
    """
    if work.visible_output_per_request <= 0:
        return
    rows = comparable_samples(work, samples)
    measured = []
    for row in rows:
        try:
            total, reasoning = float(row["output_tokens"]), float(row["reasoning_tokens"])
            if not 0 <= reasoning < total or not math.isfinite(reasoning):
                continue
            measured.append((row, total / (total - reasoning)))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    stage = [ratio for row, ratio in measured if row.get("task_stage") == work.stage]
    if len(stage) >= 3:
        ratios, basis = stage, "recent_stage_reasoning"
    elif len(measured) >= 5:
        ratios, basis = [ratio for _, ratio in measured], "recent_model_effort_reasoning"
    else:
        work.usage_samples = len(stage)
        work.usage_calibration = "insufficient_reasoning_samples"
        return
    ratios.sort()
    middle = statistics.median(ratios)
    # These are planning envelopes, not statistical confidence intervals.
    uncertainty = 1.6 if basis != "recent_stage_reasoning" or len(ratios) < 5 else 1.3
    multiplier = {
        "low": max(1, min(middle / uncertainty, ratios[len(ratios) // 4])),
        "typical": middle,
        "high": max(middle * uncertainty, ratios[3 * len(ratios) // 4]),
    }[point]
    work.output_per_request = min(work.output_cap, work.visible_output_per_request * multiplier)
    work.usage_calibration, work.usage_samples = basis, len(ratios)


def request_seconds(
    work: Work, samples: list[dict], point: str,
    metrics: dict[tuple[str, str], ModelMetrics] | None = None,
) -> tuple[float, int]:
    settings = work.settings
    # JsonProvider only sends selected effort on DeepSeek; other JSON endpoints
    # choose their own reasoning behavior. Never mix that with stream timings.
    matching = comparable_samples(work, samples)
    observed_load = work.concurrency
    if work.stage in {
        "extraction", "native_reading", "reading_edition", "math_review", "glossary_repair", "glossary_recheck",
    }:
        # These batches run behind stage barriers. A capacity of sixteen with
        # two ready requests is observed as a two-request API load. Retries are
        # sequential attempts, not extra simultaneous original tasks. Paired
        # branches retain the shared global peak definition used below.
        originals = math.ceil(work.requests / work.retry_multiplier)
        observed_load = min(work.concurrency, max(1, originals))
    same_load = [s for s in matching if int(s.get("concurrency", 1)) == observed_load]
    # A concurrent request's elapsed time includes provider contention. Prefer
    # the same load; only serial samples can safely serve as a fallback.
    matching = (same_load or [s for s in matching if int(s.get("concurrency", 1)) == 1])[-120:]
    stage = [s for s in matching if s.get("task_stage") == work.stage]
    if len(stage) >= 3:
        matching = stage
    base = service_seconds(work.input_per_request, work.output_per_request)
    if not matching:
        public = (metrics or {}).get((settings.llm_base_url.rstrip("/"), settings.llm_model))
        observed = public_request_seconds(
            public, work.input_per_request, work.output_per_request, point,
        ) if public else None
        if observed is not None:
            # -1 identifies public reference data, not a local measurement.
            return observed * work.requests, -1
        factor = {"low": 0.55, "typical": 1, "high": 2.2}[point]
        return base * factor * work.requests, 0
    # Infer service speed from completed output including hidden reasoning.
    # Reserve <=1s for request overhead; stage matching reduces differences in
    # prompt processing. Input tokens are not output-generation tokens.
    durations = []
    for sample in matching:
        elapsed = float(sample["duration_seconds"])
        overhead = min(1, elapsed / 2)
        durations.append(overhead + work.output_per_request * (elapsed - overhead)
                         / float(sample["output_tokens"]))
    ratios = sorted(durations)
    # Preserve uncertainty with few observations and resist one outlying request.
    middle = statistics.median(ratios)
    ratio = {
        "low": min(middle * 0.7, ratios[len(ratios) // 4]),
        "typical": middle,
        "high": max(middle * (1.7 if len(ratios) < 5 else 1.4), ratios[3 * len(ratios) // 4]),
    }[point]
    return ratio * work.requests, len(matching)


def scheduled_seconds(work: Work, request_total_seconds: float) -> float:
    """Approximate equal-size batch waves, including fractional expected retries."""
    if work.requests <= 0:
        return 0
    original = work.requests / work.retry_multiplier
    waves, tail = divmod(original, work.concurrency)
    # Retries belong to the originating request's sequential dependency chain.
    # In particular, a single request + 0.2 retries must take 1.2 request times
    # even when the configured worker capacity is much larger than one.
    return (waves + min(1, tail)) * request_total_seconds / work.requests * work.retry_multiplier
