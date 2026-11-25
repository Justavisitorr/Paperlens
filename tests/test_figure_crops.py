"""Actual PDF geometry must recover content outside the model's crop hint."""

import base64
import io

import pypdfium2 as pdfium
import pytest
from PIL import Image
from reportlab.pdfgen.canvas import Canvas
from test_reading_projection import b, report

from paperlens.figure_crops import render_native_figures
from paperlens.models import Box, ReadingDocument, ReadingItem


def sample(boxes):
    current = report([b("body", "Unchanged original content and evidence.")])
    current.document.parser = "openai-pdf"
    current.reading = ReadingDocument(
        version="3",
        source_version="unchanged",
        items=[
            ReadingItem(
                id=f"figure-{i}",
                kind="figure",
                page=1,
                content=f"Figure {i + 1}: caption.",
                bbox=Box(x0=box[0], y0=box[1], x1=box[2], y1=box[3]),
            )
            for i, box in enumerate(boxes)
        ],
    )
    return current


def decode(item):
    return Image.open(io.BytesIO(base64.b64decode(item.asset.split(",", 1)[1]))).convert("RGB")


def color_bounds(image, color):
    mask = Image.new("L", image.size)
    mask.putdata([255 if color(r, g, b) else 0 for r, g, b in image.getdata()])
    result = mask.getbbox()
    mask.close()
    return result


def test_right_shifted_hint_keeps_complete_vector_frame_and_original_evidence(tmp_path):
    pdf = tmp_path / "frame.pdf"
    canvas = Canvas(str(pdf), pagesize=(612, 792))
    canvas.setFillColorRGB(1, 0, 0)
    canvas.roundRect(108, 542, 396, 168, 4, stroke=0, fill=1)
    canvas.setFillColorRGB(0, 0, 0)
    canvas.drawString(112, 550, "LEFT EDGE must survive")
    canvas.save()
    current = sample([[0.209, 0.081, 0.981, 0.324]])
    before = current.model_dump()
    audit = render_native_figures(current, pdf)
    item = current.reading.items[0]
    assert audit[0]["source"] == "pdf_objects"
    assert item.asset_crop.box.x0 < 108 / 612 and item.asset_crop.box.x1 > 504 / 612
    assert item.bbox.model_dump() == before["reading"]["items"][0]["bbox"]
    assert item.content == before["reading"]["items"][0]["content"]
    assert current.document.model_dump() == before["document"]
    with decode(item) as image:
        bounds = color_bounds(image, lambda r, g, b: r > 220 and g < 40 and b < 40)
        assert bounds[0] >= 8 and image.width - bounds[2] >= 8
        assert abs(bounds[0] - (image.width - bounds[2])) <= 2
    saved = current.model_dump()
    assert render_native_figures(current, pdf) == []
    assert current.model_dump() == saved


def test_subplots_stay_together_and_neighbouring_figure_stays_out(tmp_path):
    pdf = tmp_path / "panels.pdf"
    canvas = Canvas(str(pdf), pagesize=(600, 800))
    for name, y, color in [("top", 530, (1, 0, 0)), ("bottom", 160, (0, 0, 1))]:
        for i, x in enumerate([90, 310]):
            canvas.beginForm(f"{name}-{i}", x, y, x + 180, y + 150)
            canvas.setFillColorRGB(*color)
            canvas.rect(x, y, 180, 150, stroke=0, fill=1)
            canvas.endForm()
            canvas.doForm(f"{name}-{i}")
            canvas.drawString(x + 70, y - 18, f"({chr(97 + i)})")
    canvas.save()
    current = sample([[0.22, 0.12, 0.98, 0.37], [0.22, 0.6, 0.98, 0.84]])
    assert len(render_native_figures(current, pdf)) == 2
    first, second = current.reading.items
    assert all(i.asset_crop.source == "pdf_objects" for i in [first, second])
    assert first.asset_crop.box.y1 > (800 - 512) / 800  # Includes external (a)/(b).
    with decode(first) as image:
        assert color_bounds(image, lambda r, g, b: r > 220 and g < 40 and b < 40)
        assert color_bounds(image, lambda r, g, b: b > 220 and r < 40 and g < 40) is None
    with decode(second) as image:
        assert color_bounds(image, lambda r, g, b: b > 220 and r < 40 and g < 40)
        assert color_bounds(image, lambda r, g, b: r > 220 and g < 40 and b < 40) is None


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_cropbox_and_rotation_follow_rendered_page_not_raw_pdf_coordinates(tmp_path, rotation):
    source, pdf = tmp_path / "source.pdf", tmp_path / "cropped.pdf"
    canvas = Canvas(str(source), pagesize=(600, 800))
    canvas.setFillColorRGB(1, 0, 0)
    canvas.rect(160, 420, 150, 160, stroke=0, fill=1)
    canvas.save()
    with pdfium.PdfDocument(source) as doc:
        page = doc[0]
        page.set_cropbox(100, 100, 500, 700)
        page.set_rotation(rotation)
        doc.save(pdf)
        page.close()
    with pdfium.PdfDocument(pdf) as doc:
        page = doc[0]
        bitmap = page.render(scale=1)
        try:
            with bitmap.to_pil() as image, image.convert("RGB") as rgb:
                left, top, right, bottom = color_bounds(
                    rgb, lambda r, g, b: r > 220 and g < 30 and b < 30
                )
                actual = [
                    left / rgb.width,
                    top / rgb.height,
                    right / rgb.width,
                    bottom / rgb.height,
                ]
        finally:
            bitmap.close()
            page.close()
    current = sample([[actual[0] + 0.02, actual[1], actual[2] + 0.04, actual[3]]])
    render_native_figures(current, pdf)
    item = current.reading.items[0]
    box = item.asset_crop.box
    assert item.asset_crop.source == "pdf_objects"
    assert box.x0 < actual[0] and box.x1 > actual[2]
    assert box.y0 < actual[1] and box.y1 > actual[3]
    with decode(item) as image:
        bounds = color_bounds(image, lambda r, g, b: r > 220 and g < 40 and b < 40)
        assert min(bounds[:2]) >= 8
        assert min(image.width - bounds[2], image.height - bounds[3]) >= 8


@pytest.mark.parametrize("ambiguous", [False, True])
def test_unlocated_or_ambiguous_figure_preserves_whole_page(tmp_path, ambiguous):
    pdf = tmp_path / "uncertain.pdf"
    canvas = Canvas(str(pdf), pagesize=(600, 800))
    canvas.setFillColorRGB(1, 0, 0)
    canvas.rect(100, 400, 300, 200, stroke=0, fill=1)
    canvas.drawString(25, 25, "Content outside the guessed box must remain available.")
    canvas.save()
    boxes = [[0.2, 0.24, 0.8, 0.53]] * 2 if ambiguous else [[0.7, 0.7, 0.9, 0.9]]
    current = sample(boxes)
    render_native_figures(current, pdf)
    for item in current.reading.items:
        assert item.asset_crop.source == "page"
        assert item.asset_crop.box == Box(x0=0, y0=0, x1=1, y1=1)
        with decode(item) as image:
            assert image.width / image.height == pytest.approx(600 / 800)


def test_classic_reports_are_not_changed(tmp_path):
    current = sample([[0.2, 0.2, 0.8, 0.5]])
    current.document.parser = "docling"
    before = current.model_dump()
    assert render_native_figures(current, tmp_path / "missing.pdf") == []
    assert current.model_dump() == before


def test_cached_native_result_rebuilds_images_without_another_model_request(tmp_path):
    import httpx
    from test_ai_direct import responses_reply, settings

    from paperlens.edition_native import read_native
    from paperlens.provider import JsonProvider

    pdf = tmp_path / "native.pdf"
    canvas = Canvas(str(pdf), pagesize=(612, 792))
    canvas.rect(108, 542, 396, 168, stroke=1, fill=0)
    canvas.save()
    calls = []
    hint = [0.209, 0.081, 0.981, 0.324]

    def request(req):
        calls.append(req)
        return responses_reply(
            {
                "items": [
                    {
                        "kind": "figure",
                        "page": 1,
                        "box": hint,
                        "label": "Figure 1",
                        "content": "Original caption.",
                    }
                ]
            }
        )

    configured = settings(tmp_path)
    for _ in range(2):
        with JsonProvider(configured, transport=httpx.MockTransport(request)) as provider:
            result = read_native(pdf, configured, provider)
        item = result.report.reading.items[0]
        assert item.bbox.x0 == hint[0]
        assert item.asset_crop.source == "pdf_objects"
        assert item.asset_crop.box.x0 < 108 / 612
    assert len(calls) == 1
    assert result.audit["batches"][0]["cache_hit"]
