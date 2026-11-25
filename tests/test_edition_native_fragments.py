"""Native glyph order and mixed columns must not duplicate a repaired edition."""

from test_edition_partial import source_text
from test_reading_projection import b, report

from paperlens.reading import prepare_reading
from paperlens.reading_edition import EditionBatch, EditionBlock, apply_edition


def apply(value, *rows):
    before = value.document.model_dump()
    prepare_reading(value)
    apply_edition(value, [EditionBatch(items=list(rows))])
    assert value.document.model_dump() == before


def test_reordered_author_and_affiliation_blocks_are_consumed_independently():
    value = report(
        [
            b("second", "Bob Two2"),
            b("first", "Alice One1", order=1),
            b("unit", "1Example University", order=2),
        ]
    )
    apply(
        value,
        EditionBlock(
            kind="information",
            page=1,
            category="authors",
            content="Alice One¹, Bob Two²; ¹Example University",
            source_ids=["second", "first", "unit"],
        ),
    )
    assert value.reading.items == []
    assert value.reading.structured_pages == [1]
    assert {p.block_id for p in value.reading.information_items["authors"][0].parts} == {
        "first",
        "second",
        "unit",
    }


def test_overlapping_out_of_order_glyph_fragments_have_one_continuous_paragraph():
    prefix = (
        "The evaluation covers several independent capabilities and checks the output "
        "on a broad collection of examples. We introduce a workflow for testing "
    )
    value = report(
        [
            b("body", prefix + "modheck ist", box=(0.1, 0.1, 0.45, 0.3)),
            b("small", "els. C", box=(0.1, 0.29, 0.2, 0.31), order=1),
            b("letter", "L", box=(0.25, 0.29, 0.26, 0.31), order=2),
            b("tail", "provides reliable test coverage.", box=(0.1, 0.29, 0.45, 0.4), order=3),
        ]
    )
    text = prefix + "models. CheckList provides reliable test coverage."
    apply(
        value,
        EditionBlock(
            kind="paragraph",
            page=1,
            content=text,
            source_ids=["body", "small", "letter", "tail"],
        ),
    )
    assert [i.content for i in value.reading.items] == [text]
    assert value.reading.structured_pages == [1]


def test_mixed_column_block_can_supply_two_rows_without_stealing_a_word_initial():
    value = report(
        [
            b("lead", "A procedure is a", box=(0.1, 0.1, 0.45, 0.2)),
            b("mixed", "collec- {OBJECT}.", box=(0.1, 0.2, 0.7, 0.23), order=1),
            b("tail", "tion of small examples.", box=(0.1, 0.23, 0.45, 0.3), order=2),
            b("template", "We use", box=(0.55, 0.18, 0.8, 0.2), order=3),
        ]
    )
    apply(
        value,
        EditionBlock(
            kind="paragraph",
            page=1,
            content="A procedure is a collection of small examples.",
            source_ids=["lead", "mixed", "tail"],
            box=(0.1, 0.1, 0.45, 0.3),
        ),
        EditionBlock(
            kind="paragraph",
            page=1,
            content="We use {OBJECT}.",
            source_ids=["template"],
            box=(0.55, 0.18, 0.8, 0.25),
        ),
    )
    assert len(value.reading.items) == 2
    assert all(item.content is not None for item in value.reading.items)
    assert value.reading.structured_pages == [1]


def test_partial_matching_retains_negation_and_does_not_consume_a_repeated_occurrence():
    value = report(
        [
            b("first", "Repeated paragraph. The method is not valid under load."),
            b("second", "Repeated paragraph.", order=1),
            b("third", "Independent description.", order=2),
        ]
    )
    apply(
        value,
        EditionBlock(
            kind="paragraph",
            page=1,
            content="Independent description. Repeated paragraph. The method is valid under load.",
            source_ids=["first", "second", "third"],
        ),
    )
    fallback = "".join(
        source_text(value, item) for item in value.reading.items if item.content is None
    )
    assert "Repeated paragraph." in fallback
    assert "not" in fallback
    assert value.reading.structured_pages == []


def test_latex_text_wrappers_and_arrow_do_not_create_source_fallbacks():
    value = report([b("source", "Choose {ACTION} = {move, stop}, then A → B.")])
    apply(
        value,
        EditionBlock(
            kind="paragraph",
            page=1,
            source_ids=["source"],
            content=r"Choose $\{\text{ACTION}\} = \{\text{move}, \text{stop}\}$, then A $\rightarrow$ B.",
        ),
    )
    assert len(value.reading.items) == 1
    assert value.reading.structured_pages == [1]


def test_a_word_anagram_is_not_assumed_to_be_a_cross_block_glyph_displacement():
    text = "A long literal context establishes the paragraph and its original wording. " * 3
    value = report(
        [
            b("first", text + "The form is explicit."),
            b("second", "Independent final sentence.", order=1),
        ]
    )
    apply(
        value,
        EditionBlock(
            kind="paragraph",
            page=1,
            source_ids=["first", "second"],
            content=text + "The from is explicit. Independent final sentence.",
        ),
    )
    fallback = "".join(
        source_text(value, item) for item in value.reading.items if item.content is None
    )
    assert "form" in fallback
    assert value.reading.structured_pages == []


def test_native_formula_glyphs_are_owned_by_returned_inline_tex():
    """Attention p.8/p.5: spaced decimals, Greek, ∈/×/√ and script order."""
    rows = [
        (
            "We used beam search with a beam size of 4 and length penalty α = 0 . 6 [38]. "
            "These hyperparameters were chosen after experimentation.",
            r"We used beam search with a beam size of 4 and length penalty $\alpha = 0.6$ [38]. "
            "These hyperparameters were chosen after experimentation.",
        ),
        (
            "Where the projections are parameter matrices W Q i ∈ R d model × d k , "
            "W i K ∈ R d model × d k , W V i ∈ R d model × d v and W O ∈ R hd v × d model .",
            "Where the projections are parameter matrices "
            r"$W_i^Q \in \mathbb{R}^{d_{\text{model}}\times d_k}$, "
            r"$W_i^K \in \mathbb{R}^{d_{\text{model}}\times d_k}$, "
            r"$W_i^V \in \mathbb{R}^{d_{\text{model}}\times d_v}$ and "
            r"$W^O \in \mathbb{R}^{h d_v \times d_{\text{model}}}$.",
        ),
        (
            "In addition, we scale the dot products by 1 √ d k .",
            r"In addition, we scale the dot products by $\frac{1}{\sqrt{d_k}}$.",
        ),
    ]
    for source, content in rows:
        value = report([b("body", source)])
        apply(value, EditionBlock(kind="paragraph", page=1, content=content, source_ids=["body"]))
        assert [i.content for i in value.reading.items] == [content]


def test_inline_tex_cannot_excuse_an_omitted_sentence():
    source = "The penalty was α = 0 . 6 in all runs. A second sentence was dropped entirely."
    value = report([b("body", source)])
    apply(
        value,
        EditionBlock(
            kind="paragraph",
            page=1,
            content=r"The penalty was $\alpha = 0.6$ in all runs.",
            source_ids=["body"],
        ),
    )
    assert [i.content is None for i in value.reading.items] == [False, True]


def test_spaced_text_layer_dashes_match_typeset_dashes():
    source = "They are not fair - just less unfair. We presented C heck - L ist to the team."
    value = report([b("body", source)])
    apply(
        value,
        EditionBlock(
            kind="paragraph",
            page=1,
            content="They are not fair – just less unfair. We presented CheckList to the team.",
            source_ids=["body"],
        ),
    )
    assert [i.content is not None for i in value.reading.items] == [True]


def test_numeric_minus_remains_significant():
    value = report([b("body", "The gain was 5 - 3 points.")])
    apply(
        value,
        EditionBlock(kind="paragraph", page=1, content="The gain was 5 3 points.", source_ids=["body"]),
    )
    assert any(i.content is None for i in value.reading.items)
