"""Real browser/ASGI acceptance for lazy reading and portable exports, no model calls."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import re
import shutil
import time
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

from check_work_features import original_pdf
from fastapi.testclient import TestClient
from PIL import Image

from paperlens.config import Settings
from paperlens.exporting import browser_session
from paperlens.library import LibraryStore
from paperlens.models import DashboardReport
from paperlens.server import create_app
from paperlens.storage import write_json

ROOT = Path(__file__).resolve().parents[2]
PAPER = "a" * 32
WORKSPACE = "qa-workspace"


def image_url(color: str) -> str:
    data = io.BytesIO()
    Image.new("RGB", (400, 160), color).save(data, format="PNG")
    return "data:image/png;base64," + base64.b64encode(data.getvalue()).decode()


def range_fixture_pdf() -> bytes:
    """Two valid text pages plus unused padding: transport QA, not a speed benchmark."""
    source = original_pdf().decode("ascii")
    padding = "% " + "padding " * 262144 + "\n"
    offset = len(padding)
    source = re.sub(r"(?m)^(\d{10}) 00000 n", lambda m: f"{int(m[1]) + offset:010} 00000 n", source)
    source = re.sub(r"startxref\n(\d+)", lambda m: "startxref\n" + str(int(m[1]) + offset), source)
    return source.replace("%PDF-1.4\n", "%PDF-1.4\n" + padding, 1).encode()


def fixture(output: Path) -> tuple[Settings, dict]:
    report = json.loads((ROOT / "tests/fixtures/ui_reader_revision.json").read_text("utf-8"))
    pdf = range_fixture_pdf()
    report["run"]["id"] = PAPER
    report["analysis"]["title"] = report["document"]["title"] = "Lazy reader acceptance"
    report["document"].update(sha256=hashlib.sha256(pdf).hexdigest(), parser="openai-pdf")
    second = deepcopy(report["document"]["pages"][0])
    second.update(number=2, label="2")
    report["document"]["pages"].append(second)
    report["source_pdf"] = {"data_url": "data:application/pdf;base64," + base64.b64encode(pdf).decode(),
                            "sha256": report["document"]["sha256"]}
    report["previews"] = []
    base = deepcopy(report["reading"]["items"][0])
    items = [deepcopy(base)]
    for name, color in [("near", "#af4332"), ("far", "#243faa")]:
        if name == "far":
            for i in range(25):
                item = deepcopy(base)
                item.update(id=f"gap-{i}", parts=[], content=(f"Paragraph {i}. Original research text remains readable. " * 12))
                items.append(item)
        item = deepcopy(base)
        item.update(id=f"qa-{name}", kind="figure", parts=[], label=f"Figure {name}",
                    content=f"Figure {name}. Original image.", asset=image_url(color),
                    bbox={"x0": .1, "y0": .2, "x1": .9, "y1": .5})
        items.append(item)
    report["reading"]["items"] = items
    report["reading"]["structured_pages"] = [1, 2]
    report["quality"].update(total_pages=2, parsed_pages=2)
    current = DashboardReport.model_validate(report)
    folder = output / "workspace" / "outputs" / "fixture"
    folder.mkdir(parents=True, exist_ok=True)
    write_json(folder / "report.json", current.model_dump(mode="json"))
    (folder / "source.pdf").write_bytes(pdf)
    settings = Settings(_env_file=None, paperlens_output_dir=folder.parent,
                        paperlens_cache_dir=output / "workspace" / "cache",
                        paperlens_catalog_network=False, paperlens_reference_network=False)
    return settings, current.model_dump(mode="json")


def check(output: Path) -> list[dict]:
    output.mkdir(parents=True, exist_ok=True)
    settings, full = fixture(output)
    html = (ROOT / "frontend/dist/index.html").read_text("utf-8").replace("__PAPERLENS_DATA__", "null")
    checks = []
    with TestClient(create_app(settings)) as client, browser_session() as browser:
        for width in (1440, 390):
            events, errors, ownership_violations = [], [], []
            context = browser.new_context(viewport={"width": width, "height": 960}, accept_downloads=True)
            context.set_default_timeout(15000)
            context.add_init_script("""const fetchOriginal = window.fetch.bind(window);
              window.fetch = (input, options) => {
                const url = new URL(input instanceof Request ? input.url : input, location.href);
                const headers = new Headers(options?.headers ?? (input instanceof Request ? input.headers : undefined));
                if (url.origin === location.origin && url.pathname.startsWith('/api/'))
                  headers.set('X-PaperLens-Workspace', 'qa-workspace');
                return fetchOriginal(input, {...options, headers});
              };""")
            page = context.new_page()
            page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))

            def intercept(route, _request=None, *, events=events, ownership_violations=ownership_violations):
                url = urlsplit(route.request.url)
                if url.path == "/":
                    route.fulfill(body=html, content_type="text/html")
                    return
                if not url.path.startswith("/api/"):
                    route.fulfill(status=404)
                    return
                headers = route.request.headers
                if ("/assets/" in url.path or url.path.endswith("/pdf")) and headers.get("x-paperlens-workspace") != WORKSPACE:
                    ownership_violations.append(url.path)
                    route.fulfill(status=403)
                    return
                response = client.request(route.request.method, url.path + ("?" + url.query if url.query else ""),
                                          headers={k: v for k, v in headers.items() if k not in {"host", "content-length"}},
                                          content=route.request.post_data_buffer)
                events.append({"path": url.path, "query": url.query, "range": headers.get("range"),
                               "status": response.status_code, "bytes": len(response.content)})
                # The deployed relay intentionally doesn't forward Content-Length.
                response_headers = {k: v for k, v in response.headers.items()
                                    if k not in {"content-length", "transfer-encoding", "content-encoding"}}
                route.fulfill(status=response.status_code, headers=response_headers, body=response.content)

            context.route("**/*", intercept)
            page.goto(f"http://127.0.0.1:49676/#/read/{PAPER}/text")
            near = page.locator("#qa-near img")
            near.wait_for()
            near.scroll_into_view_if_needed()
            page.wait_for_function("document.querySelector('#qa-near img')?.naturalWidth > 0")
            assert not [e for e in events if e["path"].endswith("/pdf")], "PDF fetched before requested"
            assert len([e for e in events if "/assets/" in e["path"]]) == 1, events
            assert any(e["query"] == "view=web" for e in events if e["path"].endswith("/report"))
            page.locator("#qa-far").scroll_into_view_if_needed()
            page.wait_for_function("document.querySelector('#qa-far img')?.naturalWidth > 0")
            assert len([e for e in events if "/assets/" in e["path"]]) == 2
            page.locator("#qa-near").scroll_into_view_if_needed()
            print(f"{width}: deferred images verified; opening original PDF", flush=True)
            page.get_by_role("button", name="核对原版 PDF", exact=True).click()
            page.locator('.pdf-source-page[data-ready="true"]').wait_for(timeout=30000)
            assert "Original PDF - page 1" in page.locator(".pdf-native-text").inner_text()
            first_pdf = [e for e in events if e["path"].endswith("/pdf")]
            assert first_pdf and all(e["status"] == 206 and e["range"] for e in first_pdf)
            assert sum(e["bytes"] for e in first_pdf) < len(range_fixture_pdf()) / 2
            page.get_by_role("button", name="PDF 下一页", exact=True).click()
            page.wait_for_function("document.querySelector('.pdf-source-page[data-ready=true]')?.dataset.page === '2'")
            assert "Original PDF - page 2" in page.locator(".pdf-native-text").inner_text()
            page.screenshot(path=str(output / f"lazy-original-{width}.png"))
            page.get_by_role("button", name="核对原版 PDF", exact=True).click()

            page.get_by_role("button", name="导出", exact=True).click()
            with page.expect_download() as download:
                page.get_by_role("button", name="完整数据 JSON", exact=True).click()
            json_path = output / f"export-{width}.json"
            download.value.save_as(json_path)
            exported = json.loads(json_path.read_text("utf-8"))
            assert exported["source_pdf"]["data_url"] == full["source_pdf"]["data_url"]
            assert not re.search(r'/api/library/[^" ]+/(?:assets/|pdf)', json.dumps(exported))
            page.get_by_role("button", name="导出", exact=True).click()
            with page.expect_download() as download:
                page.get_by_role("button", name="离线阅读 HTML", exact=True).click()
            html_path = output / f"offline-{width}.html"
            download.value.save_as(html_path)
            saved = html_path.read_text("utf-8")
            embedded = re.search(r'<script id="paperlens-data" type="application/json">([\s\S]*?)</script>', saved)
            assert embedded
            offline_report = json.loads(embedded[1])
            assert offline_report["source_pdf"]["data_url"] == full["source_pdf"]["data_url"]
            assert offline_report["reading"]["items"][-1]["asset"].startswith("data:image/")
            assert not errors and not ownership_violations, (errors, ownership_violations)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            print(f"{width}: ranges and complete exports verified; checking disconnected HTML", flush=True)

            offline_context = browser.new_context(viewport={"width": width, "height": 960})
            offline_requests = []
            offline_context.route(re.compile(r"^https?://"), lambda route, _request=None: route.abort())
            offline_page = offline_context.new_page()
            offline_page.on("request", lambda req, requests=offline_requests: requests.append(req.url) if req.url.startswith(("http://", "https://")) else None)
            offline_page.goto(html_path.as_uri() + "#/read/local/text")
            offline_page.locator("#qa-far").scroll_into_view_if_needed()
            offline_page.wait_for_function("document.querySelector('#qa-far img')?.naturalWidth > 0")
            offline_page.get_by_role("button", name="核对原版 PDF", exact=True).click()
            offline_page.locator('.pdf-source-page[data-ready="true"]').wait_for(timeout=30000)
            assert not offline_requests, offline_requests
            checks.append({"width": width, "near_viewport_images_only": True, "workspace_fetch_bound": True,
                           "pdf_explicit_ranges": first_pdf, "complete_json": True,
                           "offline_html_images_and_pdf_without_network": True, "page_errors": errors})
            offline_context.close()
            context.close()
    write_json(output / "browser-checks.json", checks)
    return checks


def measure_existing(output: Path, reports: Path) -> list[dict]:
    """Read-only source copies; compare JSON payloads and cold server projection."""
    measurements = []
    for index, original in enumerate(sorted(reports.rglob("report.json"))):
        destination = output / "measurements" / str(index) / "outputs" / "paper"
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, destination / "report.json")
        if (original.parent / "source.pdf").exists():
            shutil.copy2(original.parent / "source.pdf", destination / "source.pdf")
        library = LibraryStore(destination.parent)
        report = json.loads(original.read_text("utf-8"))
        start = time.perf_counter()
        light = library.web_report(report["run"]["id"])
        elapsed = time.perf_counter() - start
        full_bytes = len(json.dumps(report, ensure_ascii=False, separators=(",", ":")).encode())
        light_bytes = len(json.dumps(light, ensure_ascii=False, separators=(",", ":")).encode())
        measurements.append({"title": report["document"]["title"], "report_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
                             "full_json_bytes": full_bytes, "web_json_bytes": light_bytes,
                             "reduction_fraction": 1 - light_bytes / full_bytes,
                             "cold_projection_seconds": round(elapsed, 4)})
    write_json(output / "payload-measurements.json", measurements)
    return measurements


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / ".cache/lazy-reader")
    parser.add_argument("--reports", type=Path, help="Directory containing the reports you want to measure")
    parser.add_argument("--measure-only", action="store_true")
    args = parser.parse_args()
    if args.measure_only and not args.reports:
        parser.error("--measure-only requires --reports")
    folder = args.output.resolve()
    if not args.measure_only:
        print(json.dumps(check(folder), ensure_ascii=False, indent=2))
    if args.reports:
        print(json.dumps(measure_existing(folder, args.reports), ensure_ascii=False, indent=2))
