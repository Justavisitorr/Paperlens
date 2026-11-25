"""Code listings keep their own boundaries and native PDF line geometry."""

from __future__ import annotations

import re
from statistics import median
from textwrap import dedent

from .models import Block


def is_code(block: Block) -> bool:
    if block.docling_label == "code":
        return True
    text = block.text.strip()
    if text.startswith("```") and text.endswith("```"):
        return True
    # Require multiple actual source lines; mathematical prose is not source code.
    return (
        sum(
            bool(
                re.match(
                    r"\s*(?:#include\s*[<\"]|#define\s+\w+|def\s+\w+\(|class\s+\w+[:(]|"
                    r"(?:const |static )?(?:void|int|double|float)\s+\w+\s*[=(;])",
                    line,
                )
            )
            for line in text.splitlines()
        )
        >= 2
    )


def code_language(text: str) -> str:
    if re.search(r"#include\s*[<\"]|\bstd::|\b(?:__global__|__device__)\b", text):
        return "cpp"
    if re.search(
        r"(?m)^\s*(?:def \w+\(|from \w[.\w]* import |import (?:torch|triton|numpy)\b)", text
    ):
        return "python"
    if re.search(r"(?m)^\s*(?:const|let) \w+\s*=|\bfunction \w+\(", text):
        return "javascript"
    return "plaintext"


def code_text(block: Block, page=None, layout: dict | None = None) -> str:
    """Recover lines and indentation, omitting a separate printed line-number gutter.

    No model calls or repairs to program semantics. When no native text is available,
    retain the extracted text; source anchors continue to point at the original page.
    """
    fallback = block.text.strip("\n")
    fence = re.fullmatch(r"```[^\n]*\n([\s\S]*?)\n```", fallback.strip())
    if fence:
        fallback = fence[1]
    if page is None:
        return fallback
    b = block.bbox
    crop = page.crop(
        (
            max(0, b.x0 * page.width - 1),
            max(0, b.y0 * page.height - 1),
            min(page.width, b.x1 * page.width + 1),
            min(page.height, b.y1 * page.height + 1),
        )
    )
    chars = [c for c in crop.chars if c["text"] and c["width"] > 0]
    mono = [
        c
        for c in chars
        if re.search(r"mono|courier|consol|typewriter|sourcecode", c["fontname"], re.I)
    ]
    if len(mono) < len(chars) * 0.6 or not mono:
        native = dedent(crop.extract_text(layout=True) or "").strip("\n")
        return "\n".join(line.rstrip() for line in native.splitlines()) or fallback
    # A distinct small-font number gutter may be omitted. Keep other fonts:
    # Chinese comments and symbols commonly use a different fallback font.
    width = median(c["width"] for c in mono)
    size = median(c["size"] for c in mono)
    left = min(c["x0"] for c in mono)
    # A continuation page may contain only nested lines. Reuse the listing's
    # original left edge instead of removing its indentation on every new page.
    if layout is not None:
        if "left" in layout and abs(layout["box_left"] - b.x0) < 0.025:
            left = min(left, layout["left"] * page.width)
        layout.update(left=left / page.width, box_left=b.x0)
    content = [
        c
        for c in chars
        if not (c["x1"] < left and c["size"] < size * 0.9 and c["text"].strip().isdigit())
    ]
    rows: list[list[dict]] = []
    # Baselines align even when fonts have different ascender metrics.
    for char in sorted(content, key=lambda c: (c["bottom"], c["x0"])):
        if not rows or abs(char["bottom"] - rows[-1][0]["bottom"]) > size * 0.35:
            rows.append([])
        rows[-1].append(char)
    tops = [r[0]["bottom"] for r in rows]
    gaps = [b - a for a, b in zip(tops, tops[1:], strict=False) if b > a]
    line_height = median(sorted(gaps)[: max(1, len(gaps) // 2)]) if gaps else size * 1.2
    lines = []
    last_top = None
    for row in rows:
        top = row[0]["bottom"]
        if last_top is not None:
            lines.extend([""] * max(0, min(8, round((top - last_top) / line_height) - 1)))
        line = ""
        cursor = left
        for c in sorted(row, key=lambda c: c["x0"]):
            line += " " * max(0, round((c["x0"] - cursor) / width)) + c["text"]
            cursor = max(cursor, c["x1"])
        lines.append(line.rstrip())
        last_top = top
    return "\n".join(lines).strip("\n") or fallback
