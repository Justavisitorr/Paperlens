"""Bare prose footnote numbers do not trigger paid mathematical verification."""

import pytest

from paperlens.edition_math_review import _complexity
from paperlens.models import ReadingItem


@pytest.mark.parametrize("content", ["The tool$^{1}$ is available.", "Tools$^{1,2}$ work."])
def test_bare_footnote_markers_are_not_complex_math(content):
    assert _complexity(ReadingItem(id="p", kind="paragraph", content=content)) == 0


@pytest.mark.parametrize("kind,content", [
    ("paragraph", "The quantity $x^{2}$ is measured."),
    ("paragraph", "We measure $^{14}C$ activity."),
    ("formula", r"x^{2} + \frac{a}{b}"),
])
def test_actual_exponents_and_isotope_notation_remain_eligible(kind, content):
    assert _complexity(ReadingItem(id="p", kind=kind, content=content)) > 0
