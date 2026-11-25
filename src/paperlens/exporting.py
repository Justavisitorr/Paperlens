"""Portable report files and browser validation using the bundled Mermaid build."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path

from .errors import ConfigurationError, ValidationError
from .graphs import compile_mermaid
from .models import DashboardReport, PaperDocument
from .storage import write_json

DATA_PLACEHOLDER = "__PAPERLENS_DATA__"


def get_dashboard_template() -> str:
    packaged = files("paperlens").joinpath("assets/dashboard.html")
    if packaged.is_file():
        return packaged.read_text(encoding="utf-8")
    development = Path(__file__).resolve().parents[2] / "frontend/dist/index.html"
    if development.is_file():
        return development.read_text(encoding="utf-8")
    raise ConfigurationError("看板资源尚未构建。请运行前端构建命令或使用包含资源的发布包。")


def report_html(report: DashboardReport) -> str:
    from .reading_references import repair_reference_boundaries
    from .reading_toc import filter_source_toc

    # Only full reports carry a reading projection to repair; plain payloads
    # (tests, legacy callers) render as given.
    if isinstance(report, DashboardReport):
        filter_source_toc(report)
        repair_reference_boundaries(report)
    template = get_dashboard_template()
    if template.count(DATA_PLACEHOLDER) != 1:
        raise ConfigurationError("看板模板的数据位置不完整，请重新安装发布包。")
    # HTML script parsing happens before JSON parsing. Encode '<' to make a PDF
    # title containing '</script>' harmless without changing the decoded data.
    data = json.dumps(report.model_dump(mode="json"), ensure_ascii=False)
    data = data.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    html = template.replace(DATA_PLACEHOLDER, data)
    notice = files("paperlens").joinpath("assets/THIRD_PARTY_NOTICES.txt")
    if notice.is_file():
        attribution = json.dumps(notice.read_text(encoding="utf-8"), ensure_ascii=False).replace(
            "<", "\\u003c"
        )
        # Bundled libraries may contain the literal '</body>' in JavaScript.
        # Only the template's final closing element is an insertion boundary.
        closing_body = html.rfind("</body>")
        if closing_body >= 0:
            html = (
                html[:closing_body] + '<script id="paperlens-open-source-notices" '
                f'type="application/json">{attribution}</script>' + html[closing_body:]
            )
    return html


def extracted_text(document: PaperDocument, *, markdown: bool = False) -> str:
    lines = [f"# {document.title}" if markdown else document.title, ""]
    for page in document.pages:
        label = f" · 印刷页码 {page.label}" if page.label else ""
        heading = f"第 {page.number} 页{label}"
        lines.extend([f"## {heading}" if markdown else f"--- {heading} ---", ""])
        blocks = sorted(
            (b for b in document.blocks if b.page == page.number), key=lambda b: b.order
        )
        lines.extend(b.text + "\n" for b in blocks)
        if not blocks:
            lines.append(f"[页面状态：{page.status}]")
    return "\n".join(lines)


def analysis_markdown(report: DashboardReport) -> str:
    analysis = report.analysis
    evidence = {e.id: e for e in report.evidence}
    lines = [
        f"# {analysis.title}",
        "",
        analysis.summary,
        "",
        "## 核心问题",
        "",
        analysis.research_question,
        "",
        "## 论点与依据",
        "",
    ]
    origins = {"author": "作者陈述", "inference": "系统推断", "reader_guide": "读者解释"}
    support = {"supported": "模型核查支持", "partial": "部分支持", "unverified": "待核查"}
    for node in analysis.nodes:
        lines.extend(
            [
                f"### {node.title}",
                "",
                node.summary,
                "",
                node.explanation,
                "",
                f"来源性质：{origins[node.origin]}；{support[node.support]}。",
                "",
            ]
        )
        for eid in node.evidence_ids:
            item = evidence[eid]
            lines.extend(
                [
                    "\n".join(
                        f"> {line}"
                        for line in item.quote.replace("\r\n", "\n").replace("\r", "\n").split("\n")
                    ),
                    "",
                    f"物理页 {item.page} · {item.section or '未识别章节'} · {eid}",
                    "",
                ]
            )
    if report.quality.warnings:
        lines.extend(["## 待核查事项", ""])
        for warning in report.quality.warnings:
            parts = warning.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            lines.append("\n".join([f"- {parts[0]}", *(f"  {line}" for line in parts[1:])]))
    return "\n".join(lines)


@contextmanager
def browser_session(channel: str = "chrome"):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ConfigurationError(
            "缺少看板校验组件，请安装 requirements.txt 中的 playwright。"
        ) from exc
    with sync_playwright() as playwright:
        browser = None
        for candidate in dict.fromkeys([channel, "msedge", "chrome", "chromium"]):
            try:
                browser = playwright.chromium.launch(
                    channel=candidate,
                    headless=True,
                    timeout=20000,
                )
                break
            except Exception:
                continue
        if browser is None:
            raise ConfigurationError(
                "找不到可运行的 Chrome/Edge。请安装浏览器，或安装 Playwright Chromium。"
            )
        try:
            yield browser
        finally:
            browser.close()


def validate_graphs(path: Path, channel: str = "chrome") -> dict:
    with browser_session(channel) as browser:
        context = browser.new_context(viewport={"width": 1440, "height": 900}, offline=True)
        page = context.new_page()
        # The landing page is the library. The real validator is installed by
        # the reader; main.tsx only supplies an empty placeholder before then.
        # Authenticated workspace paths can exceed Windows browser file URL limits.
        # The report is self-contained; about:blank preserves offline mode while
        # supplying the exact same bytes without a second filesystem reader.
        page.goto("about:blank#/read/local/map")
        page.set_content(path.read_text(encoding="utf-8"), wait_until="load", timeout=60000)
        page.wait_for_function("window.__paperlensReaderReady === true", timeout=60000)
        result = page.evaluate("async () => await window.__paperlensValidate()")
        context.close()
        return result


def write_report(
    report: DashboardReport, output_dir: Path, *, browser_channel: str = "chrome"
) -> Path:
    """A report is published only after actual offline syntax and SVG checks."""
    output_dir.mkdir(parents=True, exist_ok=True)
    draft = output_dir / ".dashboard-check.html"
    final = output_dir / "dashboard.html"
    try:
        for attempt in range(2):
            draft.write_text(report_html(report), encoding="utf-8")
            checked = validate_graphs(draft, browser_channel)
            valid = {
                g["id"]: g for g in checked.get("graphs", []) if g.get("valid") and g.get("svg")
            }
            missing = [g for g in report.graphs if g.id not in valid]
            if not missing:
                for graph in report.graphs:
                    graph.validated = True
                    (output_dir / f"{graph.id}.svg").write_text(
                        valid[graph.id]["svg"], encoding="utf-8"
                    )
                report.quality.checks.append(
                    f"{len(report.graphs)} 幅导图经离线浏览器中的同版 Mermaid 解析与 SVG 渲染校验"
                    if report.graphs else "部分阅读结果已通过离线页面检查；本次尚无导图。"
                )
                break
            if attempt == 1:
                raise ValidationError("导图校验在规则修复后仍失败，未发布无法打开的看板。")
            for graph in missing:
                graph.mermaid = compile_mermaid(
                    report.analysis,
                    graph.node_ids,
                    graph.edge_ids,
                    conservative=True,
                    show_edge_labels=graph.id != "overview",
                )
            report.quality.warnings.append(
                "部分导图标题使用保守字符规则修复，完整内容保留在节点详情。"
            )
        draft.write_text(report_html(report), encoding="utf-8")
        os.replace(draft, final)
    finally:
        draft.unlink(missing_ok=True)
    write_json(output_dir / "report.json", report.model_dump(mode="json"))
    (output_dir / "analysis.md").write_text(analysis_markdown(report), encoding="utf-8")
    for graph in report.graphs:
        (output_dir / f"{graph.id}.mmd").write_text(graph.mermaid, encoding="utf-8")
    return final
