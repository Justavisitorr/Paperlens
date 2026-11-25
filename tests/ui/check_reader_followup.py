"""Synthetic UI regression for unlimited import, palettes, saved marks and citations."""

import json
import os
import socket
import threading
import time
import uuid
from pathlib import Path

import uvicorn
from playwright.sync_api import expect

from paperlens.config import Settings
from paperlens.exporting import browser_session
from paperlens.models import DashboardReport, ReadingItem, ReadingPart
from paperlens.reading import utf16
from paperlens.server import create_app
from paperlens.storage import write_json

REPO = Path(__file__).resolve().parents[2]
OUT = Path(os.environ.get("PAPERLENS_UI_OUTPUT", REPO / ".cache/ui-followup"))
OUT.mkdir(parents=True, exist_ok=True)
WORK = OUT / ("fixture-" + uuid.uuid4().hex[:8])
report = DashboardReport.model_validate_json(
    (REPO / "tests/fixtures/ui_reader_revision.json").read_bytes()
)
report.reading.items[
    0
].content = "这里研究 Pixton完备性猜想，以及高亮术语。[Pix26, Theorem 1.1 and Section 2.2]；[CLS, Theorems 1 and 7]。本文 Section 2.2。"
report.reading.items[1].kind = "heading"
report.reading.items[1].label = "2.2 本文章节"
report.reading.items[1].content = "2.2 本文章节"
reftext = "[Pix26] Pixton, A. Synthetic reference. 2026.\n[CLS] C. Author. Another synthetic reference. 2025."
block = report.document.blocks[0].model_copy(
    update={"id": "refs", "text": reftext, "raw_text": reftext, "kind": "reference"}
)
report.document.blocks.append(block)
report.reading.references = [
    ReadingItem(
        id="ref-merged",
        kind="reference",
        parts=[ReadingPart(block_id="refs", start=0, end=utf16(reftext))],
    )
]
settings = Settings(
    _env_file=None,
    llm_requires_key=False,
    llm_api_key="",
    llm_model="fixture",
    llm_base_url="http://127.0.0.1:1/v1",
    paperlens_output_dir=WORK / "outputs",
    paperlens_cache_dir=WORK / "cache",
)
write_json(settings.paperlens_output_dir / "sample/report.json", report.model_dump(mode="json"))
listener = socket.socket()
listener.bind(("127.0.0.1", 0))
port = listener.getsockname()[1]
service = uvicorn.Server(
    uvicorn.Config(
        create_app(settings), host="127.0.0.1", port=port, log_level="error", access_log=False
    )
)
thread = threading.Thread(target=service.run, kwargs={"sockets": [listener]}, daemon=True)
base = f"http://127.0.0.1:{port}"
paper = report.run.id
result = {"checks": [], "errors": [], "external_requests": [], "model_calls": 0, "submitted": []}


def record(text):
    result["checks"].append(text)
    print(text, flush=True)


try:
    thread.start()
    deadline = time.monotonic() + 20
    while not service.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert service.started
    with browser_session("chrome") as browser:
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        page.set_default_timeout(12000)
        page.on("pageerror", lambda error: result["errors"].append(str(error)))

        def route(route):
            url = route.request.url
            if url.startswith(("http:", "https:")) and not url.startswith(base + "/"):
                result["external_requests"].append(url)
                route.abort()
            elif url.endswith("/api/chat") or url.endswith("/api/chat/stream"):
                raise AssertionError("No model requests allowed in this fixture")
            elif url.endswith("/api/imports/resolve"):
                route.fulfill(
                    json={
                        "candidates": [
                            {
                                "id": "lookup-fixture",
                                "title": "另一篇合成论文",
                                "pdf_url": "https://example.invalid/paper.pdf",
                            }
                        ]
                    }
                )
            elif route.request.method == "POST" and (
                url.endswith("/api/jobs") or url.endswith("/api/imports")
            ):
                result["submitted"].append(
                    {"path": url.split(base)[1], "body": route.request.post_data}
                )
                route.fulfill(status=202, json={"id": "b" * 32})
            else:
                route.continue_()

        page.route("**/*", route)
        if os.environ.get("FOLLOWUP_PHASE") != "imports":
            for theme, label in [("dark", "暗色"), ("light", "明亮")]:
                page.goto(base + "/#/settings/appearance")
                page.get_by_role("radio", name=label, exact=True).click()
                palette = page.get_by_role("radiogroup", name=label + "配色").locator(
                    '[aria-checked="true"] .palette-preview'
                )
                expected = palette.evaluate("el=>getComputedStyle(el).backgroundColor")
                page.goto(base + f"/#/read/{paper}/text")
                surface = page.locator(".continuous-reader")
                surface.wait_for()
                actual = surface.evaluate("el=>getComputedStyle(el).backgroundColor")
                result.setdefault("palettes", {})[theme] = {"settings": expected, "reader": actual}
                assert actual == expected
            record("Full-text light/dark default paper matches the active palette shown in settings")

            citation = page.locator('#reading-0 [data-cite="Pix26"]')
            expect(citation).to_have_text("[Pix26, Theorem 1.1 and Section 2.2]")
            assert page.locator('#reading-0 [data-xref="sec:2.2"]').count() == 1
            citation.click()
            expect(page.locator("#ref-merged")).to_be_in_viewport()
            assert page.locator(".reference-card").count() == 2
            record(
                "A legacy merged reference splits locally; whole external locator jumps to Pix26 while standalone Section 2.2 remains internal"
            )
            page.locator("#reading-0").scroll_into_view_if_needed()

            def select(text):
                el = page.locator('#reading-0 [data-reading-surface="original"]')
                el.scroll_into_view_if_needed()
                el.evaluate(
                    """(el,text)=>{const walker=document.createTreeWalker(el,NodeFilter.SHOW_TEXT),at=el.textContent.indexOf(text),range=document.createRange();let offset=0,started=false;while(walker.nextNode()){const n=walker.currentNode,end=offset+n.textContent.length;if(!started&&at>=offset&&at<end){range.setStart(n,at-offset);started=true;}if(started&&at+text.length<=end){range.setEnd(n,at+text.length-offset);break;}offset=end;}getSelection().removeAllRanges();getSelection().addRange(range);el.dispatchEvent(new PointerEvent('pointerup',{bubbles:true}));}""",
                    text,
                )

            def marked(style, text):
                select(text)
                page.get_by_role("toolbar", name="选文笔记").get_by_role(
                    "button", name=style, exact=True
                ).click()
                kind = "underline" if style == "下划线" else "highlight"
                page.wait_for_function(
                    'kind=>[...CSS.highlights.keys()].some(k=>k.startsWith("reading-"+kind+"-"))',
                    arg=kind,
                )
                page.evaluate("getSelection().removeAllRanges()")
                xy = page.evaluate(
                    """kind=>{const key=[...CSS.highlights.keys()].find(k=>k.startsWith('reading-'+kind+'-'));const r=[...CSS.highlights.get(key)][0].getClientRects()[0];return {x:r.x+r.width/2,y:r.y+r.height/2}}""",
                    kind,
                )
                page.mouse.click(xy["x"], xy["y"])
                menu = page.get_by_role("dialog", name="编辑勾画")
                expect(menu).to_be_visible()
                return menu

            menu = marked("下划线", "Pixton完备性猜想")
            menu.get_by_role("button", name="收藏术语", exact=True).click()
            expect(menu.get_by_role("status")).to_contain_text("已收藏")
            saved = page.evaluate('JSON.parse(localStorage.getItem("paperlens.terms"))[0]')
            assert (
                saved["term"] == "Pixton完备性猜想"
                and saved["itemId"] == "reading-0"
                and saved["page"] == 1
                and "这里研究" in saved["quote"]
            )
            page.keyboard.press("Escape")
            menu = marked("高亮", "高亮术语")
            page.evaluate(
                "()=>{window.savedSetItem=Storage.prototype.setItem;Storage.prototype.setItem=function(k,v){if(k==='paperlens.terms')throw Error('fixture quota');return window.savedSetItem.call(this,k,v)}}"
            )
            menu.get_by_role("button", name="收藏术语", exact=True).click()
            expect(menu.get_by_role("alert")).to_contain_text("术语未能保存")
            page.evaluate("()=>{Storage.prototype.setItem=window.savedSetItem;}")
            menu.get_by_role("button", name="收藏术语", exact=True).click()
            expect(menu.get_by_role("status")).to_contain_text("已收藏")
            page.screenshot(path=str(OUT / "saved-highlight-term.png"))
            page.reload()
            page.locator(".continuous-reader").wait_for()
            assert len(page.evaluate('JSON.parse(localStorage.getItem("paperlens.terms"))')) == 2
            page.goto(base + "/#/terms")
            expect(page.get_by_text("Pixton完备性猜想", exact=True).first).to_be_visible()
            record(
                "Clicking existing underline/highlight opens collect action; source/context retained, quota failure truthful, retry and reload succeed"
            )

        decisions = [False, True, False, True]
        warnings = []

        def dialog(d):
            warnings.append(d.message)
            assert "不限" in d.message and "费用" in d.message
            d.accept() if decisions.pop(0) else d.dismiss()

        page.on("dialog", dialog)
        page.goto(base + "/#/upload")
        page.locator("input[type=file]").set_input_files(
            {"name": "fixture.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.4\n%%EOF"}
        )
        page.get_by_role("button", name="开始处理", exact=True).click()
        expect(page.get_by_role("button", name="开始处理", exact=True)).to_be_enabled()
        assert not result["submitted"]
        page.get_by_role("button", name="开始处理", exact=True).click()
        page.wait_for_function('!document.querySelector(".staged")')
        assert (
            len(result["submitted"]) == 1
            and "unlimited_confirmed" in result["submitted"][0]["body"]
        )
        page.get_by_role("tab", name="查找论文", exact=True).click()
        page.get_by_label("论文线索").fill("合成论文")
        page.locator(".import-query button[type=submit], .import-query button").click()
        page.locator(".import-candidate-choice").first.click()
        page.get_by_role("button", name="开始处理", exact=True).click()
        expect(page.get_by_role("button", name="开始处理", exact=True)).to_be_enabled()
        assert len(result["submitted"]) == 1
        page.get_by_role("button", name="开始处理", exact=True).click()
        expect(page.get_by_text("这篇论文已有提交记录。", exact=True)).to_be_visible()
        assert json.loads(result["submitted"][1]["body"])["unlimited_confirmed"] is True
        assert not decisions and len(warnings) == 4
        record(
            "Both upload and source import warn before submission; cancel sends no job and accept carries explicit acknowledgement"
        )
        assert not result["errors"] and not result["external_requests"]
        result["status"] = "passed"
        context.close()
except Exception as error:
    result["status"] = "failed"
    result["failure"] = repr(error)
    try:
        page.screenshot(path=str(OUT / "failure.png"))
    except Exception:
        pass
    raise
finally:
    service.should_exit = True
    thread.join(timeout=8)
    (OUT / ("results-" + os.environ.get("FOLLOWUP_PHASE", "all") + ".json")).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
