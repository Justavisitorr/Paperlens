"""One orchestration path for CLI and uploads, with resumable reasoning stages."""

from __future__ import annotations

import re
import shutil
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock

from .analysis import analyze_document
from .analysis.engine import AnalysisResult
from .analysis.prompts import PROMPT_VERSION
from .analysis.quantitative import collect_quantitative_results
from .audited_source import canonicalize_reading
from .auto_notes import AutoNoteService
from .config import Settings
from .control import (
    PDF_LOCK,
    ProcessingPaused,
    checkpoint,
    concurrency_limit,
    ordered_parallel_map,
    processing_concurrency,
    processing_stage,
)
from .edition_math_review import review_math
from .edition_native import input_mode as native_input_mode
from .edition_native import read_native
from .edition_text_review import review_text
from .errors import PaperLensError, ProviderError, ProviderOutcomeUnknown, ProviderRejected
from .exporting import (
    browser_session,
    extracted_text,
    get_dashboard_template,
    report_html,
    write_report,
)
from .graphs import build_views
from .input_budget import largest_input_budget
from .models import Box, DashboardReport, Quality, RunInfo, Usage
from .parsing import render_page_previews, validate_path
from .profiles import profile_limits
from .provider import JsonProvider, ProviderPool, RequestBudget, sum_usage
from .reading_parallel import request_slots
from .recovery import stage_failures
from .reference_links import resolve_references
from .storage import write_json
from .translation import TranslationService

Progress = Callable[[str, float, str], None]


@dataclass
class PipelineResult:
    output_dir: Path
    report_path: Path
    report: DashboardReport
    retryable: bool = False
    input_budget: dict | None = None


_sum_usage = sum_usage


def _reading_has_provider_error(transcription: dict, codes: set[str]) -> bool:
    audits = (
        transcription,
        transcription.get("math_review", {}),
        transcription.get("text_review", {}),
    )
    return any(
        batch.get("status") in {"partial", "unavailable"}
        and batch.get("error_code") in codes
        for audit in audits
        for batch in [audit, *audit.get("batches", [])]
    )


def _reading_outcome_unknown(transcription: dict) -> bool:
    return _reading_has_provider_error(transcription, {ProviderOutcomeUnknown.code})


def _reading_needs_retry(transcription: dict) -> bool:
    return _reading_has_provider_error(
        transcription, {ProviderError.code, ProviderOutcomeUnknown.code, ProviderRejected.code}
    )




def _run_postprocessing(
    report: DashboardReport,
    settings: Settings,
    budget: RequestBudget,
    storage_root: Path,
    output_dir: Path,
    emit: Progress,
    *,
    recovery: dict | None = None,
) -> tuple[dict[str, float], list[dict]]:
    """Run independent reading aids on stable snapshots; merge on the coordinator.

    Translation and notes consume the finalized original reading text. Neither
    depends on the other's generated output. Each has its own provider and cache;
    no worker mutates the published report or shared usage dictionaries.
    """
    branches = [name for name, enabled in (
        ("translation", settings.paperlens_auto_translate_enabled),
        ("auto_notes", settings.paperlens_auto_notes_enabled and bool(AutoNoteService.types(settings))),
    ) if enabled]
    if not branches:
        return {}, []
    started = time.monotonic()
    available = max(0, budget.limit - budget.used)
    # In serial mode a future branch cannot consume its reservation yet; making
    # the current branch wait for it would deadlock at the shared cap.
    reservations = {name: budget.reserve(1 if concurrency_limit() > 1 else 0) for name in branches}
    finished_records: dict[str, list[dict]] = {}

    def run(name: str) -> dict:
        local = report.model_copy(deep=True)
        branch_started = time.monotonic()
        records, warnings = [], []
        audit = {"status": "partial"}
        try:
            if name == "translation":
                emit(name, 0.931, "将英文正文译成中文（可与笔记同时进行）")
                routed = settings.for_role("chat")
                with processing_stage("translation"), JsonProvider(routed, budget=reservations[name]) as provider:
                    try:
                        audit = TranslationService(storage_root / "translations").generate(
                            local, settings, provider,
                            progress=lambda done, total: emit(
                                name, 0.931 + 0.003 * done / max(total, 1),
                                f"已翻译英文正文 {done}/{total} 批",
                            ),
                        )
                    finally:
                        records.append(provider.usage_record())
                if audit["remaining"]:
                    warnings.append(
                        "部分英文段落尚未翻译；原文及已有译文已保留，可在阅读页继续翻译。"
                    )
            else:
                emit(name, 0.935, "按设置为正文写页边笔记（可与翻译同时进行）")
                audit = AutoNoteService(storage_root).generate(
                    local.run.id, local, settings, {"scope": "paper"},
                    budget=reservations[name], resume=True,
                )
                records.extend(audit["usage_records"])
                if audit["errors"]:
                    warnings.append(
                        "部分自动笔记未生成；论文和已完成笔记已保留，可在阅读页重试。"
                    )
        except ProcessingPaused as exc:
            records.extend(getattr(exc, "usage_records", []))
            exc.usage_records = records
            raise
        except (PaperLensError, OSError, ValueError) as exc:
            warnings.append(
                "自动翻译未完成；论文分析已保留，可在阅读页继续翻译。"
                if name == "translation" else
                "自动笔记生成失败；论文分析已保留，可在阅读页检查设置后重试。"
            )
            audit["status"] = "partial"
            audit["error"] = str(exc) if isinstance(exc, PaperLensError) else type(exc).__name__
            audit["error_code"] = getattr(exc, "code", "processing_error")
            if getattr(exc, "details", None):
                audit["provider_error"] = exc.details
        finally:
            reservations[name].close()
            permits.release_branch(name)
            finished_records[name] = records
        audit_path = "translation-verification.json" if name == "translation" else "auto-notes-verification.json"
        try:
            write_json(output_dir / audit_path, audit)
        except OSError:
            warnings.append("附加阅读功能的处理记录未写入；已完成的译文、笔记和论文分析已保留。")
        return {
            "name": name, "seconds": round(time.monotonic() - branch_started, 3),
            "records": records, "warnings": warnings, "translations": local.translations,
            "input_budget": largest_input_budget(audit),
        }

    try:
        with request_slots() as permits:
            permits.reserve_branches(branches)
            try:
                results = ordered_parallel_map(run, branches)
            finally:
                for name in branches:
                    permits.release_branch(name)
    except ProcessingPaused as exc:
        # ordered_parallel_map drains its executor before propagating a pause.
        # Collect every branch's billed attempts, including valid cached results.
        exc.usage_records = [record for name in branches
                             for record in finished_records.get(name, [])]
        raise
    finally:
        # Also release a reservation if pause prevented its worker from launching.
        for reserved in reservations.values():
            reserved.close()
    timings, records = {}, []
    if recovery is not None:
        recovery.update(input_budget=largest_input_budget(results),
                        retryable=any(result["warnings"] for result in results))
    for result in results:
        timings[result["name"]] = result["seconds"]
        records.extend(result["records"])
        report.quality.warnings.extend(result["warnings"])
        if result["name"] == "translation":
            report.translations = result["translations"]
    timings["postprocessing"] = round(time.monotonic() - started, 3)
    try:
        write_json(output_dir / "postprocessing-verification.json", {
        "parallel": len(branches) > 1 and concurrency_limit() > 1, "branches": branches,
            "branch_seconds": {name: timings[name] for name in branches},
            "wall_seconds": timings["postprocessing"],
            "request_budget_remaining_at_start": available,
            "request_budget_used_at_finish": budget.used,
            "request_peak": permits.peak,
        })
    except OSError:
        report.quality.warnings.append("附加阅读功能的用时记录未写入；已完成的处理结果已保留。")
    return timings, records


@request_slots()
def run_pipeline(
    pdf_path: Path,
    settings: Settings,
    *,
    progress: Progress | None = None,
    source_name: str | None = None,
    reader_storage_root: Path | None = None,
) -> PipelineResult:
    settings.require_provider()
    settings.prepare_local_paths()
    checkpoint()
    with PDF_LOCK:
        source = validate_path(pdf_path, settings.paperlens_max_pages)
    # Fail missing UI/browser setup before spending API tokens on a long paper.
    get_dashboard_template()
    with browser_session(settings.paperlens_browser_channel):
        pass
    started = time.monotonic()
    timings: dict[str, float] = {}
    run_id = uuid.uuid4().hex
    safe_name = re.sub(r"[^\w\u3400-\u9fff.-]+", "_", source.stem)[:70].strip("._") or "paper"
    output_dir = settings.paperlens_output_dir / f"{safe_name}_{run_id[:8]}"
    output_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, output_dir / "source.pdf")
    last_fraction = 0.0
    progress_lock = RLock()

    def emit(stage: str, fraction: float, message: str) -> None:
        nonlocal last_fraction
        checkpoint()
        with progress_lock:
            last_fraction = max(last_fraction, min(1.0, max(0.0, fraction)))
            if progress:
                progress(stage, last_fraction, message)

    shared_budget = RequestBudget(settings.llm_max_requests)
    usage_by_model: dict[str, Usage] = {}
    usage_records: list[dict] = []
    vision_settings = settings.for_role("vision")
    if settings.paperlens_vision_model and "vision" not in settings.llm_routes:
        vision_settings = vision_settings.model_copy(update={"llm_model": settings.paperlens_vision_model})
    vision_settings = vision_settings.model_copy(update={
        "llm_max_requests": settings.llm_max_requests,
    })
    vision_model = vision_settings.llm_model
    emit("native_reading", 0.02, "AI 直接读取原 PDF 页面")
    with JsonProvider(vision_settings, budget=shared_budget,
                      responses_api=native_input_mode(vision_settings) == "pdf") as image_provider:
        native = read_native(source, vision_settings, image_provider, progress=lambda done, total: emit(
            "native_reading", 0.03 + 0.15 * done / max(total, 1),
            f"已处理原页直读 {done}/{total} 批",
        ))
        usage_by_model[vision_model] = image_provider.usage.model_copy()
        usage_records.append(image_provider.usage_record())
    document = native.report.document
    write_json(output_dir / "native-reading-verification.json", native.audit)
    document.source_name = source_name or source.name
    timings["parsing"] = round(time.monotonic() - started, 3)
    write_json(output_dir / "document.json", document.model_dump(mode="json"))
    (output_dir / "extracted.txt").write_text(extracted_text(document), encoding="utf-8")
    (output_dir / "extracted.md").write_text(
        extracted_text(document, markdown=True), encoding="utf-8"
    )

    native_incomplete = bool(native.audit["status"] != "completed")
    native_empty = bool(not native_incomplete and not document.blocks)
    report = native.report
    report.run.id = run_id
    report.run.processing_profile = settings.paperlens_processing_profile
    report.run.processing_limits = profile_limits(settings)
    write_json(output_dir / "original-document.json", document.model_dump(mode="json"))
    if report.reading:
        write_json(output_dir / "original-reading.json", report.reading.model_dump(mode="json", exclude={
            "items": {"__all__": {"asset"}}, "references": {"__all__": {"asset"}},
            "information_items": {"__all__": {"__all__": {"asset"}}},
        }))
    if not native_incomplete and not native_empty and settings.paperlens_math_enabled:
        emit("math_review", 0.19, "按原 PDF 整页审校数学内容")
    transcription_started = time.monotonic()
    transcription = dict(native.audit)
    if native_incomplete or native_empty:
        transcription["math_review"] = {
            "status": "deferred" if native_incomplete else "not_applicable", "batches": [],
        }
    else:
        with JsonProvider(vision_settings, budget=shared_budget,
                          responses_api=native_input_mode(vision_settings) == "pdf") as image_provider:
            with processing_concurrency(min(2, concurrency_limit())):
                transcription["math_review"] = review_math(
                    report, source, image_provider, vision_settings,
                    progress=lambda done, total: emit(
                        "math_review", 0.25, f"已处理数学原页校对 {done}/{total} 组",
                    ),
                )
            previous = usage_by_model.get(vision_model, Usage())
            usage_by_model[vision_model] = _sum_usage([previous, image_provider.usage])
            usage_records.append(image_provider.usage_record())
        timings["math_review"] = round(time.monotonic() - transcription_started, 3)
    timings["transcription"] = round(time.monotonic() - transcription_started, 3)
    from .reading_toc import filter_source_toc

    filter_source_toc(report)
    from .reading_references import repair_reference_boundaries

    repair_reference_boundaries(report)
    if not native_incomplete and not native_empty and settings.paperlens_text_review_enabled:
        emit("text_review", 0.30, "对照原页审校正文，修正乱码与串行")
    review_started = time.monotonic()
    review_settings = vision_settings.model_copy(update={"llm_max_requests": settings.llm_max_requests})
    post_recovery = {}
    if native_incomplete or native_empty:
        text_review = {"status": "deferred" if native_incomplete else "not_applicable",
                       "cache_hits": 0, "reviewed": 0, "candidates": 0,
                       "patches": [], "removals": [], "unresolved": [], "batches": []}
    elif settings.paperlens_text_review_enabled:
        with JsonProvider(review_settings, budget=shared_budget,
                          responses_api=native_input_mode(review_settings) == "pdf") as text_provider:
            with processing_concurrency(min(2, concurrency_limit())):
                text_review = review_text(
                    report, source, text_provider, review_settings,
                    progress=lambda done, total: emit(
                        "text_review", 0.30, f"已审校正文 {done}/{total} 批"
                    ),
                )
            if text_provider.usage.requests:
                previous = usage_by_model.get(vision_model, Usage())
                usage_by_model[vision_model] = _sum_usage([previous, text_provider.usage])
                usage_records.append(text_provider.usage_record())
    else:
        text_review = review_text(report, source, None, review_settings)
    timings["text_review"] = round(time.monotonic() - review_started, 3)
    transcription["text_review"] = text_review
    write_json(output_dir / "text-review.json", text_review)
    if text_review.get("reviewed"):
        report.quality.checks.append(
            f"模型对照原页审校正文 {text_review['reviewed']}/{text_review['candidates']} 段，"
            f"修正 {len(text_review['patches'])} 段，清理 {len(text_review['removals'])} 处重复或非正文残片"
        )
    if text_review["status"] in {"partial", "unavailable"}:
        report.quality.warnings.append("部分正文尚待核对原页，详见正文审校记录。")
    # Missing review work is a prerequisite; a completed but uncertain judgment
    # remains readable/analyzable with a warning, rather than an endless retry.
    # A formula review can intentionally cover only its top 1/2/4 batches.
    review_blocked = bool(_reading_needs_retry(transcription) or (
        settings.paperlens_text_review_enabled
        and (text_review["status"] == "unavailable" or (
            text_review["status"] == "partial" and (
                text_review.get("remaining_ids") or text_review.get("skipped")
                or not text_review.get("unresolved")
            )
        ))
    ))
    source_audit = {"blocks": []}
    if not native_incomplete and not native_empty:
        source_audit = canonicalize_reading(report)
        write_json(output_dir / "audited-source.json", source_audit)
        document = report.document
        write_json(output_dir / "document.json", document.model_dump(mode="json"))
        (output_dir / "extracted.txt").write_text(extracted_text(document), encoding="utf-8")
        (output_dir / "extracted.md").write_text(extracted_text(document, markdown=True), encoding="utf-8")
    write_json(output_dir / "reading-verification.json", transcription)
    reviewed_report = report
    analysis_empty = not any(block.kind != "reference" and block.text.strip() for block in document.blocks)
    unresolved_ids = {row["id"] for row in text_review.get("unresolved", [])}
    unresolved_blocks = {row["block_id"] for row in source_audit["blocks"] if row["item_id"] in unresolved_ids}
    source_locations = {row["item_id"]: "、".join(map(str, row["source_pages"])) for row in source_audit["blocks"]}
    source_warnings = [
        f"第 {source_locations.get(row['id'], '?')} 页正文保留待核对内容：{row.get('reason', '原页来源尚未确认')}"
        for row in text_review.get("unresolved", [])
    ]
    reviewed_report.quality.warnings.extend(source_warnings)

    analysis_settings = settings
    analysis_started = time.monotonic()
    if native_incomplete:
        interpreted = AnalysisResult(
            analysis=native.report.analysis, evidence=[],
            audit={"status": "deferred", "chunks": 0, "reason": "AI 直读尚未完成全部页面。"},
        )
    elif analysis_empty:
        interpreted = AnalysisResult(
            analysis=native.report.analysis.model_copy(update={
                "summary": "AI 直读将全部页面识别为空白页，没有可分析正文。" if native_empty
                else "审校后的阅读内容没有可分析正文；原 PDF 和审校记录保留以供核对。",
            }),
            evidence=[], audit={"status": "not_applicable", "chunks": 0},
        )
    elif review_blocked:
        interpreted = AnalysisResult(
            analysis=native.report.analysis.model_copy(update={"summary": "原页审校尚未完成；完成后继续分析审校后的正文。"}),
            evidence=[], audit={"status": "deferred", "chunks": 0, "reason": "原页审校尚未完成。"},
        )
    else:
        with ProviderPool(analysis_settings, budget=shared_budget) as provider:
            try:
                interpreted = analyze_document(
                    document,
                    provider,
                    analysis_settings,
                    progress=lambda stage, fraction, message: emit(
                        stage, 0.40 + 0.44 * min(1, max(0, (fraction - 0.20) / 0.65)), message
                    ),
                    supplemental_context={"visuals": [], "quantitative_results": [], "warnings": source_warnings,
                                          "unresolved_source_block_ids": sorted(unresolved_blocks)},
                )
            except ProcessingPaused:
                raise
            except Exception as exc:
                write_json(
                    output_dir / "failed-run.json",
                    {
                        "status": "failed",
                        "stage": "analysis",
                        "model": settings.llm_model,
                        "usage": provider.usage.model_dump(),
                        "visual_usage_by_model": {m: u.model_dump() for m, u in usage_by_model.items()},
                        "seconds": round(time.monotonic() - started, 3),
                        "error": str(exc) if isinstance(exc, PaperLensError) else type(exc).__name__,
                    },
                )
                raise
            for record in provider.usage_records():
                previous = usage_by_model.get(record["model"], Usage())
                usage_by_model[record["model"]] = _sum_usage(
                    [previous, Usage.model_validate(record["usage"])]
                )
                usage_records.append(record)
    usage = _sum_usage(list(usage_by_model.values()))
    if unresolved_blocks:
        uncertain_evidence = {e.id for e in interpreted.evidence if e.block_id in unresolved_blocks}
        for statement in [*interpreted.analysis.nodes, *interpreted.analysis.glossary]:
            if statement.support == "supported" and uncertain_evidence.intersection(statement.evidence_ids):
                statement.support = "partial"
        interpreted.audit["unresolved_source_block_ids"] = sorted(unresolved_blocks)
        interpreted.analysis.warnings.extend(source_warnings)
    timings["analysis"] = round(time.monotonic() - analysis_started, 3)
    write_json(output_dir / "verification.json", interpreted.audit)
    emit("visualization", 0.86, "生成多视图导图与原文定位图")
    views = [] if native_incomplete or analysis_empty or review_blocked else build_views(interpreted.analysis)
    cited_pages = sorted({page for e in interpreted.evidence for page in (e.source_pages or [e.page])})
    preview_pages = cited_pages[: settings.paperlens_preview_pages]
    warnings = [
        *document.warnings,
        *reviewed_report.quality.warnings,
        *interpreted.analysis.warnings,
    ]
    previews = []
    if preview_pages:
        try:
            with PDF_LOCK:
                previews = render_page_previews(source, preview_pages, max_width=1100)
        except Exception as exc:
            warnings.append(
                f"页图预览生成失败（{type(exc).__name__}），仍可通过页码和原文摘录定位。"
            )
    if len(cited_pages) > len(previews):
        warnings.append("部分证据页未内嵌页图；页码、章节、短摘录和完整正文仍可离线查看。")
    parsed_pages = sum(p.status in {"ok", "blank"} for p in document.pages)
    incomplete_pages = [p.number for p in document.pages if p.status in {"failed", "image_only"}]
    if incomplete_pages:
        warnings.append("这些物理页的正文未完整提取：" + "、".join(map(str, incomplete_pages)))
    report = DashboardReport(
        document=document,
        analysis=interpreted.analysis,
        evidence=interpreted.evidence,
        graphs=views,
        previews=previews,
        quantitative_results=collect_quantitative_results(
            document,
            interpreted.analysis,
            interpreted.evidence,
        ),
        quality=Quality(
            total_pages=len(document.pages),
            parsed_pages=parsed_pages,
            text_chars=sum(len(b.text) for b in document.blocks),
            grounded_nodes=sum(bool(n.evidence_ids) for n in interpreted.analysis.nodes),
            total_nodes=len(interpreted.analysis.nodes),
            checks=[
                "物理页面连续性与原文块编号校验",
                "引句精确或规范化匹配",
                "节点关系、层级与证据引用完整性校验",
                "逐节点模型支持性复核（不等于人工确认）",
                f"全文分为 {interpreted.audit['chunks']} 段，全部参与分析",
            ],
            warnings=list(dict.fromkeys(warnings)),
        ),
        run=RunInfo(
            id=run_id,
            created_at=datetime.now(UTC).isoformat(),
            model=settings.llm_model,
            parser=document.parser,
            status="partial" if incomplete_pages else "complete",
            timings=timings,
            usage=usage,
            prompt_version=PROMPT_VERSION,
            processing_profile=settings.paperlens_processing_profile,
            processing_limits=profile_limits(settings),
            provider_base_url=settings.llm_base_url,
            usage_scope="current_invocation",
            usage_by_model=usage_by_model,
            usage_by_provider=usage_records,
            model_routing=settings.public_configuration()["llm_routes"],
            cache_hits={
                "parsing": native.audit["cache_hits"],
                "analysis": interpreted.audit.get("cache_hits", 0),
            },
        ),
    )
    report.reading = reviewed_report.reading
    report.source_pdf = native.report.source_pdf
    for evidence in report.evidence:
        evidence.bbox = Box(x0=0, y0=0, x1=1, y1=1)
    report.quality.checks = (["已保存完成的原页直读批次；未完成页面可主动重试。"]
                            if native_incomplete else ["AI 已逐页识别为空白；原 PDF 保留以供核对。"]
                            if native_empty else ["审校后的阅读内容没有可分析正文；已保留原 PDF 和审校记录。"]
                            if analysis_empty else [
                                "已保存审校进度与可读正文；原页审校完成后再进行论文分析。",
                                *reviewed_report.quality.checks,
                            ] if review_blocked else [
                                "物理页面覆盖与 AI 转录块编号校验",
                                "分析引句与审校后的 AI 阅读正文匹配；原始转录和物理页保留以供核查",
                                *reviewed_report.quality.checks,
                                *report.quality.checks[2:],
                            ])
    if native_incomplete or analysis_empty or review_blocked:
        post_timings, post_records = {}, []
    else:
        post_timings, post_records = _run_postprocessing(
            report, settings, shared_budget,
            reader_storage_root or settings.paperlens_output_dir / ".reader", output_dir, emit,
            recovery=post_recovery,
        )
    timings.update(post_timings)
    for record in post_records:
        previous = usage_by_model.get(record["model"], Usage())
        usage_by_model[record["model"]] = _sum_usage(
            [previous, Usage.model_validate(record["usage"])]
        )
        usage_records.append(record)
    report.run.timings.update(timings)
    report.run.cache_hits["transcription"] = transcription["cache_hits"]
    report.run.cache_hits["text_review"] = text_review["cache_hits"]
    report.run.usage = usage = _sum_usage(list(usage_by_model.values()))
    report.run.usage_by_model = usage_by_model
    report.run.usage_by_provider = usage_records
    if transcription["status"] in {"unavailable", "partial"}:
        report.quality.warnings.append(
            "AI 直读未覆盖全部页面，已完成批次与原 PDF 保留，可主动重试。"
        )
    input_budget = largest_input_budget([transcription, post_recovery])
    retryable = (native_incomplete or review_blocked or _reading_needs_retry(transcription)
                 or bool(input_budget) or post_recovery.get("retryable", False))
    if retryable:
        report.run.status = "partial"
        if _reading_outcome_unknown(transcription):
            report.quality.warnings.append(
                "部分模型请求未返回结果，本次处理尚未完成；可主动重试，已完成批次将复用。"
            )
        else:
            report.quality.warnings.append(
                "部分模型请求失败，本次处理尚未完成；可主动重试，已完成批次将复用。"
            )
    write_json(output_dir / "reading-verification.json", transcription)
    report.run.failures = stage_failures(
        output_dir, report.run.model, report.run.model_routing, report
    )
    write_json(
        output_dir / "reference-links.json",
        resolve_references(
            report, settings.paperlens_cache_dir, allow_network=settings.paperlens_reference_network
        ),
    )
    report.quality.checks.append(
        "用量只统计本次执行；复用缓存的历史请求不重复计入，也不推算历史费用。"
    )
    emit("validation", 0.94, "断网校验 Mermaid 语法与实际页面渲染")
    validation_started = time.monotonic()
    # Preserve expensive completed analysis if a local browser or renderer fails.
    # The draft is explicitly unpublished and can be rebuilt after fixing setup.
    write_json(output_dir / "report-draft.json", report.model_dump(mode="json"))
    try:
        report_path = write_report(
            report, output_dir, browser_channel=settings.paperlens_browser_channel
        )
    except Exception as exc:
        write_json(
            output_dir / "failed-run.json",
            {
                "status": "failed",
                "stage": "rendering",
                "model": settings.llm_model,
                "usage": usage.model_dump(),
                "seconds": round(time.monotonic() - started, 3),
                "error": str(exc) if isinstance(exc, PaperLensError) else type(exc).__name__,
                "recovery": "Rebuild report-draft.json after resolving the local rendering error.",
            },
        )
        raise
    report.run.timings["validation"] = round(time.monotonic() - validation_started, 3)
    report.run.timings["total"] = round(time.monotonic() - started, 3)
    report_path.write_text(report_html(report), encoding="utf-8")
    write_json(output_dir / "report.json", report.model_dump(mode="json"))
    (output_dir / "report-draft.json").unlink(missing_ok=True)
    emit(report.run.status, 1.0, "已保存阅读进度，可主动重试未完成批次。" if retryable
         else "看板已生成，可直接双击离线打开")
    return PipelineResult(
        output_dir=output_dir, report_path=report_path, report=report, retryable=retryable,
        input_budget=input_budget,
    )
