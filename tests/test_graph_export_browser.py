"""Downloaded diagrams must keep their geometry and styles outside the reader."""

import json
import re

import pytest
from PIL import Image
from test_reading_projection import b, report

from paperlens.exporting import browser_session, report_html
from paperlens.graphs import build_views
from paperlens.models import PaperEdge, PaperNode


def graph_report():
    sample = report([b("b1", "A paper used only to exercise the diagram exporter.")])
    sample.analysis.nodes = [
        PaperNode(id="root", title="研究主题", kind="theme", summary="Root"),
        PaperNode(
            id="question", title="研究问题", kind="question", summary="Question", parent_id="root"
        ),
        PaperNode(
            id="result", title="研究结论", kind="conclusion", summary="Result", parent_id="root"
        ),
        PaperNode(
            id="limit", title="适用边界", kind="limitation", summary="Limit", parent_id="root"
        ),
        PaperNode(
            id="detail", title="方法细节", kind="method", summary="Detail", parent_id="question"
        ),
    ]
    sample.analysis.edges = [
        PaperEdge(id="support", source="question", target="result", relation="supports"),
        PaperEdge(id="limit", source="limit", target="result", relation="limits"),
    ]
    sample.graphs = build_views(sample.analysis)
    sample.quality.total_nodes = len(sample.analysis.nodes)
    return sample


# Compare in SVG coordinates, so viewport zoom/pan and export padding do not
# disguise an offset. Reopening the downloaded file removes all application CSS.
DIAGRAM_METRICS = """svg => {
    const inSvg = el => {
        const b = el.getBBox();
        const matrix = svg.getScreenCTM().inverse().multiply(el.getScreenCTM());
        const a = new DOMPoint(b.x, b.y).matrixTransform(matrix);
        const z = new DOMPoint(b.x + b.width, b.y + b.height).matrixTransform(matrix);
        return [a.x, a.y, z.x - a.x, z.y - a.y];
    };
    const styles = (el, keys) => {
        const style = getComputedStyle(el);
        return Object.fromEntries(keys.map(key => [key, style.getPropertyValue(key)]));
    };
    const textMetrics = el => ({
        text: el.textContent,
        chip: Boolean(el.closest('.pl-chip')),
        bounds: inSvg(el),
        styles: styles(el, [
            'text-anchor', 'dominant-baseline', 'font-family', 'font-size', 'font-weight', 'fill'
        ])
    });
    const vb = svg.viewBox.baseVal;
    const rootChip = svg.querySelector('.kind_theme .pl-chip');
    return {
        viewBox: [vb.x, vb.y, vb.width, vb.height],
        background: getComputedStyle(svg).backgroundColor,
        labels: [...svg.querySelectorAll('.pl-relation-label text, .pl-chip text')]
            .map(textMetrics),
        titles: [...svg.querySelectorAll('.node .label text')].map(textMetrics),
        relations: [...svg.querySelectorAll('.pl-relation > path')].map(el => ({
            path: el.getAttribute('d'), bounds: inSvg(el), styles: styles(el, [
                'stroke', 'stroke-dasharray', 'stroke-linecap', 'stroke-width', 'opacity'
            ])
        })),
        relationBounds: inSvg(svg.querySelector('.pl-relations')),
        nodes: [...svg.querySelectorAll('g.node[data-node-id]')].map(el => ({
            id: el.dataset.nodeId,
            bounds: inSvg(el),
            opacity: getComputedStyle(el).opacity,
            kindStripe: styles(el.querySelector('.pl-kind'), ['width', 'display']),
            styles: styles(el.querySelector(':scope > rect'), ['fill', 'stroke', 'stroke-width'])
        })),
        rootStripe: getComputedStyle(svg.querySelector('.kind_theme .pl-kind')).display,
        rootChip: {
            fill: getComputedStyle(rootChip.querySelector('text')).fill,
            background: getComputedStyle(rootChip.querySelector('rect')).fill
        },
        animations: [...svg.querySelectorAll('*')]
            .filter(el => getComputedStyle(el).animationName !== 'none').length,
        interactive: svg.querySelectorAll('[tabindex], [role="button"]').length
    };
}"""


def download_diagram(page, tmp_path, format, stem):
    page.get_by_role("button", name="导出", exact=True).click()
    label = "矢量导图 SVG" if format == "svg" else "导图图片 PNG"
    with page.expect_download() as pending:
        page.get_by_role("button", name=label, exact=True).click()
    target = tmp_path / f"{stem}.{format}"
    pending.value.save_as(target)
    assert pending.value.suggested_filename.endswith(f"-argument.{format}")
    return target


def assert_same_drawing(expected, actual):
    assert actual["rootStripe"] == expected["rootStripe"] == "none"
    assert actual["rootChip"] == expected["rootChip"]
    assert actual["rootChip"]["fill"] != actual["rootChip"]["background"]
    for key in ("labels", "titles", "relations", "nodes"):
        assert len(actual[key]) == len(expected[key])
        for before, after in zip(expected[key], actual[key], strict=True):
            assert after["bounds"] == pytest.approx(before["bounds"], abs=0.1)
            assert after["styles"] == before["styles"]
            if key in {"labels", "titles"}:
                assert after["text"] == before["text"]
                assert after["styles"]["text-anchor"] == "middle"
                if key == "labels":
                    assert after["styles"]["dominant-baseline"] == "central"
                    if after["chip"]:
                        assert after["styles"]["font-size"] == "11px"
            elif key == "relations":
                assert after["path"] == before["path"]
            else:
                assert after["id"] == before["id"]
                assert after["opacity"] == "1"
                assert after["kindStripe"] == before["kindStripe"]
    assert actual["animations"] == 0
    assert actual["interactive"] == 0


def assert_inside_canvas(metrics):
    x, y, width, height = metrics["viewBox"]
    for bounds in [metrics["relationBounds"], *(n["bounds"] for n in metrics["nodes"])]:
        left, top, w, h = bounds
        assert x + 2 <= left and y + 2 <= top
        assert left + w <= x + width - 2 and top + h <= y + height - 2


@pytest.mark.parametrize("theme,vision", [("light", "standard"), ("dark", "cvd")])
def test_downloaded_graph_keeps_layout_theme_and_unfocused_styles(tmp_path, theme, vision):
    html = tmp_path / "dashboard.html"
    html.write_text(report_html(graph_report()), encoding="utf-8")
    with browser_session() as browser:
        context = browser.new_context(
            viewport={"width": 1440, "height": 1000}, offline=True, accept_downloads=True
        )
        prefs = json.dumps(
            {
                "theme": theme,
                "motion": "reduced",
                "colorVision": vision,
                "showRelations": True,
                "mapDepth": "all",
            }
        )
        context.add_init_script(f"localStorage.setItem('paperlens.prefs', {json.dumps(prefs)})")
        page = context.new_page()
        errors, external = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "request",
            lambda request: (
                external.append(request.url)
                if request.url.startswith(("http://", "https://"))
                else None
            ),
        )
        page.goto(html.as_uri() + "#/read/local/map", wait_until="load")
        page.wait_for_function("window.__paperlensReady === true")
        page.get_by_role("tab", name="完整论证", exact=True).click()
        page.locator(".graph-drawing .pl-relation").nth(1).wait_for()
        question = page.locator('.graph-drawing g.node[data-node-id="question"]')
        question.locator(".pl-chip").click()
        page.wait_for_function(
            "document.querySelectorAll('.graph-drawing g.node[data-node-id]').length === 4"
        )
        page.locator(".graph-drawing .pl-chip.is-folded").wait_for()
        page.mouse.move(0, 0)
        svg = page.locator(".graph-drawing svg")
        original = svg.evaluate(DIAGRAM_METRICS)
        assert len(original["relations"]) == 2
        assert len(original["titles"]) == 4
        assert {n["id"] for n in original["nodes"]} == {"root", "question", "result", "limit"}
        for node in original["nodes"]:
            assert node["kindStripe"]["width"] == ("4px" if vision == "cvd" else "3px")
            assert node["kindStripe"]["display"] == ("none" if node["id"] == "root" else "inline")
        # This fixture reproduces the original clipping: same-column relations
        # extend past Mermaid's tree-only viewBox.
        rx, _, rw, _ = original["relationBounds"]
        vx, _, vw, _ = original["viewBox"]
        assert rx + rw > vx + vw
        background = page.locator(".graph-viewport").evaluate(
            "el => getComputedStyle(el).backgroundColor"
        )

        question.locator(":scope > rect").first.click()
        page.mouse.move(0, 0)
        page.wait_for_function(
            "getComputedStyle(document.querySelector('[data-node-id=limit]')).opacity < 0.5"
        )
        exported = download_diagram(page, tmp_path, "svg", "first")
        assert svg.locator("g.node.is-selected").get_attribute("data-node-id") == "question"
        assert svg.evaluate("el => el.classList.contains('has-selection')")

        standalone = context.new_page()
        standalone.goto(exported.as_uri())
        saved = standalone.locator("svg").evaluate(DIAGRAM_METRICS)
        assert_same_drawing(original, saved)
        assert_inside_canvas(saved)
        assert saved["background"] == background

        # Viewer zoom and pan must never change the exported drawing coordinates.
        previous_transform = page.locator(".graph-drawing").get_attribute("style")
        page.get_by_role("button", name="放大导图", exact=True).click()
        page.get_by_role("button", name="放大导图", exact=True).click()
        assert page.locator(".graph-drawing").get_attribute("style") != previous_transform
        zoomed = download_diagram(page, tmp_path, "svg", "zoomed")
        standalone.goto(zoomed.as_uri())
        zoomed_metrics = standalone.locator("svg").evaluate(DIAGRAM_METRICS)
        assert_same_drawing(saved, zoomed_metrics)
        assert zoomed_metrics["viewBox"] == pytest.approx(saved["viewBox"], abs=0.1)

        png = download_diagram(page, tmp_path, "png", "image")
        with Image.open(png) as image:
            assert image.width > saved["viewBox"][2]
            assert image.height > saved["viewBox"][3]
            expected_rgb = tuple(int(c) for c in re.findall(r"\d+", background)[:3])
            assert image.convert("RGBA").getpixel((0, 0)) == (*expected_rgb, 255)
        assert not errors
        assert not external
        context.close()
