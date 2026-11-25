"""Published diagrams must retain their layout outside the offline reader."""

import json
import xml.etree.ElementTree as ET

import pytest
from test_reading_projection import b, report

from paperlens.exporting import browser_session, report_html, write_report
from paperlens.graphs import build_views
from paperlens.models import DashboardReport, PaperNode


def test_publish_from_current_library_landing_template(tmp_path):
    sample = report([b("b1", "A small paper with a research question.")])
    sample.analysis.nodes = [
        PaperNode(id="n1", title="Research question", kind="question", summary="Question")
    ]
    sample.graphs = build_views(sample.analysis)
    sample.quality.total_nodes = 1
    output = write_report(sample, tmp_path)

    assert output.is_file()
    assert not (tmp_path / ".dashboard-check.html").exists()
    saved = DashboardReport.model_validate_json((tmp_path / "report.json").read_bytes())
    assert len(saved.graphs) == 3
    assert all(graph.validated for graph in saved.graphs)
    assert not saved.quality.warnings
    for graph in saved.graphs:
        svg = ET.fromstring((tmp_path / f"{graph.id}.svg").read_text(encoding="utf-8"))
        assert float(svg.attrib["width"]) > 0
        assert float(svg.attrib["height"]) > 0
        assert not svg.findall(".//{http://www.w3.org/2000/svg}style")
        assert svg[0].attrib["fill"] == "#ffffff"


BATCH_METRICS = """svg => {
    const bounds = el => {
        const b = el.getBBox();
        const m = svg.getScreenCTM().inverse().multiply(el.getScreenCTM());
        const p = new DOMPoint(b.x, b.y).matrixTransform(m);
        const q = new DOMPoint(b.x + b.width, b.y + b.height).matrixTransform(m);
        return [p.x, p.y, q.x - p.x, q.y - p.y];
    };
    return {
        viewBox: svg.getAttribute('viewBox').split(' ').map(Number),
        background: getComputedStyle(svg).backgroundColor,
        labels: [...svg.querySelectorAll('.node text')].map(el => ({
            text: el.textContent, bounds: bounds(el),
            font: getComputedStyle(el).fontFamily, size: getComputedStyle(el).fontSize,
            fill: getComputedStyle(el).fill, anchor: getComputedStyle(el).textAnchor
        })),
        nodes: [...svg.querySelectorAll('.node > rect')].map(el => ({
            bounds: bounds(el), fill: getComputedStyle(el).fill
        }))
    };
}"""


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_batch_svgs_freeze_light_styles_and_reopen_without_css(tmp_path, theme):
    sample = report([b("b1", "A paper with multiple diagram labels.")])
    sample.analysis.nodes = [
        PaperNode(id="root", title="研究主题", kind="theme", summary="Theme"),
        PaperNode(
            id="detail",
            title="Transformer 方法细节",
            kind="method",
            summary="Detail",
            parent_id="root",
        ),
        PaperNode(
            id="evidence",
            title="证据与实验结果",
            kind="evidence",
            summary="Evidence",
            parent_id="root",
        ),
    ]
    sample.graphs = build_views(sample.analysis)
    html = tmp_path / "dashboard.html"
    html.write_text(report_html(sample), encoding="utf-8")
    with browser_session() as browser:
        context = browser.new_context(offline=True)
        prefs = json.dumps({"theme": theme, "motion": "reduced"})
        context.add_init_script(f"localStorage.setItem('paperlens.prefs', {json.dumps(prefs)})")
        page = context.new_page()
        page.goto(html.as_uri() + "#/read/local/map")
        page.wait_for_function("window.__paperlensReady === true")
        # A surrounding reader stylesheet must not tint the default batch files.
        global_style = page.add_style_tag(
            content=".node text { fill: rgb(240, 0, 0) !important; } svg { --bg: #001122; }"
        )
        result = page.evaluate("async () => await window.__paperlensValidate()")
        global_style.evaluate("el => el.remove()")
        assert not result["errors"]
        assert len(result["graphs"]) == 3
        reopened = context.new_page()
        for graph in result["graphs"]:
            path = tmp_path / f"{graph['id']}.svg"
            path.write_text(graph["svg"], encoding="utf-8")
            page.evaluate(
                """markup => {
                const host = document.createElement('div');
                host.id = 'batch-preview';
                host.innerHTML = markup;
                document.body.append(host);
            }""",
                graph["svg"],
            )
            expected = page.locator("#batch-preview svg").evaluate(BATCH_METRICS)
            reopened.goto(path.as_uri())
            actual = reopened.locator("svg").evaluate(BATCH_METRICS)
            assert actual["background"] == expected["background"] == "rgb(255, 255, 255)"
            assert actual["viewBox"] == expected["viewBox"]
            assert len(actual["labels"]) == len(actual["nodes"]) >= 2
            x, y, width, height = actual["viewBox"]
            for key in ("labels", "nodes"):
                for before, after in zip(expected[key], actual[key], strict=True):
                    assert after["bounds"] == pytest.approx(before["bounds"], abs=0.1)
                    assert after["fill"] == before["fill"]
                    left, top, w, h = after["bounds"]
                    assert x < left and y < top
                    assert left + w < x + width and top + h < y + height
                    if key == "labels":
                        assert after["font"] == before["font"]
                        assert after["size"] == before["size"] == "14px"
                        assert after["anchor"] == before["anchor"] == "middle"
                        assert after["fill"] == "rgb(31, 35, 40)"
                    else:
                        assert after["fill"] == "rgb(255, 255, 255)"
            for label, node in zip(actual["labels"], actual["nodes"], strict=True):
                tx, ty, tw, th = label["bounds"]
                nx, ny, nw, nh = node["bounds"]
                assert tx + tw / 2 == pytest.approx(nx + nw / 2, abs=1)
                assert ny <= ty and ty + th <= ny + nh
            # Styles must also survive an SVG consumer that drops embedded CSS.
            assert "<style" not in graph["svg"]
            page.locator("#batch-preview").evaluate("el => el.remove()")
        context.close()
