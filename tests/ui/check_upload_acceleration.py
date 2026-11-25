"""Browser regression for pre-submit acceleration. Every API response is local."""

from __future__ import annotations

import argparse
import json
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError

from paperlens.exporting import browser_session


def form_fields(request):
    message = BytesParser(policy=policy.default).parsebytes(
        ("Content-Type: " + request.headers["content-type"] + "\r\n\r\n").encode()
        + request.post_data_buffer
    )
    return {
        part.get_param("name", header="content-disposition"): part.get_payload(decode=True).decode(
            "utf-8", errors="replace"
        )
        for part in message.iter_parts()
        if part.get_filename() is None
    }


def check(url, output):
    output.mkdir(parents=True, exist_ok=True)
    ids = [("economy", "经济"), ("standard", "标准"), ("detailed", "精细")]
    profiles = {
        "default": "standard",
        "cost_note": "",
        "profiles": [
            {
                "id": key,
                "label": label,
                "description": "测试处理档位",
                "limits": {
                    "max_requests": 100,
                    "input_chars": 10000,
                    "output_tokens": 8000,
                    "reading_output_tokens": 8000,
                    "vision_pages": 4,
                },
            }
            for key, label in ids
        ],
    }
    events, errors, remote = [], [], []
    geometries = []
    transitions = []
    hold_estimates = False
    pending_estimates = []
    legacy_job = {
        "id": "legacy-id",
        "source_name": "legacy.pdf",
        "status": "running",
        "stage": "reading",
        "message": "Working",
        "acceleration": {"enabled": True, "concurrency": 3},
        "actions": ["ultra", "pause"],
    }

    def spread(n):
        return {"low": n * 0.7, "typical": n, "high": n * 1.5}

    def estimate(mode):
        return {
            "pages": 8,
            "pages_assumed": False,
            "model": "fixture",
            "history_runs": 0,
            "chars_per_token": 2,
            "price_checked_at": None,
            "price_stale": False,
            "parallel_jobs": 2,
            "document_features": {"source": "pdf", "chars_per_page": 2000},
            "profiles": [
                {
                    "profile": key,
                    "label": label,
                    "samples": 0,
                    "same_profile": False,
                    "budget_limited": key == "economy",
                    "ceiling": None,
                    "typical": {
                        "requests": spread(10),
                        "prompt_tokens": spread(1000),
                        "completion_tokens": spread(1000),
                        "seconds": spread(
                            600 * (index + 1) / {"normal": 1, "fast": 1.5, "ultra": 2}[mode]
                        ),
                        "cost": {
                            point: {
                                "status": "partial",
                                "off_peak": [0.1, 0.2],
                                "unpriced_models": ["custom"],
                            }
                            for point in ("low", "typical", "high")
                        },
                    },
                }
                for index, (key, label) in enumerate(ids)
            ],
        }

    def speed_geometry(target, mode):
        metrics = target.locator(".tier-accelerate").evaluate_all(
            """els=>els.map(el=>{
              const b=el.getBoundingClientRect(), c=el.parentElement.getBoundingClientRect();
              const icons=[...el.querySelectorAll('svg')], icon=icons[0], s=icon.getBoundingClientRect();
              const visible=[...icon.querySelectorAll('path,polygon,polyline')].filter(p=>!p.closest('defs'));
              const style=getComputedStyle(el), canvas=document.createElement('canvas').getContext('2d');
              const color=value=>{if(value==='none')return value;canvas.fillStyle=value;return canvas.fillStyle};
              return {mode:el.dataset.speedMode, icons:icons.length,
                shapes:visible.length,maskId:icon.querySelector('mask')?.id,
                fill:visible.map(p=>color(getComputedStyle(p).fill)),color:color(style.color),
                accent:color(style.getPropertyValue('--accent')),muted:color(style.getPropertyValue('--muted')),
                background:style.backgroundColor,
                button:{width:b.width,height:b.height,x:b.left-c.left,y:b.top-c.top},
                icon:{width:s.width,height:s.height,x:s.left-b.left,y:s.top-b.top}};
            })"""
        )
        assert len(metrics) == 3
        for item in metrics:
            assert item["mode"] == mode and item["icons"] == 1, item
            assert item["shapes"] == (2 if mode == "ultra" else 1), item
            assert item["button"]["width"] == item["button"]["height"] == 30, item
            assert item["icon"]["width"] == item["icon"]["height"] == 16, item
            assert item["background"] == "rgba(0, 0, 0, 0)", item
            assert item["color"] == item["muted" if mode == "normal" else "accent"], item
            assert item["fill"] == (
                ["none"] if mode == "normal" else [item["accent"]] * item["shapes"]
            ), item
        if mode == "ultra":
            assert len({item["maskId"] for item in metrics}) == 3, metrics
        geometries.append({"viewport": target.viewport_size, "mode": mode, "metrics": metrics})
        return metrics

    def same_geometry(before, after):
        for a, b in zip(before, after, strict=True):
            for box in ("button", "icon"):
                assert all(abs(a[box][key] - b[box][key]) < 0.5 for key in a[box]), (a, b)

    def layout_snapshot(target, phase):
        return {
            "phase": phase,
            **target.evaluate("""()=>{
          const base=document.querySelector('.upload-page').getBoundingClientRect(), boxes={};
          for(const selector of ['.tier-options','.tier-option-card','.tier-estimate',
            '.tier-selected-description','.tier-acceleration-note','.estimate-card',
            '.estimate-status','.tier-budget-details','.staged-actions']) {
            [...document.querySelectorAll(selector)].forEach((e,i)=>{
              const r=e.getBoundingClientRect();boxes[selector+':'+i]={
                x:r.x-base.x,y:r.y-base.y,width:r.width,height:r.height};
            });
          }
          const retry=[...document.querySelectorAll('.estimate-card button')].find(e=>e.textContent.trim()==='重新估算');
          if(retry){const r=retry.getBoundingClientRect();boxes.retry={x:r.x-base.x,y:r.y-base.y,width:r.width,height:r.height};}
          const nodes=[...document.querySelectorAll('.estimate-card,.estimate-card header,.estimate-summary,.tier-estimate')];
          const overflow=document.documentElement.scrollWidth>innerWidth;
          return {boxes,rows:[...document.querySelectorAll('.tier-estimate')].map(e=>e.textContent.trim()),
            figures:[...document.querySelectorAll('.estimate-summary strong')].map(e=>e.textContent.trim()),
            status:document.querySelector('.estimate-status')?.textContent.trim(),
            overflow,
            sameTree:window.savedEstimateNodes?.every((e,i)=>e===nodes[i])??true};
        }"""),
        }

    def same_layout(before, after):
        assert before["boxes"].keys() == after["boxes"].keys(), (before, after)
        assert not before["overflow"] and not after["overflow"], (before, after)
        assert after["sameTree"], after
        shifts = [
            abs(a - after["boxes"][name][key])
            for name, box in before["boxes"].items()
            for key, a in box.items()
        ]
        assert max(shifts, default=0) < 0.5, (before, after)

    def same_values(before, after):
        assert len(after["rows"]) == 3 and all(after["rows"]), after
        assert before["rows"] == after["rows"] and before["figures"] == after["figures"], (
            before,
            after,
        )

    def transparent_cutout(target):
        return target.locator(".tier-accelerate svg").first.evaluate("""async svg=>{
          const clone=svg.cloneNode(true), sources=[svg,...svg.querySelectorAll('*')],
            copies=[clone,...clone.querySelectorAll('*')];
          for(let i=0;i<sources.length;i++){
            const s=getComputedStyle(sources[i]);
            for(const p of ['fill','stroke','stroke-width','color']) copies[i].style.setProperty(p,s.getPropertyValue(p));
          }
          clone.setAttribute('xmlns','http://www.w3.org/2000/svg');
          clone.setAttribute('width','96');clone.setAttribute('height','96');
          const pixels=async node=>{
            const img=new Image();img.src='data:image/svg+xml;charset=utf-8,'+encodeURIComponent(new XMLSerializer().serializeToString(node));
            await img.decode();const canvas=document.createElement('canvas');canvas.width=canvas.height=96;
            const ctx=canvas.getContext('2d');ctx.drawImage(img,0,0);return ctx.getImageData(0,0,96,96).data;
          };
          const masked=await pixels(clone);clone.querySelector('path[mask]').removeAttribute('mask');
          const solid=await pixels(clone);let transparent=0,filled=0,cutout=0;
          for(let i=3;i<masked.length;i+=4){transparent+=masked[i]===0;filled+=masked[i]>128;
            cutout+=masked[i]===0&&solid[i]>128;}
          return {transparent,filled,cutout};
        }""")

    with browser_session() as browser:
        context = browser.new_context(viewport={"width": 1280, "height": 950})
        page = context.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda dialog: dialog.accept())

        def intercept(route):
            request = route.request
            path = urlsplit(request.url).path
            if not request.url.startswith(url.rstrip("/") + "/"):
                remote.append(request.url)
                route.abort()
                return
            if not path.startswith("/api/"):
                route.continue_()
                return
            if path == "/api/health":
                result = {"ok": True, "features": ["library", "jobs", "imports"]}
            elif path == "/api/library":
                result = {"papers": []}
            elif path == "/api/jobs" and request.method == "GET":
                result = {"jobs": [legacy_job]}
            elif path == "/api/jobs/legacy-id/ultra":
                events.append({"kind": "job-ultra", **request.post_data_json})
                legacy_job.update(
                    speed_mode="ultra",
                    acceleration={"enabled": True, "mode": "ultra", "concurrency": 8},
                    actions=["pause"],
                )
                result = legacy_job
            elif path == "/api/processing-profiles":
                result = profiles
            elif path == "/api/estimates":
                fields = form_fields(request)
                events.append({"kind": "estimate", **fields})
                if hold_estimates:
                    pending_estimates.append((route, fields["speed_mode"]))
                    return
                result = estimate(fields["speed_mode"])
            elif path == "/api/jobs":
                fields = form_fields(request)
                events.append({"kind": "upload", **fields})
                result = {"id": f"{len(events):032x}"}
            elif path == "/api/imports/resolve":
                result = {
                    "candidates": [
                        {
                            "id": "fixture-candidate",
                            "title": "加速导入测试论文",
                            "url": "https://fixture.invalid/paper",
                            "pdf_url": "https://fixture.invalid/paper.pdf",
                        }
                    ]
                }
            elif path == "/api/imports":
                events.append({"kind": "import", **request.post_data_json})
                result = {"id": "f" * 32}
            else:
                route.fulfill(status=404, json={"detail": "not found"})
                return
            route.fulfill(json=result)

        context.route("**/*", intercept)
        page.goto(url.rstrip("/") + "/#/upload", wait_until="networkidle")
        page.locator(".estimate-card strong").first.wait_for()
        assert "约 20 分钟" in page.locator(".estimate-card").inner_text()
        page.get_by_role("button", name="极速 legacy.pdf", exact=True).click()
        page.get_by_text("极速 · 8 路", exact=True).wait_for()
        assert next(e for e in events if e["kind"] == "job-ultra")["confirmed"] is True
        keyboard = page.get_by_role("button", name="标准档加速", exact=True)
        keyboard.focus()
        normal_geometry = speed_geometry(page, "normal")
        keyboard.press("Enter")
        page.wait_for_function(
            "document.querySelector('.tier-accelerate').dataset.speedMode==='fast'"
        )
        same_geometry(normal_geometry, speed_geometry(page, "fast"))
        keyboard.press("Space")
        page.wait_for_function(
            "document.querySelector('.tier-accelerate').dataset.speedMode==='ultra'"
        )
        same_geometry(normal_geometry, speed_geometry(page, "ultra"))
        keyboard.press("Enter")
        page.wait_for_function(
            "document.querySelector('.tier-accelerate').dataset.speedMode==='normal'"
        )
        page.get_by_role("button", name="经济档加速", exact=True).dblclick()
        page.wait_for_function(
            "[...document.querySelectorAll('.tier-accelerate')].every(e=>e.dataset.speedMode==='ultra')"
        )
        page.get_by_text("当前设置可能不足以完成处理。", exact=True).wait_for()
        assert (
            page.get_by_role("radio", name="经济", exact=True).get_attribute("aria-checked")
            == "true"
        )
        assert "约 5 分钟" in page.locator(".estimate-card").inner_text()
        assert page.locator(".tier-accelerate svg").count() == 3
        page.get_by_role("radio", name="标准", exact=True).click()
        assert "约 10 分钟" in page.locator(".estimate-card").inner_text()
        assert page.locator('.tier-accelerate[aria-pressed="true"]').count() == 3
        for width in (1280, 480, 360):
            page.set_viewport_size({"width": width, "height": 950})
            page.wait_for_timeout(350)  # Let the navigation drawer finish its transition.
            metrics = page.locator(".tier-accelerate").evaluate_all(
                'els=>els.map(e=>{const a=e.getBoundingClientRect(),b=e.parentElement.getBoundingClientRect();return {inside:a.left>=b.left&&a.right<=b.right,buttonParent:!!e.parentElement.closest("button")}})'
            )
            assert all(m["inside"] and not m["buttonParent"] for m in metrics), metrics
            assert not page.evaluate("document.documentElement.scrollWidth>innerWidth")
            page.screenshot(path=str(output / f"cards-{width}.png"))
        page.set_viewport_size({"width": 1280, "height": 950})
        page.locator('input[type="file"]').set_input_files(
            [
                {"name": "one.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.4\none"},
                {"name": "two.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.4\ntwo"},
            ]
        )
        page.get_by_text("2 篇共 16 页", exact=False).wait_for()
        assert "已知部分" in page.locator(".estimate-card").inner_text()
        page.get_by_role("button", name="开始处理", exact=True).click()
        page.wait_for_function("document.querySelectorAll('.staged').length===0")
        uploaded = [e for e in events if e["kind"] == "upload"]
        assert len(uploaded) == 2 and all(
            e["accelerated"] == "true" and e["speed_mode"] == "ultra" and e["profile"] == "standard"
            for e in uploaded
        ), uploaded
        page.get_by_role("tab", name="查找论文", exact=True).click()
        page.get_by_label("论文线索", exact=True).fill("acceleration fixture")
        page.get_by_role("button", name="查找论文", exact=True).click()
        page.locator('.import-candidate input[type="radio"]').check()
        page.get_by_role("radio", name="精细", exact=True).click()
        assert page.locator('.tier-accelerate[aria-pressed="true"]').count() == 3
        page.get_by_role("button", name="开始处理", exact=True).click()
        page.get_by_text("这篇论文已有提交记录。", exact=True).wait_for()
        imported = [e for e in events if e["kind"] == "import"]
        assert (
            len(imported) == 1
            and imported[0]["accelerated"] is True
            and imported[0]["speed_mode"] == "ultra"
            and imported[0]["profile"] == "detailed"
        ), imported
        page.get_by_role("tab", name="上传文件", exact=False).click()
        page.get_by_role("button", name="标准档加速", exact=True).click()
        page.wait_for_function(
            "[...document.querySelectorAll('.tier-accelerate')].every(e=>e.getAttribute('aria-pressed')==='false')"
        )
        page.get_by_text("约 20 分钟", exact=True).last.wait_for()
        assert [e for e in events if e["kind"] == "estimate"][-1]["accelerated"] == "false"
        assert [e for e in events if e["kind"] == "estimate"][-1]["speed_mode"] == "normal"
        assert not errors and not remote, (errors, remote)

        mobile_context = browser.new_context(
            viewport={"width": 390, "height": 844},
            is_mobile=True,
            has_touch=True,
            device_scale_factor=2,
        )
        mobile_context.route("**/*", intercept)
        mobile = mobile_context.new_page()
        mobile.on("pageerror", lambda e: errors.append(str(e)))
        mobile.goto(url.rstrip("/") + "/#/upload", wait_until="networkidle")
        mobile_button = mobile.get_by_role("button", name="经济档加速", exact=True)
        mobile_normal_geometry = speed_geometry(mobile, "normal")
        mobile_button.tap()
        mobile.wait_for_function(
            "document.querySelector('.tier-accelerate').dataset.speedMode==='fast'"
        )
        same_geometry(mobile_normal_geometry, speed_geometry(mobile, "fast"))
        mobile_button.tap()
        mobile.wait_for_function(
            "document.querySelector('.tier-accelerate').dataset.speedMode==='ultra'"
        )
        same_geometry(mobile_normal_geometry, speed_geometry(mobile, "ultra"))
        assert mobile.locator(".tier-accelerate svg").count() == 3
        assert not mobile.evaluate("document.documentElement.scrollWidth>innerWidth")
        mobile.wait_for_timeout(350)  # Let the native touch highlight fade before the screenshot.
        mobile.screenshot(path=str(output / "cards-mobile-ultra.png"))
        mobile_button.tap()
        mobile.wait_for_function(
            "document.querySelector('.tier-accelerate').dataset.speedMode==='normal'"
        )
        assert not errors and not remote, (errors, remote)
        for width, font_size in ((1280, 13), (390, 13), (320, 13), (320, 24)):
            hold_estimates = False
            transition_context = browser.new_context(viewport={"width": width, "height": 950})
            transition_context.route("**/*", intercept)
            trial = transition_context.new_page()
            trial.add_init_script(
                f"localStorage.setItem('paperlens.prefs',JSON.stringify({{uiFontSize:{font_size}}}))"
            )
            trial.on("pageerror", lambda e: errors.append(str(e)))
            trial.goto(url.rstrip("/") + "/#/upload", wait_until="networkidle")
            assert (
                trial.evaluate(
                    "getComputedStyle(document.documentElement).getPropertyValue('--ui-font-size').trim()"
                )
                == f"{font_size}px"
            )
            trial.locator('input[type="file"]').set_input_files(
                {
                    "name": "layout.pdf",
                    "mimeType": "application/pdf",
                    "buffer": b"%PDF-1.4\nlayout",
                }
            )
            trial.get_by_text("按当前设置估算", exact=True).wait_for()
            trial.evaluate(
                "window.savedEstimateNodes=[...document.querySelectorAll('.estimate-card,.estimate-card header,.estimate-summary,.tier-estimate')]"
            )
            baseline = layout_snapshot(trial, "normal-ready")
            assert ".staged-actions:0" in baseline["boxes"], baseline
            hold_estimates = True
            button = trial.get_by_role("button", name="标准档加速", exact=True)
            button.click()
            trial.get_by_text("更新中 · 上次估算", exact=True).wait_for()
            retry = trial.get_by_role("button", name="重新估算", exact=True)
            assert retry.is_disabled()
            loading = layout_snapshot(trial, "fast-loading")
            same_layout(baseline, loading)
            same_values(baseline, loading)
            if width == 320:
                trial.locator(".estimate-card").screenshot(
                    path=str(output / f"estimate-320-font{font_size}-loading.png")
                )
            assert pending_estimates
            route, mode = pending_estimates.pop(0)
            assert mode == "fast"
            route.fulfill(json=estimate(mode))
            trial.get_by_text("按当前设置估算", exact=True).wait_for()
            complete = layout_snapshot(trial, "fast-ready")
            same_layout(baseline, complete)
            button.click()
            trial.wait_for_timeout(50)
            button.click()
            trial.wait_for_timeout(50)
            rapid = layout_snapshot(trial, "rapid-ultra-to-normal-loading")
            same_layout(baseline, rapid)
            same_values(complete, rapid)
            assert len(pending_estimates) == 2
            old_route, old_mode = pending_estimates.pop(0)
            latest_route, latest_mode = pending_estimates.pop(0)
            assert (old_mode, latest_mode) == ("ultra", "normal")
            latest_route.fulfill(json=estimate(latest_mode))
            trial.get_by_text("按当前设置估算", exact=True).wait_for()
            try:
                old_route.fulfill(json=estimate(old_mode))
            except PlaywrightError:
                pass  # The older request is allowed to have been cancelled already.
            trial.wait_for_timeout(50)
            latest = layout_snapshot(trial, "normal-ready-after-stale-ultra")
            same_layout(baseline, latest)
            same_values(baseline, latest)
            button.click()
            trial.get_by_text("更新中 · 上次估算", exact=True).wait_for()
            route, mode = pending_estimates.pop(0)
            assert mode == "fast"
            route.fulfill(status=503, json={"detail": "Synthetic estimate failure"})
            trial.get_by_text("更新失败 · 上次估算", exact=True).wait_for()
            assert retry.is_enabled()
            failed = layout_snapshot(trial, "failed-with-previous-values")
            if width == 320:
                trial.locator(".estimate-card").screenshot(
                    path=str(output / f"estimate-320-font{font_size}-failed.png")
                )
            same_layout(baseline, failed)
            same_values(baseline, failed)
            retry.click()
            trial.get_by_text("更新中 · 上次估算", exact=True).wait_for()
            assert retry.is_disabled()
            retrying = layout_snapshot(trial, "retry-loading")
            same_layout(baseline, retrying)
            same_values(baseline, retrying)
            route, mode = pending_estimates.pop(0)
            assert mode == "fast"
            route.fulfill(json=estimate(mode))
            trial.get_by_text("按当前设置估算", exact=True).wait_for()
            assert retry.is_enabled()
            recovered = layout_snapshot(trial, "retry-recovered")
            same_layout(baseline, recovered)
            same_values(complete, recovered)
            transitions.append(
                {
                    "width": width,
                    "font_size": font_size,
                    "snapshots": [
                        baseline,
                        loading,
                        complete,
                        rapid,
                        latest,
                        failed,
                        retrying,
                        recovered,
                    ],
                }
            )
            transition_context.close()
        hold_estimates = False
        theme_details = []
        preview = context.new_page()
        preview.goto(url.rstrip("/") + "/#/upload", wait_until="networkidle")
        for theme in ("light", "dark"):
            preview.evaluate(
                """theme=>localStorage.setItem('paperlens.prefs',JSON.stringify({
              theme,darkPalette:'kiln',lightPalette:'clay'
            }))""",
                theme,
            )
            preview.reload(wait_until="networkidle")
            preview.wait_for_function(
                "theme=>document.documentElement.dataset.theme===theme", arg=theme
            )
            normal_geometry = speed_geometry(preview, "normal")
            speed_button = preview.get_by_role("button", name="经济档加速", exact=True)
            speed_button.click()
            same_geometry(normal_geometry, speed_geometry(preview, "fast"))
            speed_button.click()
            preview.wait_for_function(
                "document.querySelector('.tier-accelerate').dataset.speedMode==='ultra'"
            )
            same_geometry(normal_geometry, speed_geometry(preview, "ultra"))
            preview.mouse.move(20, 20)
            detail = preview.locator(".tier-accelerate").first.evaluate("""el=>({
              theme:document.documentElement.dataset.theme,
              palette:document.documentElement.dataset.palette,
              buttonBackground:getComputedStyle(el).backgroundColor,
              cardBackground:getComputedStyle(el.parentElement.querySelector('.tier-option')).backgroundColor
            })""")
            assert detail["theme"] == theme
            detail["cutout"] = transparent_cutout(preview)
            assert detail["cutout"]["cutout"] > 0 and detail["cutout"]["filled"] > 0, detail
            assert detail["buttonBackground"] == "rgba(0, 0, 0, 0)", detail
            theme_details.append(detail)
            card_box = preview.locator(".tier-option-card").first.bounding_box()
            preview.screenshot(
                path=str(output / f"bolts-{theme}-detail.png"),
                clip={
                    "x": card_box["x"],
                    "y": card_box["y"],
                    "width": card_box["width"],
                    "height": 48,
                },
            )
        assert theme_details[0]["cardBackground"] != theme_details[1]["cardBackground"], (
            theme_details
        )
        assert not errors and not remote, (errors, remote)
    result = {
        "status": "passed",
        "events": events,
        "speed_geometries": geometries,
        "layout_transitions": transitions,
        "theme_details": theme_details,
        "errors": errors,
        "remote_requests": remote,
    }
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5188")
    parser.add_argument("--output", type=Path, default=Path(".test-tmp/upload-acceleration"))
    args = parser.parse_args()
    print(json.dumps(check(args.url, args.output), ensure_ascii=False, indent=2))
