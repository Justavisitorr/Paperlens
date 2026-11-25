"""Conservative endpoint/model-scoped concurrency from recent local measurements.

No network or paid probing happens here. A single short-task study is experimental:
it may raise the fast ceiling, while ordinary work stays at no more than three
slots until a replicated study supplies stronger evidence. Missing, stale, malformed
or unrelated measurements retain the established 2/3 defaults.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from typing import Literal

from .catalog import is_official_deepseek
from .config import Settings

FALLBACK_NORMAL = 2
FALLBACK_FAST = 3
MAX_AGE = timedelta(days=7)
SpeedMode = Literal["normal", "fast", "ultra"]


def resolve_speed_mode(speed_mode: SpeedMode | None, accelerated: bool = False) -> SpeedMode:
    if speed_mode is None:
        return "fast" if accelerated else "normal"
    if speed_mode not in {"normal", "fast", "ultra"}:
        raise ValueError("请选择普通、加速或极速。")
    return speed_mode


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def measured_concurrency(entry: dict) -> tuple[int, int] | None:
    """Choose a reliable fastest width and the smallest near-fast ordinary width.

    Near-fast means >=80% of the best batch throughput. One round cannot locate a
    reliable saturation knee; cap that provisional ordinary choice at three and
    use the fastest measured error-free option within that conservative ceiling.
    """
    rows = []
    candidates = entry.get("rows", [])
    if not isinstance(candidates, list):
        return None
    for row in candidates:
        if not isinstance(row, dict):
            continue
        width, wall = row.get("width"), row.get("wall_seconds")
        count = row.get("actual_requests")
        if (type(width) is not int or not 1 <= width <= 5 or not _number(wall) or wall <= 0
                or type(count) is not int or count < 5 or row.get("errors", 1) != 0
                or row.get("retries", 1) != 0 or row.get("observed_max_inflight") != width):
            continue
        rows.append((width, float(wall) / count))
    if len({width for width, _ in rows}) < 3:
        return None
    fastest_width, fastest_per_request = min(rows, key=lambda row: (row[1], row[0]))
    near = [width for width, seconds in rows if seconds <= fastest_per_request / 0.8]
    normal = min(near)
    if entry.get("experimental", True) and normal > 3:
        conservative = [(width, seconds) for width, seconds in rows if width <= 3]
        if not conservative:
            return None
        normal = min(conservative, key=lambda row: (row[1], row[0]))[0]
    return normal, max(normal, fastest_width)


def measured_ultra(entry: dict) -> int | None:
    """Require an error-free same-size study and >=20% gain over width five.

    One study is still experimental. Keep the smallest width within 10% of the
    fastest measured batch; never infer an unmeasured 2500-worker application.
    """
    rows = []
    candidates = entry.get("rows", [])
    if not isinstance(candidates, list):
        return None
    for row in candidates:
        if not isinstance(row, dict):
            continue
        width, wall, count = row.get("width"), row.get("wall_seconds"), row.get("actual_requests")
        if (type(width) is not int or width not in {5, 8, 12, 16}
                or not _number(wall) or wall <= 0 or type(count) is not int or count < 16
                or row.get("errors", 1) != 0 or row.get("retries", 1) != 0
                or row.get("observed_http_peak", row.get("observed_max_inflight")) != width):
            continue
        rows.append((width, float(wall), count))
    if len({width for width, _, _ in rows}) < 3 or len({count for _, _, count in rows}) != 1:
        return None
    baseline = next((wall for width, wall, _ in rows if width == 5), None)
    if baseline is None:
        return None
    best = min(wall for _, wall, _ in rows)
    if best > baseline * 0.8:
        return None
    return min(width for width, wall, _ in rows if wall <= best * 1.1)


def _fresh(recorded_at, now: datetime) -> bool:
    try:
        recorded = datetime.fromisoformat(recorded_at)
        return recorded.tzinfo is not None and timedelta(0) <= now - recorded <= MAX_AGE
    except (ValueError, TypeError):
        return False


def execution_concurrency(
    settings: Settings, accelerated: bool = False, *, speed_mode: SpeedMode | None = None,
    now: datetime | None = None,
) -> int:
    """Resolve one effective endpoint/model; never borrow another model's result."""
    mode = resolve_speed_mode(speed_mode, accelerated)
    fallback = FALLBACK_NORMAL if mode == "normal" else FALLBACK_FAST
    path = settings.paperlens_cache_dir / "telemetry" / "concurrency.json"
    try:
        if path.stat().st_size > 1_000_000:
            return fallback
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1:
            return fallback
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            return fallback
        for entry in data.get("models", []):
            if (not isinstance(entry, dict)
                    or entry.get("endpoint", "").rstrip("/") != settings.llm_base_url.rstrip("/")
                    or entry.get("model") != settings.llm_model):
                continue
            chosen = measured_concurrency(entry) if _fresh(entry["recorded_at"], current) else None
            ordinary = chosen[0] if chosen else FALLBACK_NORMAL
            fast = chosen[1] if chosen else FALLBACK_FAST
            if mode != "ultra":
                return ordinary if mode == "normal" else fast
            ultra = entry.get("ultra_study")
            if (is_official_deepseek(settings.llm_base_url) and isinstance(ultra, dict)
                    and _fresh(ultra.get("recorded_at"), current)):
                measured = measured_ultra(ultra)
                if measured:
                    return max(fast, measured)
            return fast
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    return fallback


def job_execution_concurrency(
    settings: Settings, accelerated: bool = False, *, speed_mode: SpeedMode | None = None
) -> int:
    """One safe shared stage ceiling across every configured role a job can use."""
    roles = ["main", "reading", "verification", "vision"]
    notes = settings.paperlens_auto_notes_enabled and any(
        getattr(settings, f"paperlens_auto_notes_{kind}")
        for kind in ("summary", "explain", "connections", "questions", "check")
    )
    if settings.paperlens_auto_translate_enabled or notes:
        roles.append("chat")
    ceilings = []
    for role in roles:
        effective = settings.for_role(role)
        if (role == "vision" and settings.paperlens_vision_model
                and "vision" not in settings.llm_routes):
            effective = effective.model_copy(update={"llm_model": settings.paperlens_vision_model})
        ceilings.append(execution_concurrency(effective, accelerated, speed_mode=speed_mode))
    return min(ceilings)
