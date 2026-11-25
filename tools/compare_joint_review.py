"""Run isolated, sequential review arms on a frozen edition (dry plan by default).

Uses the saved account without printing credentials. No parsing, analysis, report
publication, or production settings are changed. Receipts retain raw token usage
because the app's aggregate usage does not include cache writes or reasoning.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image

from paperlens.config import Settings
from paperlens.connections import ConnectionStore
from paperlens.control import processing_concurrency, task_stage
from paperlens.edition_math_review import MAX_IMAGES, _candidates, _crop_count, review_math
from paperlens.edition_text_review import _plan, review_text
from paperlens.models import DashboardReport
from paperlens.profiles import math_review_limit
from paperlens.provider import JsonProvider
from paperlens.reading_references import repair_reference_boundaries
from paperlens.reading_toc import filter_source_toc

ROOT = Path(__file__).resolve().parents[1]
PRICE = {
    "checked_on": "2026-11-12",
    "source": "https://developers.openai.com/api/docs/models/gpt-6.1-terra",
    "cache_source": "https://developers.openai.com/api/docs/guides/prompt-caching",
    "usd_per_million": {"input": 1.0, "cached": 0.05, "write_30m": 1.25,
                        "write_12h": 2.5, "output": 5.0},
    "note": "Estimate, not invoice. Cache-write rates are total rates. GPT-6 TTL defaults to 30m; legacy prompt_cache_retention is a distinct field. Reasoning is included in output.",
}


def read(path: Path) -> dict:
    return json.loads(path.read_text("utf-8"))


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
    tmp.replace(path)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat()


def cost(receipt: dict) -> dict:
    response = receipt.get("response") or {}
    usage = response.get("usage")
    if not isinstance(usage, dict) or "input_tokens" not in usage or "output_tokens" not in usage:
        return {"usd": None, "reason": "No confirmed usage; not treated as zero"}
    tokens_in, tokens_out = usage["input_tokens"], usage["output_tokens"]
    details = usage.get("input_tokens_details") or {}
    cached = details.get("cached_tokens", 0)
    written = details.get("cache_write_tokens", 0)
    ordinary = tokens_in - cached - written
    model = response.get("model", "")
    tier = response.get("service_tier", "default")
    if not model.startswith("gpt-6.1-terra") or tier not in {"default", "standard"}:
        return {"usd": None, "reason": "Actual model/service tier differs from frozen standard pricing"}
    # TTL is a GPT-6 billing option, not the older prompt_cache_retention field.
    options = response.get("prompt_cache_options") or receipt.get("request", {}).get("prompt_cache_options") or {}
    ttl = options.get("ttl", "30m")
    if ttl not in {"30m", "12h"} or min(ordinary, cached, written, tokens_out) < 0:
        return {"usd": None, "reason": "Unknown cache TTL or inconsistent usage"}
    write_details = details.get("cache_write_tokens_details")
    if write_details is not None:
        short_write = write_details.get("ttl_30m_tokens", 0)
        long_write = write_details.get("ttl_12h_tokens", 0)
        if min(short_write, long_write) < 0 or short_write + long_write != written:
            return {"usd": None, "reason": "Cache-write lifetime breakdown is inconsistent"}
    else:
        short_write, long_write = (written, 0) if ttl == "30m" else (0, written)
    rates = PRICE["usd_per_million"]
    input_multiplier = 2 if tokens_in > 272000 else 1
    output_multiplier = 1.5 if tokens_in > 272000 else 1
    amount = ((ordinary * rates["input"] + cached * rates["cached"]
               + short_write * rates["write_30m"] + long_write * rates["write_12h"]) * input_multiplier
              + tokens_out * rates["output"] * output_multiplier) / 1_000_000
    return {"usd": round(amount, 9), "cache_ttl": ttl, "ordinary_input_tokens": ordinary,
            "cached_tokens": cached, "cache_write_tokens": written,
            "cache_write_30m_tokens": short_write, "cache_write_12h_tokens": long_write,
            "input_tokens": tokens_in, "output_tokens": tokens_out,
            "reasoning_tokens": (usage.get("output_tokens_details") or {}).get("reasoning_tokens", 0)}


class RecordingProvider(JsonProvider):
    def __init__(self, settings: Settings, directory: Path):
        super().__init__(settings, responses_api=True)
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.receipt_lock = threading.Lock()
        self.counter = max([int(p.stem.split("-")[-1]) for p in directory.glob("request-*.json")], default=0)

    def _responses_request(self, payload: dict):
        # Store image hashes/dimensions, never another copy of the base64 PDF/images.
        safe_payload = {key: value for key, value in payload.items() if key != "input"}
        safe_payload["input"] = []
        for message in payload.get("input", []):
            content = []
            for part in message.get("content", []):
                if part.get("type") == "input_image":
                    data = base64.b64decode(part["image_url"].split(",", 1)[1])
                    with Image.open(io.BytesIO(data)) as img:
                        content.append({"type": "input_image", "sha256": hashlib.sha256(data).hexdigest(),
                                        "bytes": len(data), "width": img.width, "height": img.height,
                                        "detail": part.get("detail")})
                else:
                    content.append(part)
            safe_payload["input"].append(message | {"content": content})
        with self.receipt_lock:
            self.counter += 1
            index = self.counter
        path = self.directory / f"request-{index:04d}.json"
        receipt = {"index": index, "started_at": now(), "stage": task_stage(),
                   "state": "submitted", "request": safe_payload}
        write(path, receipt)
        print(json.dumps({"request": index, "stage": task_stage(), "state": "submitted"}), flush=True)
        started = time.monotonic()
        try:
            response = super()._responses_request(payload)
            receipt["http_status"] = response.status_code
            try:
                receipt["response"] = response.json()
            except ValueError:
                receipt["response"] = {"unparseable": True}
            receipt["state"] = "returned"
            return response
        except BaseException as exc:
            receipt.update(state="unknown", error_type=type(exc).__name__)
            raise
        finally:
            receipt.update(finished_at=now(), seconds=round(time.monotonic() - started, 3))
            receipt["cost_estimate"] = cost(receipt)
            write(path, receipt)
            print(json.dumps({"request": index, "stage": receipt["stage"], "state": receipt["state"],
                              "seconds": receipt["seconds"], "usd_estimate": receipt["cost_estimate"]["usd"]}), flush=True)


def planning(report: DashboardReport, settings: Settings) -> dict:
    from paperlens.edition_joint_review import plan_joint_review

    items = {item.id: item for item in report.reading.items}
    if len(items) != len(report.reading.items):
        raise ValueError("Freeze unique item occurrences before comparing")
    text_batches, skipped = _plan(report, settings, items, None)
    math_batches = []
    for item in _candidates(report):
        count = _crop_count(item)
        if count > MAX_IMAGES:
            continue
        if not math_batches or sum(map(_crop_count, math_batches[-1])) + count > MAX_IMAGES:
            math_batches.append([])
        math_batches[-1].append(item)
    selected = math_batches[:math_review_limit(settings)]
    return {"sequential": {"math_batches": [[i.id for i in b] for b in selected],
                            "text_batches": [[r["id"] for r in b] for b in text_batches],
                            "skipped": skipped}, "joint": plan_joint_review(report, settings)}


def summarize(directory: Path) -> dict:
    receipts = [read(p) for p in sorted((directory / "requests").glob("request-*.json"))]
    estimates = [cost(row) for row in receipts]
    fields = ("input_tokens", "output_tokens", "cached_tokens", "cache_write_tokens", "reasoning_tokens")
    token_records = []
    for receipt in receipts:
        usage = receipt.get("response", {}).get("usage") or {}
        token_records.append({"input_tokens": usage.get("input_tokens", 0),
                              "output_tokens": usage.get("output_tokens", 0),
                              "cached_tokens": (usage.get("input_tokens_details") or {}).get("cached_tokens", 0),
                              "cache_write_tokens": (usage.get("input_tokens_details") or {}).get("cache_write_tokens", 0),
                              "reasoning_tokens": (usage.get("output_tokens_details") or {}).get("reasoning_tokens", 0)})
    return {"requests": len(receipts),
            "unknown_usage_requests": sum(not isinstance(row.get("response", {}).get("usage"), dict)
                                          for row in receipts),
            "unpriced_requests": sum(x["usd"] is None for x in estimates),
            "known_estimated_usd": round(sum(x["usd"] or 0 for x in estimates), 9),
            "tokens": {key: sum(x.get(key, 0) for x in token_records) for key in fields},
            "actual_models": sorted({r.get("response", {}).get("model", "unknown") for r in receipts}),
            "actual_service_tiers": sorted({r.get("response", {}).get("service_tier", "unknown") for r in receipts}),
            "sum_request_seconds": round(sum(r.get("seconds", 0) for r in receipts), 3)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--configuration", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--arm", choices=("sequential", "joint"), required=True)
    parser.add_argument("--execute", action="store_true", help="Make authorized paid model calls")
    parser.add_argument("--resume", action="store_true", help="Explicitly reuse successful batches and retry remaining work")
    args = parser.parse_args()
    directory = args.output.resolve() / args.arm
    settings = ConnectionStore(Settings()).restore(Settings(), read(args.configuration)).for_role("vision")
    if urlsplit(settings.llm_base_url).hostname != "api.openai.com":
        raise ValueError("This frozen comparison requires the configured official OpenAI account")
    if settings.llm_model != "gpt-6.1-terra" or settings.request_reasoning_effort() != "max":
        raise ValueError("Comparison requires GPT-6.1 Terra Max")
    settings = settings.model_copy(update={"paperlens_cache_dir": directory / "cache",
                                          "paperlens_output_dir": directory / "output",
                                          "paperlens_catalog_network": False,
                                          "paperlens_reference_network": False,
                                          # Isolated caches have no model catalog. This exact
                                          # model's image support is verified in saved live receipts.
                                          "paperlens_vision_allow_unverified": True})
    report = DashboardReport.model_validate_json(args.input.read_text("utf-8"))
    if report.document.sha256 != sha(args.pdf):
        raise ValueError("PDF differs from frozen report source")
    before_filter = report.reading.model_dump(mode="json")
    filter_source_toc(report)
    repair_reference_boundaries(report)
    if before_filter != report.reading.model_dump(mode="json"):
        raise ValueError("Freeze identical deterministic layout preprocessing before both arms")
    protocol = {"input_sha256": sha(args.input), "pdf_sha256": sha(args.pdf),
                "model": settings.llm_model, "effort": settings.request_reasoning_effort(),
                "account_fingerprint": hashlib.sha256(json.dumps({
                    "account": settings.llm_account_id, "base_url": settings.llm_base_url,
                    "protocol": settings.llm_protocol,
                }, sort_keys=True).encode()).hexdigest(),
                "profile": settings.paperlens_processing_profile,
                "math_enabled": settings.paperlens_math_enabled,
                "text_enabled": settings.paperlens_text_review_enabled,
                "concurrency": 2, "service_tier_requested": "provider_default",
                "vision_capability": "confirmed from existing live image responses and official model documentation",
                "output_cap": settings.llm_max_output_tokens, "input_chars_cap": settings.llm_max_input_chars,
                "request_cap": settings.llm_max_requests, "retries": settings.llm_max_retries,
                "timeout_seconds": settings.llm_timeout_seconds, "price": PRICE,
                "scope": "review only; reuse frozen direct-read results; no downstream analysis",
                "source_hashes": {name: sha(ROOT / "src/paperlens" / name) for name in (
                    "edition_math_review.py", "edition_text_review.py", "edition_joint_review.py", "provider.py")}}
    planned = planning(report, settings)
    if not args.execute:
        write(args.output / "dry-plan.json", {"protocol": protocol, "plan": planned})
        print(json.dumps({"paid_calls": 0, "sequential_math_batches": len(planned["sequential"]["math_batches"]),
                          "sequential_text_batches": len(planned["sequential"]["text_batches"]),
                          "joint_batches": len(planned["joint"]["batches"]),
                          "joint_skipped": planned["joint"]["skipped"]}))
        return
    if directory.exists() and (directory / "started.json").exists():
        if not args.resume:
            raise ValueError("Arm already started: inspect receipts; use --resume explicitly")
        saved = read(directory / "protocol.json")
        if saved != protocol:
            raise ValueError("Resume protocol or implementation changed")
    else:
        write(directory / "protocol.json", protocol)
        write(directory / "started.json", {"started_at": now(), "arm": args.arm})
    write(directory / "plan.json", planned[args.arm])
    prior_runs = []
    if (directory / "result.json").exists():
        previous = read(directory / "result.json")
        prior_runs = previous.get("invocations") or [{
            "started_at": read(directory / "started.json")["started_at"],
            "finished_at": previous["finished_at"], "wall_seconds": previous["wall_seconds"],
            "stage_seconds": previous.get("stage_seconds", {}),
        }]
    elif args.resume:
        # An interrupted process may have saved successful batches and receipts
        # before its outer finally block. Do not turn the unobserved tail into
        # measured wall time, or count the human interruption interval as work.
        receipts = [read(p) for p in (directory / "requests").glob("request-*.json")]
        ended = max((r["finished_at"] for r in receipts if r.get("finished_at")), default=None)
        if ended:
            began = read(directory / "started.json")["started_at"]
            prior_runs = [{"started_at": began, "finished_at": ended,
                           "wall_seconds": round((datetime.fromisoformat(ended) - datetime.fromisoformat(began)).total_seconds(), 3),
                           "observed_lower_bound": True, "interrupted_before_summary": True}]
    invoked_at = now()
    started = time.monotonic()
    audits, timings = {}, {}
    provider = RecordingProvider(settings, directory / "requests")
    try:
        with processing_concurrency(2):
            stages = [("math", review_math), ("text", review_text)]
            if args.arm == "joint":
                from paperlens.edition_joint_review import review_joint
                stages = [("joint", review_joint)]
            for name, function in stages:
                stamp = time.monotonic()
                audits[name] = function(report, args.pdf, provider, settings,
                                        progress=lambda done, total, name=name: print(json.dumps({"stage": name, "done": done, "total": total}), flush=True))
                timings[name] = round(time.monotonic() - stamp, 3)
                write(directory / f"{name}-audit.json", audits[name])
                write(directory / f"after-{name}.json", report.model_dump(mode="json"))
    finally:
        provider.close()
        write(directory / "report.json", report.model_dump(mode="json"))
        wall_seconds = round(time.monotonic() - started, 3)
        invocations = prior_runs + [{"started_at": invoked_at, "finished_at": now(),
                                    "wall_seconds": wall_seconds, "stage_seconds": timings}]
        result = {"finished_at": now(), "wall_seconds": wall_seconds,
                  "active_wall_seconds_total": round(sum(run["wall_seconds"] for run in invocations), 3),
                  "invocations": invocations,
                  "stage_seconds": timings, "audit_statuses": {k: v.get("status") for k, v in audits.items()},
                  **summarize(directory)}
        write(directory / "result.json", result)
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
