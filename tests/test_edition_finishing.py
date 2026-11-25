"""Column joins and rare PDF acronym repairs require concrete source evidence."""

import pytest
from test_reading_projection import b, report

from paperlens.edition_continuation import repair_pdf_acronyms
from paperlens.models import ReadingItem, ReadingPart
from paperlens.reading import prepare_reading
from paperlens.reading_edition import EditionBatch, EditionBlock, apply_edition


def column_report(*, punctuation=",", barrier=None, right_box=None):
    blocks = [
        b("left", "可以看出" + punctuation, page=1, box=(0.093, 0.121, 0.486, 0.152), order=1),
        b(
            "right",
            "线性模型未考虑关系，效果较差。",
            page=1,
            box=right_box or (0.518, 0.102, 0.906, 0.152),
            order=3,
        ),
    ]
    if barrier:
        blocks.append(b("barrier", "New section", kind=barrier, order=2))
    value = report(blocks)
    prepare_reading(value)
    return value


def column_rows(*, punctuation=",", barrier=None):
    rows = [
        EditionBlock(
            kind="paragraph", page=1, content="可以看出" + punctuation, source_ids=["left"]
        )
    ]
    if barrier:
        rows.append(
            EditionBlock(kind=barrier, page=1, content="New section", source_ids=["barrier"])
        )
    rows.append(
        EditionBlock(
            kind="paragraph", page=1, content="线性模型未考虑关系，效果较差。", source_ids=["right"]
        )
    )
    return EditionBatch(items=rows)


def test_comma_ended_column_paragraphs_join_without_model_continuation_flag():
    value = column_report()
    original = value.document.model_dump()
    apply_edition(value, [column_rows()])
    assert [item.content for item in value.reading.items] == [
        "可以看出,线性模型未考虑关系，效果较差。"
    ]
    assert [part.block_id for part in value.reading.items[0].parts] == ["left", "right"]
    assert value.document.model_dump() == original


@pytest.mark.parametrize("barrier", ["heading", "formula", "algorithm"])
@pytest.mark.parametrize("model_includes_barrier", [True, False])
def test_column_join_cannot_cross_native_or_semantic_section_boundaries(
    barrier, model_includes_barrier
):
    value = column_report(barrier=barrier)
    apply_edition(value, [column_rows(barrier=barrier if model_includes_barrier else None)])
    paragraphs = [item for item in value.reading.items if item.kind == "paragraph"]
    assert len(paragraphs) == 2
    assert paragraphs[0].content == "可以看出,"


@pytest.mark.parametrize(
    "punctuation,right_box",
    [
        ("。", None),
        (",", (0.1, 0.18, 0.48, 0.22)),
        (",", (0.52, 0.3, 0.91, 0.4)),
    ],
)
def test_column_join_requires_unfinished_sentence_and_next_column_geometry(punctuation, right_box):
    value = column_report(punctuation=punctuation, right_box=right_box)
    apply_edition(value, [column_rows(punctuation=punctuation)])
    assert len([item for item in value.reading.items if item.kind == "paragraph"]) == 2


@pytest.mark.parametrize("marker", ["\ufffe", "\x02"])
def test_pdf_special_separator_restores_confirmed_short_acronym_only(marker):
    pdf_text = f"Model ABC{marker}D2 uses ABC-D2; dimen{marker}sionality remains ordinary."
    content = "Model ABCD2 uses ABC-D2; dimensionality remains ordinary."
    assert repair_pdf_acronyms(content, pdf_text) == (
        "Model ABC-D2 uses ABC-D2; dimensionality remains ordinary."
    )


@pytest.mark.parametrize(
    "pdf_text",
    [
        "ABC\ufffeD2 has no intact spelling.",
        "ABC-D2 and ABCD2 without a special separator.",
        "ABC\ufffeD2 and ABC-D2 and ABCD2 are distinct source spellings.",
        "ABC\ufffeD2 and ABC-D2 and AB-CD2 are ambiguous.",
        "abc\ufffed2 and abc-d2 are lowercase words.",
    ],
)
def test_missing_ambiguous_or_lowercase_pdf_evidence_never_repairs(pdf_text):
    assert repair_pdf_acronyms("ABCD2", pdf_text) == "ABCD2"


def test_pdf_acronym_repair_keeps_math_urls_dois_and_unrelated_words_literal():
    content = "SGLFM $SGLFM$ https://example.org/SGLFM 10.1007/SGLFM XSGLFM SGLFM2"
    source = "SGL\ufffeFM and SGL-FM"
    assert repair_pdf_acronyms(content, source) == (
        "SGL-FM $SGLFM$ https://example.org/SGLFM 10.1007/SGLFM XSGLFM SGLFM2"
    )


def test_pdf_regions_use_original_coordinates_and_do_not_modify_source(monkeypatch):
    from paperlens import edition_pdf_words

    value = report([b("source", "SGLFM and SGL-FM", box=(0.1, 0.2, 0.9, 0.4))])
    block = value.document.blocks[0]
    before = block.model_dump()
    item = ReadingItem(
        id="paragraph",
        kind="paragraph",
        content="SGLFM and SGL-FM",
        parts=[ReadingPart(block_id="source", end=len(block.text))],
    )
    calls = []

    class TextPage:
        def get_text_bounded(self, **kwargs):
            calls.append(kwargs)
            return "SGL\x02FM and SGL-FM"

        def close(self):
            pass

    class Page:
        def get_textpage(self):
            return TextPage()

        def get_size(self):
            return 600, 800

        def close(self):
            pass

    class Document:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def __getitem__(self, index):
            assert index == 0
            return Page()

    monkeypatch.setattr(edition_pdf_words.pdfium, "PdfDocument", lambda _: Document())
    assert edition_pdf_words.restore_pdf_acronyms([item], "unused.pdf", {block.id: block}) == 1
    assert item.content == "SGL-FM and SGL-FM"
    assert calls == [{"left": 60, "bottom": 480, "right": 540, "top": 640}]
    assert block.model_dump() == before
