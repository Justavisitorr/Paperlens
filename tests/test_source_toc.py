"""Source TOC removal is a local, source-preserving reading projection."""

import json

import pytest
from test_reading_projection import b, report

from paperlens.exporting import report_html
from paperlens.models import ReadingDocument, ReadingItem, ReadingPart, ReadingTranslation
from paperlens.reading import build_reading, utf16
from paperlens.translation import TITLE_ID, TranslationService, _digest, translatable


def text_of(reading, document):
    blocks = {block.id: block for block in document.blocks}
    return "\n".join(
        item.content
        if item.content is not None
        else "".join(
            part.join
            + blocks[part.block_id]
            .text.encode("utf-16-le")[part.start * 2 : part.end * 2]
            .decode("utf-16-le")
            for part in item.parts
        )
        for item in reading.items
    )


@pytest.mark.parametrize("title", ["Contents", "目 录"])
def test_pure_source_toc_is_not_body_or_paper_information(title):
    r = report(
        [
            b("toc", title, kind="heading"),
            b("row1", "1.1 The relation problem . . . . . 3", kind="heading", order=1),
            b("row2", "1.2 The counterexample ........ 5", kind="heading", order=2),
        ]
    )
    original = r.document.model_dump()
    r.reading = build_reading(r)
    assert r.reading.items == []
    assert not any(r.reading.information.values())
    assert set(translatable(r)) == {TITLE_ID}
    assert r.document.model_dump() == original


def test_mixed_block_and_page_keep_body_and_true_heading_with_exact_offsets():
    source = "Contents\n1.1 The relation problem ...... 3\n1.2 The counterexample ...... 5\n😀 The actual proof stays here."
    r = report(
        [
            b("mixed", source),
            b("heading", "1.1 The relation problem", kind="heading", order=1),
            b("body", "We now prove the result.", order=2),
        ]
    )
    original = r.document.model_dump()
    r.reading = build_reading(r)
    visible = text_of(r.reading, r.document)
    assert "counterexample" not in visible and "Contents" not in visible
    assert "😀 The actual proof stays here." in visible and "We now prove the result." in visible
    assert [(i.kind, i.label) for i in r.reading.items if i.kind == "heading"] == [
        ("heading", "1.1 The relation problem")
    ]
    part = next(p for i in r.reading.items for p in i.parts if p.block_id == "mixed")
    assert part.start == utf16(source[: source.index("😀")]) and part.end == utf16(source)
    assert r.document.model_dump() == original


@pytest.mark.parametrize(
    "source",
    [
        "1.1 The relation problem\nWe prove a relation between two objects.",
        "Metric readings\n1.1 Accuracy ..... 95\n1.2 Recall ..... 92\n1.3 Precision ..... 93",
        "The dots ... represent omitted terms, not page numbers.\nAccuracy .... 95\nLatency .... 12",
    ],
)
def test_numbered_headings_and_non_toc_dots_are_preserved(source):
    r = report([b("real", source, kind="heading" if source.startswith("1.1") else "text")])
    r.reading = build_reading(r)
    assert source == text_of(r.reading, r.document)


def test_saved_edition_filters_flattened_toc_and_stale_translation_without_a_model(tmp_path):
    source = "Contents\n1.1 The relation problem ...... 3\n1.2 The counterexample ...... 5\nThe surviving body."
    r = report([b("mixed", source)])
    r.reading = ReadingDocument(
        version="3",
        source_version="unchanged",
        items=[
            ReadingItem(
                id="cached",
                kind="paragraph",
                page=1,
                content=source.replace("\n", " "),
                parts=[ReadingPart(block_id="mixed", start=0, end=utf16(source))],
            )
        ],
    )
    r.translations["cached"] = ReadingTranslation(
        text="目录及正文的旧合并译文", model="old", source=_digest(source.replace("\n", " "))
    )
    original = r.document.model_dump()
    service = TranslationService(tmp_path)
    assert service.list("paper", r)["items"] == {}
    assert translatable(r)["cached"][1] == "The surviving body."
    assert "cached" not in r.translations
    before = r.model_dump()
    translatable(r)
    assert r.model_dump() == before
    assert r.reading.source_version == "unchanged" and r.document.model_dump() == original


def test_old_report_export_filters_data_without_overwriting_source(monkeypatch):
    r = report(
        [
            b("toc", "Contents\n1 Introduction ..... 2\n2 Methods ..... 3"),
            b("body", "Real body.", order=1),
        ]
    )
    r.reading = ReadingDocument(
        source_version="legacy",
        items=[
            ReadingItem(
                id="toc-item",
                kind="paragraph",
                page=1,
                parts=[ReadingPart(block_id="toc", end=utf16(r.document.blocks[0].text))],
            ),
            ReadingItem(
                id="body-item",
                kind="paragraph",
                page=1,
                parts=[ReadingPart(block_id="body", end=utf16("Real body."))],
            ),
        ],
    )
    monkeypatch.setattr("paperlens.exporting.get_dashboard_template", lambda: "__PAPERLENS_DATA__")
    rendered = json.loads(report_html(r))
    assert [item["id"] for item in rendered["reading"]["items"]] == ["body-item"]
    assert rendered["document"]["blocks"][0]["text"].startswith("Contents")


def test_flattened_source_toc_and_legacy_library_load_without_rewriting_cache(
    tmp_path, monkeypatch
):
    from paperlens.library import LibraryStore

    source = "Contents 1.1 The relation problem ...... 3 1.2 The counterexample ...... 5"
    r = report(
        [
            b("toc", source),
            b("heading", "1 Introduction", kind="heading", order=1),
            b("body", "The main text survives.", order=2),
        ]
    )
    r.reading = ReadingDocument(
        version="3",
        source_version="old",
        items=[
            ReadingItem(
                id="toc-old",
                kind="paragraph",
                content=source,
                parts=[ReadingPart(block_id="toc", end=utf16(source))],
            ),
            ReadingItem(
                id="heading-old",
                kind="heading",
                label="1 Introduction",
                parts=[ReadingPart(block_id="heading", end=utf16("1 Introduction"))],
            ),
            ReadingItem(
                id="body-old",
                kind="paragraph",
                parts=[ReadingPart(block_id="body", end=utf16("The main text survives."))],
            ),
        ],
    )
    folder = tmp_path / "paper"
    folder.mkdir()
    saved = folder / "report.json"
    saved.write_text(r.model_dump_json(), encoding="utf-8")
    original = saved.read_bytes()
    library = LibraryStore(tmp_path)
    monkeypatch.setattr(library, "get_record", lambda _: {"relative_path": "paper/report.json"})
    loaded = library.report("paper")
    assert [i.id for i in loaded.reading.items] == ["heading-old", "body-old"]
    assert loaded.reading.excluded_toc_parts
    assert saved.read_bytes() == original


def test_toc_parent_without_leaders_is_removed_but_real_same_page_heading_stays():
    r = report(
        [
            b("toc", "Contents", kind="heading"),
            b("parent", "1 Introduction 2", kind="heading", order=1),
            b(
                "children",
                "1.1 The relation problem . . . . . 2 1.2 The counterexample . . . . . 3",
                order=2,
            ),
            b("real", "1 Introduction", kind="heading", order=3),
            b("body", "Actual introduction, on the same page as the contents.", order=4),
        ]
    )
    r.reading = build_reading(r)
    assert [i.id for i in r.reading.items] == ["reading-real", "reading-body"]
    assert r.reading.items[0].label == "1 Introduction"
