"""Verify the v3 reading edition in an actual, fully offline browser."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import traceback
from pathlib import Path

from paperlens.exporting import browser_session
from paperlens.storage import write_json


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def reading_text(item: dict, blocks: dict) -> str:
    if item.get("content") is not None:
        return item["content"]
    if item.get("transcription"):
        return "".join(
            (segment.get("formula") or {}).get("latex") or segment["text"]
            for segment in item["transcription"]["segments"]
        )
    return "".join(
        part.get("join", "")
        + blocks[part["block_id"]]["text"]
        .encode("utf-16-le")[part["start"] * 2 : part["end"] * 2]
        .decode("utf-16-le")
        for part in item["parts"]
    )


def text_of(locator) -> str:
    """Replace rendered mathematics by its TeX, preserving all prose characters."""
    return locator.evaluate(r"""el=>{
      const copy=el.cloneNode(true);
      for(const math of copy.querySelectorAll('math')){
        const tex=math.querySelector('annotation[encoding="application/x-tex"]')?.textContent;
        if(tex==null)throw Error('Rendered mathematics lost its TeX annotation');
        const delimiter=math.closest('.tex-display')?'$$':'$';
        math.replaceWith(document.createTextNode(delimiter+tex+delimiter));
      }
      return copy.textContent;
    }""")


def run(html: Path, output: Path, channel: str) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    result = {
        "status": "running",
        "browser": channel,
        "html_sha256": hashlib.sha256(html.read_bytes()).hexdigest(),
        "checks": [],
        "errors": [],
        "network": [],
    }
    try:
        with browser_session(channel) as browser:
            context = browser.new_context(offline=True, viewport={"width": 1440, "height": 950})
            context.grant_permissions(["clipboard-read", "clipboard-write"])
            page = context.new_page()
            page.on("pageerror", lambda error: result["errors"].append(str(error)))
            page.on(
                "request",
                lambda request: (
                    result["network"].append(request.url)
                    if request.url.startswith(("http:", "https:"))
                    else None
                ),
            )
            page.goto(html.resolve().as_uri() + "#/read/local/text", wait_until="load")
            page.get_by_role("button", name="原文全文", exact=True).click()
            page.locator(".continuous-prose").wait_for()
            data = json.loads(page.locator("#paperlens-data").text_content())
            reading = data["reading"]
            assert reading["version"] == "3", "Use the legacy checker for earlier projections"
            blocks = {block["id"]: block for block in data["document"]["blocks"]}
            main_sections = []
            for item in reading["items"]:
                if item["kind"] != "heading":
                    continue
                label = item["label"] or reading_text(item, blocks)
                number = re.match(r"^(\d+(?:\.\d+)*)\s", label.strip())
                if not number:
                    continue
                depth = min(3, number[1].count(".") + 1)
                assert item["level"] == depth, f"Numbered heading level mismatch: {label}"
                if depth == 1:
                    main_sections.append(number[1])
                    button = page.locator(".reading-outline").get_by_role(
                        "button", name=label, exact=True
                    )
                    assert button.count() == 1 and button.is_visible(), (
                        f"Main section hidden inside an unrelated fold: {label}"
                    )
            if "因子" in data["analysis"]["title"]:
                assert main_sections == ["1", "2", "3", "4"], main_sections
            result["checks"].append(
                "Numbered heading levels match their depth and all main sections are available in the initial outline"
            )
            assert page.locator(".reading-item").count() == len(reading["items"])
            assert page.locator(".reference-card").count() == len(reading["references"])
            assert not page.locator(
                ".reading-reference,.object-transcript,.article-legend,.article-source-note,.transcription-head,.tex-formula-note"
            ).count()
            result["checks"].append(
                "All body/reference items appear once; reader diagnostic/helper rows are absent"
            )

            for span in page.locator("[data-raw-text][data-source-start]").all():
                bid = span.get_attribute("data-block-id")
                start = int(span.get_attribute("data-source-start"))
                text = span.text_content()
                encoded = text.encode("utf-16-le")
                expected = (
                    blocks[bid]["text"]
                    .encode("utf-16-le")[start * 2 : start * 2 + len(encoded)]
                    .decode("utf-16-le")
                )
                assert text == expected, f"Incorrect source coordinates: {bid}:{start}"
            result["checks"].append("Every selectable run equals its immutable UTF-16 source slice")

            semantic = [item for item in reading["items"] if item.get("content") is not None]
            assert semantic, "No structured reading content was exercised"
            math_count = 0
            for item in semantic:
                view = page.locator(f"#{item['id']}")
                content = item["content"]
                if item["kind"] == "formula":
                    if content.strip():
                        math = view.locator(".tex-formula-math math")
                        assert math.count() == 1, f"Formula did not render: {item['id']}"
                        latex = math.locator(
                            'annotation[encoding="application/x-tex"]'
                        ).text_content()
                        assert latex == content, item["id"]
                        math_count += 1
                elif item["kind"] in ("paragraph", "heading", "algorithm"):
                    selector = {
                        "paragraph": ".reading-paragraph",
                        "heading": ".article-section",
                        "algorithm": ".transcription-content",
                    }[item["kind"]]
                    actual = text_of(view.locator(selector).first)
                    assert compact(actual) == compact(content), (item["id"], actual, content)
                    if item["kind"] == "algorithm":
                        assert actual == content, f"Algorithm lines changed: {item['id']}"
                        assert (
                            view.locator(selector).first.evaluate(
                                "el=>getComputedStyle(el).whiteSpace"
                            )
                            == "pre-wrap"
                        )
                    expected_inline = len(
                        re.findall(r"(?<!\\)\$(?!\$)[^$]+(?<!\\)\$(?!\$)", content)
                    )
                    assert view.locator("math").count() >= expected_inline, (
                        f"Inline math missing: {item['id']}"
                    )
                    math_count += expected_inline
                elif item["kind"] in ("figure", "table") and content.strip():
                    actual = text_of(view.locator("figcaption"))
                    # Caption numbering is bold and its separator becomes layout spacing.
                    caption = re.sub(
                        r"^((?:Figure|Fig\.?|图|Table|Tab\.?|表)\s*\d+[a-z]?)\s*[.:：．、]?\s*",
                        r"\1",
                        content,
                        flags=re.I,
                    )
                    assert compact(caption) in compact(actual), (item["id"], actual, content)
                table = item.get("table")
                if table:
                    assert (
                        view.locator(".reading-object > .table-scroll > table > tbody > tr").count()
                        == table["rows"]
                    )
                    rendered_cells = view.locator(
                        ".reading-object > .table-scroll > table > tbody > tr > th,.reading-object > .table-scroll > table > tbody > tr > td"
                    )
                    expected_cells = sorted(
                        table["cells"], key=lambda cell: (cell["row_start"], cell["col_start"])
                    )
                    assert rendered_cells.count() == len(expected_cells), item["id"]
                    for cell, expected in zip(rendered_cells.all(), expected_cells, strict=True):
                        assert compact(text_of(cell)) == compact(expected["text"]), item["id"]
                        assert int(cell.get_attribute("rowspan")) == max(
                            1, expected["row_end"] - expected["row_start"]
                        )
                        assert int(cell.get_attribute("colspan")) == max(
                            1, expected["col_end"] - expected["col_start"]
                        )
            result["checks"].append(
                f"All {len(semantic)} semantic items preserve prose/captions; {math_count} TeX expressions and every structured table cell render"
            )

            for category, items in reading.get("information_items", {}).items():
                for item in items:
                    view = page.locator(f"#article-information #{item['id']}")
                    assert view.count() == 1, (category, item["id"])
                    assert compact(text_of(view.locator("p"))) == compact(item["content"] or ""), (
                        item["id"]
                    )
                    assert not page.locator(f".reading-item#{item['id']}").count()
            result["checks"].append(
                "Structured publication/author/declaration records stay in document end matter and preserve their content"
            )
            info = next(
                (
                    item
                    for items in reading.get("information_items", {}).values()
                    for item in items
                    if item.get("bbox")
                ),
                None,
            )
            if info:
                entry = page.locator(f"#article-information #{info['id']}")
                entry.locator("xpath=ancestor::details[1]").locator("summary").click()
                entry.locator(".source-links button").first.click()
                page.locator(
                    f'.pdf-source-page[data-ready="true"][data-page="{info["page"]}"]'
                ).wait_for(timeout=45000)
                actual = page.locator(".pdf-source-highlight").evaluate(
                    "el=>[el.style.left,el.style.top,el.style.width,el.style.height].map(parseFloat)"
                )
                box = info["bbox"]
                expected = [
                    box["x0"] * 100,
                    box["y0"] * 100,
                    (box["x1"] - box["x0"]) * 100,
                    (box["y1"] - box["y0"]) * 100,
                ]
                assert all(abs(a - b) < 0.001 for a, b in zip(actual, expected, strict=True)), (
                    actual,
                    expected,
                )
                page.screenshot(path=str(output / f"{channel}-edition-metadata-source.png"))
                page.get_by_role("button", name="关闭原始 PDF", exact=True).click()
                result["checks"].append(
                    "Structured end matter opens its own exact original page region"
                )

            refs = {
                str(item.get("reference_number") or item["label"].strip("[] ")): item
                for item in reading["references"]
            }
            for item in semantic:
                if item["kind"] not in ("paragraph", "algorithm", "heading"):
                    continue
                prose = re.sub(r"\$[^$]*\$", "", item["content"])
                expected = set()
                for citation in re.findall(r"\[[\d\s,，–—~～-]+\]", prose):
                    for match in re.finditer(r"(\d+)(?:\s*[–—~～-]\s*(\d+))?", citation):
                        start, end = int(match[1]), int(match[2] or match[1])
                        expected.update(number for number in refs if start <= int(number) <= end)
                buttons = page.locator(f"#{item['id']} [data-cite]")
                actual = set(
                    buttons.evaluate_all(
                        "es=>es.flatMap(e=>(e.dataset.cites||e.dataset.cite).split(' '))"
                    )
                )
                assert expected <= actual, (item["id"], sorted(expected - actual))
                # Each complete range must expose every intermediate existing
                # reference as one preview, rather than linking its endpoints.
                for citation in re.findall(r"\[[\d\s,，–—~～-]+\]", prose):
                    if not re.search(r"[–—~～-]", citation):
                        continue
                    range_expected = set()
                    for match in re.finditer(r"(\d+)(?:\s*[–—~～-]\s*(\d+))?", citation):
                        start, end = int(match[1]), int(match[2] or match[1])
                        range_expected.update(
                            number for number in refs if start <= int(number) <= end
                        )
                    if range_expected:
                        ranges = buttons.filter(has_text=citation)
                        assert ranges.count(), (item["id"], citation)
                        assert (
                            set((ranges.first.get_attribute("data-cites") or "").split())
                            == range_expected
                        ), (item["id"], citation)
            linked = page.locator(".reading-item [data-cite='2']")
            if not linked.count():
                linked = page.locator(".reading-item [data-cite]")
            assert linked.count(), "No real citation interaction exercised"
            number = linked.first.get_attribute("data-cite")
            linked.first.click()
            reference = refs[number]
            page.wait_for_function(
                "id=>document.getElementById(id).getBoundingClientRect().top<innerHeight",
                arg=reference["id"],
            )
            page.get_by_role("button", name="返回阅读处", exact=True).click()
            card = page.locator(f"#{reference['id']}")
            assert card.locator(".reference-actions a").get_attribute("href").startswith("https://")
            card.get_by_role("button", name="复制引用", exact=True).click()
            copied = page.evaluate("navigator.clipboard.readText()").replace("\r\n", "\n")
            assert copied == reading_text(reference, blocks), (reference["id"], copied)
            page.screenshot(path=str(output / f"{channel}-edition-references.png"))
            result["checks"].append(
                "Explicit reference numbers link real citations, return to reading, open lookup and copy the original semantic citation"
            )

            target = next(
                item
                for item in semantic
                if item["kind"] == "paragraph"
                and re.search(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{4,}", item["content"])
            )
            token = re.search(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{4,}", target["content"]).group()
            page.get_by_role("textbox", name="搜索论点和全文").fill(token)
            assert "未找到" not in page.locator(".reader-search-controls").text_content()
            assert page.locator(f"#{target['id']} mark").count()
            assert page.locator(".reading-item").count() == len(reading["items"])
            page.keyboard.press(
                "Escape"
            )  # Close the global search dropdown before reader navigation.
            page.get_by_role("button", name="下一处", exact=True).click()
            page.get_by_role("button", name="清空搜索", exact=True).click()
            page.locator(f"#{target['id']} .reading-paragraph").click()
            actions = page.locator(f"#{target['id']} .para-actions")
            actions.get_by_role("button", name="复制原文", exact=True).click()
            assert (
                page.evaluate("navigator.clipboard.readText()").replace("\r\n", "\n")
                == target["content"]
            )
            actions.get_by_role("button", name=re.compile("^原页")).first.click()
            page.locator(
                f'.pdf-source-page[data-ready="true"][data-page="{target["page"]}"]'
            ).wait_for(timeout=45000)
            assert page.locator(".pdf-source-page canvas").evaluate(
                "c=>{const d=c.getContext('2d').getImageData(0,0,c.width,c.height).data;let n=0;for(let i=0;i<d.length;i+=4)if(d[i]<200&&d[i+1]<200&&d[i+2]<200)n++;return n>100}"
            )
            assert page.locator(".pdf-native-text").inner_text().strip()
            assert page.locator(".pdf-source-highlight").count() == 1
            page.screenshot(path=str(output / f"{channel}-edition-source.png"))
            page.get_by_role("button", name="关闭原始 PDF", exact=True).click()
            result["checks"].append(
                "Semantic search retains context; paragraph copy matches content; original PDF paints pixels/native text and highlights its real region"
            )

            figures = [item for item in reading["items"] if item["kind"] == "figure"]
            if "因子" in data["analysis"]["title"]:
                from check_reading import assert_chart_ownership

                assert_chart_ownership(reading, blocks)
                assert len([item for item in figures if item["page"] == 5]) == 3
                assert page.locator(".object-algorithm").count() == 2
                assert page.locator(".object-table").count() == 1
                assert not any(
                    "基金项目" in text for text in page.locator(".reading-item").all_text_contents()
                )
                result["checks"].append(
                    "The tree/paired charts retain their complete image regions, two algorithms and one original table remain, and funding stays outside body"
                )
            assert page.locator(".object-data tbody tr").count() == len(
                data.get("quantitative_results", [])
            )
            for item in reading["items"]:
                if item.get("asset"):
                    image = page.locator(f"#{item['id']} .object-image img")
                    image.evaluate(
                        "el=>{for(let p=el.parentElement;p;p=p.parentElement)if(p instanceof HTMLDetailsElement){p.dataset.smokeOpen=String(p.open);p.open=true}}"
                    )
                    image.scroll_into_view_if_needed()
                    page.wait_for_function(
                        "id=>document.querySelector('#'+id+' .object-image img')?.naturalWidth>0",
                        arg=item["id"],
                    )
                    image.evaluate(
                        "el=>{for(let p=el.parentElement;p;p=p.parentElement)if(p instanceof HTMLDetailsElement){p.open=p.dataset.smokeOpen==='true';delete p.dataset.smokeOpen}}"
                    )
            result["checks"].append(
                "Every original object image loads and every quantitative record is attached once"
            )
            for kind in ("paragraph", "formula", "algorithm", "table", "figure"):
                item = next((item for item in reading["items"] if item["kind"] == kind), None)
                if item:
                    page.locator(f"#{item['id']}").scroll_into_view_if_needed()
                    page.screenshot(path=str(output / f"{channel}-edition-{kind}.png"))
            page.locator("#article-opening").scroll_into_view_if_needed()
            close_actions = page.get_by_role("button", name="收起段落操作", exact=True)
            if close_actions.count():
                close_actions.click()
            page.screenshot(path=str(output / f"{channel}-edition-opening.png"))
            page.get_by_role("button", name="切换深色主题", exact=True).click()
            page.wait_for_function("document.documentElement.dataset.theme==='dark'")
            page.screenshot(path=str(output / f"{channel}-edition-dark.png"))
            for width in (1366, 390):
                page.set_viewport_size({"width": width, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth<=innerWidth"), width
            page.wait_for_function(
                "document.querySelector('.sidebar').getBoundingClientRect().right<=1"
            )
            page.screenshot(path=str(output / f"{channel}-edition-mobile.png"))
            result["checks"].append("Dark/mobile reading has no horizontal page overflow")
            assert not result["errors"], result["errors"]
            assert not result["network"], result["network"]
            result["status"] = "passed"
    except Exception as error:
        result.update(status="failed", failure=str(error), traceback=traceback.format_exc())
    write_json(output / f"{channel}-reading-edition.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("html", type=Path)
    parser.add_argument(
        "--output", type=Path, default=Path(".cache/research/reader-comparison/browser-edition")
    )
    parser.add_argument("--channel", default="chrome")
    args = parser.parse_args()
    result = run(args.html, args.output, args.channel)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(result["status"] != "passed")
