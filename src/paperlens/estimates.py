"""Pre-import workload estimates and role-aware model pricing.

Document features and current settings determine token demand. Recent comparable
API measurements calibrate service time before public model metadata fallback.
Public metadata requests contain no document text and never call inference.
"""

from __future__ import annotations

import heapq
import math
from typing import Any

from .catalog import CatalogParseError, estimate_usage_cost, load_catalog
from .concurrency_policy import SpeedMode, job_execution_concurrency, resolve_speed_mode
from .config import Settings
from .estimate_timing import calibrate_usage, history_samples, request_seconds, scheduled_seconds
from .estimate_workload import DocumentFeatures, Work, workload
from .estimate_workload import inspect_pdf as inspect_pdf
from .model_metrics import load_model_metrics
from .model_metrics import usage_cost as public_usage_cost
from .profiles import PROFILE_LABELS, apply_profile

CHARS_PER_TOKEN = 2  # Legacy text-budget display; workload uses sampled language mix.
DEFAULT_PAGES = 12
POINTS = ("low", "typical", "high")


def _cost(works: list[Work], catalog, metrics=None) -> dict[str, Any]:
    groups: dict[tuple[str, str], list[float]] = {}
    for work in works:
        identity = (work.settings.llm_model, work.settings.llm_base_url)
        total = groups.setdefault(identity, [0.0, 0.0, 0.0])
        total[0] += work.requests * work.input_per_request
        total[1] += work.requests * work.output_per_request
        total[2] += work.requests
    sums = {"off_peak": [0.0, 0.0], "peak": [0.0, 0.0]}
    unknown = []
    priced = 0
    checked, stale = [], False
    for (model, endpoint), (prompt, completion, requests) in groups.items():
        full = estimate_usage_cost(
            {"prompt_tokens": round(prompt), "completion_tokens": round(completion)},
            model, catalog, base_url=endpoint,
        ) if catalog is not None else {"status": "unknown"}
        public = (metrics or {}).get((endpoint.rstrip("/"), model))
        if full["status"] == "unknown" and public is not None:
            cost = public_usage_cost(public, prompt, completion, requests)
            if cost["status"] != "unknown":
                priced += 1
                checked.append(public.checked_at)
                if cost["status"] == "partial":
                    unknown.append(model)
                for period in sums:
                    sums[period][0] += cost[period][0]
                    sums[period][1] += cost[period][1]
                continue
        if full["status"] == "unknown":
            unknown.append(model)
            continue
        priced += 1
        checked.append(catalog.checked_at.isoformat())
        stale |= bool(catalog.stale)
        for period, value in full["scenarios"].items():
            sums[period][0] += value.get("min_usd", value.get("estimated_usd", 0))
            sums[period][1] += value.get("max_usd", value.get("estimated_usd", 0))
    if not priced:
        return {"status": "unknown", "reason": "model_or_provider_price_unknown"}
    return {
        "status": "partial" if unknown else "range",
        **{period: [round(v, 4) for v in pair] for period, pair in sums.items()},
        **({"unpriced_models": sorted(set(unknown))} if unknown else {}),
        "price_checked_at": min(checked) if checked else None,
        "price_stale": stale,
    }


def _shared_branch_seconds(stages: list[str], works: dict[str, Work], service: dict[str, float],
                           concurrency: int) -> float:
    """Schedule ready independent requests under one shared worker capacity."""
    pending, active = {}, {stage: 0 for stage in stages}
    for stage in stages:
        work = works[stage]
        count = work.requests / work.retry_multiplier
        unit = service[stage] / work.requests * work.retry_multiplier
        full = math.floor(count)
        pending[stage] = [unit] * full + ([unit * (count - full)] if count > full else [])
    running, now, sequence = [], 0.0, 0
    while any(pending.values()) or running:
        while len(running) < concurrency:
            available = [stage for stage in stages if pending[stage]
                         and active[stage] < works[stage].concurrency]
            if not available:
                break
            # Share ready slots across branches, allowing either to borrow all
            # currently idle permits up to its own execution limit.
            stage = min(available, key=lambda name: active[name])
            duration = pending[stage].pop(0)
            sequence += 1
            active[stage] += 1
            heapq.heappush(running, (now + duration, sequence, stage))
        now, _, stage = heapq.heappop(running)
        active[stage] -= 1
    return now


def _schedule(durations: dict[str, float], concurrency: int,
              works: dict[str, Work] | None = None, service: dict[str, float] | None = None) -> list[dict]:
    """Stage barriers in execution order, with independent branch joins."""
    partners = {"node_review": "glossary_review", "translation": "auto_notes",
                "node_repair": "node_recheck"}
    partners |= {other: first for first, other in list(partners.items())}
    seen, result = set(), []
    for stage in durations:
        if stage in seen:
            continue
        stages = [stage]
        other = partners.get(stage)
        if other in durations:
            stages.append(other)
        seen.update(stages)
        values = [durations[name] for name in stages]
        if stage in {"node_repair", "node_recheck"}:
            # Every repair is followed by its own recheck before that batch
            # completes; other repair batches run in parallel.
            seconds = sum(values)
        elif len(stages) > 1 and works is not None and service is not None:
            seconds = _shared_branch_seconds(stages, works, service, concurrency)
        else:
            seconds = max(values) if concurrency > 1 else sum(values)
        result.append({
            "stages": stages,
            "seconds": seconds,
            "critical_stage": max(stages, key=lambda name: durations[name]),
        })
    return result


def estimate_profile(
    settings: Settings,
    profile: str,
    pages: int,
    samples: list[dict],
    catalog,
    features: DocumentFeatures | None = None,
    concurrency: int = 2,
    metrics=None,
) -> dict[str, Any]:
    effective = apply_profile(settings, profile)
    features = features or DocumentFeatures()
    scenarios = {}
    calibrated, uncalibrated, public_calibrated = set(), set(), set()
    limited = False
    typical_works = []
    typical_schedule = []
    for point in POINTS:
        works, local, clipped = workload(effective, pages, features, catalog, point, concurrency)
        durations: dict[str, float] = {}
        service: dict[str, float] = {}
        for work in works:
            calibrate_usage(work, samples, point)
            duration, count = request_seconds(work, samples, point, metrics)
            service[work.stage] = duration
            durations[work.stage] = scheduled_seconds(work, duration)
            identity = (work.settings.llm_base_url, work.settings.llm_model, work.effort)
            (calibrated if count > 0 else public_calibrated if count < 0 else uncalibrated).add(identity)
        schedule = _schedule(durations, concurrency, {work.stage: work for work in works}, service)
        seconds = sum(stage["seconds"] for stage in schedule)
        seconds += local * {"low": 0.6, "typical": 1, "high": 2.4}[point]
        scenarios[point] = {
            "requests": round(sum(w.requests for w in works)),
            "prompt_tokens": round(sum(w.requests * w.input_per_request for w in works)),
            "completion_tokens": round(sum(w.requests * w.output_per_request for w in works)),
            "seconds": max(1, round(seconds)),
            "cost": _cost(works, catalog, metrics),
        }
        if point == "typical":
            typical_works = works
            limited = clipped
            typical_schedule = [{**stage, "seconds": round(stage["seconds"], 2)} for stage in schedule]
            typical_schedule.append({"stages": ["local"], "seconds": round(local, 2),
                                     "critical_stage": "local"})
    typical = {
        field: {point: scenarios[point][field] for point in POINTS}
        for field in ("requests", "prompt_tokens", "completion_tokens", "seconds", "cost")
    }
    # Request clipping and batch transitions can move the roles used between
    # scenarios. Always return an ordered envelope around the expected result.
    for field in ("requests", "prompt_tokens", "completion_tokens", "seconds"):
        values = list(typical[field].values())
        typical[field]["low"] = min(values)
        typical[field]["high"] = max(values)
    for period in ("off_peak", "peak"):
        costs = [typical["cost"][p] for p in POINTS]
        if all(c["status"] == "range" and period in c for c in costs):
            pairs = [c[period][:] for c in costs]
            typical["cost"]["low"][period] = [min(pair[i] for pair in pairs) for i in (0, 1)]
            typical["cost"]["high"][period] = [max(pair[i] for pair in pairs) for i in (0, 1)]
    matches = [max(0, request_seconds(w, samples, "typical", metrics)[1]) for w in typical_works]
    time_basis = "recent_api_requests" if calibrated and not uncalibrated and not public_calibrated else (
        "openrouter_reference" if public_calibrated and not calibrated and not uncalibrated else
        "mixed" if calibrated or public_calibrated else "heuristic"
    )
    return {
        "profile": profile,
        "label": PROFILE_LABELS[profile],
        "samples": max(matches, default=0),
        "same_profile": False,  # No historical profile/page extrapolation is used.
        "basis": "feature_workload_heuristic",
        "time_basis": time_basis,
        "confidence": "medium" if time_basis == "recent_api_requests" and all(
            w.usage_samples >= 5 and w.usage_calibration == "recent_stage_reasoning"
            for w in typical_works
        ) else "low",
        "uncertainty": sorted({
            "workload_and_conditional_repairs_are_estimated",
            *("stage_reasoning_samples_insufficient" for w in typical_works
              if w.usage_calibration not in {"recent_stage_reasoning"}),
            *("service_speed_samples_insufficient" for n in matches if n < 5),
        }),
        "budget_limited": limited,
        "concurrency": concurrency,
        "schedule": typical_schedule,
        "cost_basis": "role_priced_workload_scenarios_partial_means_known_subtotal",
        "ceiling": {
            "requests": effective.llm_max_requests,
            "prompt_tokens": effective.llm_max_requests * effective.llm_max_input_chars // CHARS_PER_TOKEN,
            "prompt_tokens_kind": "approximate_text_budget_excludes_images_and_synthesis",
            "completion_tokens": effective.llm_max_requests * effective.llm_max_output_tokens,
            "cost": {"status": "unknown", "reason": "role_allocation_and_image_usage_unknown"},
        },
        "typical": typical,
        "workload": [
            {
                "stage": w.stage, "role": w.role, "model": w.settings.llm_model,
                "requests": round(w.requests, 2),
                "prompt_tokens": round(w.requests * w.input_per_request),
                "completion_tokens": round(w.requests * w.output_per_request),
                "concurrency": w.concurrency,
                "usage_basis": w.usage_calibration,
                "usage_samples": w.usage_samples,
                "retry_multiplier": w.retry_multiplier,
            }
            for w in typical_works
        ],
    }


def estimate(
    settings: Settings,
    *,
    pages: int | None = None,
    profiles: list[str] | None = None,
    document_features: DocumentFeatures | dict | None = None,
    accelerated: bool = False,
    concurrency: int | None = None,
    speed_mode: SpeedMode | None = None,
) -> dict[str, Any]:
    samples = history_samples(settings.paperlens_cache_dir)
    try:
        catalog = load_catalog(
            settings.paperlens_cache_dir,
            allow_network=False,
            max_age_hours=settings.paperlens_catalog_max_age_hours,
        )
    except (CatalogParseError, OSError, ValueError):
        catalog = None
    features = document_features if isinstance(document_features, DocumentFeatures) else (
        DocumentFeatures.model_validate(document_features or {})
    )
    assumed = pages is None
    pages = DEFAULT_PAGES if pages is None else pages
    if not isinstance(pages, int) or not 1 <= pages <= 100000:
        raise ValueError("页数必须在 1 到 100000 之间。")
    metrics = load_model_metrics(settings)
    mode = resolve_speed_mode(speed_mode, accelerated)
    workers = max(1, min(16, concurrency if concurrency is not None else
                          job_execution_concurrency(settings, accelerated, speed_mode=mode)))
    results = [
        estimate_profile(settings, profile, pages, samples, catalog, features, workers, metrics)
        for profile in (profiles or list(PROFILE_LABELS))
    ]
    warnings = ["uncached_import_assumed", "heuristic_workload_not_a_completion_guarantee"]
    if features.chars_per_page < 100:
        warnings.append("little_or_no_text_layer_detected")
    if pages > settings.paperlens_max_pages:
        warnings.append("document_exceeds_page_limit")
    priced = [p["typical"]["cost"]["typical"] for p in results
              if p["typical"]["cost"]["typical"]["status"] != "unknown"]
    return {
        "pages": pages,
        "pages_assumed": assumed,
        "document_features": features.model_dump(),
        "model": settings.for_role("main").llm_model,
        "history_runs": max((p["samples"] for p in results), default=0),
        "history_basis": "recent_api_requests_only",
        "parallel_jobs": settings.paperlens_max_parallel_jobs,
        "accelerated": mode != "normal",
        "speed_mode": mode,
        "concurrency": workers,
        "model_metric_sources": [
            {"model": m.model, "endpoint": m.endpoint, "source": m.source_url,
             "checked_at": m.checked_at, "price_applies": m.openrouter_route}
            for m in metrics.values()
        ],
        "time_basis": "token_workload_and_request_service_time",
        "chars_per_token": CHARS_PER_TOKEN,
        "price_checked_at": min((p["price_checked_at"] for p in priced), default=None),
        "price_stale": any(p["price_stale"] for p in priced) if priced else True,
        "warnings": warnings,
        "profiles": results,
    }


def run_cost(run: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Price a finished run's recorded usage at today's reference prices.

    Usage is grouped per model and endpoint; a group without a verified price
    makes the total partial instead of silently dropping it.
    """
    try:
        catalog = load_catalog(
            settings.paperlens_cache_dir,
            allow_network=False,
            max_age_hours=settings.paperlens_catalog_max_age_hours,
        )
    except (CatalogParseError, OSError, ValueError):
        catalog = None
    records = run.get("usage_by_provider") or [
        {
            "model": run.get("model", ""),
            "provider_base_url": run.get("provider_base_url") or "",
            "usage": run.get("usage") or {},
        }
    ]
    groups: dict[tuple[str, str], dict[str, int | None]] = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("usage"), dict):
            continue
        key = (str(record.get("model") or ""), str(record.get("provider_base_url") or ""))
        total = groups.setdefault(
            key,
            {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 0,
            },
        )
        usage = record["usage"]
        for field in ("prompt_tokens", "completion_tokens"):
            total[field] += int(usage.get(field) or 0)
        for field in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
            value = usage.get(field)
            # One record without a cache breakdown makes the group's breakdown unknown.
            total[field] = None if value is None or total[field] is None else total[field] + value
    # Usage outside the per-provider records (older runs, image calls) stays in
    # the run total; price that remainder under the run's own model.
    total = run.get("usage") or {}
    for field in ("prompt_tokens", "completion_tokens"):
        missing = int(total.get(field) or 0) - sum(g[field] for g in groups.values())
        if missing > 0 and run.get("usage_by_provider"):
            key = (str(run.get("model") or ""), str(run.get("provider_base_url") or ""))
            rest = groups.setdefault(
                key,
                {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "prompt_cache_hit_tokens": 0,
                    "prompt_cache_miss_tokens": 0,
                },
            )
            rest[field] += missing
            rest["prompt_cache_hit_tokens"] = rest["prompt_cache_miss_tokens"] = None
    items = []
    sums = {"off_peak": [0.0, 0.0], "peak": [0.0, 0.0]}
    priced = 0
    for (model, base_url), usage in groups.items():
        if not usage["prompt_tokens"] and not usage["completion_tokens"]:
            continue
        clean = {k: v for k, v in usage.items() if v is not None}
        estimate = (
            estimate_usage_cost(clean, model, catalog, base_url=base_url)
            if catalog
            else {"status": "unknown", "reason": "catalog_unavailable"}
        )
        item: dict[str, Any] = {"model": model, "status": estimate["status"]}
        if estimate["status"] != "unknown":
            priced += 1
            for period, value in estimate["scenarios"].items():
                low = value.get("estimated_usd", value.get("min_usd", 0))
                high = value.get("estimated_usd", value.get("max_usd", 0))
                item[period] = [round(low, 4), round(high, 4)]
                sums[period][0] += low
                sums[period][1] += high
        items.append(item)
    status = (
        "none"
        if not items
        else "unknown"
        if not priced
        else ("complete" if priced == len(items) else "partial")
    )
    return {
        "status": status,
        "currency": "USD",
        "price_checked_at": catalog.checked_at.isoformat() if catalog else None,
        "price_stale": bool(getattr(catalog, "stale", True)) if catalog else True,
        "models": items,
        **(
            {period: [round(a, 4), round(b, 4)] for period, (a, b) in sums.items()}
            if priced
            else {}
        ),
    }
