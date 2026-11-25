"""Review matching preserves distinct methods within the same table column."""

import pytest

from paperlens.chart_evidence import reconcile_readings
from paperlens.chart_models import ChartReading, ChartStructure
from paperlens.visual_response import VisualResponseItem


def swag_table(base_score=81.6, large_score=86.6):
    return VisualResponseItem(
        visual_id="V1",
        status="analyzed",
        chart=ChartStructure(chart_type="table", series=["Dev"]),
        results=[
            ChartReading(
                metric="Accuracy",
                method=method,
                series="Dev",
                x="Dev",
                value=value,
                value_text=str(value),
                source="figure_label",
            )
            for method, value in [("BERT_BASE", base_score), ("BERT_LARGE", large_score)]
        ],
    )


@pytest.mark.parametrize("reverse_review", [False, True])
def test_review_matches_each_method_even_with_shared_series(reverse_review):
    first, reviewed = swag_table(), swag_table()
    if reverse_review:
        reviewed.results.reverse()
    merged, conflicts = reconcile_readings(first, reviewed)
    assert not conflicts
    assert [(row.method, row.value) for row in merged.results] == [
        ("BERT_BASE", 81.6), ("BERT_LARGE", 86.6),
    ]


def test_review_conflict_hides_only_the_affected_method():
    first, reviewed = swag_table(), swag_table(large_score=86.7)
    merged, conflicts = reconcile_readings(first, reviewed)
    assert len(conflicts) == 1
    assert [(row.method, row.value, row.source) for row in merged.results] == [
        ("BERT_BASE", 81.6, "figure_label"),
        ("BERT_LARGE", None, "figure_only"),
    ]
