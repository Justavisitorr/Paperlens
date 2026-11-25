"""A structured edition must be continuous without rewriting its evidence."""

import json
from copy import deepcopy

import pytest
from test_reading_projection import b, report

from paperlens.config import Settings
from paperlens.edition_math_review import review_math
from paperlens.models import ReadingDocument, ReadingItem, ReadingPart, Usage
from paperlens.reading import prepare_reading
from paperlens.reading_edition import (
    EditionBatch,
    EditionBlock,
    _ensure_unique_reading_ids,
    apply_edition,
    validate_batch,
)


def row(kind, page, content, **kw):
    return EditionBlock(kind=kind, page=page, content=content, **kw)


def repeated_formula_edition(*, prefix=False):
    content = r"\int_{-1/4}^{1/4}f(x)\,dx=1."
    boxes = [(0.1, 0.1, 0.6, 0.2), (0.1, 0.3, 0.6, 0.4), (0.1, 0.5, 0.6, 0.6)]
    blocks = [
        b(f"source-{index}", content, kind="formula", box=box, order=index + 1)
        for index, box in enumerate(boxes)
    ]
    rows = [
        row("formula", 1, content, source_ids=[f"source-{index}"], box=box)
        for index, box in enumerate(boxes)
    ]
    if prefix:
        blocks.insert(0, b("prefix", "An unrelated heading.", kind="heading",
                           box=(0.1, 0.01, 0.6, 0.08)))
        rows.insert(0, row("heading", 1, "An unrelated heading.", source_ids=["prefix"],
                           box=(0.1, 0.01, 0.6, 0.08)))
    current = report(blocks)
    prepare_reading(current)
    original_document = current.document.model_dump(mode="json")
    apply_edition(current, [EditionBatch(items=rows)])
    return current, content, original_document


def test_apply_edition_keeps_repeated_formulas_distinct_and_source_stable():
    current, content, original_document = repeated_formula_edition()
    inserted, _, _ = repeated_formula_edition(prefix=True)

    def by_source(result):
        return {
            item.parts[0].block_id: item
            for item in result.reading.items
            if item.kind == "formula"
        }

    formulas, shifted = by_source(current), by_source(inserted)
    assert list(formulas) == ["source-0", "source-1", "source-2"]
    assert len({item.id for item in formulas.values()}) == 3
    assert {key: item.id for key, item in formulas.items()} == {
        key: item.id for key, item in shifted.items()
    }
    assert all(item.content == content for item in formulas.values())
    assert "@" not in formulas["source-2"].id  # Preserve the former last-wins ID.
    assert current.document.model_dump(mode="json") == original_document
    assert formulas["source-0"].bbox != formulas["source-1"].bbox


def test_id_normalization_preserves_unique_ids_and_handles_cross_stream_collisions():
    shared = ReadingItem(
        id="repeated", kind="paragraph", page=1, content="same",
        parts=[ReadingPart(block_id="source", end=4)],
    )
    reading = ReadingDocument(
        source_version="test", items=[shared], references=[shared],
        information_items={"footnotes": [shared]},
    )
    before = reading.model_dump(mode="json")
    _ensure_unique_reading_ids(reading)
    items = [reading.items[0], reading.references[0], reading.information_items["footnotes"][0]]
    assert len({item.id for item in items}) == 3 and items[-1].id == "repeated"
    assert items[0].id != items[1].id  # Identical location uses a deterministic suffix.
    ids = [item.id for item in items]
    _ensure_unique_reading_ids(reading)
    assert [item.id for item in items] == ids
    after = reading.model_dump(mode="json")
    for data in (before, after):
        for item in [*data["items"], *data["references"], *data["information_items"]["footnotes"]]:
            item["id"] = "ignored"
    assert before == after

    # An existing unique ID always wins over a generated collision suffix.
    reserved = items[0].id
    fresh = ReadingDocument(
        source_version="test",
        items=[shared.model_copy(update={"id": "repeated"}, deep=True),
               shared.model_copy(update={"id": "repeated"}, deep=True),
               ReadingItem(id=reserved, kind="paragraph", page=1)],
    )
    _ensure_unique_reading_ids(fresh)
    assert fresh.items[-1].id == reserved
    assert len({item.id for item in fresh.items}) == 3


def test_math_patches_target_only_the_selected_repeated_formula(tmp_path, monkeypatch):
    current, original, _ = repeated_formula_edition()
    formulas = [item for item in current.reading.items if item.kind == "formula"]
    target_id = formulas[0].id
    monkeypatch.setattr("paperlens.edition_math_review.region_images", lambda *args: ["fixture"])

    class Provider:
        def __init__(self):
            self.usage = Usage()

        def json(self, system, user, **kwargs):
            self.usage.requests += 1
            payload = json.loads(user)
            assert len({item["id"] for item in payload["items"]}) == 3
            return {"patches": [{"id": target_id, "content": r"\int f(x)\,dx=2"}]}

    configured = Settings(
        _env_file=None, paperlens_cache_dir=tmp_path,
        paperlens_math_enabled=True, paperlens_vision_allow_unverified=True,
    )
    audit = review_math(current, tmp_path / "fixture.pdf", Provider(), configured)
    assert audit["patches"] == 1
    assert formulas[0].content == r"\int f(x)\,dx=2"
    assert [item.content for item in formulas[1:]] == [original, original]


def test_edition_normalizes_only_standalone_formula_wrappers():
    raw = {
        "items": [
            row("formula", 1, "$$x_i^2$$").model_dump(),
            row("paragraph", 1, "正文 $x_i$。").model_dump(),
        ]
    }
    batch = validate_batch(raw, [1])
    assert [r.content for r in batch.items] == ["x_i^2", "正文 $x_i$。"]
    assert raw["items"][0]["content"] == "$$x_i^2$$"


def test_compact_edition_and_legacy_full_fields_have_the_same_defaults():
    compact = {"kind": "paragraph", "page": 1, "content": "Complete source text."}
    legacy = row(**compact).model_dump()
    assert validate_batch({"items": [compact]}, [1]) == validate_batch({"items": [legacy]}, [1])


@pytest.mark.parametrize(
    "page,word", [(1, "long-horizon"), (16, "regularization-path"), (22, "exploration-policy")]
)
def test_postprocessing_preserves_model_compounds_when_native_extraction_loses_hyphens(page, word):
    content = f"The {word} task remains unchanged."
    r = report([b("source", content.replace("-", ""), page=page)])
    prepare_reading(r)
    original_document = r.document.model_dump()
    raw = {"items": [{"kind": "paragraph", "page": page, "content": content,
                       "source_ids": ["source"]}]}
    apply_edition(r, [validate_batch(raw, [page], report=r)])
    assert [item.content for item in r.reading.items] == [content]
    assert r.reading.structured_pages == [page]
    assert [part.block_id for part in r.reading.items[0].parts] == ["source"]
    assert r.document.model_dump() == original_document


def cross_column_report():
    r = report([
        b("page-one", "First page paragraph.", box=(0.1, 0.2, 0.45, 0.3)),
        b("left", "This paragraph crosses columns and continues", page=2, order=1,
          box=(0.121, 0.8639, 0.49, 0.9091)),
        b("right", "with all 76.4% and 34.6% preserved.", page=2, order=2,
          box=(0.5153, 0.0773, 0.8861, 0.3962)),
    ])
    prepare_reading(r)
    return r


def cross_column_raw():
    return {"items": [
        row("paragraph", 1, "First page paragraph.", source_ids=["page-one"]).model_dump(),
        row("paragraph", 2,
            "This paragraph crosses columns and continues with all 76.4% and 34.6% preserved.",
            source_ids=["left", "right"], box=(0.121, 0.8639, 0.8861, 0.3962)).model_dump(),
    ]}


def test_inverted_cross_column_box_uses_real_same_page_sources_without_rewriting_text():
    r, raw = cross_column_report(), cross_column_raw()
    original_report, original_raw = r.model_dump(), deepcopy(raw)
    with pytest.raises(ValueError, match="范围"):
        validate_batch(raw, [1, 2])
    batch = validate_batch(raw, [1, 2], report=r)
    assert batch.items[1].box == (0.121, 0.0773, 0.8861, 0.9091)
    assert batch.items[1].source_ids == ["left", "right"]
    assert batch.items[1].content == raw["items"][1]["content"]
    assert raw == original_raw and r.model_dump() == original_report
    apply_edition(r, [batch])
    assert r.reading.structured_pages == [1, 2]
    assert [item.content for item in r.reading.items] == [entry["content"] for entry in raw["items"]]
    assert r.document.model_dump() == original_report["document"]


@pytest.mark.parametrize("bad_box", [(0.1, 0.8, 0.9, 0.2), (-0.1, 0.1, 1.1, 0.3),
                                     (0.2, 0.3, 0.2, 0.7)])
def test_invalid_auxiliary_geometry_can_only_be_replaced_by_a_supported_source_region(bad_box):
    r = report([b("source", "A uniquely supported paragraph.", box=(0.1, 0.2, 0.45, 0.3))])
    prepare_reading(r)
    raw = {"items": [row("paragraph", 1, r.document.blocks[0].text,
                         source_ids=["source"], box=bad_box).model_dump()]}
    corrected = validate_batch(raw, [1], report=r)
    assert corrected.items[0].box == (0.1, 0.2, 0.45, 0.3)
    assert corrected.items[0].content == r.document.blocks[0].text


@pytest.mark.parametrize("sources,content", [
    (["invented"], "A nonexistent paragraph."),
    (["other-page"], "Text only on the other page."),
    (["first"], "Repeated paragraph."),
])
def test_invalid_geometry_without_unique_same_page_content_support_is_still_rejected(sources, content):
    r = report([
        b("first", "Repeated paragraph.", box=(0.1, 0.1, 0.45, 0.2)),
        b("second", "Repeated paragraph.", box=(0.55, 0.1, 0.9, 0.2), order=1),
        b("other-page", "Text only on the other page.", page=2),
    ])
    prepare_reading(r)
    before = r.model_dump()
    raw = {"items": [row("paragraph", 1, content, source_ids=sources,
                         box=(0.1, 0.8, 0.9, 0.2)).model_dump()]}
    with pytest.raises(ValueError, match="范围"):
        validate_batch(raw, [1], report=r)
    assert r.model_dump() == before


def test_geometry_recovery_does_not_treat_changed_decimal_as_covered_source():
    r = report([b("source", "Coefficient 1.5.")])
    prepare_reading(r)
    original = r.document.model_dump()
    raw = {"items": [row("paragraph", 1, "Coefficient 15.", source_ids=["source"],
                         box=(0.1, 0.8, 0.9, 0.2)).model_dump()]}
    batch = validate_batch(raw, [1], report=r)
    apply_edition(r, [batch])
    assert r.document.model_dump() == original and r.reading.structured_pages == []
    assert r.reading.warnings
    fallback = next(item for item in r.reading.items if item.content is None)
    assert "." in "".join(r.document.blocks[0].text[p.start:p.end] for p in fallback.parts)






def test_semantic_body_joins_across_columns_pages_without_contact_and_preserves_source():
    r = report(
        [
            b("left", "已成为统计", order=1),
            b("right", "机器学习。由此", order=2),
            b("contact", "通信作者：陈某", order=3),
            b("next", "自然地转向交叉项。", page=2, order=4),
        ]
    )
    prepare_reading(r)
    original = r.document.model_dump()
    apply_edition(
        r,
        [
            EditionBatch(
                items=[
                    row("paragraph", 1, "已成为统计机器学习。由此", source_ids=["left", "right"]),
                    row(
                        "information",
                        1,
                        "通信作者：陈某",
                        source_ids=["contact"],
                        category="authors",
                    ),
                    row(
                        "paragraph", 2, "自然地转向交叉项。", source_ids=["next"], continuation=True
                    ),
                ]
            )
        ],
    )
    assert [i.content for i in r.reading.items] == ["已成为统计机器学习。由此自然地转向交叉项。"]
    assert r.reading.items[0].source_pages == [1, 2]
    assert [p.block_id for p in r.reading.items[0].parts] == ["left", "right", "next"]
    assert r.reading.information_items["authors"][0].content == "通信作者：陈某"
    assert r.document.model_dump() == original


def test_references_continue_at_page_boundary_with_explicit_numbers():
    r = report([b("a", "[4] AUTHOR. An inter-"), b("z", "national paper.", page=2)])
    prepare_reading(r)
    apply_edition(
        r,
        [
            EditionBatch(
                items=[
                    row(
                        "reference", 1, "AUTHOR. An inter-", reference_number="4", source_ids=["a"]
                    ),
                    row("reference", 2, "national paper.", continuation=True, source_ids=["z"]),
                ]
            )
        ],
    )
    assert len(r.reading.references) == 1
    ref = r.reading.references[0]
    assert ref.content == "AUTHOR. An international paper."
    assert ref.label == "[4]" and ref.reference_number == "4"
    assert r.reading.items == []


@pytest.mark.parametrize(("source", "corrected"), [
    ("N. V˜ u. A paper, pages 1-118, 2025.", "N. Vũ. A paper, pages 1–118, 2025."),
    ("Author. Proceedings '26, 12-24.", "Author. Proceedings ’26, 12–24."),
])
def test_reference_typography_does_not_create_phantom_fallbacks(source, corrected):
    r = report([b("ref", source, kind="reference")])
    prepare_reading(r)
    original = r.document.model_dump_json()
    apply_edition(r, [EditionBatch(items=[row("reference", 1, corrected, source_ids=["ref"])])])
    assert len(r.reading.references) == 1
    assert r.reading.references[0].content == corrected
    assert r.reading.structured_pages == [1]
    assert r.document.model_dump_json() == original


def test_reference_cleanup_keeps_a_real_omitted_word():
    r = report([b("ref", "Author. A study of missingword outcomes, 2025.", kind="reference")])
    prepare_reading(r)
    apply_edition(r, [EditionBatch(items=[row("reference", 1, "Author. A study of outcomes, 2025.", source_ids=["ref"])])])
    fallbacks = [item for item in r.reading.references if item.content is None]
    assert fallbacks
    assert "missingword" in "".join(r.document.blocks[0].text[p.start:p.end] for item in fallbacks for p in item.parts)


def test_cannot_consume_context_or_silently_drop_a_requested_page():
    for actual in ([1], [1, 2, 3]):
        with pytest.raises(ValueError, match="漏页"):
            validate_batch(
                {"items": [row("paragraph", p, "text").model_dump() for p in actual]}, [1, 2]
            )
    with pytest.raises(ValueError, match="重复"):
        validate_batch(
            {"items": [row("formula", p, "x", label="(13)").model_dump() for p in [1, 2]]}, [1, 2]
        )


def test_table_does_not_change_source_grid_and_keeps_exact_cells():
    r = report([b("table", "original table", kind="table")])
    prepare_reading(r)
    apply_edition(
        r,
        [
            EditionBatch(
                items=[
                    row(
                        "table",
                        1,
                        "实验结果",
                        source_ids=["table"],
                        head=[["方法", "RMSE"]],
                        rows=[["A", "0.123"], ["B", "0.140"]],
                    )
                ]
            )
        ],
    )
    assert [c.text for c in r.reading.items[0].table.cells] == [
        "方法",
        "RMSE",
        "A",
        "0.123",
        "B",
        "0.140",
    ]
    assert r.document.blocks[0].text == "original table"






def test_unknown_source_id_cannot_become_a_fake_annotation_anchor():
    r = report([b("real", "source")])
    prepare_reading(r)
    apply_edition(r, [EditionBatch(items=[row("paragraph", 1, "source", source_ids=["invented"])])])
    assert [p.block_id for p in r.reading.items[0].parts] == ["real"]
    apply_edition(
        r, [EditionBatch(items=[row("paragraph", 1, "unrelated", source_ids=["invented"])])]
    )
    assert r.reading.items[0].parts == []


def test_partial_replacement_keeps_tail_of_an_original_cross_page_paragraph():
    r = report([b("a", "Unfinished", page=1), b("b", " sentence.", page=2)])
    prepare_reading(r)
    assert len(r.reading.items) == 1
    apply_edition(r, [EditionBatch(items=[row("paragraph", 1, "Unfinished", source_ids=["a"])])])
    assert {p.block_id for i in r.reading.items for p in i.parts} == {"a", "b"}


def test_content_anchor_utf16_matches_source_and_does_not_bisect_math_or_citations():
    from paperlens.edition_anchors import content_anchors
    from paperlens.models import ReadingItem, ReadingPart
    from paperlens.reading import utf16

    source = b("s", "😀原文段落具有意义 [2] 而公式 x i 带有下标并提供结论。")
    item = ReadingItem(
        id="edition",
        kind="paragraph",
        content="😀原文段落具有意义 [2] 而公式 $x_i$ 带有下标并提供结论。",
        parts=[ReadingPart(block_id="s", start=0, end=utf16(source.text))],
    )
    spans = content_anchors(item, {"s": source})
    assert spans and spans[0].end > len("😀原文段落具有意义 ")
    for span in spans:
        shown = item.content.encode("utf-16-le")[span.start * 2 : span.end * 2].decode("utf-16-le")
        original = source.text.encode("utf-16-le")[
            span.source.start * 2 : span.source.end * 2
        ].decode("utf-16-le")
        assert shown == original
        assert "[2]" not in shown and "$" not in shown




def test_heading_uses_exact_source_and_figure_does_not_swallow_next_heading():
    from paperlens.models import ReadingItem, ReadingPart

    picture = b("picture", "", kind="picture", box=(0.1, 0.1, 0.45, 0.5))
    heading = b("heading", "4 结论", kind="heading", box=(0.1, 0.55, 0.45, 0.58))
    paragraph = b("body", "论文结论。", box=(0.1, 0.6, 0.45, 0.8))
    r = report([picture, heading, paragraph])
    prepare_reading(r)
    r.reading.items.insert(
        0,
        ReadingItem(
            id="image",
            kind="figure",
            page=1,
            bbox=picture.bbox,
            asset="known-crop",
            parts=[ReadingPart(block_id=picture.id, start=0, end=0)],
        ),
    )
    apply_edition(
        r,
        [
            EditionBatch(
                items=[
                    row(
                        "figure",
                        1,
                        "图 1",
                        box=(0.1, 0.1, 0.45, 0.58),
                        source_ids=["picture", "heading"],
                    ),
                    row("heading", 1, "4 结论", source_ids=["body"], box=(0.1, 0.6, 0.45, 0.8)),
                    row("paragraph", 1, "论文结论。", source_ids=["body"]),
                    row("heading", 1, "参考文献："),
                    row("reference", 1, "A paper", reference_number="1"),
                ]
            )
        ],
    )
    figure, actual_heading, _ = r.reading.items
    assert figure.asset == "known-crop" and figure.bbox == picture.bbox
    assert [p.block_id for p in figure.parts] == ["picture"]
    assert actual_heading.bbox == heading.bbox
    assert [p.block_id for p in actual_heading.parts] == ["heading"]
    assert len(r.reading.references) == 1


def test_partial_upgrade_preserves_already_rebuilt_metadata_and_page_coverage():
    r = report([b("one", "Author", page=1), b("two", "Text", page=2)])
    prepare_reading(r)
    apply_edition(
        r,
        [
            EditionBatch(
                items=[
                    row("information", 1, "Author", category="authors", source_ids=["one"]),
                ]
            )
        ],
    )
    apply_edition(r, [EditionBatch(items=[row("paragraph", 2, "Text", source_ids=["two"])])])
    assert r.reading.structured_pages == [1, 2]
    assert r.reading.information_items["authors"][0].content == "Author"


def test_floating_figure_does_not_interrupt_cross_page_sentence():
    r = report([b("a", "A weighted sum", page=1), b("z", "of values.", page=2)])
    prepare_reading(r)
    apply_edition(
        r,
        [
            EditionBatch(
                items=[
                    row("paragraph", 1, "A weighted sum", source_ids=["a"]),
                    row("figure", 2, "Figure 1"),
                    row(
                        "paragraph",
                        2,
                        "of values.",
                        source_ids=["z"],
                        continuation=True,
                        join="space",
                    ),
                ]
            )
        ],
    )
    assert [i.kind for i in r.reading.items] == ["paragraph", "figure"]
    assert r.reading.items[0].content == "A weighted sum of values."
    assert r.reading.items[0].source_pages == [1, 2]


def test_comma_inside_one_source_paragraph_does_not_create_new_paragraph():
    r = report([b("a", "实验结果表明,模型有效。")])
    prepare_reading(r)
    apply_edition(
        r,
        [
            EditionBatch(
                items=[
                    row("paragraph", 1, "实验结果表明,", source_ids=["a"]),
                    row("paragraph", 1, "模型有效。", source_ids=["a"]),
                ]
            )
        ],
    )
    assert len(r.reading.items) == 1
    assert r.reading.items[0].content == "实验结果表明,模型有效。"


def test_explicit_section_numbers_override_inconsistent_model_levels():
    titles = [
        "1 Introduction",
        "2 方法",
        "2.1 目标",
        "2.2.1 求解",
        "3 实验",
        "A.1 Proof",
        "Discussion",
    ]
    r = report([b(str(index), title, kind="heading") for index, title in enumerate(titles)])
    prepare_reading(r)
    apply_edition(
        r,
        [
            EditionBatch(
                items=[
                    row("heading", 1, title, level=3, source_ids=[str(index)])
                    for index, title in enumerate(titles)
                ]
            )
        ],
    )
    assert [item.level for item in r.reading.items] == [1, 1, 2, 3, 1, 2, 3]
