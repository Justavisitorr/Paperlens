"""Compare two report representations with the same built reader, without network/model calls.

Run check_lazy_reader.py --measure-only first to create isolated source copies.
The ASGI/browser bridge has no Internet latency or throttling. Heap values are
CDP observations, not operating-system, GPU or PDF-worker peak memory.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from paperlens.config import Settings
from paperlens.exporting import browser_session
from paperlens.server import create_app
from paperlens.storage import write_json

ROOT = Path(__file__).resolve().parents[2]


def measure(output: Path) -> list[dict]:
    html = (ROOT / "frontend/dist/index.html").read_text("utf-8").replace("__PAPERLENS_DATA__", "null")
    results = []
    with browser_session() as browser:
        for folder in sorted((output / "measurements").glob("*/outputs")):
            report = json.loads((folder / "paper/report.json").read_text("utf-8"))
            paper_id = report["run"]["id"]
            row = {"title": report["document"]["title"], "runs": []}
            for mode in ("full", "web"):
                settings = Settings(_env_file=None, paperlens_output_dir=folder,
                                    paperlens_cache_dir=folder.parent / ("timing-" + mode),
                                    paperlens_catalog_network=False, paperlens_reference_network=False)
                events, errors = [], []
                with TestClient(create_app(settings)) as client:
                    context = browser.new_context(viewport={"width": 1440, "height": 960})
                    page = context.new_page()
                    page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
                    cdp = context.new_cdp_session(page)
                    cdp.send("Performance.enable")

                    def intercept(route, _request=None, *, mode=mode, events=events):
                        url = urlsplit(route.request.url)
                        if url.path == "/":
                            route.fulfill(body=html, content_type="text/html")
                            return
                        if not url.path.startswith("/api/"):
                            route.fulfill(status=404)
                            return
                        # Both modes use the identical client code; only the API
                        # report representation differs, isolating this change.
                        query = "" if url.path.endswith("/report") and mode == "full" else url.query
                        response = client.request(route.request.method, url.path + ("?" + query if query else ""),
                                                  headers={k: v for k, v in route.request.headers.items()
                                                           if k not in {"host", "content-length"}},
                                                  content=route.request.post_data_buffer)
                        events.append({"path": url.path, "bytes": len(response.content)})
                        headers = {k: v for k, v in response.headers.items()
                                   if k not in {"content-encoding", "content-length", "transfer-encoding"}}
                        route.fulfill(status=response.status_code, headers=headers, body=response.content)

                    context.route("**/*", intercept)
                    observations = []
                    for opening in range(3):
                        if opening:
                            page.evaluate("location.hash = '#/terms'")
                            page.wait_for_function("!document.querySelector('.continuous-prose')")
                        page.add_init_script("window.__paperlensMeasurePaint = null")
                        if opening:
                            page.evaluate("window.__paperlensMeasurePaint = null")
                        start = time.perf_counter()
                        if opening:
                            page.evaluate("hash => location.hash = hash", f"#/read/{paper_id}/text")
                        else:
                            page.goto(f"http://127.0.0.1:49677/#/read/{paper_id}/text", wait_until="commit")
                        peak_observed = 0
                        while time.perf_counter() - start < 45:
                            # Two animation frames after text exists provide a
                            # reproducible first-readable event, excluding images.
                            ready = page.evaluate("""() => {
                              if (window.__paperlensMeasurePaint === true) return true;
                              if (window.__paperlensMeasurePaint == null && window.__paperlensReaderReady &&
                                  document.querySelector('.continuous-prose .reading-item')?.textContent.trim()) {
                                window.__paperlensMeasurePaint = false;
                                requestAnimationFrame(() => requestAnimationFrame(() => window.__paperlensMeasurePaint = true));
                              }
                              return false;
                            }""")
                            metrics = {m["name"]: m["value"] for m in cdp.send("Performance.getMetrics")["metrics"]}
                            peak_observed = max(peak_observed, metrics.get("JSHeapUsedSize", 0))
                            if ready:
                                break
                            page.wait_for_timeout(25)
                        assert ready, "Text never became readable"
                        readable = time.perf_counter() - start
                        # A post-GC heap observation makes retained report data
                        # comparable; it does not claim total browser memory.
                        cdp.send("HeapProfiler.collectGarbage")
                        heap = {m["name"]: m["value"] for m in cdp.send("Performance.getMetrics")["metrics"]}
                        observations.append({"opening": opening + 1, "first_readable_seconds": round(readable, 3),
                                             "sampled_js_heap_max_bytes": int(peak_observed),
                                             "retained_js_heap_bytes": int(heap["JSHeapUsedSize"])})
                    assert not errors, errors
                    row["runs"].append({"representation": mode, "observations": observations,
                                        "report_downloads": sum(e["path"].endswith("/report") for e in events),
                                        "report_body_bytes": sum(e["bytes"] for e in events if e["path"].endswith("/report"))})
                    context.close()
            results.append(row)
            write_json(output / "browser-measurements.json", results)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / ".cache/lazy-reader")
    args = parser.parse_args()
    measure(args.output.resolve())
