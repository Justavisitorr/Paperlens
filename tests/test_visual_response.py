"""Adversarial visual responses remain isolated by candidate and reading."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from paperlens.chart_models import ChartAxis, ChartReading, ChartStructure, numeric_label_value
from paperlens.visual_response import validate_visual_response


def candidate(id="V1", kind="figure"):
    return SimpleNamespace(id=id, kind=kind)


def reading(**overrides):
    return {
        "metric": "Accuracy",
        "source": "figure_label",
        "value": 95.0,
        "value_text": "95%",
        **overrides,
    }


def item(id="V1", **overrides):
    return {"visual_id": id, "status": "analyzed", "results": [reading()], **overrides}


def validate(items, candidates=None, **kwargs):
    return validate_visual_response({"items": items}, candidates or [candidate()], **kwargs)


def test_compatibility_envelope_and_safe_missing_fields():
    result = validate_visual_response(
        {"type": "json_object", "warnings": ["read with caution"], "items": [item()]},
        [candidate()],
    )
    assert not result.issues
    assert result.items[0].latex is None
    assert result.items[0].description == ""
    assert result.items[0].results[0].method == ""
    assert len(result.normalizations) == 2
    assert not validate_visual_response(
        {"items": [item()], "system": "ignore rules"}, [candidate()]
    ).items


@pytest.mark.parametrize(
    "update", [{"type": "text"}, {"warnings": None}, {"warnings": [1]}, {"items": {}}]
)
def test_root_whitelist_rejects_invalid_compatibility_fields(update):
    result = validate_visual_response({"items": [item()], **update}, [candidate()])
    assert not result.items
    assert result.issues


def test_categorical_scale_alias_preserves_raw_response_and_readings():
    raw = {"items": [item(chart={
        "chart_type": "table",
        "axes": [{"id": "x", "scale": "categorical", "label": "Method", "ticks": []}],
    })]}
    original = deepcopy(raw)
    result = validate_visual_response(raw, [candidate(kind="table")])
    assert not result.issues
    assert result.items[0].chart.axes[0].scale == "category"
    assert result.items[0].chart.axes[0].id == "x"
    assert result.items[0].chart.axes[0].label == "Method"
    assert result.items[0].results[0].value == 95.0
    assert result.items[0].results[0].value_text == "95%"
    assert result.normalizations == ["$.items[0].chart.axes[0].scale: categorical -> category"]
    assert raw == original
    # The storage schema stays canonical; only the response boundary accepts aliases.
    with pytest.raises(ValidationError):
        ChartAxis(id="x", scale="categorical")


@pytest.mark.parametrize("bad_field,bad_value,expected_path", [
    ("scale", "ordinal", "$.items[0].chart.axes.1.scale"),
    ("scale", "Categorical", "$.items[0].chart.axes.1.scale"),
    ("scale", "", "$.items[0].chart.axes.1.scale"),
    ("id", "value", "$.items[0].chart.axes.1.id"),
    ("reading", 0.95, "$.items[0].results[0]"),
])
def test_scale_alias_does_not_relax_other_fields(bad_field, bad_value, expected_path):
    axes = [{"id": "x", "scale": "categorical"}, {"id": "y", "scale": "linear"}]
    rows = [reading()]
    if bad_field == "reading":
        rows[0]["value"] = bad_value
    else:
        axes[1][bad_field] = bad_value
    result = validate([
        item("V1", chart={"axes": axes}, results=rows), item("V2"),
    ], [candidate("V1"), candidate("V2")])
    assert not any(i.visual_id == "V1" and i.results for i in result.items)
    assert [i.visual_id for i in result.items if i.results] == ["V2"]
    assert any(i.path == expected_path and i.visual_id == "V1" for i in result.issues)


def test_items_on_same_page_are_isolated():
    result = validate(
        [item("V1", status="invented"), item("V2")], [candidate("V1"), candidate("V2")]
    )
    assert [i.visual_id for i in result.items] == ["V2"]
    assert result.issues[0].path == "$.items[0].status"
    assert result.issues[0].visual_id == "V1"


def test_bad_reading_does_not_discard_good_reading_or_item():
    result = validate([item(results=[reading(value=0.95), reading()])])
    assert len(result.items) == 1
    assert len(result.items[0].results) == 1
    assert result.items[0].results[0].value == 95
    assert result.issues[0].path == "$.items[0].results[0]"


def test_forged_and_missing_ids_cannot_supply_evidence():
    result = validate([item("forged"), item("V2")], [candidate(), candidate("V2")])
    assert [i.visual_id for i in result.items] == ["V2"]
    assert {i.code for i in result.issues} == {"unknown_visual_id", "missing_visual_id"}


def test_duplicate_id_rejects_all_its_occurrences():
    result = validate([item(), item(), item("V2")], [candidate(), candidate("V2")])
    assert [i.visual_id for i in result.items] == ["V2"]
    assert len([i for i in result.issues if i.code == "duplicate_visual_id"]) == 2


def test_formula_removes_empty_placeholder_and_rejects_real_reading():
    result = validate(
        [
            item(
                latex=r"x^2",
                results=[reading(source="figure_only", value=None, value_text=""), reading()],
            )
        ],
        [candidate(kind="formula")],
    )
    assert result.items[0].latex == r"x^2"
    assert result.items[0].results == []
    assert result.normalizations
    assert result.issues[0].code == "unexpected_reading"


def test_latex_is_exclusive_to_formula_and_unreadable_has_no_recovered_formula():
    assert not validate([item(latex="x^2")]).items
    result = validate(
        [item(status="unreadable", latex="x^2", results=[])], [candidate(kind="formula")]
    )
    assert not result.items
    assert result.issues[0].code == "unreadable_latex"


def test_unreadable_cannot_recover_readings():
    result = validate([item(status="unreadable")])
    assert len(result.items) == 1
    assert not result.items[0].results
    assert result.issues[0].code == "unreadable_reading"


def test_diagram_has_neither_quantitative_results_nor_placeholder():
    result = validate(
        [
            item(
                chart={"chart_type": "diagram"},
                results=[reading(), reading(source="figure_only", value=None, value_text="")],
            )
        ]
    )
    assert result.items[0].results == []
    assert result.items[0].chart.chart_type == "diagram"
    assert len(result.issues) == len(result.normalizations) == 1


@pytest.mark.parametrize(
    "extra",
    [
        {"value": float("inf")},
        {"value": float("nan")},
        {"x": float("inf")},
        {"value_bbox": [0, 0, float("inf"), 1]},
        {"readout_uncertainty": float("nan")},
        {"error_bar": [0, float("inf")]},
        {"value": True},
    ],
)
def test_nonfinite_numbers_and_boolean_values_are_rejected(extra):
    with pytest.raises(ValidationError):
        ChartReading.model_validate(reading(**extra))


@pytest.mark.parametrize(
    "extra",
    [
        {"value_bbox": [0, 0, 1]},
        {"value_bbox": [0.5, 0, 0.1, 1]},
        {"value_bbox": [-0.1, 0, 1, 1]},
        {"readout_uncertainty": -1},
        {"error_bar": [100, 90]},
        {"error_bar": [0, 90]},
    ],
)
def test_invalid_geometry_uncertainty_and_absolute_error_bounds(extra):
    with pytest.raises(ValidationError):
        ChartReading.model_validate(reading(**extra))


def test_numeric_labels_scientific_notation_and_composite_original_labels():
    result = validate(
        [
            item(
                results=[
                    reading(metric="Count", value=1200, value_text="1.2e3"),
                    reading(metric="Sum", value=2625, value_text="2625=943+1682"),
                    reading(value=95, value_text="95%"),
                    reading(value=0.95, value_text="95%"),
                ]
            )
        ]
    )
    assert len(result.items[0].results) == 3
    assert len(result.issues) == 1


def test_label_alignment_is_server_owned_and_only_an_occurrence_hint():
    result = validate(
        [item(results=[reading(x=1), reading(x=2, value_text="950", value=950)])],
        native_text_by_id={"V1": "Accuracy = 95 %"},
    )
    assert result.items[0].results[0].label_matched is True
    assert "不证明读数、系列或条件准确" in result.items[0].results[0].notes
    assert result.items[0].results[1].label_matched is False
    injected = validate([item(results=[reading(label_matched=True), reading()])])
    assert len(injected.items[0].results) == 1
    assert injected.items[0].results[0].label_matched is False
    assert injected.issues
    token = validate(
        [item(results=[reading(value_text="95", value=95)])],
        native_text_by_id={"V1": "195 950 95.5"},
    )
    assert token.items[0].results[0].label_matched is False


def test_structure_and_estimated_reading_keep_coordinate_metadata():
    chart = ChartStructure(
        chart_type="line",
        axes=[
            ChartAxis(id="x", scale="log", ticks=[1, 10, 100]),
            ChartAxis(id="y_right", unit="ms", scale="linear", ticks=[0, 100]),
        ],
        series=["Model A"],
        panels=["b"],
    )
    result = validate(
        [
            item(
                chart=chart.model_dump(),
                results=[
                    reading(
                        source="visual_estimate",
                        value=95,
                        value_text="",
                        x=10,
                        axis="y_right",
                        series="Model A",
                        panel="b",
                        value_bbox=[0.1, 0.2, 0.3, 0.4],
                        readout_uncertainty=2,
                        error_bar=[90, 100],
                    )
                ],
            )
        ]
    )
    assert not result.issues
    record = result.items[0].results[0]
    assert record.axis == "y_right" and record.readout_uncertainty == 2
    assert not record.label_matched


def test_nonfinite_axis_and_duplicate_axis_are_rejected():
    with pytest.raises(ValidationError):
        ChartAxis(id="x", ticks=[float("inf")])
    with pytest.raises(ValidationError):
        ChartStructure(axes=[ChartAxis(id="y"), ChartAxis(id="y")])


def test_candidate_mapping_and_missing_results_are_supported():
    result = validate_visual_response(
        {"items": [{"visual_id": "V1", "status": "unreadable"}]},
        {"V1": {"kind": "figure"}},
    )
    assert not result.issues
    assert result.items[0].results == []


@pytest.mark.parametrize("ticks", [[0, 10], [-1, 10]])
def test_log_axis_requires_positive_ticks(ticks):
    with pytest.raises(ValidationError, match="positive"):
        ChartAxis(id="y", scale="log", ticks=ticks)


def test_estimated_axis_range_is_checked_on_the_assigned_axis_only():
    result = validate(
        [
            item(
                chart={
                    "chart_type": "line",
                    "axes": [
                        {"id": "y", "scale": "linear", "ticks": [0, 10]},
                        {"id": "y_right", "scale": "linear", "ticks": [0, 100]},
                    ],
                },
                results=[
                    reading(source="visual_estimate", value_text="", axis="y"),
                    reading(
                        source="visual_estimate", value_text="", axis="y_right", error_bar=[0, 110]
                    ),
                    reading(source="figure_label", axis="y"),
                ],
            )
        ]
    )
    assert len(result.items[0].results) == 2
    assert result.items[0].results[0].axis == "y_right"
    assert result.items[0].results[0].error_bar == [0, 110]
    assert result.items[0].results[1].source == "figure_label"
    assert result.issues[0].code == "axis_range_conflict"
    assert result.issues[0].path == "$.items[0].results[0].value"


def test_log_estimate_nonpositive_value_is_rejected_even_without_ticks():
    result = validate(
        [
            item(
                chart={"axes": [{"id": "y", "scale": "log"}]},
                results=[reading(source="visual_estimate", value=0, value_text="")],
            )
        ]
    )
    assert not result.items[0].results
    assert result.issues[0].code == "axis_scale_conflict"


@pytest.mark.parametrize("axis_unit,reading_unit", [("ms", "s"), ("mW", "MW")])
def test_unit_conflicts_are_not_silently_converted(axis_unit, reading_unit):
    result = validate(
        [
            item(
                chart={"axes": [{"id": "y", "unit": axis_unit}]},
                results=[reading(unit=reading_unit), reading(unit="")],
            )
        ]
    )
    assert len(result.items[0].results) == 1
    assert result.items[0].results[0].unit == ""
    assert result.issues[0].code == "unit_conflict"


@pytest.mark.parametrize(
    "box",
    [
        [0.2, 0.2, 0.8, 0.8],
        {"x0": 0.2, "y0": 0.2, "x1": 0.8, "y1": 0.8},
        SimpleNamespace(x0=0.2, y0=0.2, x1=0.8, y1=0.8),
    ],
)
def test_value_box_must_be_inside_known_candidate_geometry(box):
    visual = candidate()
    visual.bbox = box
    result = validate(
        [
            item(
                results=[
                    reading(x=1, value_bbox=[0.1, 0.3, 0.4, 0.5]),
                    reading(x=2, value_bbox=[0.2, 0.3, 0.4, 0.5]),
                    reading(x=3, value_bbox=[0.2 - 0.0000005, 0.3, 0.4, 0.5]),
                ]
            )
        ],
        [visual],
    )
    assert len(result.items[0].results) == 2
    assert result.issues[0].code == "bbox_outside_visual"
    assert result.issues[0].path.endswith(".value_bbox")


def test_invalid_or_missing_candidate_geometry_does_not_invent_a_boundary():
    visual = candidate()
    visual.bbox = [0, 0, 800, 600]
    result = validate([item(results=[reading(value_bbox=[0.1, 0.2, 0.3, 0.4])])], [visual])
    assert not result.issues
    assert len(result.items[0].results) == 1


def test_series_and_panel_names_must_match_explicit_structure():
    result = validate(
        [
            item(
                chart={"series": ["A"], "panels": ["a"]},
                results=[
                    reading(series="B"),
                    reading(panel="b"),
                    reading(series="A", panel="a"),
                    reading(),
                ],
            )
        ]
    )
    assert len(result.items[0].results) == 2
    assert {i.code for i in result.issues} == {"unknown_series", "unknown_panel"}
    assert result.items[0].results[1].series == ""
    legacy = validate(
        [item(chart={"series": [], "panels": []}, results=[reading(series="anything", panel="a")])]
    )
    assert not legacy.issues


@pytest.mark.parametrize(
    "chart",
    [None, {"axes": [{"id": "y", "scale": "unknown"}]}, {"axes": [{"id": "x", "scale": "linear"}]}],
)
def test_unknown_scale_estimate_is_retained_with_uncalibrated_note(chart):
    result = validate(
        [item(chart=chart, results=[reading(source="visual_estimate", value_text="")])]
    )
    assert not result.issues
    row = result.items[0].results[0]
    assert row.value == 95 and row.readout_uncertainty is None
    assert "估读尚未标定" in row.notes
    assert result.normalizations


def test_single_tick_does_not_imply_a_bounded_estimation_range():
    result = validate(
        [
            item(
                chart={"axes": [{"id": "y", "scale": "linear", "ticks": [10]}]},
                results=[reading(source="visual_estimate", value_text="")],
            )
        ]
    )
    assert not result.issues
    assert result.items[0].results[0].value == 95


def test_axes_with_same_id_in_distinct_panels_are_valid():
    structure = ChartStructure(
        axes=[
            ChartAxis(id="y", panel="a", ticks=[0, 1]),
            ChartAxis(id="y", panel="b", ticks=[0, 100]),
        ]
    )
    assert len(structure.axes) == 2
    with pytest.raises(ValidationError, match="within each panel"):
        ChartStructure(axes=[ChartAxis(id="y", panel="a"), ChartAxis(id="y", panel="a")])


def test_panel_axes_do_not_share_ranges_or_units():
    result = validate(
        [
            item(
                chart={
                    "panels": ["a", "b"],
                    "axes": [
                        {
                            "id": "y",
                            "panel": "a",
                            "unit": "RMSE",
                            "scale": "linear",
                            "ticks": [0, 1],
                        },
                        {
                            "id": "y",
                            "panel": "b",
                            "unit": "%",
                            "scale": "linear",
                            "ticks": [0, 100],
                        },
                    ],
                },
                results=[
                    reading(
                        panel="a", value=0.1, value_text="", unit="RMSE", source="visual_estimate"
                    ),
                    reading(panel="b", value=95, value_text="", unit="%", source="visual_estimate"),
                    reading(
                        panel="a", value=95, value_text="", unit="RMSE", source="visual_estimate"
                    ),
                ],
            )
        ]
    )
    assert [(r.panel, r.value) for r in result.items[0].results] == [("a", 0.1), ("b", 95)]
    assert len(result.issues) == 1
    assert result.issues[0].code == "axis_range_conflict"


def test_missing_panel_axis_only_falls_back_to_explicit_global_axis():
    chart = {
        "axes": [{"id": "y", "panel": "a", "unit": "RMSE", "scale": "linear", "ticks": [0, 1]}]
    }
    result = validate(
        [item(chart=chart, results=[reading(panel="b", unit="%", source="visual_estimate")])]
    )
    assert not result.issues
    assert result.items[0].results[0].value == 95
    assert "估读尚未标定" in result.items[0].results[0].notes
    chart["axes"].append({"id": "y", "scale": "linear", "unit": "%", "ticks": [0, 100]})
    fallback = validate(
        [item(chart=chart, results=[reading(panel="b", unit="%", source="visual_estimate")])]
    )
    assert not fallback.issues
    assert "估读尚未标定" not in fallback.items[0].results[0].notes


def test_conflicting_duplicate_identity_removes_all_conflicting_rows_only():
    result = validate(
        [
            item(
                results=[
                    reading(x=1),
                    reading(x=1, value=96, value_text="96%"),
                    reading(x=2),
                ]
            )
        ]
    )
    assert len(result.items[0].results) == 1
    assert result.items[0].results[0].x == 2
    assert [i.code for i in result.issues] == ["duplicate_reading_conflict"] * 2
    assert {i.path for i in result.issues} == {"$.items[0].results[0]", "$.items[0].results[1]"}


@pytest.mark.parametrize("second_dev_score", [81.6, 86.6])
def test_table_methods_remain_distinct_with_shared_dev_test_series(second_dev_score):
    rows = [
        reading(method="BERT_BASE", series="Dev", x="Dev", value=81.6, value_text="81.6"),
        reading(
            method="BERT_LARGE",
            series="Dev",
            x="Dev",
            value=second_dev_score,
            value_text=str(second_dev_score),
        ),
        reading(method="OpenAI GPT", series="Test", x="Test", value=78.0, value_text="78.0"),
        reading(method="BERT_LARGE", series="Test", x="Test", value=86.3, value_text="86.3"),
    ]
    result = validate(
        [item(chart={"chart_type": "table", "series": ["Dev", "Test"]}, results=rows)],
        [candidate(kind="table")],
    )
    assert not result.issues
    assert [(r.method, r.series, r.value) for r in result.items[0].results] == [
        (row["method"], row["series"], row["value"]) for row in rows
    ]


def test_table_same_method_and_series_conflict_does_not_remove_other_methods():
    result = validate(
        [
            item(
                chart={"chart_type": "table", "series": ["Test"]},
                results=[
                    reading(
                        method="BERT_LARGE", series="Test", x="Test",
                        value=86.3, value_text="86.3",
                    ),
                    reading(
                        method="BERT_LARGE", series="Test", x="Test",
                        value=86.4, value_text="86.4",
                    ),
                    reading(
                        method="OpenAI GPT", series="Test", x="Test",
                        value=78.0, value_text="78.0",
                    ),
                ],
            )
        ],
        [candidate(kind="table")],
    )
    assert [(r.method, r.value) for r in result.items[0].results] == [("OpenAI GPT", 78.0)]
    assert [issue.code for issue in result.issues] == ["duplicate_reading_conflict"] * 2
    assert {issue.path for issue in result.issues} == {
        "$.items[0].results[0]", "$.items[0].results[1]",
    }


def test_identical_duplicate_readings_are_deduplicated_without_provenance_upgrade():
    result = validate(
        [
            item(
                results=[
                    reading(x=1, source="visual_estimate"),
                    reading(x=1),
                ]
            )
        ]
    )
    assert not result.issues
    assert len(result.items[0].results) == 1
    assert result.items[0].results[0].source == "visual_estimate"
    assert any("identical repeated reading" in n for n in result.normalizations)


def test_legacy_conditions_and_distinct_compound_labels_do_not_merge():
    result = validate(
        [
            item(
                results=[
                    reading(condition="Experiment A"),
                    reading(condition="Experiment B", value=96, value_text="96%"),
                    reading(metric="Count", value=2625, value_text="2625=943+1682"),
                    reading(metric="Count", value=2625, value_text="2625=1000+1625"),
                    reading(metric="Count", value=2630, value_text="2630=1000+1630"),
                ]
            )
        ]
    )
    assert not result.issues
    assert len(result.items[0].results) == 5


def test_legacy_identical_numeric_labels_deduplicate_by_value():
    result = validate([item(results=[reading(), reading(value_text="95.0%")])])
    assert not result.issues
    assert len(result.items[0].results) == 1


def test_undeclared_right_axis_is_rejected_when_applicable_y_axis_is_explicit():
    result = validate(
        [
            item(
                chart={"axes": [{"id": "y", "scale": "linear"}]},
                results=[reading(axis="y_right"), reading(axis="y")],
            )
        ]
    )
    assert len(result.items[0].results) == 1
    assert result.items[0].results[0].axis == "y"
    assert result.issues[0].code == "unknown_axis"
    assert result.issues[0].path.endswith(".axis")


def test_axes_from_another_panel_do_not_reject_an_undeclared_right_axis():
    result = validate(
        [
            item(
                chart={"axes": [{"id": "y", "panel": "a", "scale": "linear"}]},
                results=[reading(axis="y_right", panel="b")],
            )
        ]
    )
    assert not result.issues
    assert result.items[0].results[0].axis == "y_right"
    legacy = validate([item(chart={"axes": []}, results=[reading(axis="y_right")])])
    assert not legacy.issues


def compact_group(**updates):
    return {
        "metric": "Score",
        "source": "visual_estimate",
        "series": "A",
        "points": [[1, 5], [2, 6]],
        **updates,
    }


def test_compact_curves_normalize_to_flat_readings_without_invented_labels():
    result = validate_visual_response(
        {"items": [{"visual_id": "V1", "status": "analyzed", "data_series": [compact_group()]}]},
        [candidate()],
    )
    assert not result.issues
    rows = result.items[0].results
    assert [(r.x, r.value, r.series) for r in rows] == [(1, 5, "A"), (2, 6, "A")]
    assert all(r.value_text == "" and not r.label_matched for r in rows)
    assert "data_series" not in result.items[0].model_dump()
    assert any("expanded 2 compact points" in n for n in result.normalizations)


def test_bad_compact_points_are_isolated_and_numeric_text_is_checked():
    result = validate(
        [
            item(
                results=[],
                data_series=[
                    compact_group(
                        source="figure_label",
                        points=[
                            [1, 5, "5"],
                            [2, float("inf")],
                            [3, 7, "8"],
                            [4, 9, 9],
                            [5, 10],
                        ],
                    )
                ],
            )
        ]
    )
    assert [(r.x, r.value) for r in result.items[0].results] == [(1, 5), (5, 10)]
    assert len(result.issues) == 3
    assert all(".data_series[0].points[" in i.path for i in result.issues)


@pytest.mark.parametrize(
    "forged",
    [{"page": 12}, {"label_matched": True}, {"confidence": 1}, {"value": 7}, {"value_text": "7"}],
)
def test_unknown_compact_group_fields_reject_only_that_group(forged):
    result = validate(
        [item(results=[], data_series=[compact_group(**forged), compact_group(series="B")])]
    )
    assert len(result.items[0].results) == 2
    assert all(r.series == "B" for r in result.items[0].results)
    assert result.issues[0].path == "$.items[0].data_series[0]"


def test_compact_group_requires_explicit_metric_source_and_list_points():
    groups = [compact_group(), compact_group(), compact_group()]
    groups[0].pop("metric")
    groups[1].pop("source")
    groups[2]["points"] = "guess points"
    result = validate([item(results=[], data_series=[*groups, compact_group(series="B")])])
    assert len(result.issues) == 3
    assert len(result.items[0].results) == 2


def test_flat_and_compact_nonempty_formats_cannot_coexist():
    result = validate([item(data_series=[compact_group()])])
    assert not result.items
    assert result.issues[0].code == "ambiguous_readings"


@pytest.mark.parametrize(
    "data_series",
    [
        [compact_group(points=[[i, i] for i in range(161)])],
        [compact_group(points=[]) for _ in range(41)],
        None,
    ],
)
def test_compact_total_and_group_limits_are_enforced(data_series):
    result = validate([item(results=[], data_series=data_series)])
    assert not result.items
    assert result.issues


def test_compact_points_keep_unreadable_constraints_and_detect_duplicate_conflicts():
    result = validate(
        [
            item(
                results=[],
                data_series=[
                    compact_group(points=[[1, 5], [1, 6], [2, 7]]),
                    compact_group(series="B", source="figure_only", points=[[1, None], [2, 5]]),
                ],
            )
        ]
    )
    assert [(r.series, r.x, r.value) for r in result.items[0].results] == [
        ("A", 2, 7),
        ("B", 1, None),
    ]
    assert {i.code for i in result.issues} == {"invalid_reading", "duplicate_reading_conflict"}


def test_compact_numeric_labels_keep_percentages_and_scientific_notation():
    result = validate(
        [
            item(
                results=[],
                data_series=[
                    compact_group(
                        source="figure_label",
                        points=[
                            [1, 95, "95%"],
                            [2, 1200, "1.2e3"],
                            [3, 0.95, "95%"],
                        ],
                    )
                ],
            )
        ]
    )
    assert [r.value for r in result.items[0].results] == [95, 1200]
    assert len(result.issues) == 1


@pytest.mark.parametrize(
    "label,value",
    [
        ("100 000", 100000),
        ("1,000,209", 1000209),
        ("1 000.5", 1000.5),
        ("−1\u202f000.5", -1000.5),
        ("100\u00a0000 %", 100000),
        ("1,000e2", 100000),
    ],
)
def test_legally_grouped_numeric_labels_are_compared_without_changing_original_text(label, value):
    result = validate([item(results=[reading(value=value, value_text=label)])])
    assert not result.issues
    assert result.items[0].results[0].value_text == label
    assert numeric_label_value(label) == value
    mismatch = validate([item(results=[reading(value=value + 1, value_text=label)])])
    assert mismatch.issues and not mismatch.items[0].results


@pytest.mark.parametrize("label", ["1,2", "12,34,567", "1 00", "1000 000", "2625=943+1682"])
def test_ambiguous_grouping_and_compound_labels_are_not_silently_reinterpreted(label):
    assert numeric_label_value(label) is None
    result = validate([item(results=[reading(value=2625, value_text=label)])])
    assert not result.issues
    assert result.items[0].results[0].value_text == label


def test_singleton_warning_strings_are_normalized_without_losing_information():
    message = "Same-model reading is not independent confirmation; 80 points may be incomplete."
    raw = {"warnings": message, "items": [item(warnings=message)]}
    result = validate_visual_response(raw, [candidate()])
    assert not result.issues
    assert result.items[0].warnings == [message]
    assert any(n.startswith("$.warnings:") and "singleton" in n for n in result.normalizations)
    assert any(n.startswith("$.items[0].warnings:") for n in result.normalizations)
    assert any(message in n for n in result.normalizations)
    assert raw["warnings"] == message and raw["items"][0]["warnings"] == message


@pytest.mark.parametrize("invalid", [123, {"warning": "bad shape"}, None])
def test_warning_compatibility_does_not_coerce_numbers_objects_or_null(invalid):
    root = validate_visual_response({"warnings": invalid, "items": [item()]}, [candidate()])
    assert not root.items and root.issues
    per_item = validate([item(warnings=invalid), item("V2")], [candidate(), candidate("V2")])
    assert [i.visual_id for i in per_item.items] == ["V2"]
    assert per_item.issues
