"""Experimental, single-pass prose/math review with an inspectable legacy scope.

This module is deliberately not wired into the production pipeline. The plan
retains the existing text candidates and the existing bounded math selection;
its image manifest explains every merged or higher-resolution replacement view.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from .config import Settings
from .control import checkpoint, ordered_parallel_map, processing_stage
from .edition_anchors import content_anchors
from .edition_math_review import MAX_IMAGES as MATH_MAX_IMAGES
from .edition_math_review import _candidates, _crop_count, validate_patches
from .edition_text_review import _SYSTEM as TEXT_SYSTEM
from .edition_text_review import TextReviewBatch, _plan, _source_pages, validate_reviews
from .errors import InputBudgetExceeded, ProviderError, ProviderOutcomeUnknown
from .formulas import strip_math_delimiters
from .input_budget import input_limit
from .models import Box, DashboardReport, ReadingItem
from .profiles import math_review_limit
from .provider import JsonProvider
from .reading_edition import page_image, vision_ready
from .storage import StageStore, fingerprint
from .transcription import region_images
from .translation import _item_text

VERSION = "experimental-1"
MAX_IMAGES = 8  # JsonProvider's actual image limit, including magnified views.
MAX_PAGES = 2
_SYSTEM = TEXT_SYSTEM + r"""
本次同时审校正文和数学，一次返回最终结果；不要提出需要再做第二遍的中间补丁。
review_scopes 给出每项的审校范围。含 text 的项同时保持原有行内数学；仅含 math 的项只修数学转录。
数学以图片为准，保留作者自己写下的错误；不得按常识补求和上限、更换下标、补推导或改写原式。
特别核对范数下标、无穷符号、上下标、负号、分母、转置、求和上下限及变量所属位置。
formula 的 content 只写 LaTeX，不含 $、$$、\(\)、\[\] 或代码围栏；其他项行内数学保留 $LaTeX$。
数学修正的 issue 用 source_mismatch，source_quote 可用原页对应公式的短 LaTeX 转写。
每项只返回一条最终 reviews 记录；公式和 algorithm 不能 removed，算法普通文字和步骤不能改写。
source_blocks.text 若存在只是独立提取的定位线索，符号仍可能损坏，图片为准。
来源坐标可能是模型估计，按完整表达式和相邻正文确认原页位置；不能替换成框旁的另一公式。
同一公式在不同位置真实重复时分别保留；无法确认位置或辨认字符时返回 unresolved。
image_indices 从1开始。同页同范围视图可能共用更高分辨率图，所有视图均来自同一原页。
"""


def _items(report: DashboardReport) -> dict[str, ReadingItem]:
    if not report.reading:
        return {}
    counts = Counter(item.id for item in report.reading.items)
    return {
        item.id if counts[item.id] == 1 else f"{item.id}@{index}": item
        for index, item in enumerate(report.reading.items)
    }


def _math_batches(report: DashboardReport, settings: Settings) -> tuple[list, list]:
    if (not report.reading or report.reading.version != "3"
            or not report.reading.structured_pages or not math_review_limit(settings)):
        return [], []
    candidates = _candidates(report)
    batches: list[list[ReadingItem]] = []
    for item in candidates:
        count = _crop_count(item)
        if count > MATH_MAX_IMAGES:
            continue
        if not batches or sum(map(_crop_count, batches[-1])) + count > MATH_MAX_IMAGES:
            batches.append([])
        batches[-1].append(item)
    return candidates, batches[:math_review_limit(settings)]


def _text_views(rows: list[dict]) -> list[dict]:
    pages = sorted({page for row in rows for page in row["pages"]})
    views = [
        {"page": page, "view": view, "scale": 2 if view == "whole" else 3,
         "render": "page"}
        for page in pages for view in ("whole", "left", "right")
    ]
    for row in rows:
        if len(row["content"]) <= 300 and row["source_regions"] and len(views) < MAX_IMAGES:
            views.append(row["source_regions"][0] | {
                "view": "region", "scale": 3, "render": "page", "item_id": row["id"],
            })
    return views


def _math_views(report: DashboardReport, ident: str, item: ReadingItem) -> list[dict]:
    blocks = {block.id: block for block in report.document.blocks}
    if report.document.parser in {"openai-pdf", "model-page-images"}:
        return [
            {"page": page, "view": "whole", "scale": 3, "render": "page"}
            for page in sorted({blocks[part.block_id].page for part in item.parts})
        ]
    if item.kind in {"formula", "algorithm"} and item.asset:
        return [{"page": item.page, "view": "asset", "render": "asset",
                 "item_id": ident, "asset_sha256": image_sha256(item.asset)}]
    boxes = (
        [(item.page, item.bbox)] if item.kind == "algorithm" and item.bbox else
        [(blocks[part.block_id].page, part.bbox or blocks[part.block_id].bbox)
         for part in item.parts]
    )
    return [
        {"page": page, "view": "region", "render": "math_region", "scale": 2.5,
         "box": [box.x0, box.y0, box.x1, box.y1], "item_id": ident, "part_index": index}
        for index, (page, box) in enumerate(boxes)
    ]


def _merge_views(views: list[dict]) -> list[dict]:
    """Keep region renderers intact; whole pages can use the existing higher scale."""
    merged: dict[str, dict] = {}
    for view in views:
        key = fingerprint({key: value for key, value in view.items() if key not in {
            "baseline_views", "item_id", "part_index", "scale",
        }} | ({} if view["view"] == "whole" else {"scale": view.get("scale")}))
        if key not in merged:
            merged[key] = view | {"baseline_views": list(view["baseline_views"])}
        else:
            retained = merged[key]
            if view.get("scale", 0) > retained.get("scale", 0):
                retained["scale"] = view["scale"]
            for baseline in view["baseline_views"]:
                if baseline not in retained["baseline_views"]:
                    retained["baseline_views"].append(baseline)
    return list(merged.values())


def _context(report: DashboardReport, items: dict, rows: list[dict]) -> list[dict]:
    blocks = {block.id: block for block in report.document.blocks}
    pages = {page for row in rows for page in row["pages"]}
    targets = {row["id"] for row in rows}
    context = [
        {"id": ident, "kind": item.kind, "pages": _source_pages(item, blocks),
         "content": _item_text(report, item)}
        for ident, item in items.items()
        if ident not in targets and set(_source_pages(item, blocks)) & pages
    ]
    available = targets | {row["id"] for row in context}
    for category, information in report.reading.information_items.items():
        for index, item in enumerate(information):
            source_pages = _source_pages(item, blocks)
            if not set(source_pages) & pages:
                continue
            ident = item.id
            if ident in available:
                ident = f"information-{category}-{index}-{ident}"
            available.add(ident)
            context.append({"id": ident, "kind": "information", "category": category,
                            "pages": source_pages, "content": _item_text(report, item)})
    return context


def _payload(rows: list[dict], views: list[dict], context: list[dict]) -> dict:
    return {
        "items": [row | {"image_indices": [
            index for index, view in enumerate(views, 1) if view["page"] in row["pages"]
        ]} for row in rows],
        "image_views": [
            {key: value for key, value in view.items()
             if key not in {"baseline_views", "asset_sha256", "part_index", "render"}}
            | {"image_index": index} for index, view in enumerate(views, 1)
        ],
        "context": context,
    }


def plan_joint_review(
    report: DashboardReport, settings: Settings, *, item_ids: set[str] | None = None,
) -> dict:
    """Read-only plan: legacy selection, all exclusions, and every input image view.

    item_ids selects original reading IDs, including all occurrences of an older
    duplicate ID. Math selection is capped *before* filtering, as in the baseline.
    No candidate content is truncated to make it fit.
    """
    items = _items(report)
    if item_ids is not None and not item_ids <= {item.id for item in items.values()}:
        raise ValueError("指定的联合审校内容不存在")
    text_batches, skipped = (
        _plan(report, settings, items, item_ids)
        if settings.paperlens_text_review_enabled and report.reading else ([], [])
    )
    math_candidates, math_batches = _math_batches(report, settings)
    occurrence = {id(item): ident for ident, item in items.items()}
    def selected(item):
        return item_ids is None or item.id in item_ids

    math_ids = [occurrence[id(item)] for item in math_candidates if selected(item)]
    math_selected = [occurrence[id(item)] for batch in math_batches for item in batch if selected(item)]
    rows = {row["id"]: row | {"review_scopes": ["text"]}
            for batch in text_batches for row in batch}
    text_ids = [ident for ident in items if ident in rows or any(s["id"] == ident for s in skipped)]
    views_by_id: dict[str, list[dict]] = {ident: [] for ident in rows}
    for batch_index, batch in enumerate(text_batches):
        for view_index, view in enumerate(_text_views(batch)):
            baseline = view | {"stage": "text", "batch": batch_index, "index": view_index}
            for row in batch:
                if (view["page"] in row["pages"]
                        and (view["view"] != "region" or view["item_id"] == row["id"])):
                    views_by_id[row["id"]].append(view | {"baseline_views": [baseline]})
    blocks = {block.id: block for block in report.document.blocks}
    for batch_index, batch in enumerate(math_batches):
        native_indices = {
            page: index for index, page in enumerate(sorted({
                blocks[part.block_id].page for item in batch for part in item.parts
            }))
        } if report.document.parser in {"openai-pdf", "model-page-images"} else {}
        image_offset = 0
        for item in batch:
            offset = image_offset
            image_offset += _crop_count(item)
            if not selected(item):
                continue
            ident = occurrence[id(item)]
            if ident not in rows:
                rows[ident] = {"id": ident, "kind": item.kind, "content": _item_text(report, item),
                               "pages": _source_pages(item, blocks), "review_scopes": []}
                views_by_id[ident] = []
            rows[ident]["review_scopes"].append("math")
            rows[ident]["source_blocks"] = [
                {"id": part.block_id, "page": blocks[part.block_id].page,
                 "box": (part.bbox or blocks[part.block_id].bbox).model_dump()}
                | ({"text": blocks[part.block_id].text} if report.document.parser not in {
                    "openai-pdf", "model-page-images",
                } else {}) for part in item.parts
            ]
            for view_index, view in enumerate(_math_views(report, ident, item)):
                baseline = view | {"stage": "math", "batch": batch_index,
                                   "index": native_indices.get(view["page"], offset + view_index),
                                   "target_id": ident}
                views_by_id[ident].append(view | {"baseline_views": [baseline]})
    # A text-overbudget paragraph may still be an eligible math-only target.
    # Preserve its missing text scope explicitly instead of claiming full review.
    skipped = [row | {"review_scopes": ["text"]} for row in skipped]
    planned = []
    current: list[dict] = []

    def batch_views(batch):
        return _merge_views([view for row in batch for view in views_by_id[row["id"]]])

    def finish(batch):
        views = batch_views(batch)
        context = _context(report, items, batch)
        omitted = []
        payload = _payload(batch, views, context)
        while context and len(json.dumps(payload, ensure_ascii=False)) > settings.llm_max_input_chars:
            omitted.append(context.pop()["id"])
        planned.append({"items": batch, "image_views": views, "context": context,
                        "omitted_context_ids": omitted,
                        "input_chars": len(json.dumps(payload, ensure_ascii=False))})

    valid_pages = {page.number for page in report.document.pages}
    for ident in items:
        if ident not in rows:
            continue
        row = rows[ident]
        if (not set(row["pages"]) <= valid_pages or len(row["pages"]) > MAX_PAGES
                or len(batch_views([row])) > MAX_IMAGES):
            skipped.append({"id": ident, "pages": row["pages"],
                            "review_scopes": row["review_scopes"],
                            "reason": "单项来源页码无效或超过联合审校页数/图片上限"})
            continue
        proposed = current + [row]
        views = batch_views(proposed)
        if current and (
            len({page for candidate in proposed for page in candidate["pages"]}) > MAX_PAGES
            or len(views) > MAX_IMAGES
            or len(json.dumps(_payload(proposed, views, []), ensure_ascii=False)) > settings.llm_max_input_chars
        ):
            finish(current)
            current = []
        current.append(row)
    if current:
        finish(current)
    candidate_ids = [ident for ident in items if ident in set(text_ids) | set(math_selected)]
    planned_ids = [row["id"] for batch in planned for row in batch["items"]]
    return {"version": VERSION, "math_limit": math_review_limit(settings),
            "text_candidate_ids": text_ids, "math_candidate_ids": math_ids,
            "math_selected_ids": math_selected,
            "math_deferred_ids": [ident for ident in math_ids if ident not in math_selected],
            "candidate_ids": candidate_ids, "eligible_ids": candidate_ids,
            "planned_ids": planned_ids, "skipped": skipped, "batches": planned}


def image_sha256(image: str) -> str:
    """Hash decoded image bytes, keeping audits free of the image data itself."""
    data = base64.b64decode(image.split(",", 1)[1]) if image.startswith("data:") else image.encode()
    return hashlib.sha256(data).hexdigest()


def render_joint_images(
    report: DashboardReport, pdf: Path, views: list[dict], *, items: dict | None = None,
) -> list[str]:
    """Render exactly the public plan's views using the unchanged legacy renderers."""
    items = _items(report) if items is None else items
    crops: dict[str, list[str]] = {}
    images = []
    for view in views:
        if view["render"] == "asset":
            images.append(items[view["item_id"]].asset)
            continue
        if view["render"] == "math_region":
            ident = view["item_id"]
            if ident not in crops:
                crops[ident] = region_images(pdf, report, items[ident])
            images.append(crops[ident][view["part_index"]])
            continue
        box = None
        if view["view"] == "region":
            x0, y0, x1, y1 = view["box"]
            box = Box(x0=max(0, x0 - 0.01), y0=max(0, y0 - 0.005),
                      x1=min(1, x1 + 0.01), y1=min(1, y1 + 0.005))
        elif view["view"] != "whole":
            box = Box(x0=0 if view["view"] == "left" else 0.48, y0=0,
                      x1=0.52 if view["view"] == "left" else 1, y1=1)
        elif view["scale"] == 3:
            box = Box(x0=0, y0=0, x1=1, y1=1)
        images.append(page_image(pdf, view["page"], box))
    return images


def validate_joint_reviews(raw: dict, rows: list[dict], context: list[dict]) -> TextReviewBatch:
    expected = {row["id"]: row for row in rows}
    batch = validate_reviews(raw, rows, context)
    for review in batch.reviews:
        row = expected[review.id]
        if review.status == "removed" and "text" not in row["review_scopes"]:
            raise ValueError("联合审校不能删除公式或数学专属审校内容")
        if review.status != "corrected":
            continue
        if row["kind"] == "formula":
            review.content = strip_math_delimiters(review.content)
            if not review.content.strip() or review.content == row["content"]:
                raise ValueError("未发生公式内容改动时应标为通过")
        elif "text" not in row["review_scopes"]:
            original = ReadingItem(id=review.id, kind=row["kind"], page=row["pages"][0],
                                   content=row["content"])
            validate_patches({"patches": [{"id": review.id, "content": review.content}]}, [original])
    return batch


def review_joint(
    report: DashboardReport, pdf: Path, provider: JsonProvider | None, settings: Settings,
    *, item_ids: set[str] | None = None, progress: Callable[[int, int], None] | None = None,
) -> dict:
    """Apply one validated final response per batch; retry only on a later invocation."""
    audit = {"version": VERSION, "model": settings.llm_model,
             "reasoning_effort": settings.request_reasoning_effort(),
             "calls": 0, "requests": 0, "cache_hits": 0, "patches": [], "removals": [],
             "unresolved": [], "batches": []}
    if not report.reading or not (settings.paperlens_text_review_enabled or math_review_limit(settings)):
        return audit | {"status": "disabled"}
    identity = {"version": VERSION, "pdf": report.document.sha256,
                "source": report.reading.source_version, "model": settings.llm_model,
                "document": fingerprint(report.document.model_dump(mode="json")),
                "account_id": settings.llm_account_id, "base_url": settings.llm_base_url,
                "protocol": settings.llm_protocol, "effort": settings.request_reasoning_effort(),
                "text_enabled": settings.paperlens_text_review_enabled,
                "math_limit": math_review_limit(settings),
                "item_ids": sorted(item_ids) if item_ids is not None else None}

    def completed_store():
        return StageStore(settings.paperlens_cache_dir / "edition-joint-review-completed",
                          fingerprint(identity | {"reading": report.reading.model_dump(mode="json")}))

    completed = completed_store().load("result")
    if completed:
        return completed | {"calls": 0, "requests": 0, "cache_hits": 1,
                            "patches": [], "removals": [], "attempted_ids": []}
    # Freeze the original batch inputs across an interrupted run. Corrections can
    # change length, math complexity, context, or the <300-char crop rule; none
    # should cause a successfully reviewed item to be paid for a second time.
    # Only exact input/output snapshots may resume this plan; user edits start a
    # new one. Stable occurrence keys also survive an approved short-item removal.
    plan_store = StageStore(settings.paperlens_cache_dir / "edition-joint-review-plan", fingerprint(identity))
    snapshot = report.reading.model_dump(mode="json")
    saved_plan = plan_store.load("result")
    items = _items(report)
    if saved_plan and snapshot in (saved_plan.get("input"), saved_plan.get("output")):
        plan = saved_plan["plan"]
        if snapshot == saved_plan.get("output"):
            items = dict(zip(saved_plan["output_keys"], report.reading.items, strict=True))
        audit["resumed_plan"] = True
    else:
        plan = plan_joint_review(report, settings, item_ids=item_ids)
        saved_plan = {"input": snapshot, "output": snapshot, "output_keys": list(items), "plan": plan}
        plan_store.save("result", saved_plan)
        audit["resumed_plan"] = False
    audit["coverage"] = {key: value for key, value in plan.items() if key != "batches"}
    if provider is None or not vision_ready(settings):
        return audit | {"status": "unavailable", "reason": "联合审校需要已确认读图能力的模型",
                        "error_code": ProviderError.code}
    blocks = {block.id: block for block in report.document.blocks}
    usage_before = getattr(getattr(provider, "usage", None), "requests", 0)

    def check_batch(planned):
        checkpoint()
        rows, views, context = planned["items"], planned["image_views"], planned["context"]
        payload = _payload(rows, views, context)
        record = {"ids": [row["id"] for row in rows],
                  "pages": sorted({page for row in rows for page in row["pages"]}),
                  "images": len(views), "image_views": views,
                  "omitted_context_ids": planned["omitted_context_ids"],
                  "cache_hit": False, "called": False}
        batch_identity = identity | {
            "items": [{key: value for key, value in row.items() if key != "content"} for row in rows],
            "image_views": views,
        }
        store = StageStore(settings.paperlens_cache_dir / "edition-joint-review", fingerprint(batch_identity))
        try:
            saved = store.load("result")
            hit = bool(saved and payload in (saved.get("input"), saved.get("output")))
            if hit:
                batch = validate_joint_reviews(saved["result"], saved["input"]["items"],
                                               saved["input"]["context"])
                record["image_sha256"] = saved.get("image_sha256", [])
            else:
                prompt = json.dumps(payload, ensure_ascii=False)
                if len(prompt) > input_limit(settings):
                    raise InputBudgetExceeded(input_limit(settings), len(prompt), "联合审校")
                budget = getattr(provider, "_budget", None)
                if budget is not None and budget.used >= budget.limit:
                    record.update(status="unavailable", reason="已达到共享模型请求预算",
                                  error_code=ProviderError.code)
                    return record, None, True
                images = render_joint_images(report, pdf, views, items=items)
                if len(images) != len(views) or len(images) > MAX_IMAGES:
                    raise ValueError("联合审校图片与计划不一致")
                record["image_sha256"] = [image_sha256(image) for image in images]
                store.save("request", {"system": _SYSTEM, "payload": payload,
                                       "identity": batch_identity, "image_sha256": record["image_sha256"]})
                record["called"] = True
                with processing_stage("joint_review"):
                    raw = provider.json(_SYSTEM, prompt, images=images,
                                        max_tokens=settings.llm_max_output_tokens)
                store.save("raw", raw)
                batch = validate_joint_reviews(raw, payload["items"], context)
                corrected = {review.id: review.content for review in batch.reviews
                             if review.status == "corrected"}
                output = payload | {"items": [row | {"content": corrected.get(row["id"], row["content"])}
                                               for row in payload["items"]]}
                store.save("result", {"input": payload, "output": output, "result": raw,
                                      "image_sha256": record["image_sha256"]})
            record.update(status="completed", cache_hit=hit)
            return record, batch, False
        except InputBudgetExceeded as exc:
            record.update(status="unavailable", reason=str(exc), error_code=exc.code, input_budget=exc.budget)
            return record, None, False
        except (ValueError, ProviderError, OSError) as exc:
            record.update(status="unavailable", reason=str(exc),
                          error_code=exc.code if isinstance(exc, ProviderError) else ProviderError.code)
            if getattr(exc, "details", None):
                record["provider_error"] = exc.details
            return record, None, isinstance(exc, ProviderError) and not isinstance(exc, ProviderOutcomeUnknown)

    results = ordered_parallel_map(
        check_batch, plan["batches"], stop_when=lambda result: result[2],
        on_complete=lambda _index, done: progress(done, len(plan["batches"])) if progress else None,
    )
    proposed_removals = {review.id for _, batch, _ in results if batch is not None
                        for review in batch.reviews if review.status == "removed"}
    reviewed: set[str] = set()
    attempted: set[str] = set()
    for record, batch, _stop in results:
        called = record.pop("called")
        audit["calls"] += int(called)
        audit["cache_hits"] += int(record["cache_hit"])
        audit["batches"].append(record)
        if called:
            attempted.update(record["ids"])
        if batch is None:
            continue
        reviewed.update(record["ids"])
        for review in batch.reviews:
            item = items.get(review.id)
            if item is None:  # Already removed by this exact resumed plan.
                continue
            if review.status == "removed" and set(review.duplicate_of) & proposed_removals:
                audit["unresolved"].append(review.model_dump(exclude={"content"}) | {
                    "status": "unresolved", "reason": "跨批审校引用了也被标为移除的内容，保留原文待核对。",
                })
                continue
            if review.status == "unresolved":
                audit["unresolved"].append(review.model_dump(exclude={"content"}))
            elif review.status == "removed":
                audit["removals"].append(review.model_dump() | {"before": item.model_dump(mode="json")})
                report.translations.pop(item.id, None)
            elif review.status == "corrected" and _item_text(report, item) != review.content:
                before = _item_text(report, item)
                item.content = review.content
                item.content_spans = content_anchors(item, blocks)
                report.translations.pop(item.id, None)
                audit["patches"].append(review.model_dump() | {"before": before})
    removed_ids = {row["id"] for row in audit["removals"]}
    report.reading.items = [item for ident, item in items.items() if ident not in removed_ids]
    plan_store.save("result", saved_plan | {
        "output": report.reading.model_dump(mode="json"),
        "output_keys": [ident for ident in items if ident not in removed_ids],
    })
    remaining = [ident for ident in plan["candidate_ids"]
                 if ident not in reviewed or any(row["id"] == ident for row in plan["skipped"])]
    audit["requests"] = getattr(getattr(provider, "usage", None), "requests", usage_before) - usage_before
    result = audit | {"status": "partial" if remaining or audit["unresolved"] else "completed",
                      "candidates": len(plan["candidate_ids"]), "reviewed": len(reviewed),
                      "attempted_ids": sorted(attempted), "validated_ids": sorted(reviewed),
                      "unresolved_ids": [row["id"] for row in audit["unresolved"]],
                      "remaining_ids": remaining, "skipped": plan["skipped"]}
    if plan["skipped"]:
        result["error_code"] = ProviderError.code
    if result["status"] == "completed":
        completed_store().save("result", result)
    return result
