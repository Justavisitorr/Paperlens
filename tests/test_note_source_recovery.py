"""Offline source-ID recovery preserves all existing grounding constraints."""
import copy

import pytest
from test_conversation import make_report

from paperlens.config import Settings
from paperlens.conversation import ConversationService, PapersChatInput

QUOTE = "A recovered exact source sentence with evaluation conditions."


def setup_case():
    report = make_report("First", "a", ["An unrelated original sentence about another method.", QUOTE])
    excerpts = {"first": {b.id: b.text for b in report.document.blocks}}
    raw = {"answer": "An explanation.[1]", "quotes": [
        {"paper": "P1", "block_id": "b1", "quote": QUOTE}]}
    return {"first": report}, excerpts, raw


def finish(tmp_path, reports, excerpts, raw, recover=True):
    refs = {pid: f"P{i}" for i, pid in enumerate(reports, 1)}
    return ConversationService(tmp_path)._finish(
        reports, Settings(_env_file=None, llm_api_key=""),
        PapersChatInput(papers=list(reports), question="Explain this object."),
        refs, excerpts, {"grounded": False, "warnings": []}, raw,
        recover_block_ids=recover)


def test_unique_supplied_source_id_can_be_recovered_without_changing_quote(tmp_path):
    reports, excerpts, raw = setup_case()
    original = copy.deepcopy(raw)
    assert not finish(tmp_path, reports, excerpts, raw, recover=False)["grounded"]
    corrected = finish(tmp_path, reports, excerpts, raw)
    assert corrected["grounded"]
    assert corrected["quotes"][0]["block_id"] == "b2"
    assert corrected["quotes"][0]["page"] == reports["first"].document.blocks[1].page
    assert corrected["quotes"][0]["quote"] == QUOTE
    assert raw == original


@pytest.mark.parametrize("case", ["ambiguous", "not_supplied", "other_paper", "unknown_paper", "invalid_index"])
def test_source_recovery_still_rejects_unsafe_or_invalid_references(tmp_path, case):
    reports, excerpts, raw = setup_case()
    if case == "ambiguous":
        block = reports["first"].document.blocks[1].model_copy(update={"id": "b3"})
        reports["first"].document.blocks.append(block)
        excerpts["first"]["b3"] = QUOTE
    elif case in {"not_supplied", "other_paper"}:
        del excerpts["first"]["b2"]
        if case == "other_paper":
            reports["second"] = make_report("Second", "b", [QUOTE])
            excerpts["second"] = {"b1": QUOTE}
    elif case == "unknown_paper":
        raw["quotes"][0]["paper"] = "P9"
    else:
        raw["answer"] = "An explanation.[2]"
    assert not finish(tmp_path, reports, excerpts, raw)["grounded"]


def test_valid_declared_source_is_preserved_even_when_another_block_repeats_text(tmp_path):
    reports, excerpts, raw = setup_case()
    raw["quotes"][0]["block_id"] = "b2"
    excerpts["first"]["b1"] = QUOTE
    reports["first"].document.blocks[0].text = QUOTE
    corrected = finish(tmp_path, reports, excerpts, raw)
    assert corrected["grounded"] and corrected["quotes"][0]["block_id"] == "b2"


def test_one_irrecoverable_quote_still_withholds_whole_answer(tmp_path):
    reports, excerpts, raw = setup_case()
    raw["quotes"].append({"paper": "P1", "block_id": "b1", "quote": "This fabricated sentence is absent."})
    raw["answer"] = "An explanation.[1][2]"
    assert not finish(tmp_path, reports, excerpts, raw)["grounded"]
