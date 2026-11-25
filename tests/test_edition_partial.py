"""Failed or unprocessed reference pages retain their original source fragments."""


import pytest
from test_reading_projection import b, report

from paperlens.models import ReadingItem, ReadingPart
from paperlens.reading import prepare_reading, utf16
from paperlens.reading_edition import EditionBatch, EditionBlock, apply_edition


def paragraph(content, source="source", **kwargs):
    return EditionBlock(kind="paragraph", page=1, content=content, source_ids=[source], **kwargs)


def source_text(value, item):
    blocks = {block.id: block for block in value.document.blocks}
    return "".join(
        blocks[part.block_id]
        .text.encode("utf-16-le")[part.start * 2 : part.end * 2]
        .decode("utf-16-le")
        for part in item.parts
    )


def test_page_presence_does_not_discard_an_omitted_same_page_paragraph():
    value = report(
        [
            b("first", "First paragraph.", box=(0.1, 0.1, 0.45, 0.2)),
            b("second", "Second paragraph.", order=1, box=(0.1, 0.3, 0.45, 0.4)),
        ]
    )
    prepare_reading(value)
    original = value.document.model_dump()
    apply_edition(
        value,
        [
            EditionBatch(
                items=[
                    EditionBlock(
                        kind="paragraph", page=1, content="First paragraph.", source_ids=["first"]
                    )
                ]
            )
        ],
    )
    assert {part.block_id for item in value.reading.items for part in item.parts} == {
        "first",
        "second",
    }
    assert value.reading.structured_pages == []
    assert value.reading.warnings
    assert value.document.model_dump() == original


@pytest.mark.parametrize("content", ["First paragraph.", "First $paragraph$."])
def test_partial_block_preserves_only_the_omitted_passage_and_successful_retry(content):
    value = report([b("source", "First paragraph.\n\nSecond paragraph.")])
    prepare_reading(value)
    original = value.document.model_dump()
    partial = EditionBatch(items=[paragraph(content)])
    apply_edition(value, [partial])
    assert len(value.reading.items) == 2
    rebuilt, fallback = value.reading.items
    assert rebuilt.content == content and fallback.content is None
    assert "Second paragraph." in source_text(value, fallback)
    assert "First paragraph" not in source_text(value, fallback)
    assert value.reading.structured_pages == []
    before = value.reading.model_dump()
    apply_edition(value, [partial])
    assert value.reading.model_dump() == before
    apply_edition(
        value, [EditionBatch(items=[paragraph("First paragraph."), paragraph("Second paragraph.")])]
    )
    assert [item.content for item in value.reading.items] == [
        "First paragraph.",
        "Second paragraph.",
    ]
    assert value.reading.structured_pages == [1]
    assert value.reading.warnings == []
    assert value.document.model_dump() == original


def test_multiple_returned_fragments_do_not_hide_an_omission_inside_one_block():
    value = report([b("source", "First paragraph.\n\nMiddle omitted.\n\nLast paragraph.")])
    prepare_reading(value)
    apply_edition(
        value, [EditionBatch(items=[paragraph("First paragraph."), paragraph("Last paragraph.")])]
    )
    first, fallback, last = value.reading.items
    assert first.content == "First paragraph." and last.content == "Last paragraph."
    assert "Middle omitted." in source_text(value, fallback)
    assert "First" not in source_text(value, fallback) and "Last" not in source_text(
        value, fallback
    )
    assert value.reading.structured_pages == []


def test_unreadable_overlapping_rows_do_not_duplicate_readable_source():
    value = report([b("source", "A complete paragraph.")])
    prepare_reading(value)
    apply_edition(
        value,
        [
            EditionBatch(
                items=[
                    paragraph("", box=(0.1, 0.1, 0.45, 0.2)),
                    paragraph("A complete paragraph."),
                    paragraph("", box=(0.1, 0.1, 0.45, 0.2)),
                ]
            )
        ],
    )
    assert len(value.reading.items) == 1
    assert value.reading.structured_pages == [1]
    assert not value.reading.warnings


def test_omitted_utf16_fragment_retains_emoji_and_source_offsets():
    value = report([b("source", "Visible 😀 text.\n\nOmitted 🧪 text.")])
    prepare_reading(value)
    apply_edition(value, [EditionBatch(items=[paragraph("Visible 😀 text.")])])
    fallback = next(item for item in value.reading.items if item.content is None)
    assert source_text(value, fallback) == "\n\nOmitted 🧪 text."
    assert fallback.parts[0].start == utf16("Visible 😀 text.")
    assert value.reading.structured_pages == []


def test_omitted_information_stays_available_until_successful_retry():
    value = report([b("contact", "E-mail: a@example.org"), b("body", "Body.", order=1)])
    prepare_reading(value)
    assert value.reading.information_parts["authors"]
    apply_edition(value, [EditionBatch(items=[paragraph("Body.", "body")])])
    assert value.reading.information_parts["authors"][0].block_id == "contact"
    assert value.reading.structured_pages == []
    apply_edition(
        value,
        [
            EditionBatch(
                items=[
                    paragraph("Body.", "body"),
                    EditionBlock(
                        kind="information",
                        page=1,
                        content="E-mail: a@example.org",
                        category="authors",
                        source_ids=["contact"],
                    ),
                ]
            )
        ],
    )
    assert value.reading.information_parts["authors"] == []
    assert len(value.reading.information_items["authors"]) == 1
    assert value.reading.structured_pages == [1]


def test_one_returned_occurrence_does_not_consume_two_identical_source_blocks():
    value = report([b("first", "Repeated paragraph."), b("second", "Repeated paragraph.", order=1)])
    prepare_reading(value)
    apply_edition(
        value,
        [
            EditionBatch(
                items=[
                    EditionBlock(
                        kind="paragraph",
                        page=1,
                        content="Repeated paragraph.",
                        source_ids=["first", "second"],
                    )
                ]
            )
        ],
    )
    assert len(value.reading.items) == 2
    assert [p.block_id for p in value.reading.items[0].parts] == ["first"]
    assert source_text(value, value.reading.items[1]) == "Repeated paragraph."
    assert value.reading.structured_pages == []


def test_one_row_can_cover_excerpts_from_multiple_blocks_without_losing_the_gap():
    value = report(
        [
            b("first", "First paragraph.\n\nOmitted paragraph."),
            b("last", "Last paragraph.", order=1),
        ]
    )
    prepare_reading(value)
    apply_edition(
        value,
        [
            EditionBatch(
                items=[
                    EditionBlock(
                        kind="paragraph",
                        page=1,
                        content="First paragraph. Last paragraph.",
                        source_ids=["first", "last"],
                    )
                ]
            )
        ],
    )
    assert len(value.reading.items) == 2
    assert [p.block_id for p in value.reading.items[0].parts] == ["first", "last"]
    assert "Omitted paragraph." in source_text(value, value.reading.items[1])
    assert "First" not in source_text(value, value.reading.items[1])
    assert value.reading.structured_pages == []


@pytest.mark.parametrize(
    "kind,source,content",
    [
        ("formula", "x = y", "x=y"),
        ("algorithm", "Step one.", "Step one."),
    ],
)
def test_broad_object_anchor_does_not_consume_neighboring_prose_in_a_mixed_block(
    kind, source, content
):
    value = report([b("source", source + "\n\nOmitted paragraph.")])
    prepare_reading(value)
    before = value.document.model_dump()
    apply_edition(
        value,
        [
            EditionBatch(
                items=[EditionBlock(kind=kind, page=1, content=content, source_ids=["source"])]
            )
        ],
    )
    fallback = next(item for item in value.reading.items if item.content is None)
    assert "Omitted paragraph." in source_text(value, fallback)
    assert source not in source_text(value, fallback)
    assert value.reading.structured_pages == []
    assert value.document.model_dump() == before


def test_original_object_ownership_keeps_shared_block_paragraph_slices():
    value = report([b("source", "Native math.\n\nBody paragraph.")])
    prepare_reading(value)
    split = utf16("Native math.")
    value.reading.items = [
        ReadingItem(
            id="formula",
            kind="formula",
            page=1,
            bbox=value.document.blocks[0].bbox,
            parts=[ReadingPart(block_id="source", start=0, end=split)],
        ),
        ReadingItem(
            id="body",
            kind="paragraph",
            page=1,
            parts=[
                ReadingPart(
                    block_id="source", start=split, end=utf16(value.document.blocks[0].text)
                )
            ],
        ),
    ]
    apply_edition(
        value,
        [
            EditionBatch(
                items=[
                    EditionBlock(
                        kind="formula",
                        page=1,
                        content=r"\frac{x}{y}",
                        source_ids=["source"],
                        box=(0.1, 0.1, 0.45, 0.2),
                    )
                ]
            )
        ],
    )
    assert len(value.reading.items) == 2
    assert source_text(value, value.reading.items[0]) == "Native math."
    assert source_text(value, value.reading.items[1]) == "\n\nBody paragraph."
    assert value.reading.structured_pages == []


@pytest.mark.parametrize(
    "source,content,missing",
    [
        ("Coefficient -1.5.", "Coefficient 1.5.", "-"),
        ("Coefficient 1.5.", "Coefficient 15.", "."),
        ("Compute x + y.", "Compute x y.", "+"),
    ],
)
def test_omitted_math_signs_and_decimal_points_are_not_treated_as_formatting(
    source, content, missing
):
    value = report([b("source", source)])
    prepare_reading(value)
    apply_edition(value, [EditionBatch(items=[paragraph(content)])])
    fallback = next(item for item in value.reading.items if item.content is None)
    assert missing in source_text(value, fallback)
    assert value.reading.structured_pages == []




def reference_report():
    value = report(
        [
            b("heading", "References", kind="heading"),
            b("start", "[4] AUTHOR. An inter-", order=1),
            b("tail", "national paper.", page=2, order=2),
            b("next", "[5] Another paper.", page=2, order=3),
        ]
    )
    prepare_reading(value)
    return value


def test_partial_reference_replacement_keeps_unprocessed_cross_page_tail():
    value = reference_report()
    original = value.document.model_dump()
    old_reference = value.reading.references[0].model_dump()
    apply_edition(
        value,
        [
            EditionBatch(
                items=[
                    EditionBlock(
                        kind="reference",
                        page=1,
                        content="AUTHOR. An inter-",
                        reference_number="4",
                        source_ids=["start"],
                    )
                ]
            )
        ],
    )
    references = value.reading.references
    assert [part.block_id for item in references for part in item.parts] == [
        "start",
        "tail",
        "next",
    ]
    tail = next(item for item in references if item.parts[0].block_id == "tail")
    assert tail.page == 2 and tail.source_pages == [2]
    assert tail.content is None and tail.transcription is None
    assert tail.parts[0].model_dump() == old_reference["parts"][1]
    assert value.reading.items == []
    assert value.document.model_dump() == original


def test_unreadable_reference_fallback_stays_in_bibliography_without_duplicate_tail():
    value = reference_report()
    original = value.document.model_dump()
    before = [item.model_dump() for item in value.reading.references]
    apply_edition(
        value,
        [
            EditionBatch(
                items=[
                    EditionBlock(
                        kind="reference",
                        page=1,
                        content="",
                        source_ids=["start"],
                        box=(0.1, 0.1, 0.45, 0.2),
                    )
                ]
            )
        ],
    )
    assert value.reading.items == []
    assert [item.model_dump() for item in value.reading.references] == before
    assert [part.block_id for item in value.reading.references for part in item.parts] == [
        "start",
        "tail",
        "next",
    ]
    assert value.document.model_dump() == original
