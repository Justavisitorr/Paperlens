"""Saved-cache chart recovery preserves source/reader identities and old usage."""

import copy
import json
import socket

import pytest

from tools import refresh_charts


def _box(x0=.2, y0=.1, x1=.8, y1=.3):
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1}


def _row(method="A", value=.5, **kwargs):
    return {"metric": "RMSE", "method": method, "dataset": "test", "value": value,
            "value_text": str(value), "unit": "", "condition": "", "source": "figure_label",
            "notes": "local label", **kwargs}


def _item(visual_id="V1", rows=None, **kwargs):
    return {"visual_id": visual_id, "status": "analyzed", "description": "cached description",
            "latex": None, "results": rows if rows is not None else [_row()], "warnings": [],
            **kwargs}


def _q(name, vid="V1", method="A", value=.4, **kwargs):
    return {"id": name, "metric": "RMSE", "method": method, "dataset": "test",
            "value": value, "value_text": str(value), "unit": "", "condition": "",
            "page": 1, "figure_label": "Fig. 1", "source": "figure_label", "precision": "reported",
            "visual_id": vid, "evidence_ids": [], "notes": "previously accepted", **kwargs}


@pytest.fixture
def saved(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline chart refresh must never use a network")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    source = tmp_path / "source"
    source.mkdir()
    cache_root = tmp_path / "cache"
    key = "a" * 64
    cache = cache_root / key
    cache.mkdir(parents=True)
    blocks = []
    for idx, y in ((1, .1), (3, .5)):
        blocks.extend([
            {"id": f"b{idx}", "page": 1, "order": idx, "bbox": _box(y0=y, y1=y + .2),
             "kind": "picture", "text": "", "raw_text": "", "caption_ids": [f"b{idx + 1}"]},
            {"id": f"b{idx + 1}", "page": 1, "order": idx + 1,
             "bbox": _box(y0=y + .22, y1=y + .25), "kind": "caption",
             "text": f"Fig. {(idx + 1) // 2}", "raw_text": f"Fig. {(idx + 1) // 2}"},
        ])
    report = {
        "schema_version": "1.0",
        "document": {"title": "Saved", "source_name": "source.pdf", "sha256": "digest",
                     "parser": "test", "pages": [{"number": 1, "width": 600, "height": 800}],
                     "blocks": blocks, "warnings": ["source warning"]},
        "analysis": {"title": "Saved", "summary": "accepted", "research_question": "why",
                     "nodes": [], "edges": []},
        "evidence": [], "graphs": [], "previews": [],
        "quality": {"total_pages": 1, "parsed_pages": 1, "text_chars": 12,
                    "grounded_nodes": 0, "total_nodes": 0},
        "run": {"id": "historic", "created_at": "2026-10-01", "model": "prior-model",
                "parser": "test", "status": "complete", "usage": {"requests": 7, "prompt_tokens": 1234},
                "usage_by_model": {"prior-model": {"requests": 7}}, "cache_hits": {"visuals": 2}},
        "visuals": [
            {"id": "V1", "page": 1, "kind": "figure", "label": "Fig. 1", "source_block_ids": ["b2"],
             "bbox": None, "status": "analyzed", "description": "accepted old figure",
             "warnings": ["旧结果暂不可用，待兼容恢复"]},
            {"id": "V2", "page": 1, "kind": "figure", "label": "Fig. 2", "source_block_ids": ["b4"],
             "bbox": None, "status": "analyzed", "description": "unrelated figure", "warnings": []},
        ],
        "quantitative_results": [_q("Q1"), _q("Q2", "V2", "Unrelated", .8),
                                 _q("Q90", None, "Text", .9, source="text")],
        "reading": {"source_version": "saved", "items": [
            {"id": "reading-figure-one", "kind": "figure", "page": 1, "visual_ids": ["V1"],
             "result_ids": ["Q1", "Q90"], "parts": [{"block_id": "b2", "start": 0, "end": 6}],
             "content": "Original reader content", "asset": "original-asset", "bbox": _box()},
            {"id": "reading-figure-two", "kind": "figure", "page": 1, "visual_ids": ["V2"],
             "result_ids": ["Q2"], "parts": [{"block_id": "b4", "start": 0, "end": 6}]},
            {"id": "paragraph-anchor", "kind": "paragraph", "page": 1, "parts": [],
             "content": "Accepted wording", "result_ids": ["Q90"], "visual_ids": []},
        ], "information": {}, "hidden_ids": ["b1"], "warnings": ["keep reader warning"]},
    }
    path = source / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    (source / "visual-verification.json").write_text(json.dumps({"cache_key": key}), encoding="utf-8")
    return path, report, cache_root, cache, tmp_path


def _refresh(saved, response, name="out"):
    path, original, cache_root, cache, tmp_path = saved
    (cache / "page-0001.json").write_text(json.dumps({"response": response}), encoding="utf-8")
    output = tmp_path / name
    audit = refresh_charts.refresh_report(path, output, cache_root=cache_root)
    actual = json.loads((output / "report.json").read_text(encoding="utf-8"))
    return actual, audit


def test_legacy_warning_envelope_recovers_six_without_source_or_usage_changes(saved):
    original = saved[1]
    rows = [_row(f"Method {idx}", idx / 10) for idx in range(1, 7)]
    response = {"type": "json_object", "warnings": ["旧版响应说明"], "items": [_item(rows=rows)]}
    actual, audit = _refresh(saved, response)
    assert actual["document"] == original["document"]
    assert actual["analysis"] == original["analysis"]
    assert actual["run"] == original["run"]
    assert audit["requests"] == 0 and audit["historical_usage_preserved"]
    assert audit["recovered_readings"] == 6
    assert audit["numeric_readings_before"] == 2 and audit["numeric_readings_after"] == 7
    assert audit["numeric_readings_added"] == 5
    assert audit["refreshed_visual_ids"] == ["V1"]
    assert audit["preserved_visual_ids"] == ["V2"]
    assert [v["id"] for v in actual["visuals"]] == ["V1", "V2"]
    assert "6 个数值候选" in actual["visuals"][0]["description"]
    assert actual["visuals"][1]["description"] == "unrelated figure"
    original_reading = copy.deepcopy(original["reading"])
    actual_reading = copy.deepcopy(actual["reading"])
    for item in original_reading["items"] + actual_reading["items"]:
        item.pop("result_ids", None)
    assert actual_reading == original_reading
    qids = {q["id"] for q in actual["quantitative_results"]}
    assert qids == {"Q2", "Q90", *[f"Q{idx}" for idx in range(91, 97)]}
    assert actual["reading"]["items"][0]["result_ids"] == ["Q90", *[f"Q{idx}" for idx in range(91, 97)]]
    assert actual["reading"]["items"][1] == original["reading"]["items"][1]
    for item in actual["reading"]["items"]:
        assert set(item.get("result_ids", [])) <= qids
    assert json.loads(saved[0].read_text(encoding="utf-8")) == original


def test_partial_failure_preserves_old_good_values_and_reuses_matching_q_id(saved):
    path, original, *_ = saved
    original["quantitative_results"].append(_q("Q3", method="B", value=.6))
    original["reading"]["items"][0]["result_ids"].append("Q3")
    path.write_text(json.dumps(original), encoding="utf-8")
    bad = _row("B", .7, value_text="0.8")
    actual, audit = _refresh(saved, {"items": [_item(rows=[_row("A", .5), bad])]})
    rows = {q["id"]: q for q in actual["quantitative_results"]}
    assert rows["Q1"]["value"] == .5 and rows["Q3"]["value"] == .6
    assert rows["Q2"] == original["quantitative_results"][1]
    assert audit["reused_result_ids"] == 1 and audit["new_result_ids"] == 0
    assert {"Q1", "Q3", "Q90"} == set(actual["reading"]["items"][0]["result_ids"])
    assert any(issue["code"] == "invalid_reading" for issue in audit["pages"][0]["issues"])


@pytest.mark.parametrize("item", [
    _item(rows=[_row(value=.5, value_text="not an actual scalar", value_bbox=[.95, .95, .99, .99])]),
    _item(status="unreadable", rows=[]),
    {"visual_id": "V1", "status": "invented", "results": []},
])
def test_failed_or_unreadable_items_do_not_remove_old_values(saved, item):
    actual, audit = _refresh(saved, {"items": [item]})
    assert actual["quantitative_results"] == saved[1]["quantitative_results"]
    assert actual["visuals"][0]["description"] == saved[1]["visuals"][0]["description"]
    assert actual["reading"] == saved[1]["reading"]
    assert not audit["refreshed_visual_ids"]


def test_chart_series_x_panel_fields_and_compatibility_condition_are_kept(saved):
    chart = {"chart_type": "line", "series": ["A"], "panels": ["(a)"],
             "axes": [{"id": "x", "panel": "(a)", "label": "epoch", "scale": "linear", "ticks": [0.0, 5.0]},
                      {"id": "y_right", "panel": "(a)", "label": "RMSE", "scale": "linear", "ticks": [0.0, 1.0]}]}
    row = _row(method="", series="A", x=5.0, panel="(a)", axis="y_right",
               condition="evaluation", source="visual_estimate", value=.5,
               readout_uncertainty=.01, error_bar=[.49, .51], value_bbox=[.3, .15, .4, .2])
    actual, audit = _refresh(saved, {"items": [_item(rows=[row], chart=chart)]})
    q = next(q for q in actual["quantitative_results"] if q["visual_id"] == "V1")
    assert q["method"] == q["series"] == "A" and q["x"] == 5.0 and q["panel"] == "(a)"
    assert q["condition"] == "evaluation；子图 (a)；x=5.0；右纵轴"
    assert q["precision"] == "approximate" and q["error_bar"] == [.49, .51]
    assert q["value_bbox"] == [.3, .15, .4, .2] and q["visual_bbox"] == pytest.approx(_box())
    assert actual["visuals"][0]["chart"]["chart_type"] == "line"
    assert audit["recovered_readings"] == 1


def test_forged_visual_does_not_create_association_with_other_reader_items(saved):
    actual, audit = _refresh(saved, {"items": [_item(), _item("V999", [_row("Forged", .99)])]})
    assert [v["id"] for v in actual["visuals"]] == ["V1", "V2"]
    assert actual["reading"]["items"][1] == saved[1]["reading"]["items"][1]
    assert actual["reading"]["items"][2] == saved[1]["reading"]["items"][2]
    assert all(q["visual_id"] != "V999" for q in actual["quantitative_results"])
    assert any(issue["code"] == "unknown_visual_id" for issue in audit["pages"][0]["issues"])


def test_cli_requires_separate_output_and_missing_cache_keeps_values(saved, capsys):
    path, original, cache_root, _, tmp_path = saved
    with pytest.raises(SystemExit):
        refresh_charts.main([str(path)])
    with pytest.raises(ValueError, match="differ"):
        refresh_charts.refresh_report(path, path.parent, cache_root=cache_root)
    output = tmp_path / "cli-out"
    refresh_charts.main([str(path), "--output", str(output), "--cache-root", str(cache_root)])
    actual = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert actual["quantitative_results"] == original["quantitative_results"]
    assert actual["run"]["usage"]["requests"] == 7
    assert sorted(p.name for p in output.iterdir()) == ["chart-refresh-audit.json", "report.json"]
    assert '"requests": 0' in capsys.readouterr().out
    with pytest.raises(ValueError, match="never overwritten"):
        refresh_charts.refresh_report(path, output, cache_root=cache_root)


def test_cache_pointer_cannot_escape_cache_root(saved):
    path, _, cache_root, _, tmp_path = saved
    (path.parent / "visual-verification.json").write_text(json.dumps({"cache_key": "../other"}), encoding="utf-8")
    with pytest.raises(ValueError, match="cache_key"):
        refresh_charts.refresh_report(path, tmp_path / "invalid", cache_root=cache_root)
    assert not (tmp_path / "invalid").exists()


def test_explicit_diagram_removes_chart_only_readings_without_dangling_references(saved):
    actual, audit = _refresh(saved, {"items": [_item(rows=[], chart={"chart_type": "diagram"})]})
    assert [q["id"] for q in actual["quantitative_results"]] == ["Q2", "Q90"]
    assert actual["reading"]["items"][0]["result_ids"] == ["Q90"]
    assert actual["visuals"][0]["chart"]["chart_type"] == "diagram"
    assert audit["refreshed_visual_ids"] == ["V1"]
