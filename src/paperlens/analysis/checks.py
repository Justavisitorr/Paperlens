"""Conservative checks for claims whose units need explicit review.

These flags do not establish semantic truth or rewrite a paper's numbers. They
prevent ambiguous percentage differences from receiving an unqualified badge.
"""

import re

from ..models import PaperNode

PERCENT_CHANGE = re.compile(
    r"(?:提升|提高|增加|下降|降低|高于|超过|超出|低于|高[+＋]|低[−-])[^。；;，,\n]{0,30}[％%]"
)


def metric_issues(node: PaperNode) -> list[str]:
    issues = []
    for text in (node.title, node.summary, node.explanation):
        for sentence in re.split(r"[。；;\n]", text):
            explicit_unknown = any(
                term in sentence
                for term in ("口径待核查", "无法判断", "无法确定", "未明确", "不确定性")
            )
            for match in PERCENT_CHANGE.finditer(sentence):
                context = sentence[max(0, match.start() - 12) : match.end()]
                if "相对" not in context and "百分点" not in match.group() and not explicit_unknown:
                    issues.append("数值变化使用百分号但未明确相对分母；需区分百分点与相对百分比。")
    return list(dict.fromkeys(issues))
