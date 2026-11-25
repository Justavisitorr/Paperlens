"""Built-reader checks: graphless recovery and real browser confirmation."""

import json
import time
from urllib.parse import urlsplit

import pytest
from test_graph_export_browser import graph_report
from test_reading_projection import b, report

from paperlens.exporting import browser_session, get_dashboard_template, write_report


@pytest.mark.parametrize("graphs", [False, True])
def test_offline_report_validation_finishes_with_or_without_graphs(tmp_path, graphs):
    sample = graph_report() if graphs else report([b("source", "Saved readable partial result.")])
    sample.run.status = "partial"
    started = time.monotonic()
    path = write_report(sample, tmp_path)
    assert path.is_file() and (tmp_path / "report.json").is_file()
    assert all(g.validated for g in sample.graphs)
    print(json.dumps({"graphs": len(sample.graphs), "validation_seconds": round(time.monotonic() - started, 2)}))


@pytest.mark.parametrize("width", [1440, 390])
def test_budget_dialog_dismissal_polling_and_confirmed_retry(width):
    html = get_dashboard_template().replace("__PAPERLENS_DATA__", "null")
    pending = {"limit_chars": 18000, "required_chars": 23000, "suggested_chars": 26000,
               "maximum_chars": 1000000, "stage": "自动笔记", "request_id": "first"}
    job = {"id": "a" * 32, "source_name": "Budget fixture.pdf", "status": "partial",
           "stage": "partial", "message": "等待确认输入预算", "actions": ["retry"],
           "input_budget": pending}
    notices, submitted, errors = [], [], []
    polls = 0
    accept = False
    with browser_session() as browser:
        context = browser.new_context(viewport={"width": width, "height": 900})
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))

        def dialog(value):
            notices.append(value.message)
            value.accept() if accept else value.dismiss()

        def intercept(route):
            nonlocal polls
            path = urlsplit(route.request.url).path
            if path == "/":
                route.fulfill(body=html, content_type="text/html")
            elif path == "/api/health":
                route.fulfill(json={"ok": True, "features": ["jobs", "library"],
                                    "running_jobs": 0, "queued_jobs": 0})
            elif path == "/api/jobs":
                polls += 1
                route.fulfill(json={"jobs": [job]})
            elif path == "/api/library":
                route.fulfill(json={"papers": []})
            elif path == f"/api/jobs/{job['id']}/retry":
                submitted.append(route.request.post_data_json)
                job.update(status="complete", stage="complete", input_budget=None, actions=[])
                route.fulfill(json=job)
            elif path == "/favicon.ico":
                route.fulfill(status=204)
            else:
                route.fulfill(status=404, json={"detail": "Offline test endpoint"})

        page.on("dialog", dialog)
        context.route("**/*", intercept)
        page.clock.install()
        page.goto("http://127.0.0.1:49673/#/upload")
        action = page.get_by_role("button", name="确认输入预算后继续 Budget fixture.pdf")
        action.wait_for()
        assert len(notices) == 1 and not submitted
        assert "18,000" in notices[0] and "26,000" in notices[0]
        # Exercise the actual next polling tick without a wall-clock sleep.
        page.clock.fast_forward(9000)
        page.wait_for_function("document.querySelector('button[title=\"确认本次输入预算后继续\"]') !== null")
        assert polls >= 2 and len(notices) == 1 and not submitted
        accept = True
        action.click()
        page.wait_for_function("!document.querySelector('button[title=\"确认本次输入预算后继续\"]')")
        assert len(notices) == 2
        assert submitted == [{"input_budget": {"confirmed": True, "limit_chars": 26000}}]
        assert not errors, errors
        context.close()
