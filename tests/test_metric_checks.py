import pytest

from paperlens.analysis.checks import metric_issues
from paperlens.models import PaperNode


@pytest.mark.parametrize(
    "text,ambiguous",
    [
        ("比基线高+27.1%", True),
        ("准确率提高8.3%", True),
        ("相对准确率提高10.6%", False),
        ("准确率提高8.3个百分点", False),
        ("测试集准确率86.3%", False),
        ("作者报告提升5.0%，但口径待核查。", False),
        ("前项口径待核查。另一实验准确率提升5.0%。", True),
    ],
)
def test_percentage_changes_require_a_clear_unit(text, ambiguous):
    node = PaperNode(id="metric", kind="evidence", title="比较结果", summary=text)
    assert bool(metric_issues(node)) is ambiguous
