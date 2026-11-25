"""Offline matching and provenance; fixture charts are never human truth."""

import json
import socket

import pytest

from tools import evaluate_charts as charts


def point(series="A", x="2020", value=10, source="figure_label"):
    return {"series": series, "x": x, "value": value, "source": source}


def score(expected, actual, **kwargs):
    return charts.evaluate_case({"id": "one", "expected": expected, "actual": actual, **kwargs})


def test_exact_labels_require_both_series_and_x():
    result = score([point()], [point()])
    assert result["precision"] == result["recall"] == 1
    assert result["explicit_label_match_rate"] == 1
    wrong = score([point()], [point(series="B")])
    assert wrong["precision"] == wrong["recall"] == 0
    assert wrong["identity_matches"] == 0
    assert wrong["missing"] == wrong["extra_items"] == 1
    assert score([point()], [point(x="2021")])["correct"] == 0


def test_missing_extra_and_wrong_value_have_separate_counts():
    result = score(
        [point(), point(x="2021", value=20)], [point(value=11), point(x="2022", value=30)]
    )
    assert result["missing"] == result["extra_items"] == result["wrong_values"] == 1
    assert result["explicit_label_match_rate"] == 0
    assert result["precision"] == result["recall"] == 0


def test_estimated_mae_uses_only_matched_keys_and_case_tolerance():
    expected = [
        point(value=10, source="visual_estimate"),
        point(x="2021", value=20, source="visual_estimate"),
    ]
    result = score(expected, [point(value=10.5), point(x="other", value=21)], tolerance=0.5)
    assert result["estimated_mae"] == 0.5
    assert result["estimated_matched"] == 1
    assert result["estimated_coverage"] == 0.5
    assert result["estimated_within_tolerance_rate"] == 1
    assert result["recall"] == result["precision"] == 0.5
    assert result["explicit_label_match_rate"] is None
    assert score(expected, [point(value=10.6)], tolerance=0.5)["wrong_values"] == 1


def test_tolerance_can_be_relative_and_handles_zero_reference():
    result = score(
        [point(value=100, source="estimate")], [point(value=101)], tolerance={"relative": 0.01}
    )
    assert result["correct"] == 1
    result = score(
        [point(value=0, source="estimate")],
        [point(value=0.1)],
        tolerance={"absolute": 0.2, "relative": 0.01},
    )
    assert result["estimated_mae"] == 0.1
    assert result["correct"] == 1
    with pytest.raises(ValueError, match="Tolerance"):
        score([point(source="estimate")], [], tolerance=-1)


def test_estimate_tolerance_does_not_relax_explicit_labels():
    assert score([point()], [point(value=10.1)], tolerance=100)["correct"] == 0
    assert score([point(value=0)], [point(value=1e-13)])["correct"] == 0


def test_zero_x_is_a_real_key_and_numeric_claim_cannot_hide_as_unreadable():
    assert score([point(x=0)], [point(x=0)])["correct"] == 1
    assert score([point(x=0)], [point(x="")])["correct"] == 0
    assert score([point()], [point(x="extra", source="figure_only")])["extra_items"] == 1


def test_duplicates_do_not_inflate_recall_or_replace_first_prediction():
    result = score([point()], [point(), point()])
    assert result["recall"] == 1
    assert result["precision"] == 0.5
    assert result["duplicate_items"] == result["extra_items"] == 1
    assert score([point()], [point(value=11), point()])["correct"] == 0
    with pytest.raises(ValueError, match="unique"):
        score([point(), point()], [point()])


def test_unknown_truth_unreadable_actual_and_nonnumeric_chart_are_unscored():
    result = score(
        [point(), point(x="2021", value=None, source="figure_only")],
        [point(value=None, source="unreadable"), point(x="2021", value=100)],
    )
    assert result["ignored_expected"] == 1
    assert result["ignored_actual"] == 2
    assert result["missing"] == 1
    assert result["wrong_values"] == result["extra_items"] == 0
    assert result["precision"] is None
    assert result["recall"] == 0
    for args in ({}, {"numeric": False}):
        empty = score([], [point()], **args)
        assert empty["status"] == "not_evaluated"
        assert empty["precision"] is empty["recall"] is None
    assert charts.number(True) is None
    assert charts.number(float("nan")) is None
    assert charts.number("10") is None


def test_partial_truth_marks_unknown_extra_items_without_penalty():
    result = score([point()], [point(), point(x="2021")], ground_truth_complete=False)
    assert result["precision"] == result["recall"] == 1
    assert result["extra_items"] == 0
    assert result["unverified_extra_items"] == 1


def test_aggregate_is_micro_average_and_does_not_mix_mae_units():
    result = charts.evaluate(
        {
            "ground_truth_source": "human",
            "cases": [
                {
                    "id": "a",
                    "expected": [point(source="estimate")],
                    "actual": [point(value=11)],
                    "tolerance": 1,
                },
                {"id": "b", "expected": [point(), point(x="2021")], "actual": [point()]},
                {"id": "diagram", "numeric": False, "expected": [], "actual": []},
            ],
        }
    )
    assert result["aggregate"]["precision"] == 1
    assert result["aggregate"]["recall"] == pytest.approx(2 / 3)
    assert "estimated_mae" not in result["aggregate"]
    assert result["cases"][0]["estimated_mae"] == 1
    assert result["not_evaluated_cases"] == 1
    assert result["independent_truth_declared"]
    assert charts.evaluate({"cases": []})["aggregate"]["precision"] is None


def test_model_truth_rejected_and_missing_provenance_disclosed():
    with pytest.raises(ValueError, match="independent"):
        charts.evaluate({"ground_truth_source": "cache", "cases": []})
    assert not charts.evaluate({"cases": []})["independent_truth_declared"]
    with pytest.raises(ValueError, match="series and x"):
        score([{"value": 10}], [])
    with pytest.raises(ValueError, match="unique"):
        charts.evaluate(
            {
                "cases": [
                    {"id": "duplicate", "expected": [], "actual": []},
                    {"id": "duplicate", "expected": [], "actual": []},
                ]
            }
        )


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_inventory_deduplicates_reports_replays_cache_without_media_or_credentials(
    tmp_path,
    monkeypatch,
):
    def no_network(*args, **kwargs):
        raise AssertionError("Offline evaluation attempted network access")

    monkeypatch.setattr(socket, "socket", no_network)
    pdf = tmp_path / "outputs/run/source.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"independent source PDF")
    sha = charts.hashlib.sha256(pdf.read_bytes()).hexdigest()
    report = {
        "document": {"title": "Test document", "sha256": sha, "source_name": "source.pdf"},
        "source_pdf": {"data_url": "data:application/pdf;base64,DO-NOT-EXPORT"},
        "llm_api_key": "secret-should-not-export",
        "visuals": [
            {"id": "V1", "kind": "figure", "page": 2, "label": "Fig. 1"},
            {"id": "V2", "kind": "formula", "page": 2, "label": "Equation 1"},
        ],
    }
    for name in ("report.json", "report-draft.json"):
        write(pdf.parent / name, report)
    write(pdf.parent / "visual-verification.json", {"cache_key": "abcdef"})
    cache = tmp_path / ".cache/paperlens/visuals/abcdef/page-0002.json"
    write(
        cache,
        {
            "response": {
                "items": [
                    {"visual_id": "V1", "results": [{"value": 1}], "description": "model-only"},
                ]
            },
            "secret": "another-secret",
        },
    )
    before = cache.read_bytes()
    result = charts.inventory(tmp_path)
    assert result["total_candidates"] == 1
    assert result["cache_pages_replayed"] == 1
    case = result["cases"][0]
    assert case["cached_numeric_readings_max"] == 1
    assert len(case["report_paths"]) == 2
    assert case["cache_paths"] == [".cache/paperlens/visuals/abcdef/page-0002.json"]
    assert case["source_pdf_candidates"] == ["outputs/run/source.pdf"]
    assert cache.read_bytes() == before
    encoded = json.dumps(result)
    assert "DO-NOT-EXPORT" not in encoded
    assert "secret-should-not-export" not in encoded
    assert "another-secret" not in encoded
    assert "model-only" not in encoded
    assert "expected" not in case
    charts.main(["--inventory", "--root", str(tmp_path), "--output", str(tmp_path / "review")])
    assert (tmp_path / "review/inventory.json").exists()


def test_evaluate_cli_exports_only_scores(tmp_path):
    truth = tmp_path / "truth.json"
    write(
        truth,
        {
            "ground_truth_source": "manual",
            "secret": "do-not-export",
            "cases": [
                {
                    "id": "a",
                    "expected": [point()],
                    "actual": [point()],
                    "image": "data:image/png;base64,do-not-export",
                },
            ],
        },
    )
    output = tmp_path / "result"
    charts.main(["--evaluate", str(truth), "--output", str(output)])
    content = (output / "evaluation.json").read_text(encoding="utf-8")
    assert "do-not-export" not in content
    assert json.loads(content)["aggregate"]["correct"] == 1


def test_audit_only_captions_keep_cache_association_explicit(tmp_path):
    directory = tmp_path / "outputs/incomplete"
    write(directory / "visual-verification.json", {"cache_key": "cached"})
    write(
        directory / "document.json",
        {
            "title": "Incomplete run",
            "sha256": "a",
            "source_name": "paper.pdf",
            "blocks": [
                {"id": "b1", "kind": "caption", "page": 2, "text": "Figure 1: Results"},
                {"id": "b2", "kind": "caption", "page": 2, "text": "Figure 1: Repeated caption"},
                {"id": "b3", "kind": "text", "page": 2, "text": "Figures 2–4 compare methods"},
            ],
        },
    )
    write(
        tmp_path / ".cache/paperlens/visuals/cached/page-0002.json",
        {
            "response": {"items": [{"visual_id": "V42", "results": [{"value": 3}]}]},
        },
    )
    result = charts.inventory(tmp_path)
    assert result["total_candidates"] == result["audits_scanned"] == 1
    case = result["cases"][0]
    assert case["visual_ids"] == []
    assert case["cache_association"] == "page_only; visual ID unresolved"
    assert case["source_block_ids"] == ["b1", "b2"]
    assert case["cached_numeric_readings_max"] == 0


def test_numeric_x_identity_matches_integer_and_float_without_changing_category_labels():
    assert charts.identity({"series": "A", "x": 5}) == charts.identity({"series": "A", "x": 5.0})
    assert charts.identity({"series": "A", "x": "005"}) != charts.identity({"series": "A", "x": 5})
