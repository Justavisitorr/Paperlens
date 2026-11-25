from pathlib import Path

import pdfplumber
import pytest
from test_reading_projection import b, report

from paperlens.config import Settings
from paperlens.profiles import apply_profile, profile_limits
from paperlens.reading import build_reading, section_level
from paperlens.reading_code import code_language, code_text, is_code
from paperlens.reading_edition import EditionBatch, EditionBlock, apply_edition
from paperlens.runtime import RuntimeSettings


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("auto", [True, True, True]),
        ("on", [True, True, True]),
        ("off", [True, True, True]),
    ],
)
def test_retired_code_policy_cannot_drop_directly_read_code(mode, expected):
    settings = Settings(_env_file=None, paperlens_code_mode=mode)
    assert [
        profile_limits(apply_profile(settings, p))["code_blocks"]
        for p in ("economy", "standard", "detailed")
    ] == expected


def test_period_headings_keep_appendices_and_listings_out_of_references():
    blocks = [
        b("title", "Research Paper", kind="heading", docling_label="title"),
        b("intro", "1. Introduction", kind="heading", order=1),
        b("body", "A paragraph.", order=2),
        b("refs", "References", kind="heading", order=3),
        b("ref", "[1] Author. Paper.", order=4),
        b("app", "A. Programs", page=2, kind="heading", order=5),
        b("sub", "A.1. Solver", page=2, kind="heading", order=6),
        b("prose", "Complete implementation follows.", page=2, order=7),
        b(
            "code",
            "void solve() {\n    double x = 1;\n}",
            page=2,
            kind="algorithm",
            docling_label="code",
            order=8,
        ),
    ]
    r = report(blocks)
    r.run.processing_limits["code_blocks"] = True
    original = r.document.model_dump()
    result = build_reading(r)
    assert len(result.references) == 1
    assert [i.label for i in result.items if i.kind == "heading"] == [
        "1. Introduction",
        "A. Programs",
        "A.1. Solver",
    ]
    assert result.items[-1].kind == "code"
    assert result.items[-1].content == blocks[-1].text
    assert result.information["titles"] == ["title"]
    assert section_level("A.1. Solver") == 2
    assert r.document.model_dump() == original


@pytest.mark.parametrize("enabled,kind", [(True, "code"), (False, "algorithm")])
def test_code_breaks_reference_region_even_without_appendix_heading(enabled, kind):
    r = report(
        [
            b("refs", "References", kind="heading"),
            b("ref", "[1] Author. Paper.", order=1),
            b("code", "int main() { return 0; }", kind="algorithm", docling_label="code", order=2),
            b("tail", "This implementation uses a linear scan.", order=3),
        ]
    )
    r.run.processing_limits["code_blocks"] = enabled
    result = build_reading(r)
    assert len(result.references) == 1
    assert [i.kind for i in result.items] == [kind, "paragraph"]


def test_native_pdf_indent_blank_lines_gutter_and_page_continuation(tmp_path: Path):
    from reportlab.pdfgen import canvas

    path = tmp_path / "code.pdf"
    pdf = canvas.Canvas(str(path), pagesize=(600, 800))

    def line(number, text, y):
        pdf.setFont("Helvetica", 8)
        pdf.drawString(64, y, str(number))
        pdf.setFont("Courier", 10)
        pdf.drawString(90, y, text)

    line(1, "int main() {", 700)
    line(2, "    int x = 2;", 685)
    line(4, "    return x;", 655)
    pdf.showPage()
    line(5, "        do_work();", 700)
    line(6, "        return 42;", 685)
    pdf.save()
    layout = {}
    block = b("code", "flattened", kind="algorithm", docling_label="code", box=(0.1, 0.1, 0.9, 0.3))
    with pdfplumber.open(path) as doc:
        text = code_text(block, doc.pages[0], layout)
        tail = code_text(block, doc.pages[1], layout)
    assert text == "int main() {\n    int x = 2;\n\n    return x;"
    assert tail == "        do_work();\n        return 42;"


def test_model_code_never_gets_math_or_word_break_repairs():
    text = '    const char* s = "$$x_i$$ <script>";\n    auto x = "inter- national";'
    r = report([b("code", text, docling_label="code", kind="algorithm")])
    r.run.processing_limits["code_blocks"] = True
    r.reading = build_reading(r)
    apply_edition(
        r,
        [
            EditionBatch(
                items=[
                    EditionBlock(
                        kind="code",
                        page=1,
                        source_ids=["code"],
                        content=text,
                        code_language="cpp",
                        box=(0.1, 0.1, 0.45, 0.2),
                    )
                ]
            )
        ],
    )
    assert r.reading.items[0].content == text
    assert r.reading.items[0].code_language == "cpp"
    assert not r.reading.references


def test_does_not_guess_prose_and_math_are_source_code():
    assert not is_code(b("formula", "We use const weights and sum_{i=1}^{n} x_i."))
    assert code_language("Read the instructions below.\n1. Compare results.") == "plaintext"
    assert code_language("#include <vector>\nstd::vector<int> values;") == "cpp"


def test_code_keeps_chinese_comments_in_a_different_pdf_font(tmp_path):
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    path = tmp_path / "comments.pdf"
    pdf = canvas.Canvas(str(path), pagesize=(600, 800))
    prefix = "memset(a, 0, sizeof a); // "
    pdf.setFont("Courier", 10)
    pdf.drawString(90, 700, prefix)
    pdf.setFont("STSong-Light", 10)
    pdf.drawString(90 + len(prefix) * 6, 700, "全部设为零")
    pdf.save()
    block = b("code", "fallback", box=(0.1, 0.1, 0.9, 0.3))
    with pdfplumber.open(path) as doc:
        assert code_text(block, doc.pages[0]) == prefix + "全部设为零"


def test_runtime_rejects_retired_code_filter(tmp_path):
    settings = Settings(_env_file=None, paperlens_cache_dir=tmp_path)
    runtime = RuntimeSettings(settings)
    assert "PAPERLENS_CODE_MODE" in runtime.update({"PAPERLENS_CODE_MODE": "off"})["errors"]
    assert profile_limits(RuntimeSettings(settings).snapshot())["code_blocks"]
