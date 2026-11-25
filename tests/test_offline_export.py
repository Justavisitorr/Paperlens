"""Offline exports use today's reader and remain portable without private notes."""

import hashlib
import json
import re

import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas
from test_reading_projection import b, report

from paperlens import exporting, server
from paperlens.config import Settings
from paperlens.exporting import browser_session
from paperlens.graphs import build_views
from paperlens.models import Evidence, FormulaRecord, PaperNode
from paperlens.reading import prepare_reading


@pytest.fixture
def offline_paper(tmp_path):
    settings = Settings(
        _env_file=None,
        paperlens_output_dir=tmp_path / "outputs",
        paperlens_cache_dir=tmp_path / "cache",
        paperlens_catalog_network=False,
    )
    directory = settings.paperlens_output_dir / "paper"
    directory.mkdir(parents=True)
    source = directory / "source.pdf"
    pdf = canvas.Canvas(str(source), pagesize=(600, 800))
    pdf.drawString(60, 710, "Offline export fixture")
    pdf.drawString(60, 620, "x = 2")
    pdf.setFillColorRGB(0.2, 0.5, 0.8)
    pdf.rect(60, 400, 180, 80, fill=1)
    pdf.save()
    sample = report(
        [
            b("body", "Raw body before correction.\n\nSecond evidence line."),
            b(
                "equation",
                "x = 2",
                kind="formula",
                order=1,
                box=(0.1, 0.20, 0.4, 0.25),
                formula=FormulaRecord(
                    latex="x=2",
                    mathml="<math><mi>x</mi><mo>=</mo><mn>2</mn></math>",
                    status="renderable",
                ),
            ),
            b("figure", "A blue rectangle", kind="picture", order=2, box=(0.1, 0.40, 0.4, 0.50)),
        ]
    )
    sample.document.title = sample.analysis.title = "Offline export fixture"
    sample.analysis.nodes = [
        PaperNode(
            id="root",
            title="Offline graph",
            kind="theme",
            summary="Local rendering",
            evidence_ids=["e-body", "e-equation"],
        )
    ]
    sample.evidence = [
        Evidence(
            id=f"e-{block.id}",
            block_id=block.id,
            page=block.page,
            bbox=block.bbox,
            quote=block.text,
        )
        for block in sample.document.blocks[:2]
    ]
    sample.graphs = build_views(sample.analysis)
    sample.quality.total_nodes = 1
    sample.document.sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    prepare_reading(sample, source)
    sample.reading.items[0].content = "Corrected body preserves $& $` $' and <tag> exactly."
    (directory / "report.json").write_text(sample.model_dump_json(), encoding="utf-8")
    (directory / "dashboard.html").write_text("outdated reader snapshot", encoding="utf-8")
    return settings, sample


def embedded_report(html):
    match = re.search(
        r'<script id="paperlens-data" type="application/json">(.*?)</script>', html, re.S
    )
    assert match is not None
    return json.loads(match.group(1))


def test_html_endpoint_uses_current_template_and_excludes_private_notes(offline_paper, monkeypatch):
    settings, sample = offline_paper
    template = (
        '<html><body>current reader<script id="paperlens-data" '
        'type="application/json">__PAPERLENS_DATA__</script></body></html>'
    )
    monkeypatch.setattr(exporting, "get_dashboard_template", lambda: template)
    with TestClient(server.create_app(settings)) as client:
        paper = client.get("/api/library").json()["papers"][0]
        response = client.put(
            f"/api/library/{paper['id']}/notes/{'a' * 32}",
            json={
                "item_id": sample.reading.items[0].id,
                "kind": "note",
                "text": "PRIVATE_NOTE_EXPORT_SENTINEL",
            },
        )
        assert response.status_code == 200
        current = client.get(paper["data_url"]).json()
        first = client.get(paper["report_url"])
        assert first.status_code == 200
        assert "current reader" in first.text
        assert "outdated reader snapshot" not in first.text
        assert "PRIVATE_NOTE_EXPORT_SENTINEL" not in first.text
        assert embedded_report(first.text) == current
        # The same saved report receives frontend fixes without re-analysis.
        template = template.replace("current reader", "updated reader")
        second = client.get(paper["report_url"])
        assert "updated reader" in second.text
        assert embedded_report(second.text) == current


def test_exported_html_opens_after_move_with_pdf_formula_and_graph_offline(offline_paper, tmp_path):
    settings, sample = offline_paper
    moved = tmp_path / "moved elsewhere"
    moved.mkdir()
    target = moved / "portable.html"
    with browser_session() as browser:
        # Drive the real App -> offlineHtml -> current service template path.
        with TestClient(server.create_app(settings)) as client:
            paper = client.get("/api/library").json()["papers"][0]
            current = client.get(paper["data_url"]).json()
            workspace = browser.new_context(accept_downloads=True)

            def service(route):
                request = route.request
                response = client.request(
                    request.method,
                    request.url.removeprefix("http://paperlens.test"),
                    content=request.post_data,
                    headers={"Content-Type": request.headers.get("content-type", "")},
                )
                route.fulfill(
                    status=response.status_code,
                    body=response.content,
                    headers=dict(response.headers),
                )

            workspace.route("http://paperlens.test/**", service)
            online = workspace.new_page()
            online.goto(f"http://paperlens.test/#/read/{paper['id']}/map")
            online.wait_for_function("window.__paperlensReady === true")
            online.get_by_role("button", name="导出", exact=True).click()
            with online.expect_download() as pending:
                online.get_by_role("button", name="离线阅读 HTML", exact=True).click()
            pending.value.save_as(target)
            assert pending.value.suggested_filename == "Offline export fixture.html"
            assert embedded_report(target.read_text(encoding="utf-8")) == current
            exports = {}
            for suffix, label in [
                ("json", "完整数据 JSON"),
                ("txt", "纯文本 TXT"),
                ("md", "论文分析 Markdown"),
                ("mmd", "导图代码 Mermaid"),
            ]:
                online.get_by_role("button", name="导出", exact=True).click()
                with online.expect_download() as pending:
                    online.get_by_role("button", name=label, exact=True).click()
                assert pending.value.suggested_filename.endswith(f".{suffix}")
                exports[suffix] = pending.value.path().read_text(encoding="utf-8")
            assert json.loads(exports["json"]) == current
            assert sample.reading.items[0].content in exports["txt"]
            assert sample.document.blocks[0].raw_text not in exports["txt"]
            for evidence in sample.evidence:
                quoted = "\n".join(f"> {line}" for line in evidence.quote.split("\n"))
                assert quoted in exports["md"]
            assert exports["md"].count("> — PDF 第 1 页") == len(sample.evidence)
            assert exports["mmd"].startswith("flowchart ")
            workspace.close()

        context = browser.new_context(offline=True, viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        requests = []
        errors = []
        page.on("request", lambda request: requests.append(request.url))
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(target.as_uri() + "#/read/local/map")
        page.wait_for_function("window.__paperlensReady === true")
        checked = page.evaluate("async () => await window.__paperlensValidate()")
        assert len(checked["graphs"]) == 3
        assert all(graph["valid"] and graph["svg"] for graph in checked["graphs"])
        page.goto(target.as_uri() + "#/read/local/text")
        page.wait_for_selector(".reading-item math")
        assert page.locator(".reading-item").all_text_contents()
        assert page.get_by_text(sample.reading.items[0].content, exact=False).count() > 0
        image = page.locator(".reading-item img").first
        image.wait_for()
        assert image.evaluate("el => el.complete && el.naturalWidth > 0")
        page.get_by_role("button", name="核对原版 PDF", exact=True).click()
        page.wait_for_selector(".pdf-source-page canvas")
        page.wait_for_function(
            "document.querySelector('.pdf-source-page canvas')?.width > 0 && "
            "!document.querySelector('.pdf-source-status')"
        )
        assert page.locator(".pdf-source-page .textLayer").inner_text().strip()
        assert not errors
        assert not page.evaluate("window.__paperlensErrors")
        # Data URLs and the one HTML document are the only allowed resources.
        assert not [url for url in requests if not url.startswith(("data:", target.as_uri()))]

        # Parse the actual downloaded Mermaid in the same bundled engine.
        mmd_report = sample.model_copy(deep=True)
        mmd_report.graphs = mmd_report.graphs[:1]
        mmd_report.graphs[0].mermaid = exports["mmd"]
        mmd_html = moved / "mermaid-check.html"
        mmd_html.write_text(exporting.report_html(mmd_report), encoding="utf-8")
        page.goto(mmd_html.as_uri() + "#/read/local/map")
        page.wait_for_function("window.__paperlensReady === true")
        checked = page.evaluate("async () => await window.__paperlensValidate()")
        assert len(checked["graphs"]) == 1
        assert checked["graphs"][0]["valid"] and checked["graphs"][0]["svg"]
        context.close()
