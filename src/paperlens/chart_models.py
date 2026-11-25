"""Chart structure and readings, independent of document models."""

from __future__ import annotations

import math
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

FiniteNumber = Annotated[float, Field(allow_inf_nan=False)]
_NUMERIC_LABEL = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\s*%?$")
_GROUPED_NUMERIC_LABEL = re.compile(
    r"^[+-]?(?:\d{1,3}(?:,\d{3})+|\d{1,3}(?:[ \u00a0\u202f]\d{3})+)"
    r"(?:\.\d+)?(?:[eE][+-]?\d+)?\s*%?$"
)


def numeric_label_value(label: str) -> float | None:
    """Parse only an unambiguous number, including legal three-digit grouping."""
    label = label.strip().replace("−", "-")
    if _NUMERIC_LABEL.fullmatch(label):
        return float(label.rstrip("%").strip())
    if _GROUPED_NUMERIC_LABEL.fullmatch(label):
        numeric = label.rstrip("%").strip()
        return float(re.sub(r"[, \u00a0\u202f]", "", numeric))
    return None


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ChartAxis(_Strict):
    id: Literal["x", "y", "y_right"]
    panel: str = Field(default="", max_length=200)
    label: str = Field(default="", max_length=300)
    unit: str = Field(default="", max_length=100)
    scale: Literal["linear", "log", "category", "unknown"] = "unknown"
    ticks: list[FiniteNumber] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def positive_log_ticks(self) -> ChartAxis:
        if self.scale == "log" and any(tick <= 0 for tick in self.ticks):
            raise ValueError("Logarithmic axis ticks must be positive")
        return self


class ChartStructure(_Strict):
    chart_type: Literal[
        "unknown", "table", "bar", "line", "scatter", "pie", "heatmap", "diagram", "other"
    ] = "unknown"
    axes: list[ChartAxis] = Field(default_factory=list, max_length=24)
    series: list[str] = Field(default_factory=list, max_length=100)
    panels: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unique_axes(self) -> ChartStructure:
        if len({(axis.panel, axis.id) for axis in self.axes}) != len(self.axes):
            raise ValueError("Chart axis IDs must be unique within each panel")
        return self


class ChartReading(_Strict):
    metric: str = Field(min_length=1, max_length=200)
    method: str = Field(default="", max_length=300)
    dataset: str = Field(default="", max_length=300)
    value: FiniteNumber | None = None
    value_text: str = Field(default="", max_length=200)
    unit: str = Field(default="", max_length=100)
    condition: str = Field(default="", max_length=700)
    source: Literal["figure_label", "visual_estimate", "figure_only"]
    notes: str = Field(default="", max_length=1000)
    panel: str = Field(default="", max_length=200)
    series: str = Field(default="", max_length=300)
    x: FiniteNumber | str | None = None
    axis: Literal["y", "y_right"] = "y"
    value_bbox: list[FiniteNumber] | None = Field(default=None, min_length=4, max_length=4)
    readout_uncertainty: FiniteNumber | None = Field(default=None, ge=0)
    error_bar: list[FiniteNumber] | None = Field(default=None, min_length=2, max_length=2)
    label_matched: bool = False

    @model_validator(mode="before")
    @classmethod
    def server_owned_fields(cls, value):
        if isinstance(value, dict) and "label_matched" in value:
            raise ValueError("label_matched is assigned by the server")
        return value

    @model_validator(mode="after")
    def check_reading(self) -> ChartReading:
        if not self.metric.strip():
            raise ValueError("A reading needs a metric")
        if self.source == "figure_only":
            if (
                self.value is not None
                or self.value_text.strip()
                or self.readout_uncertainty is not None
                or self.error_bar is not None
            ):
                raise ValueError("Unreadable values cannot contain a numeric claim")
        elif self.value is None and not self.value_text.strip():
            raise ValueError("A reading needs a value or its original label")
        printed = numeric_label_value(self.value_text)
        if self.value is not None and printed is not None:
            # Percent labels retain the printed percentage; no division by 100.
            if not math.isfinite(printed) or not math.isclose(
                self.value, printed, rel_tol=1e-12, abs_tol=0.0
            ):
                raise ValueError("value contradicts its numeric value_text")
        if self.value_bbox is not None:
            x0, y0, x1, y1 = self.value_bbox
            if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
                raise ValueError("value_bbox must be a valid normalized box")
        if self.error_bar is not None:
            lower, upper = self.error_bar
            if lower > upper or (self.value is not None and not lower <= self.value <= upper):
                raise ValueError("error_bar must contain ordered absolute bounds and the value")
        return self
