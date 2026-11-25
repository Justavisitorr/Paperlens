"""Conservative reconciliation and explanations derived from retained readings."""

from __future__ import annotations

import math
from collections import defaultdict

from .chart_models import numeric_label_value


def _identity(row):
    composite = row.value_text if row.x is None and numeric_label_value(row.value_text) is None else ""
    return (
        row.panel,
        row.series,
        row.method,
        row.dataset,
        row.metric,
        str(row.x),
        row.axis,
        row.unit,
        row.condition,
        composite,
    )


def reconcile_readings(first, reviewed):
    """One extra look can expose conflict; agreement is not independent proof."""
    if first is None or not any(r.value is not None for r in first.results):
        return reviewed, []
    original = {_identity(r): r for r in first.results if r.value is not None}
    second = {_identity(r): r for r in reviewed.results if r.value is not None}
    unmatched = original.keys() != second.keys()
    rows, conflicts = [], []
    for row in first.results:
        if row.value is None:
            rows.append(row)
            continue
        other = second.get(_identity(row))
        if other is None:
            rows.append(row)
            continue
        tolerance = 0
        if row.source == other.source == "visual_estimate":
            chart = first.chart or reviewed.chart
            axis = (
                next((a for a in chart.axes if a.id == row.axis and a.panel == row.panel), None)
                if chart
                else None
            )
            if axis is None and chart:
                axis = next((a for a in chart.axes if a.id == row.axis and not a.panel), None)
            if axis and axis.scale == "linear" and len(set(axis.ticks)) > 1:
                ticks = sorted(set(axis.ticks))
                tick_step = min(b - a for a, b in zip(ticks, ticks[1:], strict=False))
                tolerance = min(
                    tick_step / 2, max(row.readout_uncertainty or 0, other.readout_uncertainty or 0)
                )
        if not math.isclose(row.value, other.value, rel_tol=1e-12, abs_tol=tolerance):
            conflicts.append(f"{row.metric}/{row.series or row.method}/{row.x}: 两次读数不一致")
            rows.append(
                row.model_copy(
                    update={
                        "value": None,
                        "value_text": "",
                        "source": "figure_only",
                        "readout_uncertainty": None,
                        "error_bar": None,
                        "label_matched": False,
                        "notes": "两次图像读数不一致，已隐藏冲突数值；请核对原图。",
                    }
                )
            )
        else:
            # Keep the original provenance: a repeat cannot promote an estimate.
            rows.append(row)
    warnings = [*first.warnings, "局部复核由同一模型完成，不等于独立验证。", *conflicts]
    if unmatched:
        warnings.append("局部复核未能对应全部数据，未对应项保留首轮候选值；尚未得到确认。")
    return first.model_copy(
        update={
            "results": rows,
            "chart": first.chart or reviewed.chart,
            "warnings": warnings,
            "status": "analyzed" if any(r.value is not None for r in rows) else "unreadable",
        }
    ), conflicts


def data_description(item, *, has_issues=False) -> str:
    """Do not echo rejected numeric claims through the free-form description."""
    chart = item.chart
    if chart and chart.chart_type == "diagram":
        if has_issues:
            return (
                "本图被识别为流程或结构示意；响应含有不合规数值候选，已隐藏相关解读，请查看原图。"
            )
        return item.description or "本图为流程或结构示意，不提供定量读数。"
    rows = [r for r in item.results if r.value is not None]
    if not rows:
        if has_issues:
            return "图表已检查，但数值候选存在校验问题；请查看原图与逐项说明。"
        return item.description
    parts = []
    if chart and chart.axes:
        names = {"x": "横轴", "y": "纵轴", "y_right": "右纵轴"}
        scales = {"linear": "线性", "log": "对数", "category": "分类", "unknown": "刻度未确认"}
        parts.append(
            "；".join(
                (f"子图 {a.panel} " if a.panel else "")
                + f"{names[a.id]}：{a.label or '未注明'}"
                + (f"（{a.unit}）" if a.unit else "")
                + f"，{scales[a.scale]}"
                for a in chart.axes
            )
            + "。"
        )
    printed = sum(r.source == "figure_label" for r in rows)
    parts.append(
        f"已整理 {len(rows)} 个数值候选，其中图中标注 {printed} 项、估读 {len(rows) - printed} 项。"
    )
    # Compare only the same quantity/condition/x, with named distinct methods.
    groups = defaultdict(list)
    for row in rows:
        groups[
            (row.panel, row.dataset, row.metric, row.unit, row.condition, row.x, row.axis)
        ].append(row)
    for key, group in groups.items():
        if len(group) < 2 or len({r.series or r.method for r in group}) != len(group):
            continue
        if any(not (r.series or r.method) for r in group):
            continue
        lo, hi = min(group, key=lambda r: r.value), max(group, key=lambda r: r.value)
        approx = "约" if any(r.source == "visual_estimate" for r in group) else ""
        context = f"在 {key[4]} 下，" if key[4] else "在相同已记录条件下，"
        if key[5] is not None:
            context += f"x={key[5]} 时，"
        parts.append(
            context
            + f"{key[2]} 的最高候选值为 {hi.series or hi.method} 的{approx}{hi.value:g}{key[3]}，"
            f"最低为 {lo.series or lo.method} 的{approx}{lo.value:g}{key[3]}。"
        )
        if len(parts) >= 4:
            break
    if has_issues:
        parts.append("部分候选未通过校验，已从数据中排除；此处只解释保留的记录。")
    parts.append("这些是图像读取结果，尚未经人工核实；估读不代表原始精确数据。")
    return "\n\n".join(parts)
