"""Validate the built workspace without a listening server or any model request."""

from __future__ import annotations

import argparse
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from paperlens.config import Settings
from paperlens.exporting import browser_session
from paperlens.runtime import RuntimeSettings

ROOT = Path(__file__).resolve().parents[2]
JOB = "a" * 32


def original_pdf() -> bytes:
    # Two real PDF pages, so the production PDF.js path renders selectable text.
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Count 2 /Kids [3 0 R 4 0 R] >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] /Resources << /Font << /F1 5 0 R >> >> /Contents 6 0 R >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] /Resources << /Font << /F1 5 0 R >> >> /Contents 7 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for number in (1, 2):
        content = f"BT /F1 21 Tf 50 730 Td (Original PDF - page {number}) Tj 0 -40 Td /F1 13 Tf (Read now while PaperLens is processing.) Tj ET".encode()
        objects.append(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream")
    result, offsets = b"%PDF-1.4\n", [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result += str(number).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
    start = len(result)
    result += f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode()
    result += b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets[1:])
    return result + f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()


def branch(identity: str, children: list[tuple[str, str]], *, more=False) -> dict:
    def node(ident, title):
        return {"id": ident, "title": title, "year": 2017,
                "url": "https://openalex.org/" + ident, "reference_count": 2}
    return {
        "state": "available", "message": "", "node": node(identity, "Attention Is All You Need" if identity == "W1" else "Child work"),
        "references": [node(ident, title) for ident, title in children],
        "offset": 0, "next_offset": 20 if more else None, "total": 3 if more else len(children),
        "cached": False, "checked_at": "2026-11-12T00:00:00Z", "source": "OpenAlex",
        "source_url": "https://api.openalex.org/works/" + identity,
    }


def check(output: Path) -> list[dict]:
    output.mkdir(parents=True, exist_ok=True)
    html = (ROOT / "frontend/dist/index.html").read_text("utf-8").replace("__PAPERLENS_DATA__", "null")
    report = json.loads((ROOT / "tests/fixtures/ui_reader_revision.json").read_text("utf-8"))
    report["document"]["title"] = "Attention Is All You Need"
    report["analysis"]["title"] = report["document"]["title"]
    report["run"]["id"] = "fixture"
    citation = "A. Vaswani, N. Shazeer. Attention Is All You Need. 2017. https://doi.org/10.48550/arXiv.1706.03762"
    item = deepcopy(report["reading"]["items"][0])
    item.update(id="qa-reference", kind="reference", content=citation, parts=[], label="[1]", reference_number="1", href="https://doi.org/10.48550/arXiv.1706.03762")
    report["reading"]["references"] = [item]
    pdf = original_pdf()
    schema = RuntimeSettings(Settings(
        _env_file=None, paperlens_cache_dir=output / "settings-cache",
        paperlens_catalog_network=False,
    )).public()
    checks = []
    with browser_session() as browser:
        for width in (1440, 390):
            context = browser.new_context(viewport={"width": width, "height": 960})
            context.add_init_script("""window.workCopies = []; window.workRejectCopy = false;
              Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {
                writeText: async value => { if (window.workRejectCopy) throw new Error('Denied'); window.workCopies.push(value); }
              }});""")
            page = context.new_page()
            errors, tree_requests, downloads = [], [], []
            state = {"status": "running", "message": "正在整理第 1 页"}
            job = {
                "id": JOB, "source_name": "Attention Is All You Need.pdf", "stage": "reading",
                "progress": .2, "source_url": f"/api/jobs/{JOB}/source.pdf",
                "source_sha256": hashlib.sha256(pdf).hexdigest(), "actions": ["pause", "cancel"],
            }
            page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))

            def intercept(route, _request=None, *, job=job, state=state, downloads=downloads, tree_requests=tree_requests):
                url = urlsplit(route.request.url)
                path, query = url.path, parse_qs(url.query)
                if path == "/":
                    route.fulfill(body=html, content_type="text/html")
                elif path == "/api/health":
                    route.fulfill(json={"ok": True, "features": ["library", "jobs.control", "jobs.source", "references.tree", "settings"], "running_jobs": 1, "queued_jobs": 0})
                elif path == "/api/library":
                    route.fulfill(json={"papers": [{"id": "fixture", "title": report["document"]["title"]}]})
                elif path == "/api/jobs":
                    route.fulfill(json={"jobs": [{**job, **state}]})
                elif path == f"/api/jobs/{JOB}":
                    route.fulfill(json={**job, **state})
                elif path == f"/api/jobs/{JOB}/source.pdf":
                    downloads.append(path)
                    route.fulfill(body=pdf, content_type="application/pdf")
                elif path == "/api/library/fixture/report":
                    route.fulfill(json=report)
                elif path == "/api/library/fixture/references/qa-reference/tree":
                    node = query.get("node", [""])[0]
                    tree_requests.append(node)
                    if node == "W2":
                        result = branch("W2", [("W1", "Back to root"), ("W3", "Shared foundation")])
                    elif node == "W3":
                        result = branch("W3", [])
                        result.update(state="unavailable", message="OpenAlex 未提供这篇文献的参考文献记录；这不代表原论文没有引用。")
                    else:
                        result = branch("W1", [("W2", "Child work"), ("W3", "Shared foundation")])
                    route.fulfill(json=result)
                elif path == "/api/settings":
                    route.fulfill(json=schema)
                elif path == "/api/library/fixture" and route.request.method == "PATCH":
                    route.fulfill(json={"id": "fixture", "title": report["document"]["title"]})
                else:
                    route.fulfill(status=404, json={"detail": "Offline QA endpoint"})

            context.route("**/*", intercept)
            page.goto("http://127.0.0.1:49675/#/settings/appearance")
            page.get_by_role("tab", name="实验功能", exact=True).click()
            page.get_by_text("目前暂无实验功能", exact=True).wait_for()
            assert page.get_by_role("tab").all_text_contents()[-1].strip() == "实验功能"
            assert not page.get_by_role("combobox").count()
            page.screenshot(path=str(output / f"experimental-empty-{width}.png"))
            page.get_by_role("tab", name="论文处理", exact=True).click()
            page.get_by_text("公式审校", exact=True).wait_for()
            assert not page.get_by_text("解析器", exact=True).count()
            assert not page.get_by_text("数学识别设备", exact=True).count()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            page.screenshot(path=str(output / f"direct-settings-{width}.png"))
            page.evaluate("location.hash = '#/settings/appearance'")
            selector = page.get_by_label("复制引用的格式", exact=True)
            selector.select_option("bibtex")
            page.evaluate("location.hash = '#/read/fixture/text'")
            reference = page.locator("#qa-reference")
            reference.wait_for(state="attached")
            reference.scroll_into_view_if_needed()
            reference.get_by_role("button", name="复制引用", exact=True).click()
            page.wait_for_function("window.workCopies.length === 1")
            bibtex = page.evaluate("window.workCopies[0]")
            assert bibtex.startswith("@misc{ref_") and "note = {A. Vaswani" in bibtex
            key = bibtex.split("{", 1)[1].split(",", 1)[0]
            page.evaluate("location.hash = '#/settings/appearance'")
            selector.select_option("latex")
            page.evaluate("location.hash = '#/read/fixture/text'")
            reference.scroll_into_view_if_needed()
            reference.get_by_role("button", name="复制引用", exact=True).click()
            page.wait_for_function("window.workCopies.length === 2")
            assert page.evaluate("window.workCopies[1]") == "\\cite{" + key + "}"
            page.evaluate("window.workRejectCopy = true")
            reference.get_by_role("button", name="已复制", exact=True).click()
            manual = reference.get_by_role("textbox", name="待复制的LaTeX 正文引用")
            manual.wait_for()
            assert manual.input_value() == "\\cite{" + key + "}"
            reference.get_by_role("button", name="收起", exact=True).click()
            assert tree_requests == []
            reference.get_by_role("button", name="引用树", exact=True).click()
            dialog = page.get_by_role("dialog", name="多级参考文献树")
            dialog.get_by_role("button", name="展开引用：Child work", exact=True).click()
            dialog.get_by_text("已在上级路径出现，停止循环", exact=True).wait_for()
            assert tree_requests == ["", "W2"]
            assert dialog.get_by_text("也见其他分支", exact=True).count() == 2
            assert dialog.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
            assert page.locator(".reference-tree-backdrop").evaluate("el => Number(getComputedStyle(el).zIndex)") > 76
            if width < 600:
                assert page.locator(".reference-tree-backdrop").evaluate("el => getComputedStyle(el).paddingLeft") == "8px"
            page.wait_for_function("document.getAnimations().filter(a => a.effect?.target?.closest('.reference-tree-backdrop')).every(a => a.playState === 'finished')")
            dialog.screenshot(path=str(output / f"reference-tree-{width}.png"))
            page.keyboard.press("Escape")
            assert page.get_by_role("dialog").count() == 0
            assert reference.get_by_role("button", name="引用树", exact=True).evaluate("el => el === document.activeElement")

            page.evaluate(f"location.hash = '#/processing/{JOB}'")
            surface = page.locator('.pdf-source-page[data-ready="true"]')
            surface.wait_for()
            assert "Original PDF - page 1" in page.locator(".pdf-native-text").inner_text()
            assert len(downloads) == 1
            page.get_by_role("button", name="PDF 下一页", exact=True).click()
            page.wait_for_function("document.querySelector('.pdf-source-page[data-ready=true]')?.dataset.page === '2'")
            state.update(message="正在审校正文", progress=.7)
            page.get_by_text("正在审校正文", exact=True).wait_for(timeout=12000)
            assert len(downloads) == 1
            assert page.get_by_label("PDF 物理页码").input_value() == "2"
            state.update(status="partial", message="已保留正文，审校未完成", paper_id="fixture", actions=["retry"])
            page.get_by_role("link", name="打开整理结果", exact=True).wait_for(timeout=12000)
            assert page.url.endswith("/processing/" + JOB) and len(downloads) == 1
            assert page.get_by_label("PDF 物理页码").input_value() == "2"
            page.screenshot(path=str(output / f"processing-reader-{width}.png"))
            page.reload()
            page.wait_for_function("document.querySelector('.pdf-source-page[data-ready=true]')?.dataset.page === '2'")
            assert len(downloads) == 2
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            page.get_by_role("link", name="打开整理结果", exact=True).click()
            page.locator("#qa-reference").wait_for(state="attached")
            assert page.url.endswith("/read/fixture/text")
            assert not errors, errors
            checks.append({"width": width, "citation_setting_and_copy": True, "manual_copy_fallback": True,
                           "experimental_last_and_empty": True, "direct_only_processing_settings": True,
                           "lazy_tree_two_levels_and_cycle": True, "tree_focus_restored": True,
                           "real_pdf_rendered_during_processing": True, "status_updates_without_pdf_redownload": True,
                           "partial_result_transition": True, "page_restored_after_reload": True,
                           "horizontal_overflow": False, "page_errors": errors})
            context.close()
    (output / "browser-checks.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2), "utf-8")
    return checks


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / ".cache/work-features")
    args = parser.parse_args()
    print(json.dumps(check(args.output.resolve()), ensure_ascii=False, indent=2))
