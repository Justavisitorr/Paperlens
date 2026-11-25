"""Offline browser regression for targeted settings and memory-only drafts.

Run after the frontend build: python tests/ui/check_settings_navigation.py frontend/dist/index.html
All HTTP requests are intercepted; no local service, model, or real credentials are used.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from paperlens.exporting import browser_session


def check(path: Path, channel: str = "chrome") -> dict:
    html = path.read_text(encoding="utf-8")
    schema = {
        "groups": [
            {
                "id": "translation",
                "title": "阅读整理",
                "fields": [
                    {
                        "key": "PAPERLENS_AUTO_TRANSLATE_ENABLED",
                        "label": "自动翻译",
                        "type": "boolean",
                        "value": False,
                    },
                ],
            },
            {
                "id": "tiered",
                "title": "处理档位",
                "fields": [
                    {
                        "key": "PAPERLENS_VISION_MAX_PAGES",
                        "label": "看图页数",
                        "type": "number",
                        "value": 4,
                        "min": 0,
                        "max": 100,
                    },
                ],
            },
            {
                "id": "auto_notes",
                "title": "自动笔记",
                "fields": [
                    {
                        "key": "PAPERLENS_AUTO_NOTES_ENABLED",
                        "label": "处理后写",
                        "type": "boolean",
                        "value": False,
                    },
                    {
                        "key": "PAPERLENS_AUTO_NOTES_MAX_PARAGRAPHS",
                        "label": "笔记数量",
                        "type": "number",
                        "value": 6,
                        "min": 0,
                        "max": 100,
                    },
                ],
            },
        ],
    }
    profiles = {
        "default": "standard",
        "cost_note": "测试数据",
        "auto_notes": {
            "enabled": False,
            "max_paragraphs": 6,
            "context_scope": "nearby",
            "types": ["summary"],
        },
        "profiles": [
            {
                "id": name,
                "label": label,
                "description": "测试预算",
                "limits": {
                    "max_requests": 10,
                    "input_chars": 1000,
                    "output_tokens": 1000,
                    "reading_output_tokens": 1000,
                    "vision_pages": 4,
                },
            }
            for name, label in (("economy", "经济"), ("standard", "标准"), ("detailed", "精细"))
        ],
    }
    connections = {
        "providers": [
            {
                "id": "deepseek",
                "name": "测试服务商",
                "base_url": "https://fixture.invalid",
                "protocol": "openai",
                "requires_key": True,
            }
        ],
        "accounts": [],
        "routing": {role: None for role in ("main", "reading", "verification", "vision", "chat")},
        "roles": [],
    }
    responses = {
        "/api/health": {
            "ok": True,
            "queued_jobs": 0,
            "running_jobs": 0,
            "features": ["connections", "library", "jobs", "settings"],
        },
        "/api/library": {"papers": []},
        "/api/jobs": {"jobs": []},
        "/api/processing-profiles": profiles,
        "/api/connections": connections,
    }
    with browser_session(channel) as browser:
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()
        deferred = []
        unexpected = []
        profile_reads = 0
        lookup_calls = 0

        def intercept(route):
            nonlocal profile_reads, lookup_calls
            request = route.request
            pathname = urlsplit(request.url).path
            if pathname == "/api/imports/resolve" and request.method == "POST":
                lookup_calls += 1
                route.fulfill(
                    json={
                        "candidates": [
                            {
                                "id": "a" * 32,
                                "title": "设置往返候选",
                                "doi": "10.9999/settings-return",
                                "url": "https://fixture.invalid/settings-return",
                                "pdf_url": "https://fixture.invalid/settings-return.pdf",
                            }
                        ]
                    }
                )
            elif pathname == "/api/estimates" and request.method == "POST":
                # Optional estimates must not block settings navigation or draft retention.
                route.fulfill(status=503, json={"detail": "Offline navigation fixture"})
            elif pathname == "/api/exchange-rates" and request.method == "GET":
                route.fulfill(status=503, json={"detail": "Offline navigation fixture"})
            elif request.method != "GET":
                unexpected.append(f"{request.method} {pathname}")
                route.abort()
            elif pathname == "/":
                route.fulfill(body=html, content_type="text/html")
            elif pathname == "/api/settings":
                deferred.append(route)
            elif pathname == "/api/processing-profiles":
                profile_reads += 1
                route.fulfill(json=profiles)
            elif pathname in responses:
                route.fulfill(json=responses[pathname])
            elif pathname == "/favicon.ico":
                route.fulfill(status=204)
            else:
                unexpected.append(pathname)
                route.abort()

        context.route("**/*", intercept)
        page.goto("http://127.0.0.1:49671/#/upload", wait_until="load")
        page.get_by_role("radio", name="精细", exact=True).click()
        page.get_by_role("tab", name="查找论文", exact=True).click()
        page.get_by_label("论文线索", exact=True).fill("fixture paper")
        page.get_by_role("button", name="查找论文", exact=True).click()
        page.locator('.import-candidate input[type="radio"]').check()
        assert (
            page.get_by_role("radio", name="精细", exact=True).get_attribute("aria-checked")
            == "true"
        )
        page.locator(".tier-budget-details").evaluate("element => element.open = true")
        page.get_by_role("link", name="配置自动笔记", exact=True).click()
        page.wait_for_function("location.hash === '#/settings/notes/auto_notes'")
        page.evaluate("""() => {
          const input=document.createElement('input'); input.id='ui-focus-draft';
          document.querySelector('.settings-panel').prepend(input); input.focus();
        }""")
        page.wait_for_timeout(50)
        assert deferred, "Settings request must still be waiting for the fixture schema"
        deferred.pop().fulfill(json=schema)
        page.locator('[data-settings-section="auto_notes"].settings-target-highlight').wait_for()
        assert page.evaluate("document.activeElement.id") == "ui-focus-draft", (
            "Delayed settings must not take focus from a control the user has started using"
        )
        page.get_by_label("笔记数量", exact=True).fill("11")
        # Simulate an updated catalog while the settings page is open; no real save/model call.
        profiles["auto_notes"].update(enabled=True, max_paragraphs=11)
        profiles["profiles"][2]["limits"]["max_requests"] = 37
        previous_reads = profile_reads
        page.go_back()
        page.locator(".import-candidate.is-selected").wait_for(state="visible")
        assert page.get_by_label("论文线索", exact=True).input_value() == "fixture paper"
        assert page.locator('.import-candidate input[type="radio"]').is_checked()
        assert (
            page.get_by_role("radio", name="精细", exact=True).get_attribute("aria-checked")
            == "true"
        )
        page.locator(".tier-budget-details").evaluate("element => element.open = true")
        page.get_by_text("调用上限 37 次", exact=False).wait_for()
        assert "自动笔记 11 段" in page.locator(".import-candidate-actions").inner_text()
        assert profile_reads > previous_reads, "Returning to upload must refresh processing budgets"
        assert lookup_calls == 1, (
            "Returning from settings must retain the candidate without searching again"
        )
        page.get_by_role("link", name="配置自动笔记", exact=True).click()
        page.get_by_label("笔记数量", exact=True).wait_for(state="visible")
        page.get_by_role("link", name="文献库", exact=True).first.click()
        page.wait_for_function("location.hash === '#/library'")
        assert page.locator(".settings-page").count() == 1, "Drafts require retained mounting"
        page.go_back()
        page.get_by_label("笔记数量", exact=True).wait_for(state="visible")
        assert page.get_by_label("笔记数量", exact=True).input_value() == "11"
        page.go_forward()
        page.wait_for_function("location.hash === '#/library'")
        page.go_back()
        assert page.get_by_label("笔记数量", exact=True).input_value() == "11"

        page.get_by_role("tab", name="模型连接", exact=True).click()
        page.get_by_label("访问密钥", exact=True).fill("fixture-key-not-real")
        page.get_by_label("连接名称", exact=True).fill("未保存的测试连接")
        page.get_by_role("link", name="文献库", exact=True).first.click()
        assert not page.evaluate("document.activeElement.closest('.settings-page') !== null")
        page.get_by_role("link", name="设置", exact=True).click()
        assert page.get_by_label("访问密钥", exact=True).input_value() == "fixture-key-not-real"
        assert page.get_by_label("连接名称", exact=True).input_value() == "未保存的测试连接"
        assert page.evaluate("""() => !Object.values(localStorage).concat(Object.values(sessionStorage))
            .some(value => value.includes('fixture-key-not-real'))""")
        assert page.evaluate("""() => {
            const event = new Event('beforeunload', {cancelable: true});
            window.dispatchEvent(event);
            return event.defaultPrevented;
        }"""), "Refresh still warns about unsaved edits"

        page.get_by_role("link", name="导入论文", exact=True).click()
        page.locator(".tier-budget-details").evaluate("element => element.open = true")
        page.get_by_role("link", name="调整处理预算", exact=True).click()
        page.locator('[data-settings-section="tiered"].settings-target-highlight').wait_for()
        page.get_by_label("看图页数", exact=True).fill("9")
        page.wait_for_timeout(2300)
        page.evaluate("""() => {
            document.documentElement.dataset.motion = 'reduced';
            window.dispatchEvent(new Event('paperlens:settings-target'));
        }""")
        page.locator('[data-settings-section="tiered"].settings-target-highlight').wait_for()
        assert page.get_by_label("看图页数", exact=True).input_value() == "9"
        page.get_by_role("tab", name="自动笔记", exact=True).click()
        assert page.get_by_label("笔记数量", exact=True).input_value() == "11"
        assert not page.evaluate("window.__paperlensErrors"), page.evaluate(
            "window.__paperlensErrors"
        )
        assert not unexpected, unexpected
        context.close()
    return {
        "status": "passed",
        "checks": [
            "specific-settings-links",
            "async-section-reveal",
            "focus-preserved-during-load",
            "selected-tier-and-candidate-retained-after-settings",
            "updated-processing-budget-refreshed-on-return",
            "drafts-retained-back-forward",
            "connection-draft-retained",
            "no-key-in-storage",
            "beforeunload-warning",
            "repeat-highlight",
            "budget-draft-retained",
            "navigation-tolerates-unavailable-estimates-and-exchange-rates",
            "zero-model-or-unintercepted-requests",
            "no-javascript-errors",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("dashboard", type=Path)
    parser.add_argument("--channel", default="chrome")
    args = parser.parse_args()
    print(json.dumps(check(args.dashboard, args.channel), ensure_ascii=False, indent=2))
