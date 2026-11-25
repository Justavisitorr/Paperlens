"""Source-backed chart geometry and native crop checks on locally generated PDFs."""

import base64
import io
import random

import pypdfium2 as pdfium
import pytest
from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

import paperlens.chart_regions as regions
from paperlens.chart_regions import (
    MAX_IMAGE_BYTES,
    MAX_PIXELS,
    attach_chart_regions,
    native_chart_text,
    render_visual_inputs,
)
from paperlens.control import ProcessingPaused, processing_control
from paperlens.errors import InputError
from paperlens.models import Block, Box, Page, PaperDocument, VisualEvidence


def box(x0, y0, x1, y1):
    return Box(x0=x0, y0=y0, x1=x1, y1=y1)


def block(name, bounds, kind="picture", text="", captions=()):
    return Block(id=name, page=1, order=int(name[1:]), bbox=box(*bounds),
                 kind=kind, text=text, raw_text=text, caption_ids=list(captions))


def document(blocks):
    return PaperDocument(title="Synthetic", source_name="test.pdf", sha256="test",
                         parser="test", pages=[Page(number=1, width=600, height=800)],
                         blocks=blocks)


def visual(name="V1", kind="figure", bounds=None, ids=()):
    return VisualEvidence(id=name, page=1, kind=kind,
                          bbox=box(*bounds) if bounds else None, source_block_ids=list(ids))


def decode(url):
    with Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))) as image:
        return image.convert("RGB")


@pytest.fixture
def chart_pdf(tmp_path):
    path = tmp_path / "chart.pdf"
    canvas = Canvas(str(path), pagesize=(600, 800))
    canvas.setLineWidth(3)
    canvas.rect(120, 360, 360, 280)  # Plot frame: normalized (.2,.2,.8,.55).
    canvas.setFillColorRGB(1, 0, 0)
    canvas.rect(99, 465, 12, 30, fill=True, stroke=False)  # Standalone left axis label.
    canvas.setFillColorRGB(0, 0, 1)
    canvas.rect(270, 345, 40, 8, fill=True, stroke=False)  # Standalone bottom tick label.
    canvas.setFillColorRGB(0, 0, 0)
    canvas.drawString(120, 325, "Fig. 1 Local chart")
    canvas.save()
    return path


def test_composite_and_axis_ownership_preserve_identity(chart_pdf):
    doc = document([
        block("b1", (.2, .2, .8, .55), captions=("b4",)),
        block("b2", (.16, .38, .19, .42), "text", "Accuracy"),
        block("b3", (.44, .558, .53, .57), "text", "epoch"),
        block("b4", (.2, .585, .8, .62), "caption", "Fig. 1 Local chart"),
        block("b5", (.2, .7, .8, .75), "text", "Unrelated paragraph with RMSE = 999"),
    ])
    chart = visual(ids=("b4",))
    assert attach_chart_regions(doc, [chart]) == {}
    assert chart.id == "V1" and chart.bbox == box(.16, .2, .8, .57)
    assert set(chart.source_block_ids) == {"b1", "b2", "b3", "b4"}
    assert native_chart_text(doc, chart) == "Accuracy\nepoch"
    images, order = render_visual_inputs(chart_pdf, 1, [chart], crop_long_edge=2800, max_crops=7)
    assert [m["role"] for m in order] == ["page_context", "chart_crop"]
    assert order[1]["width"] > order[0]["width"] * (chart.bbox.x1 - chart.bbox.x0) * 2
    with decode(images[1]) as crop:
        colors = list(crop.getdata())
        assert sum(r > 180 and g < 100 and b < 100 for r, g, b in colors) > 100
        assert sum(b > 180 and r < 100 and g < 100 for r, g, b in colors) > 100


def test_actual_panels_share_caption_and_parent_mapping(chart_pdf):
    doc = document([
        block("b1", (.2, .2, .48, .5), captions=("b3",)),
        block("b2", (.52, .2, .8, .5), captions=("b3",)),
        block("b3", (.2, .54, .8, .58), "caption", "Fig. 1 Panels"),
    ])
    chart = visual(ids=("b3",))
    panels = attach_chart_regions(doc, [chart])
    assert chart.bbox == box(.2, .2, .8, .5)
    assert panels == {"V1": [doc.blocks[0].bbox, doc.blocks[1].bbox]}
    images, order = render_visual_inputs(chart_pdf, 1, [chart], crop_long_edge=1800,
                                        max_crops=7, include_panels=True, panels=panels)
    assert len(images) == 4
    assert [m["role"] for m in order] == ["page_context", "chart_crop", "panel_crop", "panel_crop"]
    assert all(m["parent_visual_id"] == "V1" for m in order[2:])
    assert order[2]["bbox"]["x1"] < order[3]["bbox"]["x0"]


def test_stacked_panels_and_bad_reference_do_not_steal_another_caption():
    doc = document([
        block("b1", (.2, .1, .8, .25)),
        block("b2", (.2, .27, .8, .42), captions=("b4",)),
        block("b3", (.2, .445, .8, .48), "caption", "Fig. 1"),
        block("b4", (.2, .8, .8, .84), "caption", "Fig. 2"),
    ])
    first, second = visual(ids=("b3",)), visual("V2", ids=("b4",))
    panels = attach_chart_regions(doc, [first, second])
    assert first.bbox == box(.2, .1, .8, .42)
    assert len(panels["V1"]) == 2
    assert second.bbox is None


def test_missing_boxes_are_page_context_only(chart_pdf):
    doc = document([block("b1", (.1, .6, .9, .65), "caption", "Fig. 1")])
    chart = visual(ids=("b1",))
    assert attach_chart_regions(doc, [chart]) == {} and chart.bbox is None
    assert native_chart_text(doc, chart) == ""
    images, order = render_visual_inputs(chart_pdf, 1, [chart], crop_long_edge=3000, max_crops=7)
    assert len(images) == 1 and order[0]["visual_id"] is None
    assert order[0]["bbox"] == box(0, 0, 1, 1).model_dump()


def test_chart_and_formula_budget_precedes_panels(chart_pdf):
    charts = [visual(f"V{i}", bounds=(.2, .2, .8, .55)) for i in range(1, 8)]
    formulas = [visual(f"V{i}", "formula", (.25, .65, .75, .68)) for i in range(8, 12)]
    images, order = render_visual_inputs(chart_pdf, 1, charts + formulas,
                                        crop_long_edge=99999, max_crops=99, include_panels=True,
                                        panels={"V1": [box(.2, .2, .4, .55)]})
    assert len(images) == len(order) == 8
    assert [m["role"] for m in order] == ["page_context"] + ["chart_crop"] * 5 + ["formula_crop"] * 2
    assert sum(len(url) for url in images) <= MAX_IMAGE_BYTES
    assert all(m["width"] * m["height"] <= MAX_PIXELS for m in order)
    assert all(max(m["width"], m["height"]) <= 4097 for m in order)
    images, order = render_visual_inputs(chart_pdf, 1, charts[:1] + formulas,
                                        crop_long_edge=1800, max_crops=7)
    assert len(images) == 6  # Spare slots serve formulas before optional panels.
    images, order = render_visual_inputs(chart_pdf, 1, charts[:1] + formulas,
                                        crop_long_edge=1800, max_crops=1)
    assert [meta["role"] for meta in order] == ["page_context", "chart_crop"]


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_rotated_crop_keeps_displayed_coordinates(tmp_path, rotation):
    path = tmp_path / f"rotated-{rotation}.pdf"
    canvas = Canvas(str(path), pagesize=(600, 800))
    canvas.setPageRotation(rotation)
    canvas.setFillColorRGB(1, 0, 0)
    canvas.rect(80, 100, 140, 170, fill=True, stroke=False)
    canvas.setFillColorRGB(0, 0, 1)
    canvas.rect(350, 400, 100, 80, fill=True, stroke=False)
    canvas.save()
    # A low-resolution full-page oracle identifies the red area on the displayed
    # page. The production renderer must find that same patch at higher detail.
    with pdfium.PdfDocument(path) as pdf:
        page = pdf[0]
        bitmap = page.render(scale=1)
        try:
            with bitmap.to_pil() as rendered, rendered.convert("RGB") as rgb:
                mask = Image.new("L", rgb.size)
                mask.putdata([255 if r > 220 and g < 30 and b < 30 else 0 for r, g, b in rgb.getdata()])
                left, top, right, bottom = mask.getbbox()
                bounds = (left / rgb.width, top / rgb.height, right / rgb.width, bottom / rgb.height)
                mask.close()
        finally:
            bitmap.close()
            page.close()
    chart = visual(bounds=bounds)
    images, order = render_visual_inputs(path, 1, [chart], crop_long_edge=2400, max_crops=7)
    with decode(images[1]) as crop:
        pixels = list(crop.getdata())
        assert sum(r > 200 and g < 80 and b < 80 for r, g, b in pixels) / len(pixels) > .8
        assert not any(b > 200 and r < 80 and g < 80 for r, g, b in pixels)
    assert order[1]["visual_id"] == "V1"
    assert order[1]["bbox"]["x0"] <= bounds[0]


def test_renderer_allocates_cropped_bitmaps_and_honors_pause(chart_pdf, monkeypatch):
    seen = []
    original = pdfium.PdfPage.render

    def recording(page, **kwargs):
        seen.append(kwargs)
        return original(page, **kwargs)

    monkeypatch.setattr(pdfium.PdfPage, "render", recording)
    tiny = visual(bounds=(.3, .3, .32, .32))
    render_visual_inputs(chart_pdf, 1, [tiny], crop_long_edge=2800, max_crops=7)
    assert len(seen) == 2 and max(seen[1]["crop"]) > 0
    assert seen[1]["scale"] > seen[0]["scale"] * 10

    def paused():
        raise ProcessingPaused("test")

    with processing_control(paused), pytest.raises(ProcessingPaused):
        render_visual_inputs(chart_pdf, 1, [tiny], crop_long_edge=2400, max_crops=7)
    with pytest.raises(InputError):
        render_visual_inputs(chart_pdf, 2, [tiny], crop_long_edge=2400, max_crops=7)


def test_tables_above_caption_and_strict_native_text():
    doc = document([
        block("b1", (.15, .1, .85, .14), "caption", "Table 1"),
        block("b2", (.15, .16, .85, .4), "table", "RMSE 0.75"),
        block("b3", (.14, .3, .16, .32), "text", "Outside = 9"),
        block("b4", (.2, .3, .4, .32), "text", "OCR guess 123").model_copy(update={"text_source": "ocr"}),
    ])
    table = visual(kind="table", ids=("b1",))
    attach_chart_regions(doc, [table])
    assert "b2" in table.source_block_ids
    # Fully outside or partly overlapping blocks never become native label evidence.
    table.bbox = doc.blocks[1].bbox
    assert native_chart_text(doc, table) == "RMSE 0.75"


def test_noisy_raster_pdf_obeys_total_encoded_budget(tmp_path, monkeypatch):
    path = tmp_path / "noisy.pdf"
    canvas = Canvas(str(path), pagesize=(600, 800))
    with Image.frombytes("RGB", (400, 400), random.Random(4).randbytes(400 * 400 * 3)) as noise:
        canvas.drawImage(ImageReader(noise), 120, 360, 360, 280)
    canvas.save()
    # Force the same production limiter to handle a tight budget. The PDF is
    # intentionally incompressible so quality reduction alone cannot suffice.
    monkeypatch.setattr(regions, "MAX_IMAGE_BYTES", 100_000)
    charts = [visual(f"V{i}", bounds=(.2, .2, .8, .55)) for i in (1, 2)]
    images, order = render_visual_inputs(path, 1, charts, crop_long_edge=2000, max_crops=7)
    assert len(images) == 3 and sum(len(url) for url in images) <= 100_000
    assert all(max(meta["width"], meta["height"]) < 1500 for meta in order[1:])
    assert [meta["visual_id"] for meta in order[1:]] == ["V1", "V2"]
