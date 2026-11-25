"""Body repairs preserve evidence and reuse only the exact reviewed input or output."""

import json

import pytest
from test_reading_projection import b, report

from paperlens.config import Settings
from paperlens.control import processing_concurrency
from paperlens.edition_text_review import review_text, validate_reviews
from paperlens.errors import ProviderError, ProviderOutcomeUnknown
from paperlens.estimate_timing import comparable_samples
from paperlens.estimate_workload import DocumentFeatures, Work, workload
from paperlens.models import ReadingDocument, ReadingItem, ReadingPart, ReadingTranslation, Usage
from paperlens.provider import RequestBudget


def edition(contents):
    blocks = [b(f"source-{i}", text, page=page) for i, (page, text) in enumerate(contents)]
    result = report(blocks)
    result.reading = ReadingDocument(
        source_version="source",
        items=[
            ReadingItem(
                id=f"item-{i}",
                kind="paragraph",
                page=block.page,
                parts=[ReadingPart(block_id=block.id, end=len(block.text))],
            )
            for i, block in enumerate(blocks)
        ],
    )
    return result


def settings(tmp_path, **changes):
    return Settings(
        _env_file=None,
        **{
            "paperlens_cache_dir": tmp_path,
            "llm_api_key": "test",
            "llm_max_retries": 0,
            "llm_base_url": "https://api.openai.com/v1",
            "llm_model": "gpt-6-astra",
            "llm_reasoning_effort": "high",
            "paperlens_vision_allow_unverified": True,
        }
        | changes,
    )


def correction(ident="item-0", text="The model works."):
    return {
        "id": ident,
        "status": "corrected",
        "content": text,
        "issue": "word_break",
        "reason": "原图单词 model 连续，修正错误空格。",
        "source_quote": "The model works.",
        "source_pages": [1],
    }


class Provider:
    def __init__(self, answer=None, budget=None):
        self.answer = answer or (
            lambda payload: {
                "reviews": [{"id": row["id"], "status": "ok"} for row in payload["items"]]
            }
        )
        self.usage = Usage()
        self._budget = budget
        self.calls = []

    def json(self, system, user, **options):
        if self._budget:
            self._budget.take()
        self.usage.requests += 1
        payload = json.loads(user)
        self.calls.append((payload, options))
        return self.answer(payload)


@pytest.fixture(autouse=True)
def images(monkeypatch):
    monkeypatch.setattr("paperlens.edition_text_review.page_image", lambda *args: "local-image")


def test_fallback_prose_is_repaired_without_changing_source_or_other_fields(tmp_path):
    current = edition([(1, "The mo del works.")])
    original_document = current.document.model_dump()
    original_item = current.reading.items[0].model_dump(exclude={"content", "content_spans"})
    current.translations["item-0"] = ReadingTranslation(text="旧译文", source="old")
    provider = Provider(lambda payload: {"reviews": [correction()]})
    audit = review_text(current, tmp_path / "source.pdf", provider, settings(tmp_path))
    assert audit["status"] == "completed" and audit["requests"] == 1
    assert audit["patches"][0]["before"] == "The mo del works."
    assert current.reading.items[0].content == "The model works."
    assert current.document.model_dump() == original_document
    assert (
        current.reading.items[0].model_dump(exclude={"content", "content_spans"}) == original_item
    )
    assert not current.translations
    assert len(provider.calls[0][1]["images"]) == 4
    again = review_text(current, tmp_path / "source.pdf", provider, settings(tmp_path))
    assert again["requests"] == 0 and again["cache_hits"] == 1 and not again["patches"]
    fresh = edition([(1, "The mo del works.")])
    restored = review_text(fresh, tmp_path / "source.pdf", provider, settings(tmp_path))
    assert restored["cache_hits"] == 1 and fresh.reading.items[0].content == "The model works."


def test_text_batch_and_completion_caches_isolate_accounts_and_resume(tmp_path):
    original = edition([(1, "The model works.")])
    original.document.parser = "model-page-images"
    configured = settings(tmp_path, llm_account_id="account-A")
    first, second, resumed = Provider(), Provider(), Provider()
    audit_a = review_text(original.model_copy(deep=True), tmp_path / "source.pdf", first, configured)
    audit_b = review_text(
        original.model_copy(deep=True), tmp_path / "source.pdf", second,
        configured.model_copy(update={"llm_account_id": "account-B"}),
    )
    audit_resume = review_text(
        original.model_copy(deep=True), tmp_path / "source.pdf", resumed, configured,
    )
    assert audit_a["requests"] == audit_b["requests"] == 1
    assert audit_resume["requests"] == 0 and audit_resume["cache_hits"] == 1
    assert not resumed.calls


@pytest.mark.parametrize("error_class", [ValueError, OSError])
def test_invalid_or_local_failed_text_batch_has_a_retry_error_code(tmp_path, error_class):
    def fail(_payload):
        raise error_class("failed review")

    provider = Provider(fail)
    audit = review_text(
        edition([(1, "The model works.")]), tmp_path / "source.pdf", provider,
        settings(tmp_path),
    )
    assert audit["status"] == "partial"
    assert audit["batches"][0]["error_code"] == ProviderError.code
    assert len(provider.calls) == 1  # No new automatic paid retry.


def test_unplannable_text_is_explicitly_retryable_without_calling_the_provider(tmp_path):
    provider = Provider()
    audit = review_text(
        edition([(1, "Readable source. " * 300)]), tmp_path / "source.pdf", provider,
        settings(tmp_path, llm_max_input_chars=2000),
    )
    assert audit["status"] == "partial" and audit["error_code"] == ProviderError.code
    assert audit["remaining_ids"] == ["item-0"] and not provider.calls


def test_missing_text_provider_has_a_top_level_failure_code(tmp_path):
    audit = review_text(
        edition([(1, "The model works.")]), tmp_path / "source.pdf", None,
        settings(tmp_path),
    )
    assert audit["status"] == "unavailable" and audit["error_code"] == ProviderError.code


@pytest.mark.parametrize(
    "invalid",
    [
        {"reviews": []},
        {"reviews": [correction("unknown")]},
        {"reviews": [correction(), correction()]},
        {"reviews": [correction() | {"content": ""}]},
        {"reviews": [correction() | {"source_pages": [2]}]},
        {"reviews": [correction() | {"source_quote": ""}]},
        {"reviews": [correction() | {"page": 2}]},
        {"reviews": [{"id": "item-0", "status": "ok", "content": "invented"}]},
    ],
)
def test_invalid_review_never_rewrites_body_or_creates_success_cache(tmp_path, invalid):
    current = edition([(1, "The mo del works.")])
    before = current.model_dump()
    audit = review_text(
        current, tmp_path / "source.pdf", Provider(lambda _: invalid), settings(tmp_path)
    )
    assert audit["status"] == "partial" and audit["reviewed"] == 0
    assert current.model_dump() == before
    assert not list(tmp_path.glob("edition-text-review/*/result.json"))


def test_uncertain_text_is_retained_and_is_not_reported_as_corrected(tmp_path):
    current = edition([(1, "Unreadable �")])
    provider = Provider(
        lambda _: {
            "reviews": [
                {
                    "id": "item-0",
                    "status": "unresolved",
                    "reason": "原页字形模糊。",
                    "issue": "unreadable",
                }
            ]
        }
    )
    audit = review_text(current, tmp_path / "source.pdf", provider, settings(tmp_path))
    assert audit["status"] == "partial" and audit["reviewed"] == 1 and not audit["patches"]
    assert current.reading.items[0].content is None and audit["unresolved"]


@pytest.mark.parametrize("duplicate_of", [["item-0"], "item-0"])
def test_confirmed_duplicate_fragment_is_removed_with_an_audit_and_reuses_cache(
    tmp_path, duplicate_of
):
    current = edition([(1, "The variable $x_i$ is defined here."), (1, "x i")])
    provider = Provider(
        lambda _: {
            "reviews": [
                {"id": "item-0", "status": "ok"},
                {
                    "id": "item-1",
                    "status": "removed",
                    "issue": "duplicate_text",
                    "reason": "原页中该变量只在前一段出现一次，残片是重复提取。",
                    "source_pages": [1],
                    "duplicate_of": duplicate_of,
                },
            ]
        }
    )
    original = current.document.model_dump()
    audit = review_text(current, tmp_path / "source.pdf", provider, settings(tmp_path))
    assert [item.id for item in current.reading.items] == ["item-0"]
    assert audit["removals"][0]["before"]["id"] == "item-1"
    assert current.document.model_dump() == original
    again = review_text(current, tmp_path / "source.pdf", provider, settings(tmp_path))
    assert again["requests"] == 0 and not again["removals"] and len(provider.calls) == 1


def test_icon_removal_with_omitted_issue_keeps_other_repairs_and_reuses_cache(tmp_path):
    contents = [(1, "The mo del works."), (1, "globe")]
    current = edition(contents)
    provider = Provider(
        lambda _: {
            "reviews": [
                correction(),
                {
                    "id": "item-1",
                    "status": "removed",
                    "reason": "原页此处是网站前的地球图标，不是正文单词 globe。",
                    "source_pages": [1],
                },
            ]
        }
    )
    original_document = current.document.model_dump()
    audit = review_text(current, tmp_path / "source.pdf", provider, settings(tmp_path))
    assert audit["status"] == "completed" and audit["reviewed"] == 2
    assert len(audit["patches"]) == len(audit["removals"]) == 1
    assert audit["removals"][0]["issue"] == "layout_mix"
    assert [item.content for item in current.reading.items] == ["The model works."]
    assert current.document.model_dump() == original_document
    fresh = edition(contents)
    restored = review_text(fresh, tmp_path / "source.pdf", provider, settings(tmp_path))
    assert restored["requests"] == 0 and restored["cache_hits"] == 1
    assert fresh.reading == current.reading and len(provider.calls) == 1


@pytest.mark.parametrize(
    ("content", "changes"),
    [
        ("A normal paragraph. " * 30, {}),
        ("globe", {"reason": ""}),
        ("globe", {"source_pages": []}),
        ("globe", {"source_pages": [2]}),
        ("globe", {"duplicate_of": "unknown"}),
        ("globe", {"duplicate_of": "item-0"}),
    ],
)
def test_compact_removal_still_requires_bounded_content_evidence_and_retained_ids(
    tmp_path, content, changes
):
    current = edition([(1, content)])
    before = current.model_dump()
    raw = {
        "reviews": [
            {
                "id": "item-0",
                "status": "removed",
                "reason": "原页此处是非正文残片。",
                "source_pages": [1],
            }
            | changes
        ]
    }
    audit = review_text(current, tmp_path / "source.pdf", Provider(lambda _: raw), settings(tmp_path))
    assert audit["status"] == "partial" and audit["reviewed"] == 0
    assert not audit["removals"] and current.model_dump() == before
    assert not list(tmp_path.glob("edition-text-review/*/result.json"))


def test_removal_cannot_delete_a_normal_paragraph_or_refer_to_itself(tmp_path):
    for content, duplicate in [("A normal paragraph. " * 30, "item-0"), ("x i", "item-0")]:
        current = edition([(1, content)])
        before = current.model_dump()
        provider = Provider(
            lambda _, duplicate=duplicate: {
                "reviews": [
                    {
                        "id": "item-0",
                        "status": "removed",
                        "issue": "duplicate_text",
                        "reason": "duplicate",
                        "source_pages": [1],
                        "duplicate_of": [duplicate],
                    }
                ]
            }
        )
        audit = review_text(current, tmp_path / "source.pdf", provider, settings(tmp_path))
        assert audit["status"] == "partial" and current.model_dump() == before


def test_new_prose_or_effort_invalidates_the_review_cache(tmp_path):
    current = edition([(1, "Clear source")])
    provider = Provider()
    review_text(current, tmp_path / "source.pdf", provider, settings(tmp_path))
    current.reading.items[0].content = "Different text"
    review_text(current, tmp_path / "source.pdf", provider, settings(tmp_path))
    review_text(
        current, tmp_path / "source.pdf", provider, settings(tmp_path, llm_reasoning_effort="max")
    )
    assert len(provider.calls) == 3


def test_older_repeated_item_ids_are_reviewed_and_corrected_by_occurrence(tmp_path):
    current = edition([(1, "The mo del works."), (1, "A second valid occurrence.")])
    current.reading.items[1].id = current.reading.items[0].id

    def answer(payload):
        first, second = payload["items"]
        assert first["id"] != second["id"] and first["source_regions"]
        return {"reviews": [correction(first["id"]), {"id": second["id"], "status": "ok"}]}

    audit = review_text(current, tmp_path / "source.pdf", Provider(answer), settings(tmp_path))
    assert audit["reviewed"] == audit["candidates"] == 2
    assert current.reading.items[0].content == "The model works."
    assert current.reading.items[1].content is None


def test_targeted_review_uses_other_items_as_context_without_editing_them(tmp_path):
    current = edition([(1, "The model works."), (1, "mo del")])
    provider = Provider(
        lambda payload: {"reviews": [{"id": payload["items"][0]["id"], "status": "ok"}]}
    )
    audit = review_text(
        current, tmp_path / "source.pdf", provider, settings(tmp_path), item_ids={"item-1"}
    )
    assert audit["reviewed"] == 1 and len(current.reading.items) == 2
    assert [row["id"] for row in provider.calls[0][0]["context"]] == ["item-0"]


def test_separately_displayed_ai_use_statement_is_context_and_is_never_rewritten(tmp_path):
    current = edition([(1, "AI Use Statement"), (1, "The authors used AI for language editing.")])
    heading, statement = current.reading.items
    heading.kind = "heading"
    current.reading.items = [heading]
    current.reading.information_items = {"declarations": [statement]}
    original = current.model_dump()
    provider = Provider()
    audit = review_text(current, tmp_path / "source.pdf", provider, settings(tmp_path))
    payload = provider.calls[0][0]
    assert [row["id"] for row in payload["items"]] == [heading.id]
    assert payload["context"] == [{
        "id": statement.id, "kind": "information", "category": "declarations",
        "pages": [1], "content": "The authors used AI for language editing.",
    }]
    assert audit["reviewed"] == 1 and not audit["unresolved"]
    assert current.model_dump() == original
    statement.content = "The authors did not use AI for language editing."
    review_text(current, tmp_path / "source.pdf", provider, settings(tmp_path))
    assert len(provider.calls) == 2  # Changed context cannot reuse an earlier review.


def test_compact_ok_review_is_compatible_with_legacy_full_empty_fields():
    rows = [{"id": "item-0", "pages": [1], "content": "Readable source."}]
    compact = {"id": "item-0", "status": "ok"}
    legacy = compact | {
        "content": "", "issue": "", "reason": "", "source_quote": "",
        "source_pages": [], "duplicate_of": [],
    }
    assert validate_reviews({"reviews": [compact]}, rows) == validate_reviews(
        {"reviews": [legacy]}, rows
    )


@pytest.mark.parametrize("error_class", [ProviderError, ProviderOutcomeUnknown])
def test_unknown_text_result_keeps_later_reviews_and_resumes_without_repeating_them(
    tmp_path, error_class
):
    current = edition([(page, "Readable source.") for page in range(1, 6)])
    configured = settings(tmp_path)

    def answer(payload):
        if payload["items"][0]["id"] == "item-0" and len(provider.calls) == 1:
            raise error_class("one failed prose batch")
        return {"reviews": [{"id": row["id"], "status": "ok"} for row in payload["items"]]}

    provider = Provider(answer)
    with processing_concurrency(1):
        audit = review_text(current, tmp_path / "source.pdf", provider, configured)
    continued = error_class is ProviderOutcomeUnknown
    assert audit["status"] == "partial" and audit["reviewed"] == (3 if continued else 0)
    assert audit["calls"] == (3 if continued else 1)
    assert audit["batches"][0]["status"] == "unavailable"
    assert audit["batches"][0]["error_code"] == error_class.code
    assert not list(tmp_path.glob("edition-text-review-completed/*/result.json"))
    if continued:
        with processing_concurrency(1):
            resumed = review_text(current, tmp_path / "source.pdf", provider, configured)
        assert resumed["status"] == "completed" and resumed["reviewed"] == 5
        assert resumed["cache_hits"] == 2 and resumed["calls"] == resumed["requests"] == 1
        assert len(provider.calls) == 4


def test_context_preserves_the_end_of_a_paragraph_continued_from_the_previous_page(tmp_path):
    body = "Readable first page. " * 70 + "Range $1.79\\times$–$2.43\\times$."
    current = edition([(1, body), (2, "-")])
    current.reading.items[0].source_pages = [1, 2]
    provider = Provider()
    review_text(current, tmp_path / "source.pdf", provider, settings(tmp_path), item_ids={"item-1"})
    context = provider.calls[0][0]["context"]
    assert context[0]["id"] == "item-0" and context[0]["content"] == body
    assert context[0]["pages"] == [1, 2]


def test_cross_batch_duplicate_removals_cannot_remove_each_others_retained_evidence(tmp_path):
    current = edition([(1, "a"), (2, "b"), (3, "c")])
    current.reading.items[2].source_pages = [2, 3]

    def answer(payload):
        return {
            "reviews": [
                {"id": row["id"], "status": "ok"}
                if row["id"] == "item-0"
                else {
                    "id": row["id"],
                    "status": "removed",
                    "issue": "duplicate_text",
                    "reason": "duplicate fragment",
                    "source_pages": row["pages"],
                    "duplicate_of": ["item-2" if row["id"] == "item-1" else "item-1"],
                }
                for row in payload["items"]
            ]
        }

    audit = review_text(current, tmp_path / "source.pdf", Provider(answer), settings(tmp_path))
    assert audit["status"] == "partial" and len(audit["unresolved"]) == 2
    assert not audit["removals"] and len(current.reading.items) == 3


def test_shared_budget_and_page_batching_leave_explicit_remaining_items(tmp_path):
    current = edition([(page, "Readable body") for page in range(1, 5)])
    budget = RequestBudget(1)
    provider = Provider(budget=budget)
    with processing_concurrency(1):
        audit = review_text(current, tmp_path / "source.pdf", provider, settings(tmp_path))
    assert audit["requests"] == 1 and audit["reviewed"] == 2 and len(audit["remaining_ids"]) == 2
    assert len(provider.calls[0][1]["images"]) == 8
    assert audit["batches"][-1]["error_code"] == ProviderError.code


def test_disabled_or_unverified_vision_does_not_call_the_model(tmp_path):
    current = edition([(1, "Readable body")])
    provider = Provider()
    audit = review_text(
        current,
        tmp_path / "source.pdf",
        provider,
        settings(tmp_path, paperlens_text_review_enabled=False),
    )
    assert audit["status"] == "disabled" and not provider.calls


@pytest.mark.parametrize("effort", ["medium", "high", "xhigh"])
def test_estimates_include_text_review_and_the_requested_openai_effort(tmp_path, effort):
    configured = settings(tmp_path, llm_reasoning_effort=effort)
    works, _, _ = workload(configured, 4, DocumentFeatures(), None, "typical", 2)
    assert any(work.stage == "text_review" and work.effort == effort for work in works)


def test_openai_timing_uses_matching_effort_samples(tmp_path):
    configured = settings(tmp_path)
    work = Work("text_review", "vision", configured, 1, 1000, 100, "high", 1)
    sample = {
        "endpoint": configured.llm_base_url,
        "model": configured.llm_model,
        "protocol": "openai",
        "effective_reasoning_effort": "high",
    }
    assert comparable_samples(work, [sample, sample | {"effective_reasoning_effort": "max"}]) == [
        sample
    ]
