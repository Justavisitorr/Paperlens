"""Reading structure must not rewrite source evidence to improve typography."""

from paperlens.models import (
    Block,
    Box,
    DashboardReport,
    Page,
    PaperAnalysis,
    PaperDocument,
    Quality,
    RunInfo,
)
from paperlens.reading import build_reading, markdown_table, prepare_reading, utf16
from paperlens.reading_ownership import figure_titles


def report(blocks):
    count = max(b.page for b in blocks)
    return DashboardReport(
        document=PaperDocument(
            title="Paper",
            source_name="paper.pdf",
            sha256="a" * 64,
            parser="test",
            pages=[Page(number=n, width=600, height=800) for n in range(1, count + 1)],
            blocks=blocks,
        ),
        analysis=PaperAnalysis(title="Paper", summary="", research_question="", nodes=[], edges=[]),
        evidence=[],
        graphs=[],
        previews=[],
        quality=Quality(
            total_pages=count, parsed_pages=count, text_chars=10, grounded_nodes=0, total_nodes=0
        ),
        run=RunInfo(id="test", created_at="now", model="test", parser="test", status="complete"),
    )


def b(id, text, page=1, kind="text", box=(0.1, 0.1, 0.45, 0.2), order=0, **kw):
    return Block(
        id=id,
        text=text,
        raw_text=text,
        page=page,
        kind=kind,
        bbox=Box(x0=box[0], y0=box[1], x1=box[2], y1=box[3]),
        order=order,
        **kw,
    )


def test_reference_fragments_move_to_end_without_mutating_evidence():
    blocks = [
        b("intro", "1 Introduction", kind="heading"),
        b("body", "See [4].", order=1),
        b("refs", "References", kind="heading", order=2),
        b("a", "[4] AUTHOR. An inter-", box=(0.55, 0.8, 0.9, 0.95), order=3),
        b("z", "national paper.", page=2, order=4),
        b("next", "[5] Another paper. https://doi.org/10.1234/test", page=2, order=5),
    ]
    r = report(blocks)
    original = r.document.model_dump()
    reading = build_reading(r)
    assert len(reading.references) == 2
    assert [p.block_id for p in reading.references[0].parts] == ["a", "z"]
    assert reading.references[0].parts[0].end == utf16(blocks[3].text) - 1
    assert reading.references[1].href == "https://doi.org/10.1234/test"
    assert all(i.kind != "reference" for i in reading.items)
    assert r.document.model_dump() == original


def test_funding_and_contact_do_not_capture_following_body():
    blocks = [
        b("title", "Paper", kind="heading", box=(0.2, 0.1, 0.8, 0.2)),
        b("author", "Alice", box=(0.2, 0.21, 0.8, 0.24), order=1),
        b("abstract", "Abstract: body", box=(0.1, 0.26, 0.8, 0.4), order=2),
        b("left", "正文跨栏没有结束", box=(0.1, 0.85, 0.45, 0.9), order=3),
        b("fund", "基金项目: X", box=(0.1, 0.93, 0.4, 0.94), order=4),
        b("email", "E-mail: a@example.org", box=(0.1, 0.95, 0.4, 0.96), order=5),
        b("right", "续接正文。", box=(0.55, 0.85, 0.9, 0.95), order=6),
        b("next", "下一页新段。", page=2, order=7),
    ]
    reading = build_reading(report(blocks))
    body_ids = {p.block_id for i in reading.items for p in i.parts}
    assert {"left", "right", "next", "abstract"} <= body_ids
    assert reading.information["declarations"] == ["fund"]
    assert "email" in reading.information["authors"]
    assert "right" not in reading.information["authors"]


def test_cross_column_paragraphs_join_but_new_heading_does_not():
    blocks = [
        b("one", "1 Results", kind="heading"),
        b("a", "研究显示", box=(0.1, 0.7, 0.45, 0.95), order=1, section="Results"),
        b("b", "模型有效。", box=(0.55, 0.1, 0.9, 0.3), order=2, section="Results"),
        b("h", "2 Discussion", kind="heading", order=3),
        b("c", "另一个段落。", order=4),
    ]
    reading = build_reading(report(blocks))
    assert len(reading.items) == 4
    assert [p.block_id for p in reading.items[1].parts] == ["a", "b"]
    assert reading.items[1].parts[1].join == ""


def test_table_markdown_keeps_cells_instead_of_pipes():
    block = b("t", "table", kind="table")
    block.raw_text = "| Data | Value |\n|---|---|\n| X | 123 |"
    grid = markdown_table(block)
    assert (grid.rows, grid.cols) == (2, 2)
    assert [c.text for c in grid.cells] == ["Data", "Value", "X", "123"]


def test_two_panels_share_one_caption_and_keep_both_regions():
    blocks = [
        b("h", "1 Results", kind="heading"),
        b("p1", "", kind="picture", box=(0.1, 0.3, 0.45, 0.6), order=1, caption_ids=["cap"]),
        b("p2", "", kind="picture", box=(0.5, 0.3, 0.9, 0.6), order=2, caption_ids=["cap"]),
        b("noise", "0.1 0.2 0.3", box=(0.1, 0.4, 0.3, 0.5), order=3),
        b("cap", "Figure 1: results", kind="caption", box=(0.3, 0.61, 0.7, 0.65), order=4),
    ]
    reading = build_reading(report(blocks))
    figures = [i for i in reading.items if i.kind == "figure"]
    assert len(figures) == 1
    assert figures[0].bbox.x0 == 0.1 and figures[0].bbox.x1 == 0.9
    assert "noise" in reading.hidden_ids
    assert {p.block_id for p in figures[0].parts} == {"p1", "p2", "cap"}


def test_unnumbered_complete_references_do_not_merge_at_page_break():
    blocks = [
        b("h", "References", kind="heading"),
        b("r1", "Smith J. First paper. 2020.", box=(0.55, 0.8, 0.9, 0.95), order=1),
        b("r2", "Jones M. Second paper. 2021.", page=2, order=2),
    ]
    reading = build_reading(report(blocks))
    assert len(reading.references) == 2


def test_appended_layout_objects_do_not_turn_into_author_biographies():
    blocks = [
        b(
            "caption",
            "Figure 6 results",
            page=1,
            kind="caption",
            box=(0.55, 0.51, 0.9, 0.55),
            order=2,
        ),
        b("authors", "作者简介", page=2, kind="heading", order=3),
        b("bio", "研究方向介绍", page=2, order=4),
        b("p1", "", page=1, kind="picture", box=(0.55, 0.1, 0.9, 0.3), order=10),
        b(
            "p2",
            "",
            page=1,
            kind="picture",
            box=(0.55, 0.31, 0.9, 0.5),
            order=11,
            caption_ids=["caption"],
        ),
    ]
    reading = build_reading(report(blocks))
    figures = [i for i in reading.items if i.kind == "figure"]
    assert len(figures) == 1
    assert figures[0].bbox.y0 == 0.1 and figures[0].bbox.y1 == 0.5
    assert {p.block_id for p in figures[0].parts} == {"p1", "p2", "caption"}


def test_numbered_heading_depth_overrides_flat_parser_levels():
    r = report(
        [
            b("a", "1 Introduction", kind="heading", heading_level=1),
            b("b", "1.2 Method", kind="heading", heading_level=1, order=1),
            b("c", "1.2.3 Detail", kind="heading", heading_level=1, order=2),
            b("d", "A Appendix", kind="heading", heading_level=1, order=3),
            b("e", "A.1 Additional results", kind="heading", heading_level=1, order=4),
        ]
    )
    assert [i.level for i in build_reading(r).items] == [1, 2, 3, 1, 2]


def test_explicit_reference_kind_needs_no_heading():
    r = report(
        [
            b("body", "1 Results", kind="heading"),
            b("r", "[1] Author. Title. 2010.", kind="reference", order=1),
        ]
    )
    projected = build_reading(r)
    assert len(projected.references) == 1
    assert projected.references[0].label == "[1]"


def test_table_captions_below_and_brace_pieces_and_bullets_stay_out_of_the_text():
    blocks = [
        b("intro", "1 Introduction", kind="heading", box=(0.1, 0.01, 0.45, 0.03), order=0),
        b(
            "t2",
            "Table 2: SQuAD 1.1. | a | b |",
            kind="table",
            box=(0.1, 0.05, 0.45, 0.25),
            order=1,
        ),
        b("c2", "Table 2: SQuAD 1.1.", kind="caption", box=(0.1, 0.27, 0.45, 0.3), order=2),
        b("t3", "| a | b |", kind="table", box=(0.1, 0.32, 0.45, 0.45), order=3),
        b("c3", "Table 3: SQuAD 2.0.", kind="caption", box=(0.1, 0.46, 0.45, 0.48), order=4),
        b("f", "w = x", kind="formula", box=(0.2, 0.5, 0.45, 0.6), order=5),
        b("brace", ">>>>><", box=(0.18, 0.5, 0.195, 0.6), order=6),
        b("li", "· Batch size: 16, 32", kind="heading", box=(0.1, 0.62, 0.45, 0.64), order=7),
    ]
    r = report(blocks)
    prepare_reading(r)
    items = {i.id: i for i in r.reading.items}
    assert items["reading-t2"].label.startswith("Table 2") and items["reading-t3"].label == (
        "Table 3: SQuAD 2.0."
    )
    assert not {"reading-c2", "reading-c3", "reading-brace"} & set(items)
    assert items["reading-li"].kind == "paragraph"


def test_formula_crop_fits_painted_glyphs_not_the_layout_box():
    from paperlens.object_crop import formula_box

    # The layout box clips the leading θ and reaches into the next paragraph.
    glyphs = [(99, 170, 104, 180), (105, 170, 110, 180), (115, 170, 200, 180)]
    glyphs += [(100, 190, 110, 200), (112, 190, 122, 200), (124, 190, 134, 200)]
    glyphs += [(98, 168, 99.5, 178)]  # One-glyph parsing residue block.
    layout = Box(x0=104.5 / 600, y0=168 / 800, x1=210 / 600, y1=194 / 800)
    others = [Box(x0=95 / 600, y0=188 / 800, x1=300 / 600, y1=210 / 800)]
    others.append(Box(x0=97 / 600, y0=167 / 800, x1=100 / 600, y1=179 / 800))
    fitted = formula_box(glyphs, layout, others, 600, 800)
    assert fitted.x0 * 600 < 99 and fitted.y1 * 800 <= 185 and fitted.x1 * 600 > 200


def test_bilingual_table_captions_are_both_owned_by_the_table():
    value = report(
        [
            b("intro", "1 实验", kind="heading", box=(0.1, 0.01, 0.45, 0.03)),
            b("zh", "表 1 实验数据", kind="caption", box=(0.6, 0.44, 0.76, 0.45), order=1),
            b(
                "en",
                "Table 1 Experimental datasets",
                kind="caption",
                box=(0.6, 0.46, 0.8, 0.47),
                order=2,
            ),
            b(
                "t1",
                "Table 1 Experimental datasets | a | b |",
                kind="table",
                box=(0.5, 0.475, 0.9, 0.55),
                order=3,
            ),
        ]
    )
    prepare_reading(value)
    table = next(i for i in value.reading.items if i.kind == "table")
    assert table.label == "表 1 实验数据 / Table 1 Experimental datasets"
    assert not [i for i in value.reading.items if i.kind == "paragraph"]


def test_arxiv_side_stamp_glyph_runs_are_not_body_paragraphs():
    value = report(
        [
            b("year", "3202", box=(0.027, 0.27, 0.059, 0.321)),
            b("month", "gu", box=(0.027, 0.327, 0.059, 0.352), order=1),
            b("letter", "A", kind="heading", box=(0.027, 0.352, 0.059, 0.37), order=2),
            b("body", "The dominant sequence transduction models are based on attention.",
              box=(0.17, 0.5, 0.83, 0.6), order=3),
        ]
    )
    prepare_reading(value)
    assert [i.parts[0].block_id for i in value.reading.items] == ["body"]


def test_unnumbered_panel_title_above_a_picture_is_figure_lettering():
    sections = [
        b(f"s{n}", f"{n} Section {n}", kind="heading", box=(0.17, 0.5 + n / 20, 0.5, 0.51 + n / 20), order=n)
        for n in range(1, 4)
    ]
    value = report(
        [
            *sections,
            b("title", "Scaled Dot-Product Attention", kind="heading", box=(0.24, 0.39, 0.43, 0.40), order=0),
            b("bio", "作者简介:", kind="heading", box=(0.6, 0.39, 0.7, 0.40), order=0),
            b("pic", "", kind="picture", box=(0.28, 0.42, 0.39, 0.48), order=0),
            b("photo", "", kind="picture", box=(0.6, 0.42, 0.7, 0.48), order=0),
        ]
    )
    assert figure_titles(value.document.blocks) == {"title"}
    prepare_reading(value)
    ids = {p.block_id for i in value.reading.items for p in i.parts}
    assert "title" not in ids
    assert {"s1", "s2", "s3"} <= ids
