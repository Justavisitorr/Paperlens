"""Import explicitly classified completed HTTP observations, entirely offline.

The input is a JSON list of archived HTTP records with task_stage (or stage)
already established by the run's audit. This tool never infers stages from
document content, output lengths or token limits. Its destination must be new.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import UTC, datetime
from pathlib import Path

STAGES = {
    "visuals", "chart_refinement", "extraction", "synthesis", "node_review",
    "glossary_review", "glossary_repair", "node_repair", "node_recheck",
    "glossary_recheck", "reading_edition", "math_review", "translation", "auto_notes",
}


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Observation timestamps must include a timezone")
    return parsed.astimezone(UTC)


def convert(rows: list[dict], *, endpoint: str, protocol: str,
            as_of: datetime | None = None) -> list[dict]:
    """Require complete classified observations; no text or balance data enters."""
    if not isinstance(rows, list) or not rows or len(rows) > 4000:
        raise ValueError("Expected 1 to 4000 classified observations")
    cutoff = as_of or datetime.now(UTC)
    samples, intervals = [], []
    for row in rows:
        stage = row.get("task_stage", row.get("stage"))
        if stage not in STAGES or row.get("status") != 200 or row.get("finish_reason") != "stop":
            raise ValueError("Every observation must have an audited stage and a complete successful HTTP result")
        started, finished = timestamp(row["started_at"]), timestamp(row["finished_at"])
        duration = float(row["duration_seconds"])
        if not started < finished <= cutoff or not math.isfinite(duration) or not 0 < duration <= 3600:
            raise ValueError("Invalid, incomplete or future observation timing")
        usage = row["usage"]
        inputs, outputs = int(usage["prompt_tokens"]), int(usage["completion_tokens"])
        model = row.get("model_requested", row.get("model"))
        effort = row["reasoning_effort"]
        if inputs < 0 or outputs <= 0 or not isinstance(model, str) or not model:
            raise ValueError("Invalid model or token usage")
        if effort not in {"none", "low", "high", "max", "provider_default"}:
            raise ValueError("Actual reasoning effort must be explicit")
        sample = {
            "recorded_at": finished.isoformat(), "endpoint": endpoint.rstrip("/"),
            "model": model, "protocol": protocol, "reasoning_effort": effort,
            "effective_reasoning_effort": effort, "task_stage": stage,
            "input_tokens": inputs, "output_tokens": outputs,
            "duration_seconds": duration, "stream": False, "retry_index": 0,
            "success": True, "status": "complete", "source": "archived_http_observation",
        }
        details = usage.get("completion_tokens_details") or {}
        reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
        if reasoning is not None:
            reasoning = int(reasoning)
            if not 0 <= reasoning <= outputs:
                raise ValueError("Reasoning tokens must be part of completion_tokens")
            sample["reasoning_tokens"] = reasoning
            sample["visible_output_tokens"] = outputs - reasoning
        samples.append(sample)
        intervals.append((started.timestamp(), finished.timestamp()))
    events = sorted([(start, 1) for start, _ in intervals] + [(end, -1) for _, end in intervals])
    for sample, (start, end) in zip(samples, intervals, strict=True):
        active = peak = 0
        for moment, delta in events:
            if moment >= end:
                break
            active += delta
            if moment >= start:
                peak = max(peak, active)
        if not 1 <= peak <= 16:
            raise ValueError("Observed API concurrency outside supported range")
        sample["concurrency"] = peak
    return sorted(samples, key=lambda sample: sample["recorded_at"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=Path, required=True, help="Audited classified request JSON")
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--protocol", required=True)
    parser.add_argument("--output", type=Path, required=True, help="New telemetry/llm.jsonl destination")
    args = parser.parse_args()
    rows = json.loads(args.requests.read_text(encoding="utf-8"))
    samples = convert(rows, endpoint=args.endpoint, protocol=args.protocol)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        for sample in samples:
            handle.write(json.dumps(sample, ensure_ascii=False) + "\n")
    print(json.dumps({"imported_requests": len(samples), "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
