"""Exercise the integrated continuous reader, preserving source and offline behavior."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from paperlens.exporting import browser_session
from paperlens.storage import write_json


def metadata_parts(reading: dict, blocks: dict) -> dict:
    """Slice records take precedence; older categories retain whole-block fallback."""
    sliced = reading.get("information_parts") or {}
    information = reading.get("information") or {}
    return {
        key: sliced[key]
        if key in sliced
        else [
            {"block_id": bid, "start": 0, "end": len(blocks[bid]["text"].encode("utf-16-le")) // 2}
            for bid in information.get(key, [])
            if bid in blocks
        ]
        for key in dict.fromkeys([*information, *sliced])
    }


def assert_chart_ownership(reading: dict, blocks: dict) -> None:
    """The tree and three paired charts remain four complete semantic objects."""
    figures = [i for i in reading["items"] if i["kind"] == "figure"]
    region = [i for i in figures if i["page"] in (4, 5)]
    assert len(region) == 4, "Pages 4–5 must contain the tree and Figures 2–4"
    numbered = {}
    for item in region:
        match = re.search(r"(?:\bFig\.?|图)\s*([1-4])\b", item["label"], re.I)
        assert match, f"Missing original figure label: {item['id']}"
        number = int(match[1])
        assert number not in numbered, f"Duplicate semantic Figure {number}"
        numbered[number] = item
    assert set(numbered) == {1, 2, 3, 4}
    for number in (2, 3, 4):
        item = numbered[number]
        pictures = [
            blocks[p["block_id"]]
            for p in item["parts"]
            if blocks[p["block_id"]]["kind"] == "picture"
        ]
        assert len(pictures) >= 2, f"Figure {number} lost one of its paired charts"
        assert item["asset"], f"Figure {number} needs its complete grouped image"
        box = item["bbox"]
        for picture in pictures:
            part_box = picture["bbox"]
            assert box["x0"] <= part_box["x0"] + 0.001 and box["y0"] <= part_box["y0"] + 0.001
            assert box["x1"] >= part_box["x1"] - 0.001 and box["y1"] >= part_box["y1"] - 0.001


def run(html: Path, output: Path, channel: str) -> dict:
    embedded = re.search(
        r'^\s*<script[^>\n]+id="paperlens-data"[^>]*>(.*?)</script>',
        html.read_text("utf-8"),
        re.S | re.M,
    )
    if embedded and (json.loads(embedded[1]).get("reading") or {}).get("version") == "3":
        from check_reading_edition import run as run_edition

        return run_edition(html, output, channel)
    output.mkdir(parents=True, exist_ok=True)
    result = dict(
        status="running",
        browser=channel,
        html_sha256=hashlib.sha256(html.read_bytes()).hexdigest(),
        checks=[],
        errors=[],
        network=[],
    )
    try:
        with browser_session(channel) as browser:
            context = browser.new_context(offline=True, viewport={"width": 1440, "height": 950})
            context.grant_permissions(["clipboard-read", "clipboard-write"])
            page = context.new_page()
            page.on("pageerror", lambda e: result["errors"].append(str(e)))
            page.on(
                "request",
                lambda r: (
                    result["network"].append(r.url)
                    if r.url.startswith(("http:", "https:"))
                    else None
                ),
            )
            page.goto(html.resolve().as_uri() + "#/read/local/text", wait_until="load")
            page.get_by_role("button", name="原文全文", exact=True).click()
            page.locator(".continuous-prose").wait_for()
            data = json.loads(page.locator("#paperlens-data").text_content())
            reading = data["reading"]
            blocks = {b["id"]: b for b in data["document"]["blocks"]}
            assert page.locator(".reading-item").count() == len(reading["items"])
            assert page.locator(".reference-card").count() == len(reading["references"])
            assert not page.locator(".reading-reference").count()
            assert not page.get_by_role("button", name="定量结果", exact=True).count()
            assert page.locator(".reading-outline").count() == 1
            information = metadata_parts(reading, blocks)
            for parts in information.values():
                for part in parts:
                    bid = part["block_id"]
                    # Mixed blocks may contribute disjoint slices to metadata and body.
                    for span in page.locator(
                        f'.reading-item [data-raw-text][data-block-id="{bid}"]'
                    ).all():
                        start = int(span.get_attribute("data-source-start"))
                        end = start + len(span.text_content().encode("utf-16-le")) // 2
                        assert end <= part["start"] or start >= part["end"], bid
            for span in page.locator("[data-raw-text][data-source-start]").all():
                bid = span.get_attribute("data-block-id")
                start = int(span.get_attribute("data-source-start"))
                text = span.text_content()
                expected = (
                    blocks[bid]["text"]
                    .encode("utf-16-le")[start * 2 : start * 2 + len(text.encode("utf-16-le"))]
                    .decode("utf-16-le")
                )
                assert text == expected, bid
            result["checks"].append(
                "Body, bibliography, metadata are separated; every displayed fragment matches original UTF-16 source"
            )
            transcribed = [i for i in reading["items"] if i.get("transcription")]
            for item in transcribed:
                view = page.locator(f"#{item['id']} .reading-transcription")
                assert view.count() == 1, item["id"]
                assert not view.locator("[data-raw-text],[data-source-start]").count()
                content = view.locator(".transcription-content").text_content()
                for segment in item["transcription"]["segments"]:
                    if not segment.get("formula"):
                        assert segment["text"] in content, item["id"]
                # Copy is now a shared paragraph action, also for old reports.
                view.locator(".transcription-content").click()
                page.locator(f"#{item['id']} .para-actions").get_by_role(
                    "button", name="复制原文", exact=True
                ).click()
                # Windows clipboards hand back CRLF.
                copied = page.evaluate("navigator.clipboard.readText()").replace("\r\n", "\n")
                expected = "".join(
                    (s.get("formula") or {}).get("latex") or s["text"]
                    for s in item["transcription"]["segments"]
                )
                assert copied == expected, item["id"]
            for item in reading["items"]:
                if item["kind"] in ("algorithm", "formula"):
                    view = page.locator(f"#{item['id']}")
                    assert not view.locator(".object-transcript").count()
                    assert not view.locator("[data-raw-text],[data-source-start]").count()
            if transcribed:
                target = transcribed[0]
                token = next(
                    (
                        m.group()
                        for s in target["transcription"]["segments"]
                        if not s.get("formula")
                        if (m := re.search(r"[A-Za-z]{3,}|[\u4e00-\u9fff]{2,}", s["text"]))
                    ),
                    None,
                )
                if token:
                    page.get_by_role("textbox", name="搜索论点和全文").fill(token)
                    controls = page.locator(".reader-search-controls")
                    controls.wait_for()
                    assert "未找到" not in controls.text_content()
                    assert page.locator(f"#{target['id']} .transcription-content mark").count()
                    page.get_by_role("button", name="清空搜索", exact=True).click()
                result["checks"].append(
                    "Recognition transcription preserves literal text and newlines, copies text/LaTeX, is searchable and never uses old selection offsets"
                )
            result["checks"].append(
                "Formula and algorithm diagnostics stay out of reading; their rendered content cannot create misleading source selections"
            )
            n = page.locator(".reading-item").count()
            query = "模型" if "因子" in data["analysis"]["title"] else "BERT"
            page.get_by_role("textbox", name="搜索论点和全文").fill(query)
            page.locator(".reader-search-controls").wait_for()
            assert page.locator(".reading-item").count() == n
            assert page.locator(".continuous-prose mark").count() > 0
            page.keyboard.press("Escape")
            page.get_by_role("button", name="下一处", exact=True).click()
            page.get_by_role("button", name="清空搜索", exact=True).click()
            result["checks"].append("Search highlights and jumps while keeping full context")
            folds = page.locator(".reading-outline .toc-fold")
            if folds.count():
                button = folds.first
                before = button.get_attribute("aria-expanded")
                button.click()
                assert button.get_attribute("aria-expanded") != before
                button.click()
            result["checks"].append("Contextual section outline expands and collapses")
            for i in reading["items"]:
                if i["asset"]:
                    img = page.locator(f"#{i['id']} .object-image img")
                    img.evaluate(
                        "el=>{for(let p=el.parentElement;p;p=p.parentElement)if(p instanceof HTMLDetailsElement)p.open=true}"
                    )
                    img.scroll_into_view_if_needed()
                    page.wait_for_function(
                        "id=>document.querySelector('#'+id+' .object-image img')?.naturalWidth>0",
                        arg=i["id"],
                    )
            assert page.locator(".object-data tbody tr").count() == len(
                data.get("quantitative_results", [])
            )
            result["checks"].append(
                "Every embedded object image loads; all quantitative records appear once beside the article"
            )
            numbered = next(
                (
                    r
                    for r in reading["references"]
                    if r["label"]
                    and page.locator(".reading-paragraph .citation-link")
                    .filter(has_text=r["label"])
                    .count()
                ),
                None,
            )
            if numbered:
                page.locator(".reading-paragraph .citation-link").filter(
                    has_text=numbered["label"]
                ).first.click()
                page.get_by_role("button", name="返回阅读处", exact=True).click()
                assert (
                    page.locator(f"#{numbered['id']} .reference-actions a")
                    .get_attribute("href")
                    .startswith("https://")
                )
            if "因子" in data["analysis"]["title"]:
                ref = next(r for r in reading["references"] if r["label"] == "[4]")
                text = page.locator(f"#{ref['id']} .reference-citation").text_content()
                assert "international conference" in text and "internation al" not in text
                assert_chart_ownership(reading, blocks)
                assert page.locator(".reading-figure .object-image img").count() == len(
                    [i for i in reading["items"] if i["kind"] == "figure" and i["asset"]]
                )
                assert (
                    len([i for i in reading["items"] if i["kind"] == "figure" and i["page"] == 5])
                    == 3
                )
                assert page.locator(".object-algorithm").count() == 2
                assert page.locator(".object-table").count() == 1
                assert any(i["kind"] == "heading" and i["level"] == 3 for i in reading["items"])
                assert not any(
                    "基金项目" in t for t in page.locator(".reading-item").all_text_contents()
                )
                result["checks"].append(
                    "Tree and Figures 2–4 are four complete objects; the three paired charts have three grouped images, with later figures retained"
                )
            result["checks"].append(
                "Reference index, external lookup and citation return work; cross-page reference remains continuous"
            )
            page.locator("#article-references").scroll_into_view_if_needed()
            page.locator(".reference-card").first.get_by_role(
                "button", name="复制引用", exact=True
            ).click()
            copied = page.evaluate("navigator.clipboard.readText()")
            assert (
                copied == page.locator(".reference-card .reference-citation").first.text_content()
            )
            page.screenshot(path=str(output / f"{channel}-references.png"))
            ref_search = page.locator(".endnotes-head input")
            ref_search.fill("__no_such_reference__")
            assert page.locator(".reference-card").count() == 0
            page.locator(".reference-index button").first.click()
            assert page.locator(".reference-card").count() == len(reading["references"])
            page.locator("#article-opening").scroll_into_view_if_needed()
            page.screenshot(path=str(output / f"{channel}-opening.png"))
            # A sliced footer must highlight its own coordinates rather than the mixed block.
            sliced = next(
                (
                    p
                    for parts in information.values()
                    for p in parts
                    if p.get("bbox")
                    and (
                        p["start"]
                        or p["end"] < len(blocks[p["block_id"]]["text"].encode("utf-16-le")) // 2
                    )
                ),
                None,
            )
            if sliced:
                fragment = page.locator(
                    f'#article-information [data-block-id="{sliced["block_id"]}"][data-source-start="{sliced["start"]}"]'
                ).first
                details = fragment.locator("xpath=ancestor::details[1]")
                details.locator("summary").click()
                fragment.locator("xpath=../..").get_by_role("button").click()
                page.locator('.pdf-source-page[data-ready="true"]').wait_for(timeout=45000)
                style = page.locator(".pdf-source-highlight").evaluate(
                    "el=>[el.style.left,el.style.top,el.style.width,el.style.height].map(parseFloat)"
                )
                box = sliced["bbox"]
                expected = [
                    box["x0"] * 100,
                    box["y0"] * 100,
                    (box["x1"] - box["x0"]) * 100,
                    (box["y1"] - box["y0"]) * 100,
                ]
                assert all(abs(a - b) < 0.001 for a, b in zip(style, expected, strict=True)), (
                    style,
                    expected,
                )
                page.screenshot(path=str(output / f"{channel}-metadata-source.png"))
                page.get_by_role("button", name="关闭原始 PDF", exact=True).click()
                result["checks"].append(
                    "Mixed footer metadata opens the exact sliced highlight on its original page"
                )
            # Test the actual PDF mounted inside the production reader, not only a fixture.
            target = next(i for i in reading["items"] if i["kind"] == "figure")
            # A tap on the object opens its action bar, whose first action is its page.
            page.locator(f"#{target['id']} figcaption").first.click()
            page.locator(f"#{target['id']} .para-actions").get_by_role(
                "button", name=re.compile("^原页")
            ).first.click()
            page.locator(
                f'.pdf-source-page[data-ready="true"][data-page="{target["page"]}"]'
            ).wait_for(timeout=45000)
            canvas = page.locator(".pdf-source-page canvas")
            assert canvas.evaluate(
                "c=>{const d=c.getContext('2d').getImageData(0,0,c.width,c.height).data;let n=0;for(let i=0;i<d.length;i+=4)if(d[i]<200&&d[i+1]<200&&d[i+2]<200)n++;return n>100}"
            )
            assert page.locator(".pdf-native-text").inner_text().strip()
            assert page.locator(".pdf-source-highlight").count() == 1
            page.get_by_role("button", name="放大 PDF", exact=True).click()
            page.locator('.pdf-source-page[data-ready="true"]').wait_for()
            page.screenshot(path=str(output / f"{channel}-source.png"))
            page.get_by_role("button", name="关闭原始 PDF", exact=True).click()
            result["checks"].append(
                "Integrated source PDF paints actual pixels and selectable native text at the right page and highlighted object"
            )
            page.get_by_role("button", name="切换深色主题", exact=True).click()
            page.wait_for_function("document.documentElement.dataset.theme==='dark'")
            page.locator("#article-opening").scroll_into_view_if_needed()
            page.screenshot(path=str(output / f"{channel}-dark.png"))
            for width in (1366, 390):
                page.set_viewport_size({"width": width, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth<=innerWidth"), width
            page.wait_for_function(
                "document.querySelector('.sidebar').getBoundingClientRect().right <= 1"
            )
            page.screenshot(path=str(output / f"{channel}-mobile.png"))
            result["checks"].append("Dark and mobile layouts have no horizontal overflow")
            assert not result["errors"], result["errors"]
            assert not result["network"], result["network"]
            result["status"] = "passed"
    except Exception as e:
        result.update(status="failed", failure=str(e))
    write_json(output / f"{channel}-reading.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("html", type=Path)
    parser.add_argument("--output", type=Path, default=Path("docs/validation/round3/browser"))
    parser.add_argument("--channel", default="chrome")
    args = parser.parse_args()
    result = run(args.html, args.output, args.channel)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(result["status"] != "passed")
