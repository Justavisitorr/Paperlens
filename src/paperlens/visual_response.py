"""Validate untrusted visual output without discarding unrelated good items."""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .chart_models import ChartReading, ChartStructure, numeric_label_value


class VisualResponseItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    visual_id: str
    status: Literal["analyzed", "unreadable"]
    description: str = Field(default="", max_length=3000)
    latex: str | None = Field(default=None, max_length=4096)
    results: list[ChartReading] = Field(default_factory=list, max_length=160)
    warnings: list[str] = Field(default_factory=list, max_length=15)
    chart: ChartStructure | None = None


@dataclass
class VisualValidationIssue:
    code: str
    path: str
    message: str
    visual_id: str | None = None


@dataclass
class VisualResponseValidation:
    items: list[VisualResponseItem] = field(default_factory=list)
    issues: list[VisualValidationIssue] = field(default_factory=list)
    normalizations: list[str] = field(default_factory=list)


def _candidate_field(candidate, name: str, default=None):
    if isinstance(candidate, Mapping):
        return candidate.get(name, default)
    return getattr(candidate, name, default)


def _label_matches(label: str, native_text: str) -> bool:
    """A text occurrence is an alignment hint, never proof of a correct reading."""
    if not label.strip() or not native_text:
        return False
    label = re.sub(r"\s+", " ", label.strip()).replace("−", "-")
    native_text = re.sub(r"\s+", " ", native_text).replace("−", "-")
    label = re.sub(r"\s+%", "%", label)
    native_text = re.sub(r"\s+%", "%", native_text)
    return re.search(r"(?<![\w.])" + re.escape(label) + r"(?![\w.])", native_text) is not None


def _normalized_candidate_box(candidate) -> tuple[float, float, float, float] | None:
    """Use only finite, normalized geometry supplied by trusted candidate discovery."""
    box = _candidate_field(candidate, "bbox")
    if box is None:
        return None
    if isinstance(box, (list, tuple)) and len(box) == 4:
        values = tuple(box)
    else:
        values = tuple(_candidate_field(box, field) for field in ("x0", "y0", "x1", "y1"))
    if any(
        isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v)
        for v in values
    ):
        return None
    x0, y0, x1, y1 = values
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        return None
    return values


def _reading_conflicts(reading: ChartReading, chart: ChartStructure | None, candidate):
    conflicts = []
    axis = (
        next((a for a in chart.axes if a.id == reading.axis and a.panel == reading.panel), None)
        if chart
        else None
    )
    if axis is None and chart:
        axis = next((a for a in chart.axes if a.id == reading.axis and a.panel == ""), None)
        relevant_y_axes = [
            a for a in chart.axes if a.id in {"y", "y_right"} and a.panel in {"", reading.panel}
        ]
        if axis is None and relevant_y_axes:
            conflicts.append(
                ("unknown_axis", "axis", "Reading axis is not declared for this panel or globally")
            )
    if chart:
        for name, declared in (("series", chart.series), ("panel", chart.panels)):
            reported = getattr(reading, name).strip()
            if reported and declared and reported not in {label.strip() for label in declared}:
                conflicts.append(
                    (f"unknown_{name}", name, f"Reading {name} is not declared by the chart")
                )
    if axis:
        if reading.unit.strip() and axis.unit.strip() and reading.unit.strip() != axis.unit.strip():
            conflicts.append(
                (
                    "unit_conflict",
                    "unit",
                    "Reading unit differs from its axis unit; no conversion applied",
                )
            )
        if reading.source == "visual_estimate" and reading.value is not None:
            if axis.scale == "log" and reading.value <= 0:
                conflicts.append(
                    ("axis_scale_conflict", "value", "A logarithmic-axis estimate must be positive")
                )
            elif len(set(axis.ticks)) >= 2:
                lower, upper = min(axis.ticks), max(axis.ticks)
                epsilon = 1e-9 * max(abs(lower), abs(upper), 1)
                if not lower - epsilon <= reading.value <= upper + epsilon:
                    conflicts.append(
                        (
                            "axis_range_conflict",
                            "value",
                            "Estimate is outside its declared axis tick range",
                        )
                    )
    candidate_box = _normalized_candidate_box(candidate)
    if reading.value_bbox is not None and candidate_box is not None:
        left, top, right, bottom = candidate_box
        x0, y0, x1, y1 = reading.value_bbox
        epsilon = 1e-6
        if not (
            x0 >= left - epsilon
            and y0 >= top - epsilon
            and x1 <= right + epsilon
            and y1 <= bottom + epsilon
        ):
            conflicts.append(
                (
                    "bbox_outside_visual",
                    "value_bbox",
                    "Reading box is outside the discovered visual box",
                )
            )
    return conflicts, axis


def _append_note(notes: str, caveat: str) -> str:
    if caveat in notes:
        return notes
    return notes[: 1000 - len(caveat) - 1] + ("；" if notes else "") + caveat


def _reading_identity(reading: ChartReading):
    identity = (
        reading.panel,
        reading.series,
        reading.method,
        reading.dataset,
        reading.metric,
        str(reading.x),
        reading.axis,
        reading.unit,
        reading.condition,
    )
    label = reading.value_text.strip().replace("−", "-")
    numeric = numeric_label_value(label) is not None
    # Legacy compound labels can represent distinct entries without an x field.
    # Keep their original text as the only available discriminator.
    if reading.x is None and label and not numeric:
        return (*identity, label)
    return (*identity, None)


def _expand_data_series(groups, path, visual_id, issue):
    """Unpack a bounded model wire format; report storage stays flat and explicit."""
    allowed = {
        "metric",
        "method",
        "dataset",
        "unit",
        "condition",
        "source",
        "notes",
        "series",
        "panel",
        "axis",
        "points",
    }
    if not isinstance(groups, list) or len(groups) > 40:
        issue("invalid_data_series", path, "data_series must contain at most 40 groups", visual_id)
        return None
    point_count = sum(
        len(g["points"])
        for g in groups
        if isinstance(g, dict) and isinstance(g.get("points"), list)
    )
    if point_count > 160:
        issue(
            "invalid_results",
            path,
            "Compact series exceed the total limit of 160 readings",
            visual_id,
        )
        return None
    expanded = []
    for group_index, group in enumerate(groups):
        group_path = f"{path}[{group_index}]"
        if not isinstance(group, dict):
            issue("invalid_series_group", group_path, "Series group must be an object", visual_id)
            continue
        unknown = set(group) - allowed
        if unknown:
            issue(
                "invalid_series_group",
                group_path,
                f"Unknown series fields: {sorted(unknown)}",
                visual_id,
            )
            continue
        if "metric" not in group or "source" not in group:
            issue(
                "invalid_series_group",
                group_path,
                "Series group requires explicit metric and source",
                visual_id,
            )
            continue
        points = group.get("points")
        if not isinstance(points, list):
            issue(
                "invalid_series_group", group_path + ".points", "points must be a list", visual_id
            )
            continue
        metadata = {key: value for key, value in group.items() if key != "points"}
        for point_index, point in enumerate(points):
            point_path = f"{group_path}.points[{point_index}]"
            if (
                not isinstance(point, list)
                or len(point) not in {2, 3}
                or (len(point) == 3 and not isinstance(point[2], str))
            ):
                issue(
                    "invalid_series_point",
                    point_path,
                    "Point must be [x,value] or [x,value,value_text] with string text",
                    visual_id,
                )
                continue
            expanded.append(
                (
                    {
                        **metadata,
                        "x": point[0],
                        "value": point[1],
                        "value_text": point[2] if len(point) == 3 else "",
                    },
                    point_path,
                )
            )
    return expanded


def validate_visual_response(
    raw, candidates, native_text_by_id: Mapping[str, str] | None = None
) -> VisualResponseValidation:
    result = VisualResponseValidation()

    def issue(code, path, message, visual_id=None):
        result.issues.append(VisualValidationIssue(code, path, message, visual_id))

    if isinstance(candidates, Mapping):
        candidate_by_id = dict(candidates)
    else:
        candidate_by_id = {_candidate_field(c, "id"): c for c in candidates}
    if not isinstance(raw, dict):
        issue("invalid_root", "$", "Visual response must be an object")
        return result
    unknown = set(raw) - {"items", "type", "warnings"}
    if unknown:
        issue("unknown_root_field", "$", f"Unknown root fields: {sorted(unknown)}")
        return result
    if "type" in raw and raw["type"] != "json_object":
        issue("invalid_root_type", "$.type", "type must be json_object")
        return result
    if isinstance(raw.get("warnings"), str):
        raw = {**raw, "warnings": [raw["warnings"]]}
        result.normalizations.append("$.warnings: wrapped singleton warning string in list")
    if "warnings" in raw and (
        not isinstance(raw["warnings"], list)
        or any(not isinstance(w, str) for w in raw["warnings"])
    ):
        issue("invalid_root_warnings", "$.warnings", "warnings must be a list of strings")
        return result
    if not isinstance(raw.get("items"), list) or len(raw["items"]) > 100:
        issue("invalid_items", "$.items", "items must be a list of at most 100 items")
        return result
    if "type" in raw:
        result.normalizations.append("$.type: accepted compatibility envelope")
    if raw.get("warnings"):
        result.normalizations.append("$.warnings: " + "; ".join(raw["warnings"]))
    counts = Counter(
        item.get("visual_id")
        for item in raw["items"]
        if isinstance(item, dict) and isinstance(item.get("visual_id"), str)
    )
    for index, item_raw in enumerate(raw["items"]):
        path = f"$.items[{index}]"
        if not isinstance(item_raw, dict):
            issue("invalid_item", path, "Visual item must be an object")
            continue
        visual_id = item_raw.get("visual_id")
        if not isinstance(visual_id, str) or visual_id not in candidate_by_id:
            issue("unknown_visual_id", path + ".visual_id", "ID is not a given candidate")
            continue
        if counts[visual_id] > 1:
            issue("duplicate_visual_id", path + ".visual_id", "Duplicate candidate ID", visual_id)
            continue
        if isinstance(item_raw.get("warnings"), str):
            item_raw = {**item_raw, "warnings": [item_raw["warnings"]]}
            result.normalizations.append(
                path + ".warnings: wrapped singleton warning string in list"
            )
        chart_raw = item_raw.get("chart")
        if isinstance(chart_raw, dict) and isinstance(chart_raw.get("axes"), list):
            axes = list(chart_raw["axes"])
            for axis_index, axis_raw in enumerate(axes):
                if isinstance(axis_raw, dict) and axis_raw.get("scale") == "categorical":
                    axes[axis_index] = {**axis_raw, "scale": "category"}
                    result.normalizations.append(
                        f"{path}.chart.axes[{axis_index}].scale: categorical -> category"
                    )
            # Normalize only the response copy; paid raw caches remain unchanged.
            item_raw = {**item_raw, "chart": {**chart_raw, "axes": axes}}
        readings_raw = item_raw.get("results", [])
        if not isinstance(readings_raw, list) or len(readings_raw) > 160:
            issue(
                "invalid_results",
                path + ".results",
                "results must contain at most 160 rows",
                visual_id,
            )
            continue
        groups = item_raw.get("data_series", [])
        if groups and readings_raw:
            issue(
                "ambiguous_readings",
                path,
                "Nonempty results and data_series cannot coexist",
                visual_id,
            )
            continue
        if "data_series" in item_raw:
            expanded = _expand_data_series(groups, path + ".data_series", visual_id, issue)
            if expanded is None:
                continue
        else:
            expanded = []
        readings_with_paths = expanded or [
            (r, f"{path}.results[{i}]") for i, r in enumerate(readings_raw)
        ]
        if expanded:
            result.normalizations.append(
                path + f".data_series: expanded {len(expanded)} compact points"
            )
        try:
            item = VisualResponseItem.model_validate(
                {**{k: v for k, v in item_raw.items() if k != "data_series"}, "results": []}
            )
        except ValidationError as exc:
            for error in exc.errors(include_url=False):
                suffix = "." + ".".join(str(x) for x in error["loc"]) if error["loc"] else ""
                issue("invalid_item", path + suffix, error["msg"], visual_id)
            continue
        kind = _candidate_field(candidate_by_id[visual_id], "kind", "figure")
        if kind != "formula" and item.latex is not None:
            issue(
                "unexpected_latex",
                path + ".latex",
                "Only formula candidates may have latex",
                visual_id,
            )
            continue
        if item.status == "unreadable" and item.latex:
            issue(
                "unreadable_latex",
                path + ".latex",
                "Unreadable candidates cannot recover a formula",
                visual_id,
            )
            continue
        diagram = item.chart is not None and item.chart.chart_type == "diagram"
        reading_paths = []
        for row_raw, row_path in readings_with_paths:
            try:
                reading = ChartReading.model_validate(row_raw)
            except ValidationError as exc:
                for error in exc.errors(include_url=False):
                    suffix = "." + ".".join(str(x) for x in error["loc"]) if error["loc"] else ""
                    issue("invalid_reading", row_path + suffix, error["msg"], visual_id)
                continue
            if kind == "formula" or diagram:
                if reading.source == "figure_only":
                    result.normalizations.append(row_path + ": removed empty placeholder")
                else:
                    issue(
                        "unexpected_reading",
                        row_path,
                        "Formula and diagram items cannot carry quantitative readings",
                        visual_id,
                    )
                continue
            if item.status == "unreadable" and reading.source != "figure_only":
                issue(
                    "unreadable_reading",
                    row_path,
                    "Unreadable candidates cannot carry recovered values",
                    visual_id,
                )
                continue
            conflicts, axis = _reading_conflicts(reading, item.chart, candidate_by_id[visual_id])
            if conflicts:
                for code, field_name, message in conflicts:
                    issue(code, row_path + "." + field_name, message, visual_id)
                continue
            if reading.source == "visual_estimate" and (axis is None or axis.scale == "unknown"):
                caveat = "坐标轴刻度类型未知，估读尚未标定；数值仍为模型近似读数。"
                reading = reading.model_copy(update={"notes": _append_note(reading.notes, caveat)})
                result.normalizations.append(
                    row_path + ": estimate retained with unknown-scale caveat"
                )
            native_text = (native_text_by_id or {}).get(visual_id, "")
            if reading.source == "figure_label":
                matched = _label_matches(reading.value_text, native_text)
                notes = reading.notes
                if matched:
                    caveat = "文字匹配仅说明标签出现在本地文本，不证明读数、系列或条件准确。"
                    if len(notes) + len(caveat) + 1 > 1000:
                        result.normalizations.append(
                            row_path + ".notes: shortened to add alignment caveat"
                        )
                    notes = _append_note(notes, caveat)
                reading = reading.model_copy(update={"label_matched": matched, "notes": notes})
            item.results.append(reading)
            reading_paths.append(row_path)
        duplicate_groups = defaultdict(list)
        for row_index, reading in enumerate(item.results):
            if reading.value is not None:
                duplicate_groups[_reading_identity(reading)].append(row_index)
        removed_indices = set()
        for indices in duplicate_groups.values():
            if len(indices) < 2:
                continue
            if len({item.results[index].value for index in indices}) > 1:
                removed_indices.update(indices)
                for index in indices:
                    issue(
                        "duplicate_reading_conflict",
                        reading_paths[index],
                        "Same reading identity has conflicting values; all conflicting rows removed",
                        visual_id,
                    )
            else:
                removed_indices.update(indices[1:])
                for index in indices[1:]:
                    result.normalizations.append(
                        reading_paths[index] + ": removed identical repeated reading"
                    )
        item.results = [r for index, r in enumerate(item.results) if index not in removed_indices]
        result.items.append(item)
    for visual_id in candidate_by_id:
        if visual_id not in counts:
            issue("missing_visual_id", "$.items", "Candidate ID is missing", visual_id)
    return result
