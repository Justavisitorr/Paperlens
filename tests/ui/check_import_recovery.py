"""Real-browser checks for duplicate imports and durable conversation undo.

Run after the final frontend build:
  .venv/Scripts/python.exe tests/ui/check_import_recovery.py frontend/dist/index.html
Every request is intercepted by fixtures, including all writes. No real service/model is used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from paperlens.exporting import browser_session

STAMP = "2026-10-08T12:00:00Z"
A, B = "a" * 32, "b" * 32
PDF = "%PDF-1.4 same-content-fixture"
EXISTING_PDF = "%PDF-1.4 already-read-fixture"


def conversation(identity: str, title: str) -> dict:
    return {
        "id": identity,
        "title": title,
        "created_at": STAMP,
        "updated_at": STAMP,
        "pinned": False,
        "refs": [],
        "messages": [
            {
                "id": identity,
                "role": "user",
                "content": "测试问题",
                "created_at": STAMP,
                "refs": [],
                "reply": None,
                "error": None,
            }
        ],
    }


class Workspace:
    def __init__(self, html: str):
        self.html = html
        self.docs = {A: conversation(A, "对话 A"), B: conversation(B, "对话 B")}
        self.jobs: list[dict] = []
        self.uploads = 0
        self.imports = 0
        self.searches = 0
        self.restores = 0
        self.fail_restore = False
        self.hold_save = False
        self.pending_saves = []
        self.unexpected = []
        self.events = []
        self.papers = [
            {
                "id": "existing-paper",
                "title": "已有论文",
                "source_name": "existing.pdf",
                "document_sha256": hashlib.sha256(EXISTING_PDF.encode()).hexdigest(),
                "source_url": "https://fixture.invalid/existing",
                "doi": "10.9999/existing",
                "pages": 1,
                "node_count": 1,
                "evidence_count": 1,
                "status": "complete",
                "created_at": STAMP,
            }
        ]

    def add_job(self, identity: str):
        self.jobs.append(
            {
                "id": identity,
                "source_name": "fixture.pdf",
                "status": "queued",
                "stage": "queued",
                "message": "测试队列",
                "created_at": STAMP,
                "actions": ["cancel"],
                "processing_profile": "standard",
                "profile_label": "标准",
            }
        )

    def intercept(self, route):
        request = route.request
        path, method = urlsplit(request.url).path, request.method
        if path == "/" and method == "GET":
            route.fulfill(body=self.html, content_type="text/html")
        elif path == "/api/health":
            route.fulfill(
                json={
                    "ok": True,
                    "queued_jobs": len(self.jobs),
                    "running_jobs": 0,
                    "features": ["chat", "conversations", "library", "jobs"],
                }
            )
        elif path == "/api/library" and method == "GET":
            route.fulfill(json={"papers": self.papers})
        elif path == "/api/jobs" and method == "GET":
            route.fulfill(json={"jobs": self.jobs})
        elif path == "/api/jobs" and method == "POST":
            self.uploads += 1
            identity = f"{self.uploads:032x}"
            self.add_job(identity)
            self.jobs[-1]["source_sha256"] = hashlib.sha256(PDF.encode()).hexdigest()
            route.fulfill(status=202, json={"id": identity})
        elif path == "/api/processing-profiles":
            route.fulfill(
                json={
                    "default": "standard",
                    "cost_note": "仅测试数据",
                    "profiles": [
                        {
                            "id": name,
                            "label": name,
                            "description": "测试预算",
                            "limits": {
                                "max_requests": 10,
                                "input_chars": 1000,
                                "output_tokens": 1000,
                                "reading_output_tokens": 1000,
                                "vision_pages": 0,
                            },
                        }
                        for name in ("economy", "standard", "detailed")
                    ],
                }
            )
        elif path == "/api/imports/resolve" and method == "POST":
            self.searches += 1
            existing = "existing" in request.post_data_json["query"]
            route.fulfill(
                json={
                    "candidates": [
                        {
                            "id": f"{self.searches + 100:032x}",
                            "title": "已有论文" if existing else "线上论文",
                            "doi": "10.9999/existing" if existing else "10.9999/online",
                            "url": "https://fixture.invalid/existing"
                            if existing
                            else "https://fixture.invalid/online",
                            "pdf_url": "https://fixture.invalid/source.pdf",
                        }
                    ]
                }
            )
        elif path == "/api/imports" and method == "POST":
            self.imports += 1
            identity = f"{self.imports + 1000:032x}"
            self.add_job(identity)
            route.fulfill(status=202, json={"id": identity})
        elif path == "/api/conversations" and method == "GET":
            route.fulfill(
                json={
                    "conversations": [
                        {
                            **{
                                key: doc[key]
                                for key in ("id", "title", "created_at", "updated_at", "pinned")
                            },
                            "paper_ids": [],
                            "message_count": len(doc["messages"]),
                            "preview": "测试问题",
                        }
                        for doc in self.docs.values()
                    ]
                }
            )
        elif path.startswith("/api/conversations/"):
            identity = path.rsplit("/", 1)[-1]
            if method == "GET":
                route.fulfill(json=self.docs.get(identity, {}))
            elif method == "DELETE":
                self.events.append(("delete", identity))
                self.docs.pop(identity, None)
                route.fulfill(json={"deleted": True})
            elif method == "PUT":
                if self.hold_save:
                    self.pending_saves.append(route)
                elif self.fail_restore:
                    self.fail_restore = False
                    route.fulfill(status=503, json={"detail": "测试恢复失败"})
                else:
                    self.save(route)
            else:
                self.unexpected.append(f"{method} {path}")
                route.abort()
        elif path == "/api/settings":
            route.fulfill(json={"groups": []})
        elif path.endswith("/report"):
            # This case checks the existing-result destination, not its report renderer.
            route.fulfill(status=503, json={"detail": "测试报告未提供"})
        elif path.endswith("/thumbnail") or path == "/favicon.ico":
            route.fulfill(status=204)
        else:
            self.unexpected.append(f"{method} {path}")
            route.abort()

    def save(self, route):
        doc = route.request.post_data_json
        self.events.append(("save", doc["id"]))
        self.docs[doc["id"]] = doc
        route.fulfill(json={"id": doc["id"], "updated_at": STAMP})


def stage(page, names: list[str], content: str = PDF):
    page.evaluate(
        """({names, content}) => {
        const files = new DataTransfer();
        names.forEach(name => files.items.add(new File([content], name,
            {type: 'application/pdf', lastModified: 12345})));
        const input = document.querySelector('input[type=file]');
        input.files = files.files;
        input.dispatchEvent(new Event('change', {bubbles: true}));
    }""",
        {"names": names, "content": content},
    )


def search(page, query: str):
    page.get_by_label("论文线索", exact=True).fill(query)
    page.get_by_role("button", name="查找论文", exact=True).click()
    page.locator('.import-candidate input[type="radio"]').first.check()


def check(path: Path, channel: str = "chrome", save_race: bool = True) -> dict:
    workspace = Workspace(path.read_text(encoding="utf-8"))
    checks = []
    with browser_session(channel) as browser:
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        context.route("**/*", workspace.intercept)
        page = context.new_page()
        confirmations = []

        def cancel_confirmation(dialog):
            confirmations.append(dialog.message)
            dialog.dismiss()

        page.on("dialog", cancel_confirmation)
        page.goto("http://127.0.0.1:49672/#/upload", wait_until="load")
        page.get_by_role("tab", name="上传文件").wait_for()
        stage(page, ["a.pdf", "a.pdf", "renamed.pdf"])
        assert page.locator(".staged li").count() == 2
        page.locator(".staged-actions").get_by_role("button", name="开始处理").click()
        page.locator(".staged").wait_for(state="detached")
        assert workspace.uploads == 1, "Renamed byte-identical copies must create only one task"
        stage(page, ["third-name.pdf"])
        page.locator(".staged-actions").get_by_role("button", name="开始处理").click()
        page.locator(".staged").wait_for(state="detached")
        assert workspace.uploads == 1
        checks += [
            "staged-duplicate-ignored",
            "renamed-content-submitted-once",
            "active-task-reused",
        ]
        page.reload(wait_until="load")
        page.get_by_role("tab", name="上传文件").wait_for()
        stage(page, ["after-reload.pdf"])
        page.locator(".staged-actions").get_by_role("button", name="开始处理").click()
        page.locator(".staged").wait_for(state="detached")
        assert workspace.uploads == 1, "Fresh App state must reuse an active job by source SHA"
        checks.append("active-source-sha-reused-after-reload")
        stage(page, ["existing.pdf"], EXISTING_PDF)
        page.locator(".staged-actions").get_by_role("button", name="开始处理").click()
        page.locator(".staged").wait_for(state="detached")
        assert workspace.uploads == 1
        assert "已有阅读结果" in confirmations[-1] and "模型调用" in confirmations[-1]
        checks.append("existing-library-sha-confirmation")

        page.get_by_role("tab", name="查找论文", exact=True).click()
        search(page, "online")
        first = page.locator('.import-candidate input[type="radio"]').get_attribute("value")
        page.locator(".import-candidate-actions").get_by_role(
            "button", name="开始处理", exact=True
        ).click()
        page.get_by_role("button", name="查看已有任务", exact=True).wait_for()
        search(page, "online again")
        assert page.locator('.import-candidate input[type="radio"]').get_attribute("value") != first
        assert page.get_by_role("button", name="查看已有任务", exact=True).is_visible()
        assert (
            page.locator(".import-candidate-actions")
            .get_by_role("button", name="开始处理", exact=True)
            .count()
            == 0
        )
        assert workspace.imports == 1
        checks.append("changed-candidate-id-reuses-stable-source")
        page.locator('.side-nav a[href="#/library"]').click()
        page.get_by_role("button", name="查看《已有论文》详情", exact=True).click()
        page.get_by_role("button", name="删除论文", exact=True).click()
        assert all(text in confirmations[-1] for text in ("生成的解析结果", "无法撤销", "PDF 保留"))
        assert len(workspace.papers) == 1
        checks.append("single-delete-accurate-scope-cancel-safe")
        page.locator('.side-nav a[href="#/upload"]').click()
        page.get_by_role("tab", name="查找论文", exact=True).click()
        search(page, "online after navigation")
        assert page.get_by_role("button", name="查看已有任务", exact=True).is_visible()
        assert workspace.imports == 1
        checks.append("import-receipt-survives-navigation")
        search(page, "existing")
        page.get_by_role("button", name="打开已有结果", exact=True).click()
        page.wait_for_function("location.hash === '#/read/existing-paper/text'")
        assert workspace.imports == 1
        checks.append("existing-result-opens-correct-paper")

        page.locator('.side-nav a[href="#/chat"]').click()
        page.get_by_role("button", name="删除对话 对话 A", exact=True).click()
        page.get_by_role("button", name="恢复对话 对话 A", exact=True).wait_for()
        page.get_by_role("button", name="删除对话 对话 B", exact=True).click()
        page.get_by_role("button", name="恢复对话 对话 B", exact=True).wait_for()
        assert not workspace.docs
        page.locator('.side-nav a[href="#/library"]').click()
        page.locator('.side-nav a[href="#/chat"]').click()
        assert page.locator(".chat-deleted .undo-bar").count() == 2
        page.reload(wait_until="load")
        page.get_by_role("button", name="恢复对话 对话 A", exact=True).wait_for()
        assert page.locator(".chat-deleted .undo-bar").count() == 2
        checks.append("deleted-conversations-survive-reload")
        workspace.fail_restore = True
        page.get_by_role("button", name="恢复对话 对话 A", exact=True).click()
        page.get_by_text("恢复未完成，可重试", exact=False).wait_for()
        assert page.locator(".chat-deleted .undo-bar").count() == 2
        assert not workspace.docs
        page.get_by_role("button", name="恢复对话 对话 A", exact=True).click()
        page.get_by_role("button", name="恢复对话 对话 A", exact=True).wait_for(state="detached")
        page.get_by_role("button", name="恢复对话 对话 B", exact=True).click()
        page.locator(".chat-deleted").wait_for(state="detached")
        assert set(workspace.docs) == {A, B}
        checks += [
            "multiple-deletions-survive-navigation",
            "failed-restore-retains-undo",
            "all-deleted-conversations-restored",
        ]

        if save_race:
            page.locator(f'.chat-row a[href="#/chat/{A}"]').click()
            page.get_by_label("对话标题", exact=True).fill("对话 A 修改")
            workspace.hold_save = True
            page.get_by_label("对话标题", exact=True).press("Enter")
            page.wait_for_timeout(500)
            assert workspace.pending_saves, "Fixture must hold an in-flight auto-save"
            page.get_by_role("button", name="删除对话 对话 A", exact=True).click()
            page.wait_for_timeout(80)
            workspace.hold_save = False
            for pending in workspace.pending_saves:
                workspace.save(pending)
            workspace.pending_saves.clear()
            page.get_by_role("button", name="恢复对话 对话 A 修改", exact=True).wait_for()
            page.wait_for_timeout(100)
            assert A not in workspace.docs, (
                "Late auto-save must not recreate a deleted conversation"
            )
            assert page.locator(f'.chat-row a[href="#/chat/{A}"]').count() == 0, (
                "Late auto-save must not republish a deleted conversation summary"
            )
            checks.append("inflight-autosave-cannot-resurrect-deletion")

        assert not page.evaluate("window.__paperlensErrors"), page.evaluate(
            "window.__paperlensErrors"
        )
        assert not workspace.unexpected, workspace.unexpected
        context.close()
    return {
        "status": "passed",
        "checks": checks + ["zero-real-service-or-model-requests", "no-js-errors"],
        "fixture_uploads": workspace.uploads,
        "fixture_imports": workspace.imports,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("dashboard", type=Path)
    parser.add_argument("--channel", default="chrome")
    parser.add_argument("--skip-save-race", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            check(args.dashboard, args.channel, not args.skip_save_race),
            ensure_ascii=False,
            indent=2,
        )
    )
