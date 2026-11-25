"""Exercise real Reader interactions against the isolated, offline frontend fixture.

Build first: cd frontend; npm exec vite build -- --config tests/reader-interactions.config.ts
Run: .venv/Scripts/python.exe tests/ui/check_reader_interactions.py
No service requests or user library mutations are permitted.
"""

from pathlib import Path

from paperlens.exporting import browser_session


def check() -> None:
    fixture = Path(__file__).resolve().parents[2] / "frontend/tests/reader-interactions-dist/tests/reader-interactions.html"
    with browser_session("chrome") as browser:
        context = browser.new_context(offline=True, viewport={"width": 1440, "height": 900})
        context.route("http://**", lambda route: route.abort())
        context.route("https://**", lambda route: route.abort())
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        def located(item_id: str, limit: int = 250) -> None:
            page.wait_for_function("([id, limit]) => { const y = document.getElementById(id)?.getBoundingClientRect().top; return y >= 120 && y < limit; }", arg=[item_id, limit])
        page.goto(fixture.as_uri())
        page.wait_for_selector(".continuous-reader")
        page.get_by_role("textbox", name="搜索论点和全文").fill("Needle")
        page.locator(".search-result").nth(3).click()
        page.wait_for_function("document.querySelector('.reader-search-controls').textContent.startsWith('4/6')")
        located("reading-b4")
        page.get_by_role("button", name="下一处", exact=True).click()
        page.wait_for_function("document.querySelector('.reader-search-controls').textContent.startsWith('5/6')")
        located("reading-b5")
        page.get_by_role("button", name="上一处", exact=True).click()
        page.wait_for_function("document.querySelector('.reader-search-controls').textContent.startsWith('4/6')")
        located("reading-b4")
        # Closing search leaves the query usable; clear only when explicitly requested.
        page.get_by_role("textbox", name="搜索论点和全文").focus()
        page.get_by_role("button", name="关闭搜索结果").click()
        assert page.get_by_role("textbox", name="搜索论点和全文").input_value() == "Needle"
        page.get_by_role("button", name="清除", exact=True).click()

        # The target ref is absent from the DOM until citation activation clears filtering.
        page.get_by_label("搜索参考文献", exact=True).fill("Alpha")
        assert page.locator("#ref2").count() == 0
        citation = page.locator('[data-cite="2"]').first
        citation.scroll_into_view_if_needed()
        citation_position = page.locator(".reader-main").evaluate("e => e.scrollTop")
        citation.click()
        page.wait_for_selector("#ref2")
        assert page.get_by_label("搜索参考文献", exact=True).input_value() == ""
        located("ref2", 900)
        page.get_by_role("button", name="返回原处", exact=True).click()
        page.wait_for_function("top => Math.abs(document.querySelector('.reader-main').scrollTop - top) < 2", arg=citation_position)

        # Viewer opens alone, and consumes shortcut ownership without moving the reader.
        image = page.get_by_role("button", name="放大图片：Figure 1", exact=True)
        image.scroll_into_view_if_needed()
        before = page.locator(".reader-main").evaluate("e => e.scrollTop")
        image.click()
        page.wait_for_selector('[role="dialog"][aria-modal="true"]')
        assert page.locator(".source-inspector").count() == 0
        initial_hash = page.evaluate("location.hash")
        for key in ["1", "3", "j", "k", "Control+k"]:
            page.keyboard.press(key)
        assert page.evaluate("location.hash") == initial_hash
        assert not page.locator(".search-results").count()
        assert abs(page.locator(".reader-main").evaluate("e => e.scrollTop") - before) < 2
        assert page.evaluate("!!document.activeElement.closest('[role=dialog]')")
        page.keyboard.press("Escape")
        page.wait_for_selector(".image-viewer", state="detached")
        assert image.evaluate("e => document.activeElement === e")
        assert abs(page.locator(".reader-main").evaluate("e => e.scrollTop") - before) < 2

        # The explicit source transition closes the viewer and keeps the prose anchor.
        image.click()
        page.get_by_role("dialog").get_by_role("button", name="查看原页", exact=True).click()
        page.wait_for_selector(".source-inspector")
        assert page.locator(".image-viewer").count() == 0
        assert page.locator("#fixture-figure").bounding_box()["y"] < 900
        page.locator(".pdf-source-scroll").focus()
        source_position = page.locator(".reader-main").evaluate("e => e.scrollTop")
        for key in ["j", "k"]:
            page.keyboard.press(key)
        assert page.locator(".reader-main").evaluate("e => e.scrollTop") == source_position
        assert page.locator(".pdf-source-scroll").evaluate("e => document.activeElement === e")
        page.get_by_role("button", name="关闭原始 PDF").click()
        page.wait_for_selector(".source-inspector", state="detached")
        assert page.locator("#fixture-figure").bounding_box()["y"] < 900
        # Independent original-page entry works without ever opening a viewer.
        page.locator("#fixture-figure .object-source-tools button").click()
        page.wait_for_selector(".source-inspector")
        assert page.locator(".image-viewer").count() == 0
        assert not errors, errors
        context.close()
    print("PASS: search selection/next/previous, hidden citation, viewer modal keys/focus, original page, PDF key boundary")


if __name__ == "__main__":
    check()

