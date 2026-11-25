"""Wrong model rectangles must not override unambiguous source identities."""

import pytest
from test_reading_projection import b, report

from paperlens.edition_layout import anchor_row, matching_object
from paperlens.models import ReadingItem, ReadingPart
from paperlens.reading_edition import EditionBlock


@pytest.mark.parametrize(
    "label,content,source",
    [
        ("(8)", r"\sum x_i", "broken native math (8)"),
        ("", r"\mathrm{RMSE} = \sqrt{x}", "RMSE = unreadable root"),
        ("", r"sparsity = n_z/n_a", "sparsity = nz na"),
    ],
)
def test_formula_identity_selects_source_not_intro_rectangle(label, content, source):
    intro = b("intro", "The objective is given below.")
    equation = b("equation", source, kind="formula", box=(0.55, 0.7, 0.9, 0.8))
    r = report([intro, equation])
    before = r.document.model_dump()
    row = EditionBlock(
        kind="formula",
        page=1,
        content=content,
        label=label,
        source_ids=["intro"],
        box=(0.1, 0.1, 0.45, 0.2),
    )
    fixed = anchor_row(row, r, [])
    assert fixed.source_ids == ["equation"]
    assert fixed.box == (0.55, 0.7, 0.9, 0.8)
    assert fixed.content == row.content and row.source_ids == ["intro"]
    assert r.document.model_dump() == before


@pytest.mark.parametrize(
    "kind,label",
    [
        ("algorithm", "算法 2"),
        ("figure", "Fig. 2"),
        ("table", "Table 2"),
    ],
)
def test_numbered_object_reuses_original_region_and_exposes_exact_parts(kind, label):
    intro = b("intro", "Introduction")
    body = b("object", "Source object", kind="text", box=(0.5, 0.4, 0.9, 0.8))
    r = report([intro, body])
    old = ReadingItem(
        id="local",
        kind=kind,
        page=1,
        label=label + " Source caption",
        bbox=body.bbox,
        asset="original-image",
        parts=[ReadingPart(block_id="object", start=2, end=10)],
    )
    row = EditionBlock(
        kind=kind, page=1, label=label, source_ids=["intro"], box=(0.1, 0.1, 0.45, 0.2)
    )
    fixed = anchor_row(row, r, [old])
    assert fixed.source_ids == ["object"] and fixed.box == (0.5, 0.4, 0.9, 0.8)
    assert matching_object(fixed, [old]) is old
    assert old.parts[0].start == 2 and old.asset == "original-image"


def test_ambiguous_number_never_chooses_by_model_geometry():
    r = report(
        [b("a", "x (2)", kind="formula"), b("b", "y (2)", kind="formula", box=(0.5, 0.5, 0.9, 0.6))]
    )
    row = EditionBlock(
        kind="formula",
        page=1,
        label="(2)",
        content="z",
        source_ids=["unknown"],
        box=(0.5, 0.5, 0.9, 0.6),
    )
    assert anchor_row(row, r, []) is row


def test_long_continuation_is_paragraph_while_true_source_heading_stays_heading():
    sentence = "计算损失函数关于变量的梯度，之后用梯度更新待求变量，如此迭代便可优化目标函数。"
    r = report(
        [
            b("previous", "即每次随机选取一个样本", order=1),
            b("continuation", sentence, page=2, order=2),
            b("heading", "2.1 目标函数", page=2, kind="heading", order=3),
        ]
    )
    fixed = anchor_row(
        EditionBlock(kind="heading", page=2, content=sentence, source_ids=["heading"]), r, []
    )
    assert fixed.kind == "paragraph" and fixed.continuation and fixed.label == ""
    assert fixed.source_ids == ["continuation"]
    heading = anchor_row(EditionBlock(kind="heading", page=2, content="2.1 目标函数"), r, [])
    assert heading.kind == "heading" and heading.source_ids == ["heading"]


def test_biography_text_belongs_to_author_information_not_reference_column():
    r = report(
        [
            b("bio-title", "作者简介：", kind="heading", box=(0.5, 0.3, 0.8, 0.33)),
            b("bio", "张某，主要研究方向为机器学习。", box=(0.65, 0.4, 0.9, 0.6), order=1),
            b("ref", "References continue here.", box=(0.1, 0.4, 0.45, 0.6), order=2),
        ]
    )
    fixed = anchor_row(
        EditionBlock(kind="paragraph", page=1, content=r.document.blocks[1].text), r, []
    )
    assert fixed.kind == "information" and fixed.category == "authors"
    untouched = anchor_row(
        EditionBlock(kind="paragraph", page=1, content=r.document.blocks[2].text), r, []
    )
    assert untouched.kind == "paragraph"


def test_existing_cross_column_paragraph_keeps_both_source_slices():
    r = report(
        [
            b("left", "已成为统计", box=(0.1, 0.8, 0.45, 0.9)),
            b("right", "机器学习。", box=(0.55, 0.8, 0.9, 0.9), order=1),
        ]
    )
    old = ReadingItem(
        id="local",
        kind="paragraph",
        page=1,
        parts=[
            ReadingPart(block_id="left", start=0, end=5),
            ReadingPart(block_id="right", start=0, end=5),
        ],
    )
    row = EditionBlock(
        kind="paragraph", page=1, content="已成为统计机器学习。", source_ids=["left"]
    )
    assert anchor_row(row, r, [old]).source_ids == ["left", "right"]
