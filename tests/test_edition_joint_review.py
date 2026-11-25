"""Joint review preserves legacy coverage and image evidence without a second pass."""

import json

import pytest
from test_reading_projection import b, report

from paperlens.config import Settings
from paperlens.control import ProcessingPaused, processing_concurrency, processing_control
from paperlens.edition_joint_review import plan_joint_review, render_joint_images, review_joint
from paperlens.errors import InputBudgetExceeded, ProviderError, ProviderOutcomeUnknown
from paperlens.models import ReadingDocument, ReadingItem, ReadingPart, ReadingTranslation, Usage
from paperlens.provider import RequestBudget
from paperlens.reading import utf16


def edition(contents, *, native=True):
    blocks = [b(f"source-{i}", content, page=page, kind="formula" if kind == "formula" else "text")
              for i, (page, kind, content) in enumerate(contents)]
    current = report(blocks)
    if native:
        current.document.parser = "model-page-images"
    current.reading = ReadingDocument(
        version="3", source_version="source", structured_pages=[page.number for page in current.document.pages],
        items=[ReadingItem(id=f"item-{i}", kind=kind, page=page, content=content,
                           parts=[ReadingPart(block_id=blocks[i].id, end=utf16(content))])
               for i, (page, kind, content) in enumerate(contents)],
    )
    return current


def settings(tmp_path, **changes):
    return Settings(_env_file=None, **{
        "paperlens_cache_dir": tmp_path, "paperlens_vision_allow_unverified": True,
        "llm_api_key": "test", "llm_model": "gpt-6-astra", "llm_reasoning_effort": "high",
        "llm_max_retries": 0,
    } | changes)


def correction(ident, content, *, page=1):
    return {"id": ident, "status": "corrected", "content": content,
            "issue": "source_mismatch", "reason": "对照原页修正识别错误。",
            "source_quote": content, "source_pages": [page]}


class Provider:
    def __init__(self, answer=None, budget=None):
        self.answer = answer or (lambda payload: {
            "reviews": [{"id": row["id"], "status": "ok"} for row in payload["items"]],
        })
        self._budget = budget or RequestBudget(120)
        self.usage = Usage()
        self.calls = []

    def json(self, system, user, **options):
        self._budget.take()
        self.usage.requests += 1
        payload = json.loads(user)
        self.calls.append((payload, options, system))
        return self.answer(payload)


@pytest.fixture(autouse=True)
def images(monkeypatch):
    monkeypatch.setattr("paperlens.edition_joint_review.page_image", lambda pdf, page, box=None:
                        f"image:{page}:{box.model_dump() if box else None}")
    monkeypatch.setattr("paperlens.edition_joint_review.region_images", lambda pdf, report, item:
                        [f"crop:{item.id}:{i}" for i in range(len(item.parts))])


def test_joint_repairs_prose_and_formula_once_and_preserves_source(tmp_path):
    current = edition([(1, "paragraph", r"The mo del uses $x^2$."), (1, "formula", r"\sum_i x_i^2")])
    original = current.model_copy(deep=True)
    current.translations["item-0"] = ReadingTranslation(text="旧译文", source="old")
    provider = Provider(lambda _: {"reviews": [
        correction("item-0", r"The model uses $x_i^2$."),
        correction("item-1", r"$$\sum_{i=1}^k x_i^2$$"),
    ]})
    configured = settings(tmp_path)
    audit = review_joint(current, tmp_path / "source.pdf", provider, configured)
    assert audit["status"] == "completed" and audit["calls"] == audit["requests"] == 1
    assert len(audit["patches"]) == 2 and not current.translations
    assert current.document == original.document
    assert current.reading.items[0].parts == original.reading.items[0].parts
    assert current.reading.items[0].content_spans
    assert current.reading.items[1].content == r"\sum_{i=1}^k x_i^2"
    payload, options, system = provider.calls[0]
    assert all(row["review_scopes"] == ["text", "math"] if row["kind"] == "paragraph"
               else row["review_scopes"] == ["math"] for row in payload["items"])
    assert len(options["images"]) == 4  # Whole + left + right + original text region.
    assert "一次返回最终结果" in system
    again = review_joint(current, tmp_path / "source.pdf", provider, configured)
    assert again["requests"] == 0 and again["cache_hits"] == 1 and not again["patches"]
    restored = review_joint(original, tmp_path / "source.pdf", provider, configured)
    assert restored["requests"] == 0 and restored["cache_hits"] == 1
    assert original.reading == current.reading


@pytest.mark.parametrize(("profile", "expected"), [("economy", 6), ("standard", 12), ("detailed", 24)])
def test_plan_keeps_legacy_math_cap_and_full_text_scope(tmp_path, profile, expected):
    current = edition([(1, "formula", r"\sum_i x_i")] * 30 + [
        (1, "paragraph", "Plain prose."), (1, "heading", "Introduction"),
        (1, "algorithm", "Set $x_i=0$."),
    ])
    before = current.model_dump()
    plan = plan_joint_review(current, settings(tmp_path, paperlens_processing_profile=profile))
    assert len(plan["math_candidate_ids"]) == 30
    assert len(plan["math_selected_ids"]) == expected
    assert len(plan["math_deferred_ids"]) == 30 - expected
    assert plan["text_candidate_ids"] == ["item-30", "item-31"]
    assert set(plan["planned_ids"]) == set(plan["math_selected_ids"] + plan["text_candidate_ids"])
    assert len(plan["candidate_ids"]) == expected + 2
    assert current.model_dump() == before


def test_whole_view_uses_higher_scale_and_retains_all_original_magnified_views(tmp_path):
    current = edition([(1, "paragraph", "First."), (1, "paragraph", "Second."),
                       (1, "formula", r"\frac{a}{b}"), (2, "formula", r"\sum_i x_i")])
    plan = plan_joint_review(current, settings(tmp_path))
    views = plan["batches"][0]["image_views"]
    page_one = [view for view in views if view["page"] == 1]
    assert len([view for view in page_one if view["view"] == "whole"]) == 1
    whole = next(view for view in page_one if view["view"] == "whole")
    assert whole["scale"] == 3
    assert {(row["stage"], row["scale"]) for row in whole["baseline_views"]} == {("text", 2), ("math", 3)}
    # Both paragraphs have identical source boxes; one exact region image suffices.
    region = next(view for view in page_one if view["view"] == "region")
    assert {row["item_id"] for row in region["baseline_views"]} == {"item-0", "item-1"}
    assert {view["view"] for view in page_one} == {"whole", "left", "right", "region"}
    rendered = render_joint_images(current, tmp_path / "source.pdf", views)
    assert "'x1': 1.0" in rendered[views.index(whole)]
    assert len(rendered) == len(views)


def test_non_native_formula_asset_and_math_regions_keep_legacy_renderer(tmp_path):
    current = edition([(1, "formula", r"\frac{a}{b}"), (1, "paragraph", r"Norm $\ell_2$."),
                       (1, "algorithm", r"Compute $x_i^2$.")], native=False)
    current.reading.items[0].asset = "data:image/png;base64,YQ=="
    plan = plan_joint_review(current, settings(tmp_path))
    views = plan["batches"][0]["image_views"]
    rendered = render_joint_images(current, tmp_path / "source.pdf", views)
    assert "data:image/png;base64,YQ==" in rendered
    assert "crop:item-1:0" in rendered
    # Identical math crop boxes have identical source pixels and may be shared.
    assert {view["render"] for view in views} == {"asset", "math_region", "page"}


@pytest.mark.parametrize("change", [
    {"source_quote": ""}, {"source_pages": [2]}, {"reason": ""}, {"content": ""},
])
def test_math_correction_requires_the_same_source_evidence_as_prose(tmp_path, change):
    current = edition([(1, "formula", r"\frac{a}{b}")])
    before = current.model_dump()
    provider = Provider(lambda _: {"reviews": [correction("item-0", r"\frac{a}{c}") | change]})
    audit = review_joint(current, tmp_path / "source.pdf", provider, settings(tmp_path))
    assert audit["status"] == "partial" and audit["reviewed"] == 0
    assert current.model_dump() == before
    assert not list(tmp_path.glob("edition-joint-review/*/result.json"))


@pytest.mark.parametrize("answer", [
    {"reviews": []},
    {"reviews": [{"id": "unknown", "status": "ok"}]},
    {"reviews": [{"id": "item-0", "status": "ok"}, {"id": "item-0", "status": "ok"}]},
    {"reviews": [{"id": "item-0", "status": "removed", "issue": "layout_mix",
                  "reason": "图中是非正文。", "source_pages": [1]}]},
])
def test_invalid_or_removed_formula_response_is_atomic(tmp_path, answer):
    current = edition([(1, "formula", r"\sum_i x_i")])
    before = current.model_dump()
    provider = Provider(lambda _: answer)
    audit = review_joint(current, tmp_path / "source.pdf", provider, settings(tmp_path))
    assert audit["status"] == "partial" and current.model_dump() == before
    assert len(provider.calls) == 1  # No implicit second pass or model repair retry.


def test_math_only_algorithm_cannot_rewrite_instructions(tmp_path):
    current = edition([(1, "algorithm", r"Do not change $x_i^2$.")])
    before = current.model_dump()
    provider = Provider(lambda _: {"reviews": [correction("item-0", r"Change $x_i^3$.")]})
    audit = review_joint(current, tmp_path / "source.pdf", provider, settings(tmp_path))
    assert audit["status"] == "partial" and current.model_dump() == before


def test_unreadable_math_is_retained_with_reason_and_cached(tmp_path):
    current = edition([(1, "formula", r"\sum_i x_i")])
    provider = Provider(lambda _: {"reviews": [{"id": "item-0", "status": "unresolved",
                                              "reason": "原页求和上限无法辨认。"}]})
    audit = review_joint(current, tmp_path / "source.pdf", provider, settings(tmp_path))
    assert audit["status"] == "partial" and audit["reviewed"] == 1
    assert audit["unresolved_ids"] == ["item-0"] and not audit["remaining_ids"]
    assert current.reading.items[0].content == r"\sum_i x_i"
    again = review_joint(current, tmp_path / "source.pdf", provider, settings(tmp_path))
    assert again["requests"] == 0 and again["unresolved"] == audit["unresolved"]


def test_short_duplicate_prose_can_reference_retained_formula(tmp_path):
    current = edition([(1, "formula", r"\sum_i x_i^2"), (1, "paragraph", "x i 2")])
    provider = Provider(lambda _: {"reviews": [
        {"id": "item-0", "status": "ok"},
        {"id": "item-1", "status": "removed", "reason": "原页公式只出现一次。",
         "source_pages": [1], "duplicate_of": "item-0"},
    ]})
    audit = review_joint(current, tmp_path / "source.pdf", provider, settings(tmp_path))
    assert audit["status"] == "completed" and [item.id for item in current.reading.items] == ["item-0"]
    assert audit["removals"][0]["before"]["id"] == "item-1"
    again = review_joint(current, tmp_path / "source.pdf", provider, settings(tmp_path))
    assert again["calls"] == 0 and not again["removals"]


def test_partial_failure_resumes_successful_batches_without_repeating_them(tmp_path):
    original = edition([(1, "paragraph", "First typo."), (3, "paragraph", "Second.")])
    current = original.model_copy(deep=True)
    failed = False

    def answer(payload):
        nonlocal failed
        ident = payload["items"][0]["id"]
        if ident == "item-1" and not failed:
            failed = True
            raise ValueError("invalid response")
        return {"reviews": [correction("item-0", "First corrected.") if ident == "item-0"
                            else {"id": ident, "status": "ok"}]}

    provider = Provider(answer)
    # Force separate batches by expanding each target's physical page coverage.
    current.reading.items[0].source_pages = [1, 2]
    original.reading.items[0].source_pages = [1, 2]
    with processing_concurrency(1):
        first = review_joint(current, tmp_path / "source.pdf", provider, settings(tmp_path))
        assert first["status"] == "partial" and first["validated_ids"] == ["item-0"]
        resumed = review_joint(current, tmp_path / "source.pdf", provider, settings(tmp_path))
    assert resumed["status"] == "completed" and resumed["cache_hits"] == 1
    assert resumed["calls"] == resumed["requests"] == 1 and len(provider.calls) == 3
    fresh = review_joint(original, tmp_path / "source.pdf", provider, settings(tmp_path))
    assert fresh["calls"] == 0 and fresh["cache_hits"] == 2


@pytest.mark.parametrize(("kind", "before", "after"), [
    ("paragraph", "Short typo.", "Complete corrected paragraph. " * 12),
    ("formula", r"\frac{a}{b}", "a"),
])
def test_resume_freezes_success_scope_when_repairs_change_crop_rule_or_math_rank(
    tmp_path, kind, before, after,
):
    current = edition([(1, kind, before), (3, "paragraph", "Later batch.")])
    current.reading.items[0].source_pages = [1, 2]
    failed = False

    def answer(payload):
        nonlocal failed
        ident = payload["items"][0]["id"]
        if ident == "item-1" and not failed:
            failed = True
            raise ValueError("retry later")
        return {"reviews": [correction(ident, after) if ident == "item-0" else
                            {"id": ident, "status": "ok"}]}

    provider = Provider(answer)
    with processing_concurrency(1):
        first = review_joint(current, tmp_path / "source.pdf", provider, settings(tmp_path))
        assert first["status"] == "partial" and current.reading.items[0].content == after
        resumed = review_joint(current, tmp_path / "source.pdf", provider, settings(tmp_path))
    assert resumed["status"] == "completed" and resumed["candidates"] == 2
    assert resumed["cache_hits"] == resumed["calls"] == 1 and resumed["resumed_plan"]
    assert len(provider.calls) == 3


def test_resume_keeps_original_occurrence_keys_after_short_item_removal(tmp_path):
    current = edition([(1, "paragraph", "globe"), (3, "formula", r"\sum_i x_i")], native=False)
    current.reading.items[0].source_pages = [1, 2]
    current.reading.items[1].id = "item-0"
    current.reading.items[1].asset = "data:image/png;base64,YQ=="
    failed = False

    def answer(payload):
        nonlocal failed
        ident = payload["items"][0]["id"]
        if ident == "item-0@0":
            return {"reviews": [{"id": ident, "status": "removed", "source_pages": [1],
                                 "reason": "原页只有网站图标，不是正文。"}]}
        if not failed:
            failed = True
            raise ValueError("retry later")
        return {"reviews": [{"id": ident, "status": "ok"}]}

    provider = Provider(answer)
    with processing_concurrency(1):
        first = review_joint(current, tmp_path / "source.pdf", provider, settings(tmp_path))
        assert first["status"] == "partial" and len(current.reading.items) == 1
        resumed = review_joint(current, tmp_path / "source.pdf", provider, settings(tmp_path))
    assert resumed["status"] == "completed" and resumed["reviewed"] == 2
    assert resumed["cache_hits"] == resumed["calls"] == 1
    assert provider.calls[-1][0]["items"][0]["id"] == "item-0@1"
    assert provider.calls[-1][1]["images"] == ["data:image/png;base64,YQ=="]


def test_shared_request_budget_stops_unlaunched_batches(tmp_path):
    current = edition([(1, "paragraph", "First."), (3, "paragraph", "Second.")])
    current.reading.items[0].source_pages = [1, 2]
    provider = Provider(budget=RequestBudget(1))
    with processing_concurrency(1):
        audit = review_joint(current, tmp_path / "source.pdf", provider, settings(tmp_path))
    assert audit["status"] == "partial" and audit["requests"] == 1
    assert audit["remaining_ids"] == ["item-1"]
    assert audit["batches"][1]["error_code"] == ProviderError.code


def test_pause_keeps_completed_batch_cache_before_coordinator_mutation(tmp_path):
    current = edition([(1, "paragraph", "First typo."), (3, "paragraph", "Second.")])
    current.reading.items[0].source_pages = [1, 2]
    provider = Provider(lambda payload: {"reviews": [
        correction("item-0", "First corrected.") if row["id"] == "item-0" else
        {"id": row["id"], "status": "ok"} for row in payload["items"]
    ]})

    def pause():
        if provider.calls:
            raise ProcessingPaused("pause")

    with processing_control(pause, concurrency=lambda: 1), pytest.raises(ProcessingPaused):
        review_joint(current, tmp_path / "source.pdf", provider, settings(tmp_path))
    assert current.reading.items[0].content == "First typo."
    assert len(list(tmp_path.glob("edition-joint-review/*/result.json"))) == 1
    resumed = review_joint(current, tmp_path / "source.pdf", provider, settings(tmp_path))
    assert resumed["status"] == "completed" and resumed["cache_hits"] == 1
    assert len(provider.calls) == 2


@pytest.mark.parametrize("error_type", [ProviderError, ProviderOutcomeUnknown])
def test_provider_failure_has_no_same_batch_retry(tmp_path, error_type):
    current = edition([(1, "paragraph", "First."), (3, "paragraph", "Second.")])
    current.reading.items[0].source_pages = [1, 2]

    def answer(payload):
        if payload["items"][0]["id"] == "item-0":
            raise error_type("failed")
        return {"reviews": [{"id": row["id"], "status": "ok"} for row in payload["items"]]}

    provider = Provider(answer)
    with processing_concurrency(1):
        audit = review_joint(current, tmp_path / "source.pdf", provider, settings(tmp_path))
    assert audit["status"] == "partial"
    assert len(provider.calls) == (2 if error_type is ProviderOutcomeUnknown else 1)


def test_oversized_math_input_is_not_truncated_or_sent(tmp_path):
    current = edition([(1, "formula", r"\sum_i " + "x" * 3000)])
    provider = Provider()
    audit = review_joint(current, tmp_path / "source.pdf", provider,
                         settings(tmp_path, llm_max_input_chars=2000))
    assert audit["status"] == "partial" and not provider.calls
    assert audit["remaining_ids"] == ["item-0"]
    assert audit["batches"][0]["error_code"] == InputBudgetExceeded.code
    assert audit["batches"][0]["input_budget"]["required_chars"] > 3000


def test_skipped_text_remains_in_coverage_and_never_counts_as_reviewed(tmp_path):
    current = edition([(1, "paragraph", "Large paragraph. " * 200)])
    provider = Provider()
    audit = review_joint(current, tmp_path / "source.pdf", provider,
                         settings(tmp_path, llm_max_input_chars=2000))
    assert audit["coverage"]["text_candidate_ids"] == ["item-0"]
    assert audit["status"] == "partial" and audit["reviewed"] == 0
    assert audit["remaining_ids"] == ["item-0"] and audit["skipped"] and not provider.calls


def test_optional_context_omission_is_exposed_without_truncating_targets(tmp_path):
    current = edition([(1, "paragraph", "Short target."), (1, "paragraph", "Context " * 400)])
    plan = plan_joint_review(current, settings(tmp_path, llm_max_input_chars=2000), item_ids={"item-0"})
    batch = plan["batches"][0]
    assert batch["omitted_context_ids"] == ["item-1"]
    assert batch["items"][0]["content"] == "Short target." and not batch["context"]


def test_account_effort_and_changed_content_invalidate_joint_cache(tmp_path):
    current = edition([(1, "paragraph", "Source.")])
    provider = Provider()
    configured = settings(tmp_path, llm_account_id="account-A")
    review_joint(current, tmp_path / "source.pdf", provider, configured)
    review_joint(current, tmp_path / "source.pdf", provider,
                 configured.model_copy(update={"llm_account_id": "account-B"}))
    review_joint(current, tmp_path / "source.pdf", provider,
                 configured.model_copy(update={"llm_reasoning_effort": "max"}))
    current.reading.items[0].content = "Different source."
    review_joint(current, tmp_path / "source.pdf", provider, configured)
    assert len(provider.calls) == 4


def test_duplicate_ids_are_addressed_by_occurrence_without_changing_report_ids(tmp_path):
    current = edition([(1, "paragraph", "First."), (1, "formula", r"\sum_i x_i")])
    current.reading.items[1].id = "item-0"
    provider = Provider()
    audit = review_joint(current, tmp_path / "source.pdf", provider, settings(tmp_path), item_ids={"item-0"})
    assert audit["status"] == "completed" and audit["candidates"] == 2
    assert audit["coverage"]["math_selected_ids"] == ["item-0@1"]
    assert audit["coverage"]["text_candidate_ids"] == ["item-0@0"]
    assert [row["id"] for row in provider.calls[0][0]["items"]] == ["item-0@0", "item-0@1"]
    assert [item.id for item in current.reading.items] == ["item-0", "item-0"]


def test_disabled_scopes_and_invalid_ids_do_not_spend(tmp_path):
    current = edition([(1, "paragraph", "Source."), (1, "formula", r"\sum_i x_i")])
    provider = Provider()
    audit = review_joint(current, tmp_path / "source.pdf", provider,
                         settings(tmp_path, paperlens_math_enabled=False, paperlens_text_review_enabled=False))
    assert audit["status"] == "disabled" and not provider.calls
    text_only = plan_joint_review(current, settings(tmp_path, paperlens_math_enabled=False))
    assert text_only["candidate_ids"] == ["item-0"] and not text_only["math_selected_ids"]
    with pytest.raises(ValueError, match="不存在"):
        review_joint(current, tmp_path / "source.pdf", provider, settings(tmp_path), item_ids={"unknown"})
