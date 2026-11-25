"""Regression checks for real SVG interaction and browser image export.

Run with a generated dashboard, without a server or network:
  python tests/ui/check_frontend.py outputs/.../dashboard.html
These checks exercise the rendered browser, not just Mermaid syntax.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from paperlens.exporting import browser_session


def check(path: Path, channel: str) -> dict:
    with browser_session(channel) as browser:
        context = browser.new_context(
            offline=True, accept_downloads=True, viewport={"width": 1440, "height": 900}
        )
        page = context.new_page()
        requests: list[str] = []
        page.on(
            "request",
            lambda request: (
                requests.append(request.url)
                if request.url.startswith(("https://", "http://"))
                else None
            ),
        )
        page.goto(path.resolve().as_uri(), wait_until="load")
        page.wait_for_function("window.__paperlensReady === true")
        assert not page.evaluate("window.__paperlensErrors")
        assert page.locator(".graph-drawing foreignObject").count() == 0, (
            "Pure SVG labels required for PNG"
        )
        nodes = page.locator(".graph-drawing g.node[data-node-id]")
        assert nodes.count() == page.locator(".graph-drawing g.node").count(), (
            "Every node must be interactive"
        )
        title = nodes.first.get_attribute("aria-label")
        nodes.first.click()
        assert page.locator(".node-details h2").inner_text() == title
        nodes.last.focus()
        title = nodes.last.get_attribute("aria-label")
        page.keyboard.press("Enter")
        assert page.locator(".node-details h2").inner_text() == title

        page.locator(".export-button").click()
        with page.expect_download(timeout=15000) as download:
            page.locator(".export-menu button").filter(has_text="当前导图图片").click()
        png = Path(download.value.path())
        assert png.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"), "Download must be a real PNG"

        page.get_by_role("button", name="切换深色主题", exact=True).click()
        page.wait_for_function(
            "window.__paperlensReady === true && document.documentElement.dataset.theme === 'dark'"
        )
        assert page.locator(".graph-drawing foreignObject").count() == 0
        assert not requests, requests
        assert not page.evaluate("window.__paperlensErrors")
        result = {
            "status": "passed",
            "browser": channel,
            "checks": [
                "pure-svg-labels",
                "all-nodes-clickable",
                "keyboard-node-activation",
                "valid-offline-png-download",
                "theme-rerender",
                "zero-network-requests",
            ],
        }
        context.close()
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("--channel", default="chrome")
    args = parser.parse_args()
    print(json.dumps(check(args.report, args.channel), ensure_ascii=False, indent=2))
