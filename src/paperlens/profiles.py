"""AI reading tiers: full page coverage, configurable analysis and review depth."""
from __future__ import annotations

from typing import Literal

from .config import Settings

ProcessingProfile = Literal["economy", "standard", "detailed"]
PROFILE_LABELS = {"economy": "经济", "standard": "标准", "detailed": "精细"}
FACTORS = {"economy": 0.5, "standard": 1, "detailed": 2}

def apply_profile(settings: Settings, profile: ProcessingProfile = "standard") -> Settings:
    if profile not in PROFILE_LABELS:
        raise ValueError("请选择经济、标准或精细处理。")
    return settings.model_copy(deep=True, update={"paperlens_processing_profile": profile})

def profile_limits(settings: Settings) -> dict:
    return {
        "ai_direct": True,
        "max_requests": settings.llm_max_requests,
        "vision_enabled": True,
        "math_enabled": settings.paperlens_math_enabled,
        "auto_translate": settings.paperlens_auto_translate_enabled,
        "code_blocks": True,
        "input_chars": settings.llm_max_input_chars,
        "output_tokens": settings.llm_max_output_tokens,
        "auto_note_paragraphs": settings.paperlens_auto_notes_max_paragraphs if settings.paperlens_auto_notes_enabled else 0,
        "math_review_batches": math_review_limit(settings),
        **analysis_limits(settings),
    }

def analysis_limits(settings: Settings) -> dict[str, int]:
    """Text depth changes even when visual/OCR features are disabled.

    These are generation budgets and prompt targets, never limits on source coverage
    or evidence verification. The user's single-response ceiling always applies.
    """
    profile = settings.paperlens_processing_profile
    reading = settings.paperlens_reading_output_tokens
    if profile == "economy":
        reading = min(4800, max(512, reading // 2))
    return {
        "reading_output_tokens": min(reading, settings.llm_max_output_tokens),
        "synthesis_output_tokens": min(
            {"economy": 16000, "standard": 32000, "detailed": 48000}[profile],
            settings.llm_max_output_tokens,
        ),
        "analysis_node_target": {"economy": 12, "standard": 26, "detailed": 36}[profile],
        "analysis_glossary_target": 5 if profile == "economy" else 10,
        "analysis_text_percent": 55 if profile == "economy" else 100,
    }

def math_review_limit(settings: Settings) -> int:
    return {"economy": 1, "standard": 2, "detailed": 4}[settings.paperlens_processing_profile] if settings.paperlens_math_enabled else 0

def profile_catalog(settings: Settings) -> dict:
    descriptions = {
        "economy": "精简解读与公式审校，AI 直读仍覆盖全文。",
        "standard": "AI 直读全文，按当前设置分析与审校。",
        "detailed": "展开文字解释，增加公式审校，AI 直读仍覆盖全文。",
    }
    return {
        "default": "standard",
        "profiles": [{"id": ident, "label": label, "description": descriptions[ident],
                      "factor": FACTORS[ident], "limits": profile_limits(apply_profile(settings, ident)),
                      "unlimited_limits": []} for ident, label in PROFILE_LABELS.items()],
        "auto_notes": {
            "enabled": settings.paperlens_auto_notes_enabled,
            "max_paragraphs": settings.paperlens_auto_notes_max_paragraphs,
            "context_scope": settings.paperlens_auto_notes_context,
            "types": [name for name in ("summary", "explain", "connections", "questions", "check") if getattr(settings, f"paperlens_auto_notes_{name}")],
        },
        "rule": "各档均 AI 直读全文；档位调整解释深度与公式审校批次，总请求上限按当前设置。",
        "cost_note": "实际花费以服务商账单为准。",
    }
