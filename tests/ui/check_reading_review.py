"""Deterministic offline first-read overview, node navigation and experiment conditions.

Build: cd frontend; npm exec vite build -- --config tests/reading-review.config.ts
Run with PYTHONPATH=src: python tests/ui/check_reading_review.py
"""
from pathlib import Path

from paperlens.exporting import browser_session


def check() -> None:
    root = Path(__file__).resolve().parents[2]
    fixture = root / "frontend/tests/reading-review-dist/tests/reading-review.html"
    screenshots = root / ".tmp/ui-review"
    screenshots.mkdir(parents=True, exist_ok=True)
    with browser_session("chrome") as browser:
        for width in (1440, 390):
            context = browser.new_context(offline=True, viewport={"width": width, "height": 900})
            context.route("http://**", lambda route: route.abort())
            context.route("https://**", lambda route: route.abort())
            page = context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda error, error_log=errors: error_log.append(str(error)))
            page.goto(fixture.as_uri() + "#/read/fixture")
            page.wait_for_selector('.reading-overview')
            assert "Existing one-sentence summary" in page.locator('.reading-overview').inner_text()
            assert "Existing contribution" in page.locator('.reading-overview').inner_text()
            assert "Existing limitation" in page.locator('.reading-overview').inner_text()
            page.screenshot(path=str(screenshots / f"overview-{width}.png"), full_page=True)
            page.get_by_role("button", name="进入论证图", exact=True).click()
            page.wait_for_selector('.map-stage')
            assert page.get_by_role("tab", name="Argument", exact=True).get_attribute("aria-selected") == "true"
            page.get_by_role("button", name="阅读总览", exact=True).click()
            assert page.locator('.review-checklist').count() == 0
            page.locator('.reading-overview button[data-node-id="n2"]').click()
            page.wait_for_selector('.node-details')
            assert page.locator('.node-details h2').inner_text() == "Limited conclusion"
            assert "partial" in page.locator('.node-details .support-tag').get_attribute('class')
            assert "这不代表论文未说明" in page.locator('.node-details .experiment-conditions').inner_text()
            assert page.locator('.node-details [data-evidence-id="e1"][data-block-id="b1"]').count() >= 1
            page.goto(fixture.as_uri() + "#/read/fixture/text")
            page.wait_for_selector('.continuous-reader')
            assert page.locator('.reading-overview').count() == 0
            page.locator('.object-data > summary').click()
            page.locator('.experiment-conditions [data-evidence-id="e1"]').click()
            page.wait_for_selector('.source-inspector')
            page.goto(fixture.as_uri() + "#/read/fixture/map")
            page.wait_for_selector('.map-stage')
            assert page.locator('.reading-overview').count() == 0
            page.goto(fixture.as_uri() + "#/read/fixture")
            page.wait_for_selector('.reading-overview')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            assert not errors, errors
            context.close()
    print("PASS: desktop/mobile overview, explicit deep links, overview node navigation, unchanged support, condition citations and source entry")


if __name__ == "__main__":
    check()
