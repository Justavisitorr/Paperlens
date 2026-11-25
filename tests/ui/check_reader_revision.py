"""Deterministic local browser regression; all model requests are intercepted."""

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
from paperlens.models import DashboardReport, ReadingTranslation
from paperlens.server import create_app
from paperlens.storage import write_json

TASK = Path(
    os.environ.get(
        "PAPERLENS_UI_OUTPUT", str(Path(__file__).resolve().parents[2] / ".cache/ui-revision")
    )
)
REPO = Path(__file__).resolve().parents[2]
OUT = TASK / "delivery/ui-revision"
OUT.mkdir(parents=True, exist_ok=True)
WORK = TASK / ("ui-fixture-" + uuid.uuid4().hex[:8])
report = DashboardReport.model_validate_json(
    (REPO / "tests/fixtures/ui_reader_revision.json").read_bytes()
)
report.analysis.title = "UI 确定性夹具"
report.document.title = "UI 确定性夹具"
report.analysis.summary = "这是界面测试夹具，包含 P_{g,n} 与 $x_i^2$。"
report.analysis.research_question = "如何理解 ker q_{g,n} 与 ker q = P？"
report.analysis.nodes[0].title = "概念 P_{g,n}"
report.analysis.nodes[0].summary = "说明 P_{g,n} 与 ker q = P。"
report.analysis.nodes[0].explanation = "容易理解的 P_{g,n}。"
base_node = report.analysis.nodes[0]
for kind in ["contribution", "limitation"]:
    report.analysis.nodes = [n for n in report.analysis.nodes if n.kind != kind]
    for i in range(3):
        report.analysis.nodes.append(
            base_node.model_copy(
                update={
                    "id": f"ui_{kind}_{i}",
                    "kind": kind,
                    "title": f"测试要点 {i + 1} P_{{g,n}}",
                    "summary": f"夹具内容 {i + 1}：这是用于验证卡片切换和布局的说明，并非论文结论。",
                    "parent_id": base_node.id,
                }
            )
        )
items = [i for i in report.reading.items if i.kind == "paragraph"][:3]
for i, item in enumerate(items):
    item.content = (
        f"Paragraph {i}. Repeat phrase. Consider $x_i^2$ and citation [1]. Repeat phrase. End."
    )
    item.content_spans = []
    item.transcription = None
    report.translations[item.id] = ReadingTranslation(
        text="译文中的术语😀。这里仍有公式 $x_i^2$。", source=item.content
    )
raw = report.document.blocks[0].model_copy(
    update={"id": "ui-raw", "text": "RawTerm is a source token. Another sentence."}
)
report.document.blocks.append(raw)
items[2].parts = [
    items[2].parts[0].model_copy(update={"block_id": raw.id, "start": 0, "end": len(raw.text)})
]
items[2].content = None
report.reading.items = items
report.reading.references = []
report.reading.information = {}
report.reading.information_items = {}
node_ids = {n.id for n in report.analysis.nodes}
report.analysis.edges = [
    e for e in report.analysis.edges if e.source in node_ids and e.target in node_ids
]
for node in report.analysis.nodes:
    if node.parent_id and node.parent_id not in node_ids:
        node.parent_id = base_node.id
for graph in report.graphs:
    graph.node_ids = list(node_ids)
    graph.edge_ids = [e.id for e in report.analysis.edges]
report.quality.total_nodes = len(report.analysis.nodes)
report = DashboardReport.model_validate(report.model_dump())
settings = Settings(
    _env_file=None,
    llm_api_key="",
    llm_requires_key=False,
    llm_model="fixture",
    llm_base_url="http://127.0.0.1:1/v1",
    paperlens_output_dir=WORK / "outputs",
    paperlens_cache_dir=WORK / "cache",
    paperlens_catalog_network=False,
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
result = {
    "status": "running",
    "checks": [],
    "errors": [],
    "external_requests": [],
    "model_calls": 0,
    "mock_requests": [],
    "workspace": str(WORK),
}
base = f"http://127.0.0.1:{port}"
paper = report.run.id


def record(text):
    result["checks"].append(text)
    (OUT / ("results-" + os.environ.get("UI_CHECK_PHASE", "all") + ".json")).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(text, flush=True)


try:
    thread.start()
    deadline = time.monotonic() + 20
    while not service.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert service.started
    with browser_session("chrome") as browser:
        context = browser.new_context(viewport={"width": 1440, "height": 1050})
        page = context.new_page()
        page.set_default_timeout(12000)
        page.on("pageerror", lambda error: result["errors"].append(str(error)))
        pending = []

        def route_request(route):
            url = route.request.url
            if url.startswith(("http:", "https:")) and not url.startswith(base + "/"):
                result["external_requests"].append(url)
                route.abort()
            elif "/api/chat" in url and route.request.method == "POST":
                result["mock_requests"].append(route.request.post_data_json)
                pending.append(route)
            else:
                route.continue_()

        page.route("**/*", route_request)

        def complete_chat(success=True):
            assert pending
            route = pending.pop(0)
            if not success:
                route.fulfill(
                    status=503,
                    content_type="application/json",
                    body=json.dumps({"detail": "确定性网络失败"}),
                )
                return
            reply = {
                "answer": "确定性回复：请核对原文 [1]。",
                "model": "fixture-only",
                "grounded": True,
                "quotes": [
                    {
                        "n": 1,
                        "paper_id": paper,
                        "block_id": raw.id,
                        "page": raw.page,
                        "quote": raw.text,
                    }
                ],
                "warnings": [],
            }
            if route.request.url.endswith("/stream"):
                route.fulfill(
                    content_type="application/x-ndjson",
                    body=json.dumps({"type": "done", "reply": reply}) + "\n",
                )
            else:
                route.fulfill(content_type="application/json", body=json.dumps(reply))

        def select(locator, text):
            locator.scroll_into_view_if_needed()
            locator.evaluate(
                """(el,text)=>{
              const at=el.textContent.indexOf(text);if(at<0)throw Error('Missing selected text');
              const walker=document.createTreeWalker(el,NodeFilter.SHOW_TEXT), range=document.createRange();
              let offset=0,started=false;
              while(walker.nextNode()){const n=walker.currentNode,end=offset+n.textContent.length;
                if(!started&&at>=offset&&at<end){range.setStart(n,at-offset);started=true;}
                if(started&&at+text.length<=end){range.setEnd(n,at+text.length-offset);break;}offset=end;}
              getSelection().removeAllRanges();getSelection().addRange(range);
              el.dispatchEvent(new PointerEvent('pointerup',{bubbles:true}));
            }""",
                text,
            )

        def notes():
            return context.request.get(base + f"/api/library/{paper}/notes").json()["notes"]

        def panel():
            page.get_by_role("button", name="阅读笔记", exact=True).click()
            return page.locator(".reading-notes-panel")

        if os.environ.get("UI_CHECK_PHASE", "all") == "all":
            page.goto(base + f"/#/read/{paper}")
            expect(page.locator(".reading-overview")).to_be_visible()
            assert page.locator(".mode-nav button").count() == 5
            assert page.locator(".mode-nav button").first.locator("svg").count() == 1
            assert page.locator(".reading-overview math").count() >= 6
            cards = page.get_by_role("region", name="局限与适用边界", exact=True)
            cards.get_by_role("tab").nth(1).click()
            assert cards.locator(".overview-card-preview").count() == 2
            page.screenshot(path=str(OUT / "overview-desktop.png"))
            cards.get_by_role("tab").nth(1).press("ArrowRight")
            expect(cards.get_by_role("tab").nth(2)).to_have_attribute("aria-selected", "true")
            cards.get_by_role("button", name="上一张：", exact=False).click()
            expect(cards.get_by_role("tab").nth(1)).to_have_attribute("aria-selected", "true")
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path=str(OUT / "overview-mobile.png"), animations="disabled")
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            page.set_viewport_size({"width": 1440, "height": 1050})
            for key, mode in [
                ("2", "map"),
                ("3", "text"),
                ("4", "glossary"),
                ("5", "quality"),
                ("1", "overview"),
            ]:
                page.locator(".mode-nav").click(position={"x": 2, "y": 2})
                page.keyboard.press(key)
                expect(page.locator(".reader")).to_have_class(
                    __import__("re").compile("mode-" + mode)
                )
            assert page.get_by_text("阅读核查清单", exact=True).count() == 0
            page.keyboard.press("2")
            node = page.locator(f'g.node[data-node-id="{base_node.id}"]')
            node.wait_for()
            (OUT / "graph-node.html").write_text(node.inner_html(), encoding="utf-8")
            page.screenshot(path=str(OUT / "graph-math.png"))
            assert node.locator('.graph-math [baseline-shift="sub"]').count() >= 1
            node.click()
            assert page.locator(".node-summary math").count() >= 1
            page.screenshot(path=str(OUT / "graph-math.png"))
            for label, suffix in [("矢量导图", "svg"), ("导图图片", "png")]:
                page.get_by_role("button", name="导出", exact=True).click()
                with page.expect_download() as download:
                    page.get_by_role("button", name=label, exact=False).click()
                target = OUT / ("graph-export." + suffix)
                download.value.save_as(target)
                assert target.stat().st_size > 500
            assert 'baseline-shift="sub"' in (OUT / "graph-export.svg").read_text(encoding="utf-8")
            assert (OUT / "graph-export.png").read_bytes().startswith(b"\x89PNG")
            record(
                "Overview icon, five shortcuts, card side previews/keyboard/mobile, SVG scripts and MathML details, SVG/PNG downloads; checklist absent"
            )

            page.goto(base + "/#/settings/appearance")
            page.get_by_role("radio", name="导图", exact=True).click()
            assert (
                page.evaluate('JSON.parse(localStorage.getItem("paperlens.prefs")).fullReadMode')
                == "map"
            )
            page.reload()
            page.get_by_role("radio", name="导图", exact=True).wait_for()
            page.goto(base + "/#/library")
            page.get_by_role("button", name="查看《UI 确定性夹具》详情", exact=True).click()
            page.get_by_role("link", name="阅读全文", exact=True).click()
            expect(page.locator(".reader")).to_have_class(__import__("re").compile("mode-map"))
            page.goto(base + f"/#/read/{paper}")
            expect(page.locator(".reading-overview")).to_be_visible()
            record(
                "Full-read preference persists and opens map; ordinary paper route still opens overview"
            )

            page.goto(base + f"/#/read/{paper}/text")
            surface = page.locator(f'#{items[0].id} [data-reading-surface="original"]')
            select(surface, "Repeat phrase")
            toolbar = page.get_by_role("toolbar", name="选文笔记")
            expect(toolbar).to_be_visible()
            assert toolbar.get_by_role("button", name="提问", exact=True).count() == 0
            toolbar.get_by_role("button", name="收藏术语", exact=True).click()
            expect(toolbar.get_by_role("button", name="已收藏", exact=True)).to_be_visible()
            saved = page.evaluate('JSON.parse(localStorage.getItem("paperlens.terms"))')
            assert (
                saved[0]["term"] == "Repeat phrase"
                and saved[0]["itemId"] == items[0].id
                and "Repeat phrase" in saved[0]["quote"]
            )
            toolbar.get_by_role("button", name="笔记", exact=True).click()
            composer = page.get_by_role("dialog", name="写笔记")
            composer.locator("textarea").fill("我的理解需要核对")
            composer.get_by_role("button", name="保存", exact=True).click()
            composer.wait_for(state="hidden")
            assert not result["mock_requests"]
            note_panel = panel()
            card = (
                note_panel.locator(".reading-note-card").filter(has_text="我的理解需要核对").first
            )
            card.get_by_role("button", name="AI 回复", exact=True).click()
            expect(card.get_by_text("正在生成 · 引文尚未核对", exact=True)).to_be_visible()
            assert result["mock_requests"][-1]["reading_items"][0]["item_id"] == items[0].id
            assert "Repeat phrase" in result["mock_requests"][-1]["question"]
            complete_chat()
            expect(
                note_panel.get_by_text("确定性回复：请核对原文 [1]。", exact=True)
            ).to_be_visible()
            card.get_by_role("button", name="AI 回复", exact=True).first.click()
            expect(card.get_by_text("正在生成 · 引文尚未核对", exact=True)).to_be_visible()
            complete_chat(False)
            expect(card.get_by_text("确定性网络失败", exact=True)).to_be_visible()
            assert note_panel.get_by_text("确定性回复：请核对原文 [1]。", exact=True).count() == 1
            assert len(notes()) == 2
            page.reload()
            panel()
            expect(
                page.locator(".reading-notes-panel")
                .get_by_text("我的理解需要核对", exact=True)
                .first
            ).to_be_visible()
            record(
                "Reflowed selection saves term with item/context; ordinary note save sends no AI; explicit reply has source context, loading, saved answer, failure message and reload persistence"
            )

            page.get_by_role("button", name="关闭阅读笔记", exact=True).click()
            page.get_by_role("radio", name="译文", exact=True).click()
            select(page.locator(f'#{items[0].id} [data-reading-surface="translation"]'), "术语😀")
            page.get_by_role("toolbar", name="选文笔记").get_by_role(
                "button", name="收藏术语", exact=True
            ).click()
            assert (
                page.evaluate('JSON.parse(localStorage.getItem("paperlens.terms"))[0].term')
                == "术语😀"
            )
            page.keyboard.press("Escape")
            page.get_by_role("radio", name="原文", exact=True).click()
            select(page.locator(f"#{items[2].id} [data-raw-text]").first, "RawTerm")
            page.locator(".selection-tool").get_by_role(
                "button", name="收藏术语", exact=True
            ).click()
            assert (
                page.evaluate('JSON.parse(localStorage.getItem("paperlens.terms"))[0].blockId')
                == "ui-raw"
            )
            page.reload()
            assert len(page.evaluate('JSON.parse(localStorage.getItem("paperlens.terms"))')) == 3
            record(
                "Translated Unicode selections and original raw-block selections save to the same term library and survive reload"
            )

        if os.environ.get("UI_CHECK_PHASE", "all") in ("all", "extras"):
            page.goto(base + f"/#/read/{paper}/text")
            page.locator(".continuous-reader").wait_for()
            question_id = "b" * 32
            response = context.request.put(
                base + f"/api/library/{paper}/notes/{question_id}",
                data={
                    "item_id": items[0].id,
                    "kind": "question",
                    "text": "旧问题内容",
                    "quote": "Repeat phrase",
                },
            )
            assert response.ok, response.text()
            response = context.request.post(
                base + f"/api/library/{paper}/annotations",
                data={
                    "block_id": raw.id,
                    "start": 0,
                    "end": 7,
                    "text": "RawTerm",
                    "note": "旧勾画笔记",
                },
            )
            assert response.ok, response.text()
            annotation_id = response.json()["id"]
            page.reload()
            note_panel = panel()
            for text, parent in [("旧问题内容", question_id), ("旧勾画笔记", None)]:
                card = note_panel.locator(".reading-note-card").filter(has_text=text).first
                card.get_by_role("button", name="AI 回复", exact=True).click()
                expect(card.get_by_text("正在生成 · 引文尚未核对", exact=True)).to_be_visible()
                complete_chat()
                page.wait_for_function(
                    "async ({paper,text,parent})=>{const r=await fetch(`/api/library/${paper}/notes`);return (await r.json()).notes.some(n=>n.kind==='ai'&&n.question===text&&n.parent_id===parent)}",
                    arg={"paper": paper, "text": text, "parent": parent},
                )
            assert any(n["id"] == question_id and n["text"] == "旧问题内容" for n in notes())
            originals = context.request.get(base + f"/api/library/{paper}/annotations").json()[
                "annotations"
            ]
            assert any(a["id"] == annotation_id and a["note"] == "旧勾画笔记" for a in originals)
            page.get_by_role("button", name="关闭阅读笔记", exact=True).click()
            surface = page.locator(f'#{items[0].id} [data-reading-surface="original"]')
            select(surface, "Consider")
            page.get_by_role("toolbar", name="选文笔记").get_by_role(
                "button", name="高亮", exact=True
            ).click()
            page.wait_for_function(
                "async paper=>{const r=await fetch(`/api/library/${paper}/notes`);return (await r.json()).notes.some(n=>n.mark&&!n.text)}",
                arg=paper,
            )
            mark = next(n for n in notes() if n.get("mark") and not n["text"])
            note_panel = panel()
            card = note_panel.locator(f'[data-note-id="{mark["id"]}"]')
            card.get_by_role("button", name="AI 回复", exact=True).click()
            expect(card.get_by_text("正在生成 · 引文尚未核对", exact=True)).to_be_visible()
            complete_chat()
            page.wait_for_function(
                "async ({paper,parent})=>{const r=await fetch(`/api/library/${paper}/notes`);return (await r.json()).notes.some(n=>n.kind==='ai'&&n.parent_id===parent)}",
                arg={"paper": paper, "parent": mark["id"]},
            )
            record(
                "Legacy question and annotation content preserved; both and an empty highlight receive explicit saved AI replies"
            )
            page.get_by_role("button", name="关闭阅读笔记", exact=True).click()
            select(surface, "Paragraph")
            page.evaluate(
                "()=>{window.__storageSet=Storage.prototype.setItem;Storage.prototype.setItem=function(key,value){if(key==='paperlens.terms')throw Error('fixture quota');return window.__storageSet.call(this,key,value)}}"
            )
            toolbar = page.get_by_role("toolbar", name="选文笔记")
            toolbar.get_by_role("button", name="收藏术语", exact=True).click()
            expect(toolbar.get_by_role("alert")).to_contain_text("术语未能保存")
            page.evaluate("() => { Storage.prototype.setItem = window.__storageSet; }")
            record("Failed term storage shows an actionable error instead of a false saved state")
        if os.environ.get("UI_CHECK_PHASE", "all") in ("all", "budget"):
            page.goto(base + "/#/upload")
            page.locator("input[type=file]").set_input_files(
                {"name": "fixture.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.4\n%%EOF"}
            )
            page.locator(".tier-budget-details").evaluate("el=>el.open=true")
            budget = page.get_by_role("link", name="调整处理预算", exact=True).first
            budget.wait_for()
            budget.scroll_into_view_if_needed()
            page.screenshot(path=str(OUT / "budget-alignment.png"))
            alignment = budget.evaluate(
                """el=>{const parent=el.parentElement,range=document.createRange();range.selectNode(parent.firstChild);const t=range.getBoundingClientRect();range.selectNodeContents(el);const b=range.getBoundingClientRect();return {text_center:t.y+t.height/2,button_center:b.y+b.height/2,delta:Math.abs(t.y+t.height/2-b.y-b.height/2)}}"""
            )
            result["budget_alignment"] = alignment
            assert alignment["delta"] < 2, alignment
            budget.click()
            expect(page.locator('[data-settings-section="tiered"]')).to_be_visible()
            record(
                "Processing budget action aligns with surrounding text and opens its real settings section"
            )
        assert not result["external_requests"] and not result["errors"], result
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
    (OUT / ("results-" + os.environ.get("UI_CHECK_PHASE", "all") + ".json")).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
