"""Matched quotations must retain their local experimental qualifications."""

import pytest

from paperlens.analysis.evidence import EvidenceRegistry, normalize_quote, quote_span
from paperlens.models import Block, Box, Page, PaperDocument


def registry_for(text, raw_text=None, kind="text"):
    return EvidenceRegistry(
        PaperDocument(
            title="Context regression",
            source_name="test.pdf",
            sha256="fixture",
            parser="fixture",
            pages=[Page(number=1, width=600, height=800)],
            blocks=[
                Block(
                    id="b1",
                    page=1,
                    order=0,
                    text=text,
                    raw_text=text if raw_text is None else raw_text,
                    kind=kind,
                    bbox=Box(x0=0, y0=0, x1=1, y1=1),
                )
            ],
        )
    )


@pytest.mark.parametrize(
    "source,quote",
    [
        ("classification\naccuracy reached 93.2%", "classification accuracy reached 93.2%"),
        ("classifi-\ncation accuracy reached 93.2%", "classification accuracy reached 93.2%"),
        ("CLASSIFICATION accuracy reached ９３.２％", "classification accuracy reached 93.2%"),
        (
            "The ﬁnal accuracy on Cafe\u0301 reached 93.2%",
            "The final accuracy on Café reached 93.2%",
        ),
        (
            "Straße classification accuracy reached 93.2%",
            "STRASSE classification accuracy reached 93.2%",
        ),
    ],
)
def test_normalized_match_keeps_local_conditions_in_long_blocks(source, quote):
    text = "Unrelated background. " * 350 + " Held-out test split only. " + source + " End. " * 350
    registry = registry_for(text)
    evidence_id = registry.resolve("b1", quote, {"b1"})
    assert evidence_id
    assert registry.items[evidence_id].match == "normalized"
    context = registry.contexts([evidence_id])[0]["context"]
    assert "Held-out test split only." in context
    assert source in context
    assert len(context) <= 4000


def test_raw_only_match_uses_raw_local_context_instead_of_unrelated_text():
    raw = "Background. " * 700 + "Test split only. Accuracy reached 93.2%." + " More. " * 500
    registry = registry_for("Different reconstructed content. " * 400, raw)
    evidence_id = registry.resolve("b1", "Accuracy reached 93.2%.", {"b1"})
    context = registry.contexts([evidence_id])[0]["context"]
    assert "Test split only." in context
    assert "Accuracy reached 93.2%." in context
    assert len(context) <= 4000


def test_reconstructed_text_match_uses_its_own_offset():
    quote = "Accuracy reached 93.2%."
    text = "Reordered background. " * 500 + "Validation split only. " + quote
    registry = registry_for(text, "Short raw input. " + quote)
    evidence_id = registry.resolve("b1", quote, {"b1"})
    assert "Validation split only." in registry.contexts([evidence_id])[0]["context"]


def test_whitespace_only_quote_is_never_a_normalized_match():
    assert normalize_quote(" \n\t ") == ""
    registry = registry_for("Background " * 500)
    assert registry.resolve("b1", "\u2002" * 10, {"b1"}) is None


def test_excessive_layout_whitespace_keeps_whole_quote_and_local_condition():
    source = "Classification" + " \n" * 5000 + "accuracy reached 93.2%"
    registry = registry_for("Background. " * 500 + "Test split only. " + source + " on MNIST.")
    eid = registry.resolve("b1", "Classification accuracy reached 93.2%", {"b1"})
    context = registry.contexts([eid])[0]["context"]
    assert "Classification accuracy reached 93.2%" in context
    assert "Test split only." in context and "on MNIST." in context
    assert len(context) <= 4000


def test_hangul_jamo_maps_individual_syllables_in_a_continuous_sequence():
    source = "\u1100\u1161" * 3000 + "\u1102\u1161\u1103\u1161" + "\u1100\u1161" * 3000
    assert quote_span(source, "나다") == (6000, 6004)


def test_zero_combining_class_unicode_composition_uses_the_same_quote_normalization():
    # Bengali vowel signs compose even though both have combining class zero.
    source = "Prefix " + "\u09c7\u09be" + " suffix"
    assert quote_span(source, "\u09cb suffix") == (7, len(source))
