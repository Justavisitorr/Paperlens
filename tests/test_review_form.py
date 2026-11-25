"""A human review form must neither execute source text nor invent reviewed evidence."""

import hashlib
import json
import re

from paperlens.exporting import browser_session
from tools import audit_reports
from tools.create_review_form import render_form


def sample_packet(report_sha256="a" * 64):
    hostile = (
        '</script><script>window.injectionExecuted=true</script><img src="https://example.com">'
    )
    node = {
        "id": "n1",
        "kind": "contribution",
        "title": hostile,
        "summary": "全部断言需要复核。",
        "explanation": "解释也属于判断范围。",
        "origin": "author",
        "support": "producer-secret-label",
        "sources": [
            {
                "evidence_id": "e1",
                "block_id": "b1",
                "page": 1,
                "quote": hostile,
                "block_text": hostile,
            }
        ],
    }
    return {
        "paper_id": "sample",
        "report_sha256": report_sha256,
        "pdf_sha256": "b" * 64,
        "report_path": "report.json",
        "nodes": [node, {**node, "id": "n2"}],
        "source_blocks": [{"id": "b1", "page": 1, "text": hostile}],
    }


def test_review_form_escapes_source_text_and_hides_producer_labels():
    packet = sample_packet()
    html = render_form(packet)
    payload = re.search(r'<script id="packet" type="application/json">(.*?)</script>', html, re.S)
    assert payload is not None
    assert "<" not in payload.group(1)
    assert json.loads(payload.group(1))["nodes"][0]["title"] == packet["nodes"][0]["title"]
    assert "producer-secret-label" not in html
    assert html.count("</script>") == 2


def test_offline_review_export_requires_explicit_reading_and_matches_audit_protocol(
    tmp_path, monkeypatch
):
    report = tmp_path / "report.json"
    report.write_text('{"report":"immutable test snapshot"}', encoding="utf-8")
    packet = sample_packet(hashlib.sha256(report.read_bytes()).hexdigest())
    form = tmp_path / "form.html"
    form.write_text(render_form(packet), encoding="utf-8")
    with browser_session("chrome") as browser:
        context = browser.new_context(offline=True, accept_downloads=True)
        page = context.new_page()
        external_requests = []
        page.on(
            "request",
            lambda request: (
                external_requests.append(request.url)
                if request.url.startswith(("https://", "http://"))
                else None
            ),
        )
        page.goto(form.as_uri())
        page.wait_for_selector('html[data-ready="true"]')
        assert page.evaluate("window.injectionExecuted") is None
        assert page.locator("input[type=checkbox]:checked").count() == 0
        assert page.locator("h2").first.inner_text().endswith(packet["nodes"][0]["title"])
        page.locator(".verdict").first.select_option("supported")
        page.get_by_role("button", name="导出人工复核 JSON").click()
        assert "无法导出" in page.locator("#status").inner_text()
        page.locator(".rationale").first.fill("已逐项对照原文，支持该节点的全部断言。")
        page.get_by_role("button", name="导出人工复核 JSON").click()
        assert "无法导出" in page.locator("#status").inner_text()
        page.locator("input[type=checkbox]").first.focus()
        page.keyboard.press("Space")
        page.reload()
        assert page.locator("input[type=checkbox]:checked").count() == 1
        with page.expect_download() as download:
            page.get_by_role("button", name="导出人工复核 JSON").click()
        review = json.loads(download.value.path().read_text(encoding="utf-8"))
        assert review["reviewer_type"] == "human"
        assert review["report_sha256"] == packet["report_sha256"]
        assert len(review["judgments"]) == 1
        assert review["judgments"][0]["checked_block_ids"] == ["b1"]
        assert not external_requests
        # A changed report at the same file URI must never inherit the old verdicts.
        changed = {**packet, "report_sha256": "c" * 64}
        form.write_text(render_form(changed), encoding="utf-8")
        page.reload()
        assert page.locator(".verdict").first.input_value() == "not_reviewed"
        assert page.locator("input[type=checkbox]:checked").count() == 0
    monkeypatch.setattr(audit_reports, "ROOT", tmp_path)
    (tmp_path / "sample.packet.json").write_text(json.dumps(packet), encoding="utf-8")
    (tmp_path / "sample.review.json").write_text(json.dumps(review), encoding="utf-8")
    summary = audit_reports.summarize(tmp_path)
    assert summary["reviewer_types"] == ["human"]
    assert summary["counts"] == {"supported": 1, "not_reviewed": 1}
