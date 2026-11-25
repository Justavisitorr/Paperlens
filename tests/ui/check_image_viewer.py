"""Offline Chrome checks for image transforms, mobile viewing and PDF navigation.

Build the reader-interactions and pdf-source frontend fixture configs first.
Run: .venv/Scripts/python.exe tests/ui/check_image_viewer.py
The PDF is synthetic, no service/model calls are made, and only a temporary HTML
file is created and removed. Existing report/source files remain untouched.
"""

from __future__ import annotations

import base64
import io
import json
import uuid
from pathlib import Path

from reportlab.pdfgen.canvas import Canvas

from paperlens.exporting import browser_session

ROOT = Path(__file__).resolve().parents[2]


def _pdf_report():
    buffer = io.BytesIO()
    canvas = Canvas(buffer, pagesize=(600, 800))
    for number in range(1, 4):
        canvas.drawString(60, 720, f"Synthetic PDF source page {number}")
        canvas.rect(360, 320, 180, 300)
        canvas.showPage()
    canvas.save()
    return {
        "document": {"source_name": "Synthetic navigation PDF", "blocks": [],
                     "pages": [{"number": number, "width": 600, "height": 800} for number in range(1, 4)]},
        "previews": [],
        "source_pdf": {"data_url": "data:application/pdf;base64," + base64.b64encode(buffer.getvalue()).decode()},
    }


def check() -> None:
    reader = ROOT / "frontend/tests/reader-interactions-dist/tests/reader-interactions.html"
    template = ROOT / "frontend/tests/pdf-source-dist/tests/pdf-source.html"
    temporary = ROOT / ".tmp" / f"image-viewer-{uuid.uuid4().hex}.html"
    temporary.parent.mkdir(parents=True, exist_ok=True)
    report = _pdf_report()
    temporary.write_text(template.read_text(encoding="utf-8").replace(
        "__PDF_TEST_DATA__", json.dumps(report).replace("<", "\\u003c")), encoding="utf-8")
    try:
        with browser_session("chrome") as browser:
            context = browser.new_context(offline=True, viewport={"width": 1440, "height": 900})
            page = context.new_page()
            errors, remote = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: remote.append(request.url)
                    if request.url.startswith(("http:", "https:")) else None)
            page.goto(reader.as_uri())
            entry = page.get_by_role("button", name="放大图片：Figure 1", exact=True)
            entry.click()
            page.wait_for_function("document.querySelector('.image-viewer-viewport img')?.style.visibility==='visible'")
            image = page.locator(".image-viewer-viewport img")
            page.get_by_role("button", name="向右旋转图片90度").click()
            assert "rotate(90deg)" in image.get_attribute("style")
            page.get_by_text("更多调整", exact=True).click()
            page.get_by_role("button", name="水平镜像", exact=True).click()
            style = image.get_attribute("style")
            assert style.index("scaleX(-1)") < style.index("rotate(90deg)")
            page.get_by_role("button", name="向右旋转图片90度").click()
            assert "rotate(0deg)" in image.get_attribute("style")
            page.get_by_role("button", name="复位", exact=True).click()
            for _ in range(6):
                page.get_by_role("button", name="放大图片", exact=True).click()
            bounds = page.locator(".image-viewer-viewport").bounding_box()
            before = image.get_attribute("style")
            x, y = bounds["x"] + bounds["width"] / 2, bounds["y"] + bounds["height"] / 2
            page.mouse.move(x, y)
            page.mouse.down()
            page.mouse.move(x + 60, y, steps=5)
            page.mouse.up()
            assert image.get_attribute("style") != before
            page.keyboard.press("Escape")
            assert entry.evaluate("e => document.activeElement === e")
            page.set_viewport_size({"width": 390, "height": 844})
            entry.click()
            assert page.get_by_role("dialog").bounding_box() == {"x": 0, "y": 0, "width": 390, "height": 844}
            page.wait_for_function("document.querySelector('.image-viewer-viewport img')?.style.visibility==='visible'")
            actual = page.locator(".image-viewer-viewport img").evaluate(
                "e => { const m = new DOMMatrix(getComputedStyle(e).transform); return Math.hypot(m.a, m.b); }")
            assert page.get_by_label("图片缩放比例").inner_text() == f"{round(actual * 100)}%"
            page.keyboard.press("Escape")
            page.set_viewport_size({"width": 1250, "height": 900})
            page.goto(temporary.as_uri())
            page.locator('.pdf-source-page[data-ready="true"]').wait_for(timeout=30000)
            field = page.get_by_role("textbox", name="PDF 物理页码")
            field.fill("")
            assert field.input_value() == ""
            field.fill("3")
            assert page.locator(".pdf-source-page").get_attribute("data-page") == "1"
            field.press("Enter")
            page.locator('.pdf-source-page[data-page="3"][data-ready="true"]').wait_for(timeout=30000)
            for _ in range(2):
                page.get_by_role("button", name="放大 PDF", exact=True).click()
                page.locator('.pdf-source-page[data-ready="true"]').wait_for()
            page.locator(".pdf-source-scroll").evaluate("e => { e.scrollTop=700; e.scrollLeft=120; }")
            measure = """() => { const s=document.querySelector('.pdf-source-scroll'),
                p=document.querySelector('.pdf-source-page'), sr=s.getBoundingClientRect(), pr=p.getBoundingClientRect();
                return {x:(sr.left+s.clientWidth/2-pr.left)/pr.width, y:(sr.top+s.clientHeight/2-pr.top)/pr.height}; }"""
            before = page.evaluate(measure)
            page.get_by_role("button", name="放大 PDF", exact=True).click()
            page.locator('.pdf-source-page[data-ready="true"]').wait_for()
            after = page.evaluate(measure)
            assert abs(before["x"] - after["x"]) < .005 and abs(before["y"] - after["y"]) < .005, (before, after)
            assert not errors and not remote, (errors, remote)
            context.close()
    finally:
        temporary.unlink(missing_ok=True)
    print("PASS: image rotation, screen mirror, drag, Esc focus, mobile full screen, actual scale; PDF draft/submit and zoom center; zero external requests")


if __name__ == "__main__":
    check()
