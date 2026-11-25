"""Browser checks against the Vite-only upload harness; no builds or model calls.

Start frontend Vite on a spare local port, then pass its base URL. All API requests
are handled by the harness with controlled promises, including aborted responses.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from paperlens.exporting import browser_session


def check(base_url: str, screenshot: Path | None = None) -> dict:
    with browser_session("chrome") as browser:
        page = browser.new_page(viewport={"width": 1120, "height": 1100})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base_url.rstrip("/") + "/tests/upload-estimate.html")

        def wait_requests(count):
            page.wait_for_function("count => window.uploadEstimateCheck?.requests().length === count", arg=count)

        def finish(index, seconds, pages=8, failed=False):
            page.evaluate("args => window.uploadEstimateCheck.finish(...args)", [index, seconds, pages, failed])

        def quote(minutes):
            page.get_by_role("region", name="时间与费用估算").get_by_text(f"约 {minutes} 分钟", exact=True).wait_for()

        wait_requests(1)
        assert page.locator(".drop-zone-compact").count() == 1
        assert page.get_by_label("待处理文件").inner_text().startswith("alpha.pdf")
        finish(0, 120)
        quote(2)
        assert "8 页" in page.get_by_label("待处理文件").inner_text()
        positions = page.evaluate("""() => ['.staged','.drop-zone','.tier-picker','.staged-actions'].map(s => document.querySelector(s).getBoundingClientRect().top)""")
        assert positions == sorted(positions), positions

        # Mode refresh retains this file's quote and explicitly marks it stale.
        page.get_by_role("button", name="标准档加速", exact=True).click()
        wait_requests(2)
        quote(2)
        assert "更新" in page.locator(".estimate-card").inner_text()
        assert page.locator(".estimate-card").get_attribute("aria-busy") == "true"
        # Start another mode request before the earlier one finishes.
        page.get_by_role("button", name="标准档加速", exact=True).click()
        wait_requests(3)
        requests = page.evaluate("window.uploadEstimateCheck.requests()")
        assert requests[1]["aborted"] and requests[2]["mode"] == "ultra"
        finish(2, 60)
        quote(1)
        finish(1, 540)  # Ignored even if a fetch implementation returns after abort.
        page.wait_for_timeout(50)
        quote(1)
        assert "约 9 分钟" not in page.locator(".estimate-card").inner_text()

        # Returning from settings refreshes without presenting the old quote as current.
        page.get_by_role("button", name="离开上传页", exact=True).click()
        page.get_by_role("button", name="返回上传页", exact=True).click()
        wait_requests(4)
        quote(1)
        assert "更新" in page.locator(".estimate-card").inner_text()
        finish(3, 0, failed=True)
        page.get_by_text("更新失败 · 上次估算", exact=True).wait_for()
        quote(1)
        assert page.locator(".estimate-card").get_attribute("aria-busy") == "false"
        retry = page.get_by_role("button", name="重新估算", exact=True)
        assert retry.is_enabled()
        retry.click()
        wait_requests(5)
        quote(1)
        assert retry.is_disabled()
        assert page.locator(".estimate-card").get_attribute("aria-busy") == "true"
        finish(4, 120)
        quote(2)
        assert retry.is_enabled()
        assert page.evaluate("window.uploadEstimateCheck.requests()[4].name") == "pages:8"

        # A different document must never receive alpha's quote, even on failure.
        page.get_by_role("button", name="选择 B", exact=True).click()
        wait_requests(6)
        assert page.locator(".estimate-time strong").first.inner_text() == "—"
        assert "8 页" not in page.get_by_label("待处理文件").inner_text()
        finish(5, 0, failed=True)
        page.get_by_text("暂时无法估算时间与费用", exact=True).wait_for()
        assert page.locator(".estimate-time strong").first.inner_text() == "—"
        assert retry.is_enabled()
        retry.click()
        wait_requests(7)
        assert retry.is_disabled()
        assert page.locator(".estimate-time strong").first.inner_text() == "—"
        assert page.evaluate("window.uploadEstimateCheck.requests()[6].name") == "beta.pdf"
        finish(6, 180, pages=11)
        quote(3)
        assert retry.is_enabled()
        assert "11 页" in page.get_by_label("待处理文件").inner_text()
        assert "alpha.pdf" not in page.get_by_label("待处理文件").inner_text()
        if screenshot:
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot), full_page=True)
        assert not errors, errors
        page.close()
    return {"status": "passed", "checks": ["same-file-refresh-retains-marked-quote", "aborted-out-of-order-response-ignored",
            "settings-return-refreshes", "failure-stops-loading-and-keeps-old-same-file-quote",
            "same-file-failure-retry-recovers-with-metadata", "new-file-failure-retry-recovers-without-old-quote",
            "new-file-never-gets-old-quote", "known-page-counts-without-extra-requests", "compact-file-area-before-options", "zero-model-calls"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    print(json.dumps(check(args.base_url, args.screenshot), ensure_ascii=False, indent=2))
