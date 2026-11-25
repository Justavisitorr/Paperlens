"""A crop may present native source content without duplicating it as prose."""

from collections import defaultdict
from contextlib import nullcontext
from types import SimpleNamespace

from PIL import Image
from reportlab.pdfgen.canvas import Canvas
from test_reading_projection import b, report

from paperlens.models import Box, ReadingItem
from paperlens.reading import prepare_reading, utf16
from paperlens.reading_edition import EditionBatch, EditionBlock, apply_edition
from paperlens.reading_visual_ownership import claim_visual_sources, pdf_image_regions


def figure_pdf(tmp_path):
    image = tmp_path / "diagram.png"
    Image.new("RGB", (120, 80), "navy").save(image)
    path = tmp_path / "diagram.pdf"
    canvas = Canvas(str(path), pagesize=(600, 800))
    canvas.drawImage(str(image), 60, 400, 240, 240)
    canvas.drawString(60, 380, "Figure 7: An embedded diagram.")
    canvas.save()
    return path


def test_rendered_crop_owns_contained_native_table_without_a_second_image(tmp_path):
    value = report([
        b("matrix", "Input | Output", kind="table", box=(.1, .2, .5, .5)),
        b("caption", "Figure 7: A matrix.", kind="caption", box=(.1, .51, .5, .55), order=1),
    ])
    before = value.document.model_dump()
    prepare_reading(value)
    apply_edition(value, [EditionBatch(items=[EditionBlock(
        kind="figure", page=1, box=(.1, .2, .5, .55), label="Figure 7",
        source_ids=["caption"], content="A matrix.",
    )])], figure_pdf(tmp_path))
    assert [item.kind for item in value.reading.items] == ["figure"]
    assert {part.block_id for part in value.reading.items[0].parts} == {"matrix", "caption"}
    assert value.reading.structured_pages == [1]
    assert value.document.model_dump() == before


def test_image_text_is_owned_by_rendered_figure_but_nearby_prose_is_retained(tmp_path):
    value = report([
        b("ocr", "BUTTON choice A choice B", box=(.12, .25, .45, .45), text_source="ocr"),
        b("caption", "Figure 7: An embedded diagram.", kind="caption", box=(.1, .51, .5, .55), order=1),
        b("prose", "An independent explanation must remain.", box=(.1, .56, .5, .62), order=2),
    ])
    before = value.document.model_dump()
    prepare_reading(value)
    apply_edition(value, [EditionBatch(items=[EditionBlock(
        kind="figure", page=1, box=(.1, .2, .5, .65), label="Figure 7",
        source_ids=["caption"], content="An embedded diagram.",
    )])], figure_pdf(tmp_path))
    figure = next(item for item in value.reading.items if item.kind == "figure")
    assert {part.block_id for part in figure.parts} == {"ocr", "caption"}
    paragraphs = [item for item in value.reading.items if item.kind == "paragraph"]
    assert {part.block_id for item in paragraphs for part in item.parts} == {"prose"}
    assert value.document.model_dump() == before
    assert value.reading.structured_pages == []


def test_model_crop_alone_does_not_consume_prose():
    block = b("body", "Ordinary scanned body.", text_source="ocr")
    figure = ReadingItem(id="f", kind="figure", page=1, asset="image", bbox=Box(x0=0, y0=0, x1=1, y1=1))
    covered = defaultdict(list)
    claim_visual_sources([figure], [], {block.id: block}, covered, {})
    assert figure.parts == []
    assert dict(covered) == {}


def test_mixed_image_and_prose_block_is_not_claimed_by_a_partial_crop():
    block = b("mixed", "Inside image. Outside image.", box=(.1, .2, .5, .7))
    box = Box(x0=.1, y0=.2, x1=.5, y1=.5)
    figure = ReadingItem(id="f", kind="figure", page=1, asset="image", bbox=box)
    covered = defaultdict(list)
    claim_visual_sources([figure], [], {block.id: block}, covered, {1: [box]})
    assert figure.parts == []
    assert dict(covered) == {}


def test_pdf_image_bounds_are_read_from_native_objects(tmp_path):
    regions = pdf_image_regions(figure_pdf(tmp_path), {1})
    assert len(regions[1]) == 1
    assert regions[1][0] == Box(x0=.1, y0=.2, x1=.5, y1=.5)


def test_whole_page_scanned_body_is_not_a_specific_figure_region(tmp_path):
    image = tmp_path / "scan.png"
    Image.new("RGB", (120, 160), "white").save(image)
    path = tmp_path / "scan.pdf"
    canvas = Canvas(str(path), pagesize=(600, 800))
    canvas.drawImage(str(image), 0, 0, 600, 800)
    canvas.save()
    assert dict(pdf_image_regions(path, {1})) == {}


def test_invalid_raster_geometry_is_skipped_without_clamping_or_losing_other_regions(tmp_path, monkeypatch):
    path = tmp_path / "metadata.pdf"
    path.write_bytes(b"fixture")
    page = SimpleNamespace(width=600, height=800, bbox=(0, 0, 600, 800), close=lambda: None,
                           images=[
                               dict(x0=300, top=160, x1=60, bottom=400),
                               dict(x0=60, top=160, x1=900, bottom=400),
                               dict(x0=60, top=160, bottom=400),
                               dict(x0=60, top=160, x1=300, bottom=400),
                           ])
    monkeypatch.setattr("paperlens.reading_visual_ownership._display_page", lambda value: value)
    monkeypatch.setattr("paperlens.reading_visual_ownership.pdfplumber.open",
                        lambda _: nullcontext(SimpleNamespace(pages=[page])))
    assert pdf_image_regions(path, {1}) == {1: [Box(x0=.1, y0=.2, x1=.5, y1=.5)]}


def test_unreadable_auxiliary_pdf_metadata_preserves_sources(tmp_path, monkeypatch):
    path = tmp_path / "metadata.pdf"
    path.write_bytes(b"fixture")

    def unreadable(_):
        raise RuntimeError("damaged image metadata")

    monkeypatch.setattr("paperlens.reading_visual_ownership.pdfplumber.open", unreadable)
    assert pdf_image_regions(path, {1}) == {}


def test_braced_inline_footnote_preserves_number_without_a_duplicate_word():
    value = report([b("body", "An accompanying tool7 performs the check.")])
    prepare_reading(value)
    apply_edition(value, [EditionBatch(items=[EditionBlock(
        kind="paragraph", page=1, source_ids=["body"],
        content="An accompanying tool$^{7}$ performs the check.",
    )])])
    assert len(value.reading.items) == 1
    assert value.reading.structured_pages == [1]
    part = value.reading.items[0].parts[0]
    assert (part.block_id, part.start, part.end) == ("body", 0, utf16(value.document.blocks[0].text))


def test_incorrect_footnote_number_is_still_retained_as_source():
    value = report([b("body", "An accompanying tool7 performs the check.")])
    prepare_reading(value)
    apply_edition(value, [EditionBatch(items=[EditionBlock(
        kind="paragraph", page=1, source_ids=["body"],
        content="An accompanying tool$^{9}$ performs the check.",
    )])])
    assert value.reading.structured_pages == []
    assert any(item.content is None for item in value.reading.items)


def test_complementary_tables_keep_every_cell_and_distinct_ids_but_share_one_crop(tmp_path):
    value = report([b("matrix", "First grid. Second grid.", kind="table", box=(.1, .2, .5, .5))])
    prepare_reading(value)
    apply_edition(value, [EditionBatch(items=[
        EditionBlock(kind="table", page=1, box=(.1, .2, .5, .5), source_ids=["matrix"],
                     head=[["Capability", "Value"]], rows=[["First", "15%"]]),
        EditionBlock(kind="table", page=1, box=(.1, .2, .5, .5), source_ids=["matrix"],
                     head=[["Case", "Expected", "Predicted"]], rows=[["Second", "neg", "pos"]]),
    ])], figure_pdf(tmp_path))
    items = value.reading.items
    assert len(items) == 2 and len({item.id for item in items}) == 2
    assert sum(bool(item.asset) for item in items) == 1
    assert [[cell.text for cell in item.table.cells] for item in items] == [
        ["Capability", "Value", "First", "15%"],
        ["Case", "Expected", "Predicted", "Second", "neg", "pos"],
    ]
    assert all({part.block_id for part in item.parts} == {"matrix"} for item in items)
