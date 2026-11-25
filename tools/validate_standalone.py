"""Isolated browser acceptance. No cloud changes, real mail, OAuth or model calls."""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import threading
import time
from pathlib import Path

import uvicorn
from fastapi.testclient import TestClient

from paperlens.exporting import browser_session
from paperlens.models import DashboardReport
from paperlens.standalone import create_standalone
from paperlens.standalone_config import StandaloneConfig
from paperlens.storage import write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    config = StandaloneConfig(output / "data", origin, dev_mail=True)
    app = create_standalone(config)
    service = app.state.standalone
    runner = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False, proxy_headers=False))
    thread = threading.Thread(target=runner.run, daemon=True)
    thread.start()
    for _ in range(100):
        if runner.started:
            break
        time.sleep(0.05)
    assert runner.started
    observations, errors = {}, []
    try:
        with browser_session() as browser:
            first = browser.new_context(viewport={"width":1440,"height":1000}, accept_downloads=True)
            page = first.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(origin + "/")
            page.locator("#email").fill("acceptance@example.com")
            page.locator("#request").click()
            page.locator("#code-form:not([hidden])").wait_for()
            email_file = next((config.root / "test-mail").glob("*.json"))
            code = json.loads(email_file.read_text("utf-8"))["code"]
            page.locator("#code").fill(code)
            page.locator("#code-form button").click()
            page.wait_for_url(origin + "/**")
            page.wait_for_function("!!window.paperLensAccount && document.querySelector('.shell')")
            account = page.evaluate("window.paperLensAccount.user")
            observations["email_login"] = True
            policy = service.policy(account["id"])
            report = DashboardReport.model_validate_json(args.report.read_bytes())
            destination = config.root / "workspaces" / policy.workspace_id / "outputs" / report.run.id
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(args.report, destination / "report.json")
            if args.report.with_name("source.pdf").is_file():
                shutil.copyfile(args.report.with_name("source.pdf"), destination / "source.pdf")
            term = [{"id":"acceptance::attention", "term":"Attention", "explanation":"A synchronized research term.",
                     "paperId":report.run.id, "paperTitle":report.document.title, "origin":"selection", "added":"2026-11-12T00:00:00Z"}]
            page.evaluate("value => localStorage.setItem('paperlens.terms', JSON.stringify(value))", term)
            page.evaluate("() => window.paperLensSync.flush()")
            page.reload()
            page.wait_for_function("!!window.paperLensAccount && document.querySelector('.shell')")
            page.goto(origin + "/#/terms")
            page.get_by_text("A synchronized research term.", exact=True).wait_for()
            page.screenshot(path=str(output / "desktop.png"), full_page=True)
            # A second browser has its own session and no local storage.
            second = browser.new_context(viewport={"width":390,"height":844}, accept_downloads=True)
            second.add_cookies([{"name":service.session_cookie,"value":service.store.session(account["id"]),"url":origin,"httpOnly":True,"sameSite":"Lax"}])
            mobile = second.new_page()
            mobile.on("pageerror", lambda error: errors.append(str(error)))
            mobile.goto(origin + "/#/terms")
            mobile.get_by_text("A synchronized research term.", exact=True).wait_for()
            observations["cross_device_hydration"] = True
            mobile.screenshot(path=str(output / "mobile.png"), full_page=True)
            assert mobile.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            # Both devices edit the same base revision; neither silently wins.
            page.evaluate("() => localStorage.setItem('paperlens:citation-copy-format', '\"bibtex\"')")
            page.evaluate("() => window.paperLensSync.flush()")
            mobile.evaluate("() => localStorage.setItem('paperlens:citation-copy-format', '\"biblatex\"')")
            mobile.evaluate("() => window.paperLensSync.flush()")
            assert mobile.evaluate("Object.keys(window.paperLensSync.snapshot().conflicts).length") == 1
            mobile.goto(origin + "/account")
            mobile.locator("#conflict-section:not([hidden])").wait_for()
            mobile.get_by_role("button", name="保留服务器版本").click()
            mobile.wait_for_function("window.paperLensSync.snapshot().pending === 0")
            observations["conflict_resolution"] = True
            # Offline change is durable, then resumes after connectivity returns.
            second.set_offline(True)
            mobile.evaluate("() => localStorage.setItem('paperlens:reading-position:acceptance', '{\"id\":\"item-1\",\"fraction\":0.7}')")
            assert mobile.evaluate("window.paperLensSync.snapshot().pending") > 0
            second.set_offline(False)
            mobile.evaluate("() => window.paperLensSync.flush()")
            mobile.wait_for_function("window.paperLensSync.snapshot().pending === 0")
            observations["offline_outbox_recovered"] = True
            # Independent user cannot see the first user's paper, terms or keys.
            bob = service.store.identity("email", "second-account@example.com", "Second account")
            third = browser.new_context()
            third.add_cookies([{"name":service.session_cookie,"value":service.store.session(bob),"url":origin,"httpOnly":True,"sameSite":"Lax"}])
            private = third.new_page()
            private.goto(origin + "/")
            private.wait_for_function("!!window.paperLensAccount")
            assert private.evaluate("localStorage.getItem('paperlens.terms')") is None
            assert private.evaluate("async () => (await (await fetch('/api/library')).json()).papers.length") == 0
            observations["second_account_isolated"] = True
            # Real existing paper can be read lazily and exported as a complete offline HTML.
            page.goto(origin + "/#/read/" + report.run.id)
            page.get_by_role("button", name="导出", exact=True).wait_for(timeout=30000)
            page.get_by_role("button", name="导出", exact=True).click()
            with page.expect_download(timeout=60000) as download:
                page.get_by_role("button", name="离线阅读 HTML").click()
            offline_path = output / "offline.html"
            download.value.save_as(offline_path)
            text = offline_path.read_text("utf-8")
            assert "/_paperlens/client.js?workspace=" not in text
            assert "await window.paperLensCloudReady;" not in text
            offline_context = browser.new_context(offline=True)
            offline_page = offline_context.new_page()
            offline_page.on("pageerror", lambda error: errors.append(str(error)))
            offline_page.goto(offline_path.as_uri())
            offline_page.wait_for_function("!!document.querySelector('.shell')")
            assert offline_page.locator("a[href='/account']").count() == 0
            assert not offline_page.evaluate("window.paperLensAccount || null")
            observations["offline_export_bytes"] = offline_path.stat().st_size
            observations["offline_export_independent"] = True
            page.goto(origin + "/account")
            page.locator("#name").filter(has_text="acceptance").wait_for()
            page.screenshot(path=str(output / "account.png"), full_page=True)
            page.locator("#passphrase").fill("browser-migration-password")
            with page.expect_download(timeout=60000) as transfer:
                page.locator("#export-workspace").click()
            transfer_path = output / "transfer.plbackup"
            transfer.value.save_as(transfer_path)
            private.goto(origin + "/account")
            private.locator("#name").filter(has_text="Second account").wait_for()
            private.locator("#passphrase").fill("browser-migration-password")
            private.locator("#import-workspace").set_input_files(transfer_path)
            private.get_by_text("资料已导入，请回到文献库查看。", exact=True).wait_for(timeout=60000)
            private.goto(origin + "/#/terms")
            private.get_by_text("A synchronized research term.", exact=True).wait_for()
            observations["encrypted_migration_browser_flow"] = True
            observations["browser_errors"] = errors
            assert not errors, errors
            browser.close()
    finally:
        runner.should_exit = True
        thread.join(timeout=15)
        assert not thread.is_alive()
    # A cold start from a copied data root verifies full-service backup portability.
    restored = output / "restored"
    shutil.copytree(config.root, restored)
    restored_app = create_standalone(StandaloneConfig(restored, origin, dev_mail=True))
    restored_service = restored_app.state.standalone
    assert restored_service.store.identities(account["id"])
    assert restored_service.store.snapshot(account["id"], restored_service.policy(account["id"]))["paperlens.terms"]["value"]
    with TestClient(restored_app, base_url=origin) as restored_client:
        restored_client.cookies.set(restored_service.session_cookie, restored_service.store.session(account["id"]))
        restored_session = restored_client.get("/auth/session").json()
        bound = {"X-PaperLens-Workspace": restored_session["workspace"]}
        restored_library = restored_client.get("/api/library", headers=bound).json()
        assert restored_library["papers"][0]["id"] == report.run.id
        assert restored_client.get("/auth/data", headers=bound).json()["items"]["paperlens.terms"]["value"]
    observations["cold_copy_restore"] = True
    write_json(output / "acceptance.json", observations)
    print(json.dumps(observations, ensure_ascii=False))


if __name__ == "__main__":
    main()
