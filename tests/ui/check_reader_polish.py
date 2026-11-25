"""One synthetic local browser pass for the four reported reading UI details."""

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
from paperlens.models import DashboardReport
from paperlens.server import create_app
from paperlens.storage import write_json

REPO = Path(__file__).resolve().parents[2]
OUT = Path(os.environ.get("PAPERLENS_UI_OUTPUT", REPO / ".cache/ui-polish"))
OUT.mkdir(parents=True, exist_ok=True)
WORK = OUT / ("fixture-" + uuid.uuid4().hex[:8])
data = json.loads((REPO / "tests/fixtures/ui_reader_revision.json").read_text(encoding="utf-8"))
node = data["analysis"]["nodes"][0]
titles = ["结论的适用范围与主要局限", "Chow 忠实性与 Janda 比较的约束"]
data["analysis"]["nodes"] += [
    dict(
        node,
        id=f"L{i}",
        kind="limitation",
        title=f"{titles[i % 2]}：完整标题 {i + 1} 与 $R$ 的关系",
        summary="这里保留完整内容与证据入口。",
    )
    for i in range(12)
]
baseitem = data["reading"]["items"][0]
data["reading"]["items"][0]["content"] = "这里研究 Pixton完备性猜想，以及公式边界。参见 [Fab]。"
caption = r"Table 1: Notation used across the proof. The formal coefficient ring $R$ and the faithful Chow coefficient algebra $\mathfrak{B}$ have different roles. Prices $20 and $30 remain prose."
data["reading"]["items"] += [
    dict(
        baseitem,
        id="table-polish",
        kind="table",
        label="Table 1",
        content=caption,
        table={
            "rows": 1,
            "cols": 1,
            "cells": [{"text": "$R$", "row_start": 0, "row_end": 1, "col_start": 0, "col_end": 1}],
        },
    ),
    dict(
        baseitem,
        id="figure-polish",
        kind="figure",
        label="Figure 1",
        content=r"Figure 1: Coefficients $\mathfrak{B}$ in $R$.",
    ),
]
data["translations"] = {
    "table-polish": {
        "text": r"表 1：形式系数环 $R$ 与忠实代数 $\mathfrak{B}$。",
        "model": "fixture",
        "source": "fixture",
    },
    "figure-polish": {
        "text": r"图 1：$R$ 中的 $\mathfrak{B}$。",
        "model": "fixture",
        "source": "fixture",
    },
}
data["reading"]["information_items"] = {
    "footnotes": [
        dict(
            baseitem,
            id="footnote-polish",
            content=r"Source note: coefficient ring $R$ and $\mathfrak{B}$.",
        )
    ]
}
labels = ["Fab", "EGH+", "LongReferenceLabel2026"]
data["reading"]["references"] = [
    dict(
        baseitem,
        id=f"ref-polish-{i}",
        kind="reference",
        label=label,
        reference_number=label,
        content=f"[{label}] Author. A complete synthetic bibliography entry and its original title. 2026.",
    )
    for i, label in enumerate(labels)
]
data["quality"]["total_nodes"] = len(data["analysis"]["nodes"])
report = DashboardReport.model_validate(data)
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
result = {"checks": [], "errors": [], "external_requests": [], "model_calls": 0}


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
        context = browser.new_context(viewport={"width": 1440, "height": 1050}, has_touch=True)
        context.add_init_script(
            "localStorage.setItem('paperlens:reading-language',JSON.stringify('bilingual'))"
        )
        page = context.new_page()
        page.set_default_timeout(10000)
        page.on("pageerror", lambda e: result["errors"].append(str(e)))

        def route(route):
            url = route.request.url
            if url.startswith(("http:", "https:")) and not url.startswith(base + "/"):
                result["external_requests"].append(url)
                route.abort()
            elif route.request.method == "POST" and any(
                p in url for p in ("/api/chat", "/translate", "/check", "/api/jobs", "/api/imports")
            ):
                result["errors"].append("Unexpected model/processing submission: " + url)
                route.abort()
            else:
                route.continue_()

        page.route("**/*", route)
        layout_only = os.environ.get("UI_POLISH_PHASE") == "references"
        if not layout_only:
            page.goto(base + f"/#/read/{paper}")
            cards = page.get_by_role("region", name="局限与适用边界", exact=True)
            expect(cards).to_be_visible()
            tabs = cards.get_by_role("tab")
            expect(tabs.first).to_have_text("1")
            tabs.first.press("ArrowRight")
            expect(tabs.nth(1)).to_have_attribute("aria-selected", "true")
            cards.locator(".overview-card-preview.next").click()
            expect(tabs.nth(2)).to_have_attribute("aria-selected", "true")
            cards.screenshot(path=str(OUT / "carousel-desktop.png"))
            page.set_viewport_size({"width": 390, "height": 844})
            tabs.nth(2).press("End")
            expect(tabs.last).to_have_attribute("aria-selected", "true")
            assert cards.locator("h4").inner_text().startswith(titles[1])
            assert cards.locator(".overview-card-tabs").evaluate(
                "el=>{const t=el.querySelector('[aria-selected=true]').getBoundingClientRect(),r=el.getBoundingClientRect();return getComputedStyle(el).scrollbarWidth==='none'&&t.left>=r.left-1&&t.right<=r.right+1}"
            )
            cards.locator(".overview-card-preview.previous").tap()
            expect(tabs.nth(10)).to_have_attribute("aria-selected", "true")
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            cards.screenshot(path=str(OUT / "carousel-mobile.png"))
            record(
                "Compact numbered carousel retains complete current title, keyboard and touch side previews; selected tab stays visible without a scrollbar"
            )

            page.set_viewport_size({"width": 1440, "height": 1050})
            page.goto(base + f"/#/read/{paper}/text")
            for kind in ("table", "figure"):
                expect(page.locator(f"#{kind}-polish .caption-en math")).to_have_count(2)
                expect(page.locator(f"#{kind}-polish .caption-main math")).to_have_count(2)
            expect(page.locator("#table-polish .caption-en")).to_contain_text(
                "Prices $20 and $30 remain prose."
            )
            page.get_by_text("原文脚注", exact=True).click()
            expect(page.locator("#footnote-polish math")).to_have_count(2)
            page.locator("#table-polish").screenshot(path=str(OUT / "table-caption.png"))
            record(
                "Real table and figure bilingual caption paths render R and mathfrak B as MathML; footnote formulas render and currency stays prose"
            )

        else:
            page.goto(base + f"/#/read/{paper}/text")
        ref = page.locator("#ref-polish-0")
        ref.scroll_into_view_if_needed()
        assert ref.locator(".reference-actions").evaluate(
            "el=>[...el.querySelectorAll('button,a')].every(x=>getComputedStyle(x).fontSize==='12px'&&getComputedStyle(x).fontFamily===getComputedStyle(el).fontFamily)"
        )
        for action in ("搜索原文", "复制引用", "返回引用", "PDF", "笔记", "核对原文"):
            assert action in ref.locator(".reference-actions").inner_text()
        page.locator(".reference-list").screenshot(path=str(OUT / "references-desktop.png"))
        longref = page.locator("#ref-polish-2")
        assert longref.evaluate(
            "el=>el.querySelector('.reference-number').getBoundingClientRect().right<=el.querySelector('.reference-citation').getBoundingClientRect().left"
        )
        result["viewports"] = [1440, 390, 320]
        for width in (390, 320):
            page.set_viewport_size({"width": width, "height": 844})
            for idx in (0, 1):
                tag = page.locator(f"#ref-polish-{idx} .reference-number")
                assert tag.evaluate(
                    "el=>{const r=document.createRange();r.selectNodeContents(el);return r.getClientRects().length===1&&el.scrollWidth<=el.clientWidth+1}"
                )
            assert page.locator(".reference-list").evaluate("el=>el.scrollWidth<=el.clientWidth+1")
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            assert longref.evaluate(
                "el=>el.querySelector('.reference-number').getBoundingClientRect().bottom<=el.querySelector('.reference-citation').getBoundingClientRect().top"
            )
        assert ref.locator(".reference-citation").evaluate(
            "el=>getComputedStyle(el).fontSize==='14px'"
        )
        ref.scroll_into_view_if_needed()
        ref.screenshot(path=str(OUT / "reference-short-mobile.png"))
        longref.scroll_into_view_if_needed()
        longref.screenshot(path=str(OUT / "reference-long-mobile.png"))
        record(
            "Fab and EGH+ remain intact; long label occupies its own row at 390/320px and a side column at 1440px; no horizontal overflow; all six compact actions remain"
        )

        if not layout_only:
            page.set_viewport_size({"width": 390, "height": 844})
            el = page.locator('#reading-0 [data-reading-surface="original"]')
            el.scroll_into_view_if_needed()
            el.evaluate(
                """(el,text)=>{const w=document.createTreeWalker(el,NodeFilter.SHOW_TEXT),at=el.textContent.indexOf(text),r=document.createRange();let o=0,s=false;while(w.nextNode()){const n=w.currentNode,e=o+n.textContent.length;if(!s&&at>=o&&at<e){r.setStart(n,at-o);s=true;}if(s&&at+text.length<=e){r.setEnd(n,at+text.length-o);break;}o=e;}getSelection().removeAllRanges();getSelection().addRange(r);el.dispatchEvent(new PointerEvent('pointerup',{bubbles:true}));}""",
                "Pixton完备性猜想",
            )
            page.get_by_role("toolbar", name="选文笔记").get_by_role(
                "button", name="下划线", exact=True
            ).click()
            page.wait_for_function(
                "[...CSS.highlights.keys()].some(k=>k.startsWith('reading-underline-'))"
            )
            page.evaluate("getSelection().removeAllRanges()")
            xy = page.evaluate(
                """()=>{const k=[...CSS.highlights.keys()].find(k=>k.startsWith('reading-underline-'));const r=[...CSS.highlights.get(k)][0].getClientRects()[0];return {x:r.x+r.width/2,y:r.y+r.height/2}}"""
            )
            page.mouse.click(xy["x"], xy["y"])
            menu = page.get_by_role("dialog", name="编辑勾画")
            page.evaluate(
                "()=>{window.savedSetItem=Storage.prototype.setItem;Storage.prototype.setItem=function(k,v){if(k==='paperlens.terms')throw Error('quota');return window.savedSetItem.call(this,k,v)}}"
            )
            menu.get_by_role("button", name="收藏术语", exact=True).press("Enter")
            expect(menu.get_by_role("alert")).to_contain_text("术语未能保存")
            expect(menu.get_by_role("status")).to_have_count(0)
            page.evaluate("()=>{Storage.prototype.setItem=window.savedSetItem}")
            menu.get_by_role("button", name="收藏术语", exact=True).press("Enter")
            expect(menu.get_by_role("status")).to_have_text("✓ 已收藏")
            expect(menu.get_by_role("button", name="已收藏", exact=True)).to_have_count(0)
            expect(menu.get_by_role("alert")).to_have_count(0)
            assert menu.inner_text().count("已收藏") == 1
            assert menu.locator(".reading-note-actions").evaluate(
                "el=>{const ys=[...el.children].map(x=>x.getBoundingClientRect().top);return Math.max(...ys)-Math.min(...ys)<3}"
            )
            menu.screenshot(path=str(OUT / "saved-term-mobile.png"))
            page.reload()
            assert len(page.evaluate("JSON.parse(localStorage.getItem('paperlens.terms'))")) == 1
            record(
                "Saved-mark popup has one success status and one action row; keyboard save, explicit quota failure, retry and reload persistence pass"
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
    (OUT / ("results-" + os.environ.get("UI_POLISH_PHASE", "all") + ".json")).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
