"""Local review stays inside candidate, image and shared request budgets."""

import hashlib
import json

import pytest
from test_reading_projection import b, report

from paperlens.config import Settings
from paperlens.control import processing_concurrency
from paperlens.edition_math_review import review_math, validate_patches
from paperlens.errors import ProviderError, ProviderOutcomeUnknown
from paperlens.models import Box, ReadingDocument, ReadingItem, ReadingPart, Usage
from paperlens.provider import RequestBudget


def test_review_normalizes_formula_wrappers_but_not_prose_delimiters():
    r = edition([("formula", "x^2"), ("paragraph", "正文 $x$。")])
    raw = {
        "patches": [
            {"id": r.reading.items[0].id, "content": "```latex\n$$x^3$$\n```"},
            {"id": r.reading.items[1].id, "content": "正文 $x_i$。"},
        ]
    }
    batch = validate_patches(raw, r.reading.items)
    assert [p.content for p in batch.patches] == ["x^3", "正文 $x_i$。"]


def edition(contents):
    blocks = [
        b(f"source-{i}", "该范数用于分组并保留原文，ℓ 2,1。", kind="formula")
        for i, _ in enumerate(contents)
    ]
    r = report(blocks)
    r.reading = ReadingDocument(
        version="3",
        source_version="source",
        structured_pages=[1],
        items=[
            ReadingItem(
                id=f"item-{i}",
                kind=kind,
                content=content,
                page=1,
                bbox=blocks[i].bbox,
                parts=[ReadingPart(block_id=blocks[i].id, start=0, end=len(blocks[i].text))],
            )
            for i, (kind, content) in enumerate(contents)
        ],
    )
    return r


def settings(tmp_path, **kw):
    return Settings(
        _env_file=None, paperlens_cache_dir=tmp_path, paperlens_vision_allow_unverified=True, **kw
    )


class Provider:
    def __init__(self, answer=None, budget=None):
        self.answer = answer or (lambda payload: {"patches": []})
        self._budget = budget or RequestBudget(120)
        self.usage = Usage()
        self.calls = []
        self.prompts = []

    def json(self, system, user, **kw):
        self._budget.take()
        self.usage.requests += 1
        payload = json.loads(user)
        self.calls.append((payload, kw))
        self.prompts.append((system, user))
        return self.answer(payload)


@pytest.fixture(autouse=True)
def mock_images(monkeypatch):
    monkeypatch.setattr(
        "paperlens.edition_math_review.page_image", lambda pdf, page, box: f"whole-page-{page}"
    )
    monkeypatch.setattr(
        "paperlens.edition_math_review.region_images",
        lambda pdf, report, item: (
            ["local-image"] * (1 if item.kind == "algorithm" and item.bbox else len(item.parts))
        ),
    )


def test_patch_changes_only_reading_content_and_reuses_success_cache(tmp_path):
    r = edition([("paragraph", "该范数用于分组并保留原文，$\\ell_2$。")])
    before = r.document.model_dump()
    original_item = r.reading.items[0].model_dump(exclude={"content", "content_spans"})
    corrected = "该范数用于分组并保留原文，$\\ell_{2,1}$。"
    provider = Provider(lambda payload: {"patches": [{"id": "item-0", "content": corrected}]})
    s = settings(tmp_path)
    audit = review_math(r, tmp_path / "source.pdf", provider, s)
    assert audit["patches"] == 1 and audit["requests"] == 1
    assert r.reading.items[0].content == corrected and r.reading.items[0].content_spans
    assert r.document.model_dump() == before
    assert r.reading.items[0].model_dump(exclude={"content", "content_spans"}) == original_item
    again = review_math(r, tmp_path / "source.pdf", provider, s)
    assert again["cache_hits"] == 1 and again["patches"] == again["requests"] == 0
    assert len(provider.calls) == 1


def test_native_math_cache_isolates_accounts_and_resumes_the_same_account(tmp_path):
    original = edition([("formula", r"\frac{a}{b}=c")])
    original.document.parser = "model-page-images"
    first, second, resumed = Provider(), Provider(), Provider()
    configured = settings(tmp_path, llm_account_id="account-A")
    audit_a = review_math(original.model_copy(deep=True), tmp_path / "source.pdf", first, configured)
    audit_b = review_math(
        original.model_copy(deep=True), tmp_path / "source.pdf", second,
        configured.model_copy(update={"llm_account_id": "account-B"}),
    )
    audit_resume = review_math(
        original.model_copy(deep=True), tmp_path / "source.pdf", resumed, configured,
    )
    assert audit_a["requests"] == audit_b["requests"] == 1
    assert audit_resume["requests"] == 0 and audit_resume["cache_hits"] == 1
    assert not resumed.calls


@pytest.mark.parametrize("error_class", [ValueError, OSError])
def test_invalid_or_local_failed_math_batch_has_a_retry_error_code(tmp_path, error_class):
    def fail(_payload):
        raise error_class("failed review")

    provider = Provider(fail)
    audit = review_math(
        edition([("formula", r"\frac{a}{b}=c")]), tmp_path / "source.pdf", provider,
        settings(tmp_path),
    )
    assert audit["status"] == "partial"
    assert audit["batches"][0]["error_code"] == ProviderError.code
    assert len(provider.calls) == 1  # No new automatic paid retry.


def test_math_input_budget_failure_is_explicit_and_never_calls_the_provider(tmp_path):
    provider = Provider()
    audit = review_math(
        edition([("formula", r"\sum_{i=1}" + "x" * 3000)]), tmp_path / "source.pdf",
        provider, settings(tmp_path, llm_max_input_chars=2000),
    )
    assert audit["status"] == "partial" and not provider.calls
    assert audit["batches"][0]["status"] == "unavailable"
    assert audit["batches"][0]["error_code"] == "input_budget_exceeded"
    assert audit["batches"][0]["input_budget"]["required_chars"] > 2000
    from paperlens.input_budget import temporary_input_budget

    with temporary_input_budget(audit["batches"][0]["input_budget"]["suggested_chars"]):
        recovered = review_math(
            edition([("formula", r"\sum_{i=1}" + "x" * 3000)]), tmp_path / "source.pdf",
            provider, settings(tmp_path, llm_max_input_chars=2000),
        )
    assert recovered["batches"][0]["status"] == "completed" and len(provider.calls) == 1
    cached = review_math(
        edition([("formula", r"\sum_{i=1}" + "x" * 3000)]), tmp_path / "source.pdf",
        provider, settings(tmp_path, llm_max_input_chars=2000),
    )
    assert cached["cache_hits"] == 1 and len(provider.calls) == 1


def test_unverified_math_capability_has_a_top_level_failure_code(tmp_path, monkeypatch):
    monkeypatch.setattr("paperlens.edition_math_review._vision_ready", lambda _settings: False)
    provider = Provider()
    audit = review_math(
        edition([("formula", r"\frac{a}{b}=c")]), tmp_path / "source.pdf",
        provider, settings(tmp_path),
    )
    assert audit["status"] == "unavailable" and audit["error_code"] == ProviderError.code
    assert not provider.calls


@pytest.mark.parametrize("parser", ["openai-pdf", "model-page-images", "docling"])
def test_math_source_text_is_only_sent_when_independently_extracted(tmp_path, parser):
    r = edition([("formula", r"\sum_{j=1}^k x_j")])
    r.document.parser = parser
    item, block = r.reading.items[0], r.document.blocks[0]
    block.text = item.content
    item.asset = "original-formula-crop"
    original = r.model_dump()
    provider = Provider()
    audit = review_math(r, tmp_path / "source.pdf", provider, settings(tmp_path))
    candidate = provider.calls[0][0]["items"][0]
    source = candidate["source_blocks"][0]
    assert ("text" in source) == (parser == "docling")
    assert source["id"] == block.id and source["page"] == block.page
    assert source["box"] == block.bbox.model_dump()
    assert candidate["content"] == item.content and candidate["image_indices"] == [1]
    expected_image = item.asset if parser == "docling" else "whole-page-1"
    assert provider.calls[0][1]["images"] == [expected_image]
    assert audit["reviewed"] == 1 and r.model_dump() == original
    again = review_math(r, tmp_path / "source.pdf", provider, settings(tmp_path))
    assert again["cache_hits"] == 1 and len(provider.calls) == 1


@pytest.mark.parametrize("parser", ["openai-pdf", "model-page-images"])
def test_native_math_uses_unique_whole_pages_without_trusting_or_changing_boxes(
    tmp_path, monkeypatch, parser
):
    current = edition(
        [("formula", r"\int_{-1/4}^{1/4} f(x)\,dx=1")] * 3
        + [("paragraph", r"The value is $\int_{-1/4}^{1/4} f(x)\,dx=1$.")]
    )
    current.document.parser = parser
    current.document.pages.append(current.document.pages[0].model_copy(update={"number": 2}))
    current.reading.structured_pages = [1, 2]
    current.document.blocks[2].page = current.reading.items[2].page = 2
    current.document.blocks[1].bbox = current.reading.items[1].bbox = Box(
        x0=0.4, y0=0.575, x1=0.6, y1=0.596
    )
    current.reading.items[3].parts.append(current.reading.items[2].parts[0].model_copy())
    for item in current.reading.items[:3]:
        item.asset = "unverified-neighbour-formula-crop"
    before = current.model_dump()
    rendered = []

    def whole_page(pdf, page, box):
        rendered.append((page, box.model_dump()))
        return f"whole-page-{page}"

    def no_estimated_crop(*args):
        pytest.fail("Native mathematical evidence must use whole original pages")

    monkeypatch.setattr("paperlens.edition_math_review.page_image", whole_page)
    monkeypatch.setattr("paperlens.edition_math_review.region_images", no_estimated_crop)
    provider = Provider()
    audit = review_math(current, tmp_path / "source.pdf", provider, settings(tmp_path))
    payload, options = provider.calls[0]
    assert audit["version"] == "1.2-full-pages" and audit["reviewed"] == 4
    assert audit["batches"][0]["images"] == 2
    assert rendered == [(page, {"x0": 0, "y0": 0, "x1": 1, "y1": 1}) for page in (1, 2)]
    assert options["images"] == ["whole-page-1", "whole-page-2"]
    assert payload["image_views"] == [
        {"image_index": 1, "page": 1, "view": "whole"},
        {"image_index": 2, "page": 2, "view": "whole"},
    ]
    rows = {row["id"]: row for row in payload["items"]}
    assert {ident: row["image_indices"] for ident, row in rows.items()} == {
        "item-0": [1], "item-1": [1], "item-2": [2], "item-3": [1, 2]
    }
    assert rows["item-1"]["source_blocks"][0]["box"] == before["document"]["blocks"][1]["bbox"]
    assert all("text" not in block for row in rows.values() for block in row["source_blocks"])
    assert "未经核实" in provider.prompts[0][0] and "不能因估计框" in provider.prompts[0][0]
    assert current.model_dump() == before
    again = review_math(current, tmp_path / "source.pdf", provider, settings(tmp_path))
    assert again["cache_hits"] == 1 and again["requests"] == 0
    assert len(provider.calls) == 1 and len(rendered) == 2


def test_docling_math_keeps_the_frozen_prompt_payload_and_crop_contract(tmp_path):
    current = edition([("formula", r"\sum_{j=1}^k x_j")])
    current.document.parser = "docling"
    current.reading.items[0].asset = "fitted-original-crop"
    provider = Provider()
    audit = review_math(current, tmp_path / "source.pdf", provider, settings(tmp_path))
    system, user = provider.prompts[0]
    assert hashlib.sha256(system.encode()).hexdigest() == (
        "33e647bdc5cf4358f9bc9d44624622234239d8ac862d2eb11e8a01156e1c4ed9"
    )
    assert user == json.dumps(
        {"items": [{
            "id": "item-0", "kind": "formula", "page": 1,
            "content": r"\sum_{j=1}^k x_j", "image_indices": [1],
            "source_blocks": [{
                "id": "source-0", "page": 1,
                "box": {"x0": 0.1, "y0": 0.1, "x1": 0.45, "y1": 0.2},
                "text": "该范数用于分组并保留原文，ℓ 2,1。",
            }],
        }]},
        ensure_ascii=False,
    )
    assert provider.calls[0][1]["images"] == ["fitted-original-crop"]
    assert audit["version"] == "1.1" and audit["reviewed"] == 1


@pytest.mark.parametrize("error_class", [ProviderError, ProviderOutcomeUnknown])
def test_unknown_math_result_continues_independent_batches_and_reuses_completed_checks(
    tmp_path, error_class
):
    current = edition([("formula", r"\sum_{j=1} x_j")] * 7)
    configured = settings(tmp_path)

    def answer(payload):
        if payload["items"][0]["id"] == "item-0" and len(provider.calls) == 1:
            raise error_class("one failed math batch")
        return {"patches": []}

    provider = Provider(answer)
    with processing_concurrency(1):
        audit = review_math(current, tmp_path / "source.pdf", provider, configured)
    continued = error_class is ProviderOutcomeUnknown
    assert audit["status"] == "partial" and audit["reviewed"] == (1 if continued else 0)
    assert audit["calls"] == (2 if continued else 1)
    assert audit["batches"][0]["status"] == "unavailable"
    assert audit["batches"][0]["error_code"] == error_class.code
    if continued:
        with processing_concurrency(1):
            resumed = review_math(current, tmp_path / "source.pdf", provider, configured)
        assert resumed["status"] == "completed" and resumed["reviewed"] == 7
        assert resumed["cache_hits"] == resumed["calls"] == resumed["requests"] == 1
        assert len(provider.calls) == 3


def test_math_review_cannot_reverse_a_prose_claim(tmp_path):
    r = edition([("paragraph", r"推广 $\ell_1$ 是非平凡的。")])
    provider = Provider(
        lambda payload: {
            "patches": [{"id": "item-0", "content": r"推广 $\ell_{2,1}$ 是非常平凡的。"}]
        }
    )
    audit = review_math(r, tmp_path / "source.pdf", provider, settings(tmp_path))
    assert audit["patches"] == 0
    assert r.reading.items[0].content == r"推广 $\ell_1$ 是非平凡的。"


def test_formula_review_uses_the_complete_fitted_original_image(tmp_path):
    r = edition([("formula", r"\frac{\partial y}{\partial x}=1")])
    r.reading.items[0].asset = "data:image/webp;base64,exact-full-equation"
    provider = Provider()
    audit = review_math(r, tmp_path / "source.pdf", provider, settings(tmp_path))
    assert audit["reviewed"] == 1
    assert provider.calls[0][1]["images"] == [r.reading.items[0].asset]


@pytest.mark.parametrize(
    "answer",
    [
        {"patches": [{"id": "not-candidate", "content": "changed"}]},
        {"patches": [{"id": "item-0", "content": "   "}]},
        {"patches": [{"id": "item-0", "content": "a"}, {"id": "item-0", "content": "b"}]},
        {"patches": [{"id": "item-0", "content": "a", "page": 2}]},
        {},
    ],
)
def test_invalid_patch_is_atomic_and_never_deletes_or_moves_items(tmp_path, answer):
    r = edition([("formula", r"\sum_{j=1}^k x_j")])
    before = r.model_dump()
    provider = Provider(lambda payload: answer)
    audit = review_math(r, tmp_path / "source.pdf", provider, settings(tmp_path))
    assert audit["status"] == "partial" and audit["patches"] == 0
    assert r.model_dump() == before
    assert not list(tmp_path.glob("edition-math-review/*/result.json"))


def test_limit_counts_images_not_items_and_single_variables_are_free(tmp_path):
    r = edition(
        [("formula", r"\sum_{j=1} x_j")] * 4
        + [("paragraph", "变量 $x_i$、$v_i$、$\\omega_i$ 与 $k$，文献$^{[3]}$。")]
    )
    for item in r.reading.items[:4]:
        item.parts.append(item.parts[0].model_copy())
    provider = Provider()
    audit = review_math(
        r,
        tmp_path / "source.pdf",
        provider,
        settings(tmp_path, paperlens_processing_profile="economy"),
    )
    assert audit["candidates"] == 4 and audit["reviewed"] == 3
    assert len(provider.calls) == 1 and len(provider.calls[0][1]["images"]) == 6
    assert all(row["id"] != "item-4" for row in provider.calls[0][0]["items"])


@pytest.mark.parametrize(
    "profile,regions,calls",
    [
        ("economy", 2, 1),
        ("standard", 4, 2),
        ("detailed", 8, 4),
        ("standard", 40, 2),
    ],
)
def test_profile_caps_paid_batches_even_with_larger_region_setting(
    tmp_path, profile, regions, calls
):
    r = edition([("formula", r"\sum_{j=1} x_j")] * 30)
    provider = Provider()
    audit = review_math(
        r,
        tmp_path / "source.pdf",
        provider,
        settings(
            tmp_path,
            paperlens_processing_profile=profile,
            paperlens_transcription_max_regions=regions,
        ),
    )
    assert audit["calls"] == calls == len(provider.calls)
    assert all(len(kwargs["images"]) <= 6 for _, kwargs in provider.calls)


def test_shared_provider_budget_includes_previous_requests(tmp_path):
    r = edition([("formula", r"\sum_{j=1} x_j")] * 7)
    budget = RequestBudget(2)
    budget.take()  # Earlier pipeline work already consumed a real attempt.
    provider = Provider(budget=budget)
    audit = review_math(r, tmp_path / "source.pdf", provider, settings(tmp_path))
    assert audit["requests"] == audit["calls"] == 1 and budget.used == 2
    assert audit["status"] == "partial" and audit["reviewed"] == 6
    assert audit["batches"][-1]["error_code"] == ProviderError.code


def test_disabled_math_review_and_legacy_edition_cannot_spend(tmp_path):
    r = edition([("formula", r"\sum x")])
    provider = Provider()
    audit = review_math(
        r,
        tmp_path / "source.pdf",
        provider,
        settings(tmp_path, paperlens_math_enabled=False),
    )
    assert audit["status"] == "disabled" and not provider.calls
    r.reading.version = "2"
    audit = review_math(r, tmp_path / "source.pdf", provider, settings(tmp_path))
    assert audit["status"] == "disabled" and not provider.calls
