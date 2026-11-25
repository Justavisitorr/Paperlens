"""Bounded LaTeX-to-MathML conversion and honest formula provenance.

This module does not reconstruct mathematics from flattened PDF text. Rendering
validates syntax only; an image/model transcription still needs source review.
No TeX executable, shell command, file include or network resource is evaluated.
"""

from __future__ import annotations

import re
from typing import Literal
from xml.etree import ElementTree as ET

from paperlens.models import Block, FormulaRecord

FormulaSource = Literal["pdf_text", "docling", "vision"]
GLYPH_MARKER = "[未识别字形]"
GLYPH_NOISE = re.compile(r"\(cid:\s*\d+\)|\ufffd|\x00", re.I)
_MAX_LATEX = 4096
_MAX_DEPTH = 32
_MATHML_NS = "http://www.w3.org/1998/Math/MathML"
_FORBIDDEN = re.compile(
    r"\\(?:input|include|includegraphics|write\d*|openout|openin|read|immediate|"
    r"catcode|csname|endcsname|def|edef|gdef|xdef|let|futurelet|newcommand|renewcommand|"
    r"providecommand|newenvironment|usepackage|documentclass|special|href|url|html\w*|"
    r"class|style|require|verb|loop|repeat|if\w*|every\w*)\b",
    re.I,
)
_TAGS = {
    "math",
    "mrow",
    "mi",
    "mn",
    "mo",
    "mtext",
    "mspace",
    "mfrac",
    "msqrt",
    "mroot",
    "msub",
    "msup",
    "msubsup",
    "munder",
    "mover",
    "munderover",
    "mmultiscripts",
    "mprescripts",
    "none",
    "mtable",
    "mtr",
    "mtd",
    "mstyle",
    "mphantom",
    "mpadded",
    "menclose",
    "mfenced",
}
_ATTRS = {
    "display",
    "mathvariant",
    "stretchy",
    "largeop",
    "movablelimits",
    "accent",
    "accentunder",
    "width",
    "height",
    "depth",
    "linebreak",
    "linethickness",
    "notation",
    "columnalign",
    "rowalign",
    "columnspacing",
    "rowspacing",
    "columnlines",
    "rowlines",
    "frame",
    "framespacing",
    "displaystyle",
    "scriptlevel",
    "separator",
    "fence",
    "form",
    "voffset",
    "lspace",
    "rspace",
    "minsize",
    "maxsize",
    "mathsize",
    "mathcolor",
    "mathbackground",
    "open",
    "close",
    "separators",
    "equalrows",
    "equalcolumns",
}


def glyph_warnings(raw: str) -> list[str]:
    count = len(GLYPH_NOISE.findall(raw))
    return [f"{count} 处字形无法可靠解码，已显示占位并保留原始文本；请查看原页。"] if count else []


def strip_math_delimiters(latex: str) -> str:
    text = latex.strip()
    while True:
        fence = re.fullmatch(r"```(?:latex|tex|math)?[ \t]*\r?\n([\s\S]*?)\r?\n```", text, re.I)
        if fence:
            text = fence[1].strip()
            continue
        for opening, closing in (("$$", "$$"), (r"\[", r"\]"), (r"\(", r"\)"), ("$", "$")):
            if len(text) <= len(opening) + len(closing):
                continue
            if not (text.startswith(opening) and text.endswith(closing)):
                continue
            inner = text[len(opening) : -len(closing)]
            # Keep separate expressions and prose intact; escaped currency is literal.
            tokens = re.findall(r"\\[\s\S]|[$\\]", inner)
            if ("$" in tokens or tokens[-1:] == ["\\"]) if opening == "$" else closing in inner:
                continue
            text = inner.strip()
            break
        else:
            return text


def is_latex_candidate(text: str) -> bool:
    """Only recognize explicit markup, never infer fractions from text order."""
    return bool(
        re.search(
            r"\\(?:frac|dfrac|tfrac|sqrt|sum|prod|int|begin|operatorname|mathrm|"
            r"mathbf|mathbb|alpha|beta|theta|lambda|delta|sigma|hat|bar|left)\b",
            text,
        )
        or re.fullmatch(
            r"\s*(?:\$\$[\s\S]+\$\$|\$[^$]+\$|\\\[[\s\S]+\\\]|"
            r"\\\([\s\S]+\\\))\s*",
            text,
        )
    )


def looks_like_formula(text: str) -> bool:
    """Conservative flag for standalone expressions; this is not formula OCR."""
    if not text.strip() or len(text) > 500 or re.search(r"[\u3400-\u9fff]", text):
        return False
    if is_latex_candidate(text):
        if strip_math_delimiters(text) != text.strip():
            return True
        without_commands = re.sub(r"\\[A-Za-z]+", "", text)
        return not re.search(r"[A-Za-z]{3,}", without_commands)
    if not re.search(r"[=≤≥≈∑∏∫]|\barg\s*(?:min|max)\b", text, re.I):
        return False
    words = re.findall(r"[A-Za-z]{3,}", text)
    math_words = {"arg", "argmin", "argmax", "min", "max", "log", "exp", "sin", "cos", "tan", "lim"}
    return not any(word.casefold() not in math_words for word in words)


def _validate_input(latex: str) -> str | None:
    if not latex or len(latex) > _MAX_LATEX:
        return "公式为空或超过 4096 字符限制。"
    if GLYPH_NOISE.search(latex) or GLYPH_MARKER in latex:
        return "公式仍含未识别字形，不能作为有效 LaTeX 编译。"
    if _FORBIDDEN.search(latex):
        return "只支持数学表达式；文件、宏定义、链接和执行指令不可用。"
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", latex):
        return "公式包含控制字符。"
    # Unescaped braces are structural. Escaped braces remain ordinary symbols.
    depth = 0
    for token in re.findall(r"\\.|[{}]", latex):
        if token == "{":
            depth += 1
        elif token == "}":
            depth -= 1
        if depth < 0 or depth > _MAX_DEPTH:
            return "公式括号不匹配或嵌套超过 32 层。"
    if depth:
        return "公式括号不匹配。"
    return None


def _safe_mathml(xml: str) -> str:
    if len(xml) > 262144:
        raise ValueError("MathML is too large")
    root = ET.fromstring(xml)
    for element in root.iter():
        if not element.tag.startswith("{" + _MATHML_NS + "}"):
            raise ValueError("Unexpected namespace")
        if element.tag.split("}", 1)[1] not in _TAGS:
            raise ValueError("Unsupported MathML element")
        if any(
            name not in _ATTRS or len(value) > 120 or re.search(r"url\s*\(|[<>]", value, re.I)
            for name, value in element.attrib.items()
        ):
            raise ValueError("Unsupported MathML attribute")
    if "\\" in "".join(root.itertext()):
        raise ValueError("Unrecognized LaTeX command")
    return xml


def _converter_input(latex: str) -> str:
    """Adapt equivalent notation unsupported by the pinned converter.

    Preserve the original LaTeX in FormulaRecord. Tokenize escaped backslashes
    too, so a row separator followed by literal letters is never a command.
    """
    equivalents = {
        r"\arg": r"\operatorname{arg}",
        r"\begin{aligned}": r"\begin{split}",
        r"\end{aligned}": r"\end{split}",
    }
    return re.sub(
        r"\\(?:begin|end)\{aligned\}|\\[A-Za-z]+|\\.",
        lambda match: equivalents.get(match[0], match[0]),
        latex,
    )


def compile_formula(latex: str, *, source: FormulaSource = "pdf_text") -> FormulaRecord:
    """Return a safe renderable candidate or an explicit error, never a guess."""
    text = strip_math_delimiters(latex)
    error = _validate_input(text)
    if error:
        return FormulaRecord(
            latex=text[:_MAX_LATEX], source=source, status="invalid", warnings=[error]
        )
    try:
        from latex2mathml.converter import convert

        mathml = _safe_mathml(convert(_converter_input(text), display="block"))
    except ImportError:
        return FormulaRecord(
            latex=text,
            source=source,
            status="unresolved",
            warnings=["LaTeX 编译依赖未安装，请安装项目依赖后重试。"],
        )
    except Exception as exc:
        return FormulaRecord(
            latex=text,
            source=source,
            status="invalid",
            warnings=[f"LaTeX 未通过安全语法检查（{type(exc).__name__}），请查看原页。"],
        )
    return FormulaRecord(
        latex=text,
        mathml=mathml,
        source=source,
        status="renderable",
        verified=False,
        warnings=["LaTeX 可排版；尚未确认与原页数学含义一致。"],
    )


def annotate_block(
    block: Block, *, source: FormulaSource = "pdf_text", latex_candidate: str | None = None
) -> Block:
    """Annotate extraction in place, retaining raw text and original geometry."""
    block.text_warnings = list(dict.fromkeys(block.text_warnings + glyph_warnings(block.raw_text)))
    if block.kind in {"text", "heading"} and looks_like_formula(block.raw_text):
        block.kind = "formula"
    if block.kind != "formula":
        return block
    candidate = latex_candidate
    if candidate is None and is_latex_candidate(block.raw_text):
        candidate = block.raw_text
    block.formula = (
        compile_formula(candidate, source=source)
        if candidate
        else FormulaRecord(
            source=source,
            status="unresolved",
            verified=False,
            warnings=["PDF 仅提取到线性公式文本，尚未恢复二维结构；请查看原页，不能据此补猜公式。"],
        )
    )
    return block


def apply_formula_candidate(block: Block, latex: str, *, source: FormulaSource = "vision") -> Block:
    """Attach a visually recovered candidate to an already located formula.

    A separate copy allows callers to compare the candidate against the original
    block. Even successful compilation leaves semantic verification pending.
    """
    if block.kind != "formula":
        raise ValueError("Formula recovery requires a located formula block")
    if block.bbox.x1 <= block.bbox.x0 or block.bbox.y1 <= block.bbox.y0:
        raise ValueError("Formula recovery requires a nonempty source region")
    result = block.model_copy(deep=True)
    result.formula = compile_formula(latex, source=source)
    return result
