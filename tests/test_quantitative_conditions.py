"""Text readouts and condition hints stay grounded in the cited experiment."""

import pytest
from test_evidence_contexts import registry_for

from paperlens.analysis.quantitative import collect_quantitative_results
from paperlens.models import (
    PaperAnalysis,
    PaperDocument,
    PaperNode,
    QuantitativeResult,
)


def extract(text, quote=None, *, kind="text", origin="author", raw_text=None):
    registry = registry_for(text, raw_text, kind)
    eid = registry.resolve("b1", quote or text, {"b1"})
    assert eid
    analysis = PaperAnalysis(
        title="Results",
        summary="Results",
        research_question="How well?",
        nodes=[
            PaperNode(
                id="result",
                kind="conclusion",
                title="Result",
                summary=quote or text,
                evidence_ids=[eid],
                origin=origin,
            )
        ],
        edges=[],
    )
    return collect_quantitative_results(
        PaperDocument(
            title="Results",
            source_name="test.pdf",
            sha256="test",
            parser="fixture",
            pages=list(registry.pages.values()),
            blocks=list(registry.blocks.values()),
        ),
        analysis,
        list(registry.items.values()),
    )


def test_textual_metric_captures_explicit_local_conditions_and_provenance():
    text = (
        "Accuracy reached 93.2% on CIFAR-10 test split, n=1000, "
        "compared with ResNet-18, batch size=32, averaged over 5 seeds."
    )
    (result,) = extract(text)
    assert result.value == 93.2 and result.unit == "%"
    assert result.precision == "reported" and result.source == "text"
    assert result.node_ids == ["result"]
    by_kind = {condition.kind: condition for condition in result.conditions}
    assert by_kind["dataset"].value == "on CIFAR-10"
    assert by_kind["split"].value == "test split"
    assert by_kind["sample_size"].value == "n=1000"
    assert by_kind["baseline"].value == "compared with ResNet-18"
    assert by_kind["hyperparameters"].value == "batch size=32"
    assert by_kind["evaluation_protocol"].value == "averaged over 5 seeds"
    assert all(condition.evidence_ids == result.evidence_ids for condition in result.conditions)
    assert not result.missing_conditions


def test_unextracted_conditions_are_hints_not_fabricated_data():
    (result,) = extract("Accuracy reached 93.2%.")
    assert result.dataset == "" and result.conditions == []
    assert result.missing_conditions == ["dataset", "split"]
    assert "仅来自" in result.notes
    assert "sample_size" not in result.missing_conditions
    assert "hardware" not in result.missing_conditions


def test_long_normalized_evidence_uses_local_conditions_not_far_experiment():
    text = (
        "Accuracy reached 81.0% on MNIST training set. "
        + "Background. " * 600
        + "Accuracy reached 93.2% on CIFAR-10 test\nsplit."
    )
    (result,) = extract(text, "Accuracy reached 93.2% on CIFAR-10 test split.")
    assert result.value == 93.2
    assert "CIFAR-10" in result.dataset
    assert "MNIST" not in result.condition
    assert any(condition.kind == "split" for condition in result.conditions)


def test_raw_only_quote_gets_conditions_from_matched_raw_source():
    (result,) = extract(
        "Reconstructed content has no experiment.",
        "Accuracy reached 93.2%",
        raw_text="Accuracy reached 93.2% on MNIST test set.",
    )
    assert "MNIST" in result.dataset and "test set" in result.condition


def test_table_text_has_table_provenance_and_ambiguous_split_is_unextracted():
    (result,) = extract("Accuracy = 93.2% on MNIST training set and test set.", kind="table")
    assert result.source == "table_text"
    assert "split" in result.missing_conditions
    assert not any(condition.kind == "split" for condition in result.conditions)


def test_comparison_and_runtime_have_applicable_hints():
    (comparison,) = extract("Accuracy improved by 8.3% on MNIST test split.")
    assert "baseline" in comparison.missing_conditions
    assert "百分点" in comparison.notes
    (runtime,) = extract("Latency reached 12 ms on MNIST dataset.")
    assert runtime.unit == "ms" and "hardware" in runtime.missing_conditions
    assert "split" not in runtime.missing_conditions


@pytest.mark.parametrize("link", ["improved to", "decreased to", "improved at"])
def test_changed_final_values_are_not_misrepresented_as_change_amounts(link):
    (result,) = extract(f"Accuracy {link} 90% on MNIST test split.")
    assert result.metric.lower() == "accuracy" and result.value == 90
    assert "baseline" not in result.missing_conditions
    assert "百分点" not in result.notes


def test_identical_readouts_in_different_experiments_use_their_own_spans():
    first, second = extract(
        "Accuracy reached 90% on MNIST test set. Accuracy reached 90% on CIFAR-10 validation set."
    )
    assert first.value == second.value == 90
    assert "MNIST" in first.dataset and "test set" in first.condition
    assert "CIFAR-10" in second.dataset and "validation set" in second.condition
    assert first.id != second.id


def test_plain_decimal_end_and_table_rows_do_not_mix_experiment_conditions():
    first, second = extract("RMSE reached 0.75 on MNIST. RMSE reached 0.25 on CIFAR-10.")
    assert "MNIST" in first.dataset and "CIFAR-10" in second.dataset
    first, second = extract(
        "Accuracy = 90% on MNIST test set\nAccuracy = 80% on CIFAR-10 validation set", kind="table"
    )
    assert "MNIST" in first.dataset and "test set" in first.condition
    assert "CIFAR-10" in second.dataset and "validation set" in second.condition


def test_new_condition_fields_roundtrip_and_validate_existing_references():
    from pydantic import ValidationError

    from paperlens.models import DashboardReport, Quality, RunInfo

    registry = registry_for("Accuracy reached 93.2% on MNIST test set.")
    eid = registry.resolve("b1", registry.blocks["b1"].text, {"b1"})
    document = PaperDocument(
        title="Paper",
        source_name="test.pdf",
        sha256="test",
        parser="fixture",
        pages=list(registry.pages.values()),
        blocks=list(registry.blocks.values()),
    )
    analysis = PaperAnalysis(
        title="Paper",
        summary="",
        research_question="",
        edges=[],
        nodes=[
            PaperNode(
                id="result",
                kind="conclusion",
                title="Result",
                summary="Accuracy",
                evidence_ids=[eid],
            )
        ],
    )
    values = collect_quantitative_results(document, analysis, list(registry.items.values()))
    report = DashboardReport(
        document=document,
        analysis=analysis,
        evidence=list(registry.items.values()),
        graphs=[],
        previews=[],
        quantitative_results=values,
        quality=Quality(
            total_pages=1, parsed_pages=1, text_chars=100, grounded_nodes=1, total_nodes=1
        ),
        run=RunInfo(
            id="test", created_at="now", model="fixture", parser="fixture", status="complete"
        ),
    )
    assert (
        DashboardReport.model_validate_json(report.model_dump_json()).quantitative_results == values
    )
    invalid = report.model_dump()
    invalid["quantitative_results"][0]["conditions"][0]["evidence_ids"] = ["EFAKE"]
    with pytest.raises(ValidationError, match="condition and node references"):
        DashboardReport.model_validate(invalid)
    invalid = report.model_dump()
    invalid["quantitative_results"][0]["node_ids"] = ["unknown"]
    with pytest.raises(ValidationError, match="condition and node references"):
        DashboardReport.model_validate(invalid)


@pytest.mark.parametrize(
    "text",
    [
        "Accuracy increased from 83.0% to 93.2%.",
        "Accuracy reached 93.2% ± 0.5%.",
        "Accuracy reached 92-94%.",
        "There were 1000 samples in our experiment.",
        "RMSE reached 1e999.",
    ],
)
def test_unlabelled_or_ambiguous_numbers_are_not_exact_single_readouts(text):
    assert extract(text) == []


def test_reader_inference_does_not_become_a_reported_paper_result():
    assert extract("Accuracy reached 93.2%.", origin="inference") == []


def test_explicit_approximate_text_keeps_its_precision():
    (result,) = extract("准确率约93.2%，在MNIST测试集。")
    assert result.value == 93.2 and result.precision == "approximate"


def test_old_visual_results_remain_compatible_without_promoting_estimates():
    from paperlens.models import PaperDocument

    registry = registry_for("No metric readouts.")
    document = PaperDocument(
        title="Paper",
        source_name="test.pdf",
        sha256="test",
        parser="fixture",
        pages=list(registry.pages.values()),
        blocks=list(registry.blocks.values()),
    )
    analysis = PaperAnalysis(title="Paper", summary="", research_question="", nodes=[], edges=[])
    old = QuantitativeResult(
        id="Q1",
        metric="accuracy",
        value=93.2,
        dataset="MNIST",
        condition="test set",
        source="visual_estimate",
        precision="approximate",
        page=1,
    )
    (result,) = collect_quantitative_results(document, analysis, [], [old])
    assert old.conditions == []
    assert result.precision == "approximate"
    assert {c.kind for c in result.conditions} == {"dataset", "other"}
    assert "split" in result.missing_conditions
    unavailable = old.model_copy(
        update={"source": "figure_only", "precision": "unavailable", "value": None}
    )
    (result,) = collect_quantitative_results(document, analysis, [], [unavailable])
    assert result.value is None and result.missing_conditions == []
