"""One reviewed source feeds reasoning, evidence, resume and the reading view."""

import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from test_analysis_engine import ScriptedProvider
from test_edition_math_review import Provider as MathProvider
from test_edition_text_review import Provider as TextProvider
from test_edition_text_review import edition

from paperlens import pipeline
from paperlens.analysis.engine import AnalysisResult, analyze_document, document_chunks
from paperlens.analysis.evidence import EvidenceRegistry
from paperlens.audited_source import canonicalize_reading
from paperlens.config import Settings
from paperlens.edition_math_review import review_math
from paperlens.edition_text_review import review_text
from paperlens.models import DashboardReport, PaperNode, TableCell, TableGrid, Usage
from paperlens.reading import utf16
from paperlens.translation import _item_text as displayed_text


def test_corrections_replace_all_evidence_text_and_preserve_unchanged_ids():
    original = edition([(1, "Accuracy is 90%; the result is significant."),
                        (1, "The unchanged control paragraph."), (1, r"$x = a+b$")])
    original.reading.items[2].kind = original.document.blocks[2].kind = "formula"
    current = original.model_copy(deep=True)
    current.reading.items[0].content = "Accuracy is 80%; the result is not significant."
    current.reading.items[2].content = r"$x = a-b$"
    audit = canonicalize_reading(current)
    changed, unchanged, formula = current.document.blocks
    assert changed.id != "source-0" and formula.id != "source-2"
    assert unchanged.id == "source-1"
    assert changed.text == changed.raw_text == current.reading.items[0].content
    assert formula.text == formula.raw_text == r"$x = a-b$"
    assert changed.source_block_ids == ["source-0"]
    assert original.document.blocks[0].text == "Accuracy is 90%; the result is significant."
    registry = EvidenceRegistry(current.document)
    assert registry.resolve(changed.id, changed.text, {changed.id})
    assert registry.resolve(changed.id, original.document.blocks[0].text, {changed.id}) is None
    assert registry.resolve("source-0", original.document.blocks[0].text, {"source-0"}) is None
    assert registry.resolve(formula.id, r"$x = a+b$", {formula.id}) is None
    assert audit["blocks"][0]["source_parts"][0]["block_id"] == "source-0"
    assert current.reading.items[0].parts[0].end == utf16(changed.text)
    assert {span.source.block_id for span in current.reading.items[0].content_spans} == {changed.id}
    assert "90%" not in str(document_chunks(current.document, 10000))


def merged_reading():
    current = edition([(1, "The experiment demonstrates "), (2, "a significant increase."),
                       (2, "-"), (2, "A separate, unchanged paragraph.")])
    for index, block in enumerate(current.document.blocks):
        block.order = index
    item = current.reading.items[0]
    item.parts.extend(current.reading.items[1].parts)
    item.source_pages = [1, 2]
    item.content = "The experiment demonstrates no significant increase."
    current.reading.items = [item, current.reading.items[3]]
    return current


def test_merged_and_removed_items_have_auditable_page_ranges_and_stable_ids():
    current = merged_reading()
    audit = canonicalize_reading(current)
    merged, unchanged = current.document.blocks
    assert merged.page == 1 and merged.source_pages == [1, 2]
    assert merged.source_block_ids == ["source-0", "source-1"]
    assert unchanged.id == "source-3" and audit["removed_source_ids"] == ["source-2"]
    assert [row["block_id"] for row in audit["blocks"]] == [merged.id, unchanged.id]
    registry = EvidenceRegistry(current.document)
    evidence_id = registry.resolve(merged.id, merged.text, {merged.id})
    assert registry.items[evidence_id].source_pages == [1, 2]
    assert registry.contexts([evidence_id])[0]["source_pages"] == [1, 2]
    assert document_chunks(current.document, 10000)[0][0]["source_pages"] == [1, 2]
    current.evidence = list(registry.items.values())
    DashboardReport.model_validate(current.model_dump())
    misleading_page = current.model_dump()
    misleading_page["evidence"][0]["source_pages"] = []
    with pytest.raises(ValueError, match="source pages"):
        DashboardReport.model_validate(misleading_page)
    again = merged_reading()
    canonicalize_reading(again)
    assert again.document == current.document
    assert again.reading.source_version == current.reading.source_version
    changed = merged_reading()
    changed.reading.items[0].content = "The experiment demonstrates an uncertain change."
    canonicalize_reading(changed)
    assert changed.document.blocks[0].id != merged.id
    assert changed.reading.source_version != current.reading.source_version


def test_table_cells_and_corrected_caption_are_available_to_analysis_once():
    current = edition([(1, "Table 1: Result\nMethod | Score\nBaseline | 90")])
    grid = TableGrid(rows=2, cols=2, cells=[
        TableCell(text=text, row_start=row, row_end=row + 1, col_start=col, col_end=col + 1)
        for row, values in enumerate([["Method", "Score"], ["Baseline", "90"]])
        for col, text in enumerate(values)
    ])
    current.document.blocks[0].kind = current.reading.items[0].kind = "table"
    current.document.blocks[0].table = grid
    item = current.reading.items[0]
    item.content = "Table 1: Result"
    item.table = grid.model_copy(deep=True)
    unchanged = current.model_copy(deep=True)
    canonicalize_reading(unchanged)
    assert unchanged.document.blocks[0].id == "source-0"
    item.content = "Table 1: Accuracy"
    item.table.cells[-1].text = "80"
    canonicalize_reading(current)
    block = current.document.blocks[0]
    assert block.text == "Table 1: Accuracy\nMethod | Score\nBaseline | 80"
    assert block.raw_text == block.text and block.table.cells[-1].text == "80"
    registry = EvidenceRegistry(current.document)
    assert registry.resolve(block.id, "Baseline | 80", {block.id})
    assert registry.resolve(block.id, "Baseline | 90", {block.id}) is None


@pytest.mark.parametrize("caption_mode", ["label", "source-prefix", "empty", "missing"])
def test_table_fallback_keeps_caption_separate_and_does_not_duplicate_cells(caption_mode):
    caption = "Table 1: Result" if caption_mode in {"label", "source-prefix"} else ""
    current = edition([(1, caption + "\nMethod | Score\nBaseline | 90")])
    block, item = current.document.blocks[0], current.reading.items[0]
    block.kind = item.kind = "table"
    block.table = TableGrid(rows=2, cols=2, cells=[
        TableCell(text=text, row_start=row, row_end=row + 1, col_start=col, col_end=col + 1)
        for row, values in enumerate([["Method", "Score"], ["Baseline", "90"]])
        for col, text in enumerate(values)
    ])
    item.table = block.table.model_copy(deep=True)
    item.label = caption if caption_mode == "label" else ""
    item.content = "" if caption_mode == "empty" else None
    unchanged = current.model_copy(deep=True)
    canonicalize_reading(unchanged)
    assert unchanged.document.blocks[0].id == "source-0"
    item.table.cells[-1].text = "80"
    canonicalize_reading(current)
    canonical = current.document.blocks[0]
    assert canonical.text == caption + "\nMethod | Score\nBaseline | 80"
    assert canonical.text.count("Method | Score") == canonical.text.count("Baseline | 80") == 1
    assert "Baseline | 90" not in canonical.text
    # Both readingText (browser) and translation use explicit content first;
    # its caption never falls back to the full canonical table in item.parts.
    assert item.content == displayed_text(current, item) == caption
    assert item.parts[0].end == utf16(canonical.text)
    assert item.table.cells[-1].text == "80"


def test_source_pages_validate_and_old_reports_remain_readable():
    legacy = edition([(1, "Existing legacy text remains readable.")]).model_dump()
    for block in legacy["document"]["blocks"]:
        block.pop("source_pages")
        block.pop("source_block_ids")
    restored = DashboardReport.model_validate(legacy)
    assert restored.document.blocks[0].id == "source-0"
    assert restored.document.blocks[0].source_pages == []
    invalid = merged_reading()
    invalid.reading.items[0].source_pages = [1, 3]
    with pytest.raises(ValueError, match="outside the original PDF"):
        canonicalize_reading(invalid)
    broken = restored.model_dump()
    broken["document"]["blocks"][0]["source_pages"] = [1, 2]
    with pytest.raises(ValueError, match="source pages"):
        DashboardReport.model_validate(broken)


def test_metadata_only_source_parts_survive_without_stale_raw_text():
    current = edition([(1, "An ordinary body paragraph."), (1, "Alice, Example University.")])
    author = current.reading.items.pop()
    current.reading.information = {"authors": ["source-1"]}
    current.reading.information_parts = {"authors": author.parts}
    current.document.blocks[1].raw_text = "Carol, Wrong University."
    audit = canonicalize_reading(current)
    assert audit["removed_source_ids"] == []
    metadata = next(block for block in current.document.blocks if block.id == "source-1")
    assert metadata.text == metadata.raw_text == "Alice, Example University."
    assert current.reading.information_parts["authors"] == author.parts
    assert EvidenceRegistry(current.document).resolve("source-1", "Carol, Wrong University.", {"source-1"}) is None


def test_same_reviewed_source_reuses_analysis_and_changed_review_invalidates_it(tmp_path):
    class DynamicProvider(ScriptedProvider):
        def json(self, system, user, **kwargs):
            answer = super().json(system, user, **kwargs)
            payload = json.loads(user)
            if "blocks" in payload:
                answer["findings"][0]["evidence"][0]["block_id"] = payload["blocks"][0]["block_id"]
            return answer

    def canonical(text):
        result = edition([(1, "The result supports the tested hypothesis.")])
        result.reading.items[0].content = text
        canonicalize_reading(result)
        return result.document

    configured = Settings(_env_file=None, paperlens_cache_dir=tmp_path)
    corrected = "The result does not support the tested hypothesis."
    first_provider = DynamicProvider()
    first = analyze_document(canonical(corrected), first_provider, configured)
    assert first_provider.calls == 3
    reused_provider = DynamicProvider()
    reused = analyze_document(canonical(corrected), reused_provider, configured)
    assert reused_provider.calls == 0 and reused.evidence == first.evidence
    changed_provider = DynamicProvider()
    changed = analyze_document(canonical(corrected + " Replication is needed."), changed_provider, configured)
    assert changed_provider.calls == 3
    assert changed.evidence[0].block_id != first.evidence[0].block_id


def test_real_review_caches_replay_original_transcript_before_canonical_freeze(tmp_path, monkeypatch):
    original = edition([(1, "The result is significant at 90%."), (1, r"\frac{a}{b}=c")])
    original.document.parser = "openai-pdf"
    original.reading.version = "3"
    original.reading.structured_pages = [1]
    original.document.blocks[1].kind = original.reading.items[1].kind = "formula"
    for item, block in zip(original.reading.items, original.document.blocks, strict=True):
        item.content = block.text
    configured = Settings(_env_file=None, llm_api_key="test", paperlens_cache_dir=tmp_path / "cache",
                          paperlens_vision_allow_unverified=True)
    monkeypatch.setattr("paperlens.edition_math_review.page_image", lambda *args, **kwargs: "image")
    monkeypatch.setattr("paperlens.edition_text_review.page_image", lambda *args, **kwargs: "image")
    math = MathProvider(lambda _: {"patches": [{"id": "item-1", "content": r"\frac{a}{b}=d"}]})
    text = TextProvider(lambda _: {"reviews": [{
        "id": "item-0", "status": "corrected", "content": "The result is not significant at 80%.",
        "reason": "原页含否定词，数字为 80", "issue": "source_mismatch",
        "source_quote": "not significant at 80%", "source_pages": [1],
    }]})
    first, resumed = original.model_copy(deep=True), original.model_copy(deep=True)
    for current in (first, resumed):
        assert current.reading.source_version == "source"
        math_audit = review_math(current, tmp_path / "source.pdf", math, configured)
        text_audit = review_text(current, tmp_path / "source.pdf", text, configured)
        assert current.reading.source_version == "source"
        canonicalize_reading(current)
    assert len(math.calls) == len(text.calls) == 1
    assert math_audit["cache_hits"] == text_audit["cache_hits"] == 1
    assert first.document == resumed.document
    assert first.reading.source_version == resumed.reading.source_version != "source"
    assert first.document.blocks[0].text == "The result is not significant at 80%."
    assert first.document.blocks[1].text == r"\frac{a}{b}=d"


@pytest.fixture
def pipeline_fixture(tmp_path, monkeypatch):
    original = edition([(1, "The result is significant at 90%."), (1, r"$x = a+b$"), (1, "-")])
    original.document.parser = "openai-pdf"
    for index, block in enumerate(original.document.blocks):
        block.order = index
        block.source_ref = "ai-direct:unverified-position"
    original.document.blocks[1].kind = original.reading.items[1].kind = "formula"
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF fixture: model output is injected")
    configured = Settings(
        _env_file=None, llm_api_key="test", paperlens_cache_dir=tmp_path / "cache",
        paperlens_output_dir=tmp_path / "output", paperlens_reference_network=False,
        paperlens_auto_translate_enabled=False, paperlens_auto_notes_enabled=False,
    )

    class Provider:
        def __init__(self, *args, **kwargs):
            self.usage = Usage()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def usage_record(self):
            return {"model": configured.llm_model, "usage": self.usage.model_dump()}

        def usage_records(self):
            return []

    monkeypatch.setattr(pipeline, "validate_path", lambda path, _: path)
    monkeypatch.setattr(pipeline, "get_dashboard_template", lambda: "template")
    monkeypatch.setattr(pipeline, "browser_session", lambda *args: nullcontext())
    monkeypatch.setattr(pipeline, "read_native", lambda *args, **kwargs: SimpleNamespace(
        report=original.model_copy(deep=True), audit={"status": "completed", "cache_hits": 1, "batches": []},
    ))
    monkeypatch.setattr(pipeline, "JsonProvider", Provider)
    monkeypatch.setattr(pipeline, "ProviderPool", Provider)
    monkeypatch.setattr(pipeline, "build_views", lambda *_: [])
    monkeypatch.setattr(pipeline, "render_page_previews", lambda *args, **kwargs: [])
    monkeypatch.setattr(pipeline, "resolve_references", lambda *args, **kwargs: {})
    monkeypatch.setattr(pipeline, "report_html", lambda *_: "<html>Fixture</html>")
    monkeypatch.setattr(pipeline, "write_report", lambda _, output, **kwargs: output / "dashboard.html")
    return original, source, configured


@pytest.mark.parametrize("review_state", ["corrected", "unresolved", "failed"])
def test_pipeline_analysis_follows_review_and_only_incomplete_work_is_deferred(
    pipeline_fixture, monkeypatch, review_state,
):
    original, source, configured = pipeline_fixture
    original.reading.items[0].asset = "data:image/png;base64,do-not-duplicate"
    calls, inputs = [], []

    def math_review(report, *args, **kwargs):
        calls.append("math")
        assert report.reading.source_version == "source"
        assert report.document.blocks[1].text == r"$x = a+b$"
        report.reading.items[1].content = r"$x = a-b$"
        # An intentional profile cap is not an incomplete provider batch.
        return {"status": "partial", "batches": [{"status": "completed"}]}

    def text_review(report, *args, **kwargs):
        calls.append("text")
        assert report.reading.source_version == "source"
        assert report.document.blocks[0].text == original.document.blocks[0].text
        if review_state != "unresolved":
            report.reading.items[0].content = "The result is not significant at 80%."
        report.reading.items.pop()
        return {
            "status": "completed" if review_state == "corrected" else "partial",
            "cache_hits": 0, "reviewed": 1, "candidates": 2, "patches": [{"id": "item-0"}],
            "removals": [{"id": "item-2"}],
            "remaining_ids": ["item-0"] if review_state == "failed" else [],
            "unresolved": [{"id": "item-0", "reason": "原页数字仍不清楚", "source_pages": [1]}]
            if review_state == "unresolved" else [],
            "batches": [{"status": "unavailable", "error_code": "provider_error", "pages": [1],
                         "ids": ["item-0"], "reason": "fixture failure"}]
            if review_state == "failed" else [{"status": "completed"}],
        }

    def analyze(document, *args, **kwargs):
        calls.append("analysis")
        inputs.append(document.model_dump())
        expected_text = (original.document.blocks[0].text if review_state == "unresolved"
                         else "The result is not significant at 80%.")
        assert [block.text for block in document.blocks] == [expected_text, r"$x = a-b$"]
        assert all(block.raw_text == block.text for block in document.blocks)
        registry = EvidenceRegistry(document)
        block = document.blocks[0]
        eid = registry.resolve(block.id, block.text, {block.id})
        assert bool(registry.resolve(block.id, original.document.blocks[0].text, {block.id})) is (review_state == "unresolved")
        assert bool(kwargs["supplemental_context"]["warnings"]) is (review_state == "unresolved")
        analysis = original.analysis.model_copy(deep=True)
        analysis.nodes = [PaperNode(id="claim", title="Result", summary="Observed result", kind="conclusion",
                                    evidence_ids=[eid], support="supported")]
        return AnalysisResult(analysis, list(registry.items.values()), {"chunks": 1})

    def postprocess(*args, **kwargs):
        calls.append("post")
        return {}, []

    monkeypatch.setattr(pipeline, "review_math", math_review)
    monkeypatch.setattr(pipeline, "review_text", text_review)
    monkeypatch.setattr(pipeline, "analyze_document", analyze)
    monkeypatch.setattr(pipeline, "_run_postprocessing", postprocess)
    result = pipeline.run_pipeline(source, configured)
    assert calls == (["math", "text"] if review_state == "failed" else ["math", "text", "analysis", "post"])
    assert result.retryable is (review_state == "failed")
    assert result.report.run.status == ("partial" if review_state == "failed" else "complete")
    if review_state == "unresolved":
        assert result.report.document.blocks[0].text == original.document.blocks[0].text
        assert result.report.reading.items[0].content is None
    else:
        assert result.report.reading.items[0].content == "The result is not significant at 80%."
    saved_original = json.loads((result.output_dir / "original-document.json").read_text("utf-8"))
    assert saved_original["blocks"][0]["text"] == original.document.blocks[0].text
    assert "do-not-duplicate" not in (result.output_dir / "original-reading.json").read_text("utf-8")
    assert json.loads((result.output_dir / "audited-source.json").read_text("utf-8"))["removed_source_ids"] == ["source-2"]
    if review_state == "failed":
        assert not inputs and not result.report.analysis.nodes
        assert result.report.run.failures[0].stage == "text_review"
        assert result.report.run.failures[0].pages == [1]
        assert result.report.run.failures[0].message == "fixture failure"
        assert not any("全部参与分析" in check for check in result.report.quality.checks)
    else:
        expected = "partial" if review_state == "unresolved" else "supported"
        assert result.report.analysis.nodes[0].support == expected
        assert any("原页数字仍不清楚" in warning for warning in result.report.quality.warnings) is (review_state == "unresolved")
        assert not result.report.run.failures
    DashboardReport.model_validate(result.report.model_dump())
