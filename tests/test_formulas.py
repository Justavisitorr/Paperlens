"""Conversion compatibility and safety regressions, independent of model output."""

from xml.etree import ElementTree as ET

import pytest

from paperlens.formulas import compile_formula, strip_math_delimiters


@pytest.mark.parametrize(
    "wrapper",
    ["{}", "${}$", "$ \n{}\n $", "$$\n{}\n$$", r"\[{}\]", r"\({}\)", "```latex\n$${}$$\n```"],
)
def test_formula_field_wrappers_are_not_math_syntax(wrapper):
    latex = r"\theta^{t+1}=\theta^t-\eta\left(\frac{\partial f}{\partial\theta^t}+2\lambda\theta^t\right)"
    formula = compile_formula(wrapper.format(latex))
    assert formula.status == "renderable" and formula.latex == latex
    assert formula.verified is False


@pytest.mark.parametrize(
    "latex", [r"\$100", "$a$ + $b$", "text $a$", "$a", r"\begin{aligned}a&=b\\c&=d\end{aligned}"]
)
def test_delimiter_normalization_keeps_nonwrappers_and_row_separators(latex):
    assert strip_math_delimiters(latex) == latex


@pytest.mark.parametrize(
    "latex",
    [
        r"\arg\min_x \frac{1}{2}\|x-y\|^2",
        r"\begin{aligned} x &= y+1 \\ z &= \begin{matrix}a\\b\end{matrix} \end{aligned}",
        r"\underset{V}{\operatorname{argmin}}\sum_{i=1}^n\ell(\hat{y}_i,y_i)+\lambda\|V\|_F^2",
    ],
)
def test_optimizer_and_aligned_notation_preserve_source(latex):
    formula = compile_formula(latex, source="vision")
    assert formula.status == "renderable"
    assert formula.latex == latex and formula.verified is False
    root = ET.fromstring(formula.mathml)
    if "aligned" in latex:
        ns = {"m": "http://www.w3.org/1998/Math/MathML"}
        assert len(root.findall(".//m:mtable", ns)) == 2
        assert "x" in "".join(root.itertext()) and "z" in "".join(root.itertext())


@pytest.mark.parametrize(
    "latex",
    [
        r"\input{secret}",
        r"\href{https://example.org}{x}",
        r"\unknown{x}",
        r"\frac{x}{",
        r"x+(cid:4890)",
        r"\text{<script>alert(1)</script>}",
    ],
)
def test_unsafe_or_unknown_formulas_never_become_renderable(latex):
    formula = compile_formula(latex)
    assert formula.status == "invalid" and formula.mathml is None
