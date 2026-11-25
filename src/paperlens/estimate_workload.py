"""Small, explicit workload assumptions for an uncached paper import.

These are estimates of work performed by pipeline.py, not model output limits.
No paper text, model call or historical document usage is needed here.
"""

from __future__ import annotations

import math
import re
from contextlib import closing
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from .catalog import get_model
from .config import Settings
from .profiles import analysis_limits, math_review_limit


class DocumentFeatures(BaseModel):
    """Bounded scalar observations; safe to round-trip without retaining text."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    chars_per_page: float = Field(default=2800, ge=0, le=50000)
    latin_fraction: float = Field(default=0.8, ge=0, le=1)
    visual_pages_fraction: float = Field(default=0.35, ge=0, le=1)
    math_density: float = Field(default=1, ge=0, le=100)
    code_fraction: float = Field(default=0.04, ge=0, le=1)
    sampled_pages: int = Field(default=0, ge=0, le=8)
    source: Literal["assumed", "pdf_sample"] = "assumed"
    native_chunk_chars: list[Annotated[int, Field(ge=0, le=1000000)]] = Field(default_factory=list, max_length=100)
    native_chunk_limit: int = Field(default=0, ge=0, le=1000000)
    native_text_chars: int = Field(default=0, ge=0, le=10000000)
    native_math_probe_complete: bool = False
    native_math_review_candidates: int = Field(default=0, ge=0, le=100000)
    structure_source: Literal["unavailable", "native_parser_preview"] = "unavailable"
    structure_warning: Literal[
        "", "native_preview_timeout", "native_preview_failed", "native_preview_empty",
        "native_preview_chunk_limit",
    ] = ""


def inspect_pdf(path: Path, settings: Settings | None = None) -> tuple[int | None, DocumentFeatures]:
    """Inspect <= 8 evenly spaced pages, <= 12k chars and 200 objects per page."""
    pages = None
    try:
        import pypdfium2 as pdfium

        from .control import PDF_LOCK

        with PDF_LOCK, pdfium.PdfDocument(path) as pdf:
            pages = len(pdf)
            if not pages:
                return None, DocumentFeatures()
            indices = sorted({round(i * (pages - 1) / 7) for i in range(8)})
            chars, latin, letters, visuals, maths, codes = 0, 0, 0, 0, 0, 0
            for index in indices:
                with closing(pdf[index]) as page:
                    with closing(page.get_textpage()) as textpage:
                        text = textpage.get_text_range(0, min(12000, textpage.count_chars()))
                    chars += len(text.strip())
                    latin += len(re.findall(r"[A-Za-z]", text))
                    letters += len(re.findall(r"[A-Za-z\u3400-\u9fff]", text))
                    # Captions and image objects are hints, not an image segmentation pass.
                    has_image = any(o.type == 3 for o in islice(page.get_objects(), 200))
                    visuals += bool(has_image or re.search(
                        r"(?im)^\s*(?:fig(?:ure)?\.?|table|图|表)\s*\d", text
                    ))
                    maths += len(re.findall(r"(?m)[∑∫∏√≈≠≤≥]|^.{0,50}=.{0,50}$", text))
                    codes += len(re.findall(
                        r"(?m)^\s*(?:def |class |#include|for\s*\(|if\s*\(|return\b|import\s)", text
                    ))
            count = len(indices)
            features = DocumentFeatures(
                chars_per_page=round(chars / count),
                latin_fraction=round(latin / max(1, letters), 3),
                visual_pages_fraction=visuals / count,
                math_density=min(100, maths / count),
                code_fraction=min(1, codes * 50 / max(1, chars)),
                sampled_pages=count,
                source="pdf_sample",
            )
        return pages, features
    except Exception:  # noqa: BLE001 - invalid PDFs use explicit assumptions
        return pages, DocumentFeatures()


@dataclass
class Work:
    stage: str
    role: str
    settings: Settings
    requests: float
    input_per_request: float
    output_per_request: float
    effort: str
    concurrency: int = 1
    visible_output_per_request: float = 0
    output_cap: float = 0
    retry_multiplier: float = 1
    usage_calibration: str = "heuristic"
    usage_samples: int = 0


def vision_settings(settings: Settings) -> Settings:
    resolved = settings.for_role("vision")
    if settings.paperlens_vision_model and "vision" not in settings.llm_routes:
        resolved = resolved.model_copy(update={"llm_model": settings.paperlens_vision_model})
    return resolved


def vision_available(settings: Settings, catalog) -> bool:
    if settings.paperlens_vision_allow_unverified:
        return True
    return bool(catalog and get_model(
        catalog, settings.llm_model, base_url=settings.llm_base_url
    ).vision == "supported")


def workload(
    settings: Settings, pages: int, features: DocumentFeatures, catalog, point: str,
    concurrency: int = 2,
) -> tuple[list[Work], float, bool]:
    """Return ordered stages, local seconds and whether the request cap cuts work.

    The low/high cases vary document density and conditional repair work, with
    modest retry headroom in the high case. Caps only clip work; they never supply
    the expected number of tokens. Independent batches use the resolved job limit;
    optional translation and notes can overlap after reading is ready.
    """
    variation = {"low": 0.75, "typical": 1.0, "high": 1.35}[point]
    parallel = max(1, min(16, concurrency))
    if features.source == "assumed":
        variation = {"low": 0.55, "typical": 1.0, "high": 1.6}[point]
    cpt = 1.5 + features.latin_fraction * 2.2
    # A sparse sample may be a scanned PDF or front matter; keep a cautious
    # fallback instead of claiming a nearly free import of unsupported input.
    chars = pages * max(500, features.chars_per_page) * variation
    native_chunks = []
    # Without a structural preview, JSON block metadata is uncertain. This
    # fallback is explicit and deliberately varies across workload scenarios.
    overhead = {"low": 1.15, "typical": 1.4, "high": 1.8}[point]
    chunks = len(native_chunks) if native_chunks else max(
        1, math.ceil(chars * overhead / settings.llm_max_input_chars)
    )
    limits = analysis_limits(settings)
    text_scale = limits["analysis_text_percent"] / 100
    # Prompt targets describe output depth; all source chunks are still read.
    target = limits["analysis_node_target"]
    minimum = {"economy": 8, "standard": 14, "detailed": 24}[settings.paperlens_processing_profile]
    # Follow the prompt range; only genuinely tiny inputs use its short-paper
    # exception. Source length is not one node per arbitrary N characters.
    nodes = round((minimum + (target - minimum) * {"low": 0, "typical": 0.5, "high": 1}[point])
                  * min(1, max(0.25, chars / 5000)))
    term_minimum = 3 if settings.paperlens_processing_profile == "economy" else 4
    terms = round(term_minimum + (limits["analysis_glossary_target"] - term_minimum)
                  * {"low": 0, "typical": 0.5, "high": 1}[point])
    note_types = sum(getattr(settings, f"paperlens_auto_notes_{name}")
                     for name in ("summary", "explain", "connections", "questions", "check"))
    notes_enabled = settings.paperlens_auto_notes_enabled and note_types > 0
    repair_fraction = {"low": 0.0, "typical": 0.25, "high": 0.65}[point]
    retry = 1 + (min(0.3, settings.llm_max_retries * 0.1) if point == "high" else 0)
    works: list[Work] = []

    def add(stage, role, calls, prompt, output, *, effort=None, cap=None, concurrency=1):
        resolved = vision_settings(settings) if role == "vision" else settings.for_role(role)
        if calls <= 0:
            return
        active_effort = resolved.request_reasoning_effort(effort)
        # A prior only. Actual reasoning usage is calibrated separately by
        # model, selected effort and task stage when telemetry provides it.
        reasoning = {"none": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5, "max": 6, "default": 3}
        multiplier = reasoning[active_effort]
        if active_effort != "none":
            multiplier = max(1, multiplier * {"low": 0.6, "typical": 1, "high": 3}[point])
        maximum = min(cap or resolved.llm_max_output_tokens, resolved.llm_max_output_tokens)
        works.append(Work(
            stage, role, resolved, calls,
            max(1, prompt),
            min(output * multiplier, maximum),
            active_effort,
            concurrency,
            visible_output_per_request=output,
            output_cap=maximum,
            retry_multiplier=retry,
        ))

    calls = math.ceil(pages / 2)
    add("native_reading", "vision", calls, chars / calls / cpt + 5200,
        chars / calls / cpt * 1.4 + 900, concurrency=min(2, parallel))
    section_output = (chars / chunks / cpt * 0.4 + 220) * text_scale
    chunk_prompt = sum(native_chunks) / chunks / cpt + 900 if native_chunks else chars / chunks / cpt * overhead + 900
    add("extraction", "reading", chunks, chunk_prompt,
        section_output, effort="none", cap=limits["reading_output_tokens"], concurrency=parallel)
    extract_tokens = min(section_output, limits["reading_output_tokens"],
                         settings.llm_max_output_tokens) * chunks
    # Visible Chinese node text follows the prompt's 80–180 character summary
    # and 40–100 character explanation, plus title/JSON/evidence-ID overhead.
    # The token allowance is distinct from hidden reasoning, calibrated later.
    add("synthesis", "main", 1, extract_tokens + 1700,
        1000 + (nodes * 180 + terms * 100) * text_scale,
        cap=limits["synthesis_output_tokens"])
    add("node_review", "verification", math.ceil(nodes / 6),
        1800 + 6 * 700 * text_scale, 6 * 60, cap=16000,
        concurrency=parallel if parallel >= 4 else max(1, parallel - 1))
    # Terms are reviewed both before and after repairs, in batches of three.
    add("glossary_review", "verification", math.ceil(terms / 3),
        1600 + (nodes * 400 + terms * 120) * text_scale + 3 * 900, 3 * 100,
        cap=16000, concurrency=parallel if parallel >= 4 else 1)
    add("glossary_repair", "verification", terms / 3 * repair_fraction,
        2200 + nodes * 120 * text_scale, 3 * 350 * text_scale, cap=16000, concurrency=parallel)
    add("node_repair", "verification", nodes / 3 * repair_fraction, 4800, 3 * 400,
        cap=16000, concurrency=parallel)
    add("node_recheck", "verification", nodes / 3 * repair_fraction, 3800, 3 * 70,
        cap=16000, concurrency=parallel)
    add("glossary_recheck", "verification", math.ceil(terms / 3),
        1600 + (nodes * 400 + terms * 120) * text_scale + 3 * 900, 3 * 100,
        cap=16000, concurrency=parallel)
    if settings.paperlens_math_enabled:
        review_calls = min(math_review_limit(settings), math.ceil(pages * features.math_density / 8))
        add("math_review", "vision", review_calls, 6800, 1400, concurrency=min(2, parallel))
    if settings.paperlens_text_review_enabled:
        review_calls = max(math.ceil(pages / 2),
                           math.ceil(chars * 1.2 / max(500, settings.llm_max_input_chars - 1800)))
        add("text_review", "vision", review_calls, chars / review_calls / cpt + 6200,
            300 + chars / review_calls / cpt * repair_fraction,
            concurrency=min(2, parallel))
    if settings.paperlens_auto_translate_enabled and features.latin_fraction > 0.75:
        # TranslationService groups <= 16 items and also obeys its input budget.
        translation_chars = chars * 0.85
        batches = max(math.ceil(translation_chars / max(500, settings.llm_max_input_chars - 1700)),
                      math.ceil(translation_chars / (16 * 500)))
        add("translation", "chat", batches, translation_chars / batches / cpt + 650,
            translation_chars / batches / 2 + 160,
            concurrency=parallel)
    if notes_enabled:
        count = min(settings.paperlens_auto_notes_max_paragraphs, max(1, math.ceil(chars / 500)))
        context_chars = {"nearby": 1500, "section": chars / 6, "paper": chars}[
            settings.paperlens_auto_notes_context
        ]
        add("auto_notes", "chat", count,
            min(context_chars, settings.llm_max_input_chars * 0.65) / cpt + 800,
            180 + note_types * 180, concurrency=max(1, parallel - 1))
    # PDF rendering and report export; no local OCR models.
    local_seconds = 12 + pages * 0.3
    remaining = settings.llm_max_requests
    limited = False
    optional: list[Work] = []
    for work in works:
        planned = work.requests * retry
        if work.stage in {"translation", "auto_notes"}:
            work.requests = planned
            optional.append(work)
            continue
        work.requests = min(remaining, planned)
        limited |= planned > remaining
        remaining = max(0, remaining - work.requests)
    optional_total = sum(work.requests for work in optional)
    if optional_total > remaining:
        limited = True
        # Both branches share the remaining cap. Use a proportional planning
        # allocation instead of pretending translation must exhaust it first.
        for work in optional:
            work.requests *= remaining / optional_total
    return [w for w in works if w.requests > 0], local_seconds, limited
