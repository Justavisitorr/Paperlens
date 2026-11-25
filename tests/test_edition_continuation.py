"""Cross-page text joining is grounded in original parts, without model calls."""

import pytest

from paperlens.edition_continuation import (
    join_continuation,
    normalize_reference,
    repair_word_breaks,
)
from paperlens.models import Block, Box, ReadingItem, ReadingPart


def fragments(source, left, right, *, kind="reference", number="4"):
    block = Block(
        id="source",
        page=6,
        order=0,
        text=source,
        raw_text=source,
        bbox=Box(x0=0, y0=0, x1=1, y1=1),
    )
    previous = ReadingItem(
        id="left",
        kind=kind,
        page=6,
        content=left,
        reference_number=number,
        parts=[ReadingPart(block_id="source", end=len(source.encode("utf-16-le")) // 2)],
    )
    current = ReadingItem(
        id="right",
        kind=kind,
        page=7,
        content=right,
        reference_number=number,
        continuation=True,
    )
    return previous, current, {block.id: block}


@pytest.mark.parametrize("policy", ["auto", "none", "dehyphenate"])
def test_model_completed_word_is_restored_before_cross_page_join(policy):
    previous, current, blocks = fragments(
        "RENDLE S. Proceedings of the fifth ACM internation[4]",
        "[4] RENDLE S. Proceedings of the fifth ACM international-",
        "al conference on Web search and data mining.",
    )
    before = previous.model_dump(), current.model_dump(), blocks["source"].model_dump()
    assert join_continuation(previous, current, blocks, join=policy) == (
        "RENDLE S. Proceedings of the fifth ACM international conference on Web search and data mining."
    )
    assert before == (previous.model_dump(), current.model_dump(), blocks["source"].model_dump())


def test_generic_completion_uses_source_hyphen_even_when_model_omits_it():
    previous, current, blocks = fragments(
        "Study of the electroencephalo-",
        "Study of the electroencephalography",
        "graphy signals.",
        kind="paragraph",
        number="",
    )
    assert join_continuation(previous, current, blocks) == (
        "Study of the electroencephalography signals."
    )


@pytest.mark.parametrize(
    "source,left,right,expected",
    [
        (
            "the fifth ACM international-",
            "the fifth ACM international-",
            "al conference",
            "the fifth ACM internationalal conference",
        ),
        (
            "the fifth ACM internation",
            "the fifth ACM international",
            "al conference",
            "the fifth ACM international al conference",
        ),
        (
            "a different study internation[4]",
            "the fifth ACM international-",
            "al conference",
            "the fifth ACM internationalal conference",
        ),
        (
            "the fifth ACM internation[5]",
            "the fifth ACM international-",
            "al conference",
            "the fifth ACM internationalal conference",
        ),
        (
            "the fifth ACM internation[4]",
            "the fifth ACM international-",
            "conference",
            "the fifth ACM internationalconference",
        ),
    ],
)
def test_no_word_correction_without_matching_source_context_and_next_fragment(
    source, left, right, expected
):
    previous, current, blocks = fragments(source, left, right)
    assert join_continuation(previous, current, blocks) == expected


@pytest.mark.parametrize(
    "policy,left,right,expected",
    [
        ("space", "model-", "based", "model- based"),
        ("none", "model-", "based", "model-based"),
        ("dehyphenate", "recom-", "mender", "recommender"),
        ("auto", "recom-", "mender", "recommender"),
        ("auto", "sentence", "continues", "sentence continues"),
        ("auto", "跨页", "续接", "跨页续接"),
    ],
)
def test_explicit_join_policies_and_default_boundaries(policy, left, right, expected):
    previous, current, blocks = fragments(left, left, right, kind="paragraph", number="")
    assert join_continuation(previous, current, blocks, join=policy) == expected


@pytest.mark.parametrize(
    "text,number,expected",
    [
        ("[4] Author. Title.", "4", "Author. Title."),
        (" [4] [4] Author.", "4", "[4] Author."),
        ("[5] Author.", "4", "[5] Author."),
        ("Author mentions [4].", "4", "Author mentions [4]."),
        ("[4] Author.", "", "[4] Author."),
    ],
)
def test_reference_normalization_only_strips_one_matching_leading_number(text, number, expected):
    assert normalize_reference(text, number) == expected


def test_reference_marker_on_continuation_is_normalized_using_previous_number():
    previous, current, blocks = fragments("Author. Title", "[4] Author. Title", "[4] continued.")
    current.reference_number = ""
    assert join_continuation(previous, current, blocks, join="space") == "Author. Title continued."


def test_missing_source_keeps_original_semantic_join_behavior():
    previous, current, _ = fragments("ignored", "the ACM international-", "al conference")
    assert join_continuation(previous, current, {}) == "the ACM internationalal conference"


def test_explicit_line_breaks_are_repaired_from_literal_source_words():
    content = "internation-\nal conference; regres-\nsion; Statist-\nical Society; al-\nternating method."
    source = "international conference; regression; Statistical Society; alternating method."
    assert repair_word_breaks(content, source) == source


def test_source_compounds_and_ambiguous_spellings_keep_model_hyphens():
    content = "context-aware methods, model-based learning, co-operate."
    source = "context-aware methods; model-based learning; cooperate or co-operate."
    assert repair_word_breaks(content, source) == content


@pytest.mark.parametrize("word", ["long-horizon", "regularization-path", "exploration-policy"])
def test_flattened_source_words_cannot_erase_correct_model_compounds(word):
    assert repair_word_breaks(word, word.replace("-", "")) == word


def test_unmarked_hyphens_are_not_guessed_to_be_line_breaks():
    assert repair_word_breaks("regres-sion", "regression") == "regres-sion"
    assert repair_word_breaks("co-\noperate", "co-operate") == "co-\noperate"
    assert repair_word_breaks("123-\n456", "123456") == "123-\n456"


def test_missing_acronym_hyphen_is_restored_only_from_exact_source_acronym():
    content = "SGLFM and ABCD12 retain contextaware and recommender."
    source = "SGL-FM and ABC-D12 retain context-aware and recom-mender."
    assert repair_word_breaks(content, source) == (
        "SGL-FM and ABC-D12 retain contextaware and recommender."
    )


@pytest.mark.parametrize(
    "content,source",
    [
        ("regres-sion", "preregression"),
        ("regres-sion", "regres-sion"),
        ("regres-sion", "regressions"),
        ("regres-sion", ""),
        ("SGLFM", "SGL-FM or SGLFM"),
        ("sglfm", "SGL-FM"),
        ("123-456", "123456"),
    ],
)
def test_unconfirmed_words_acronyms_and_numeric_identifiers_are_untouched(content, source):
    assert repair_word_breaks(content, source) == content


def test_word_break_repair_is_idempotent_and_keeps_model_casing():
    source = "Statistical regression uses SGL-FM."
    content = "STATIST-\nICAL regres-\nsion uses SGLFM."
    repaired = repair_word_breaks(content, source)
    assert repaired == "STATISTICAL regression uses SGL-FM."
    assert repair_word_breaks(repaired, source) == repaired


def test_word_repairs_leave_formulas_urls_and_dois_literal():
    source = "abcd regression SGL-FM"
    content = "$abc-d$ $$abc-d$$ https://example.org/abc-d 10.1007/abc-d regres-\nsion SGLFM"
    assert repair_word_breaks(content, source) == (
        "$abc-d$ $$abc-d$$ https://example.org/abc-d 10.1007/abc-d regression SGL-FM"
    )


def test_native_extraction_missing_hyphen_does_not_erase_method_acronyms():
    assert repair_word_breaks("GL-FM and SEC-REG", "GLFM and SECREG") == "GL-FM and SEC-REG"


def test_cross_page_auto_join_preserves_a_compound_attested_elsewhere_in_source():
    previous, current, blocks = fragments(
        "The Dream-RSI method improves Dream-",
        "The Dream-RSI method improves Dream-",
        "RSI performance.",
        kind="paragraph",
        number="",
    )
    assert join_continuation(previous, current, blocks) == (
        "The Dream-RSI method improves Dream-RSI performance."
    )
    # Explicit model evidence for a word break still controls the join.
    assert join_continuation(previous, current, blocks, join="dehyphenate").endswith(
        "DreamRSI performance."
    )


def test_bare_numeric_powers_are_typeset_once_without_changing_signs():
    from paperlens.edition_continuation import wrap_numeric_powers

    text = r"范围 {10^{-6}, 10^{3}}，已有 $10^{-5}$，https://example.org/10^{2}"
    expected = r"范围 {$10^{-6}$, $10^{3}$}，已有 $10^{-5}$，https://example.org/10^{2}"
    assert wrap_numeric_powers(text) == expected
    assert wrap_numeric_powers(expected) == expected
