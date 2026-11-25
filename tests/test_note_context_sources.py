"""Optional note context cannot mention source intervals absent from the request."""
import copy
import json

from test_auto_notes import PAPER, config, report

from paperlens.auto_notes import AutoNoteService, _prune_note_context
from paperlens.models import ReadingPart


def prepared_case(tmp_path):
    current = report()
    target = next(item for item in current.reading.items if item.id == "p2")
    prepared = AutoNoteService(tmp_path)._prepare(
        PAPER, current, config(tmp_path, paperlens_auto_notes_context="nearby"), target)
    return current, list(prepared)


def replace_source(prepared, identifier, text):
    prepared[3] = {PAPER: dict(prepared[3][PAPER])}
    body = json.loads(prepared[4])
    if text is None:
        prepared[3][PAPER].pop(identifier)
        body["blocks"] = [block for block in body["blocks"] if block["block_id"] != identifier]
    else:
        prepared[3][PAPER][identifier] = text
        for block in body["blocks"]:
            if block["block_id"] == identifier:
                block["text"] = text
    prepared[4] = json.dumps(body, ensure_ascii=False)


def test_optional_neighbor_requires_every_source_and_keeps_core_request_unchanged(tmp_path):
    current, prepared = prepared_case(tmp_path)
    replace_source(prepared, "b1", None)
    before = json.loads(prepared[4])
    target = copy.deepcopy(before["reading_context"][0])
    target.pop("neighbor_paragraphs")
    result = _prune_note_context(current, PAPER, prepared)
    after = json.loads(result[4])
    selected = copy.deepcopy(after["reading_context"][0])
    selected.pop("neighbor_paragraphs")
    assert selected == target
    assert after["blocks"] == before["blocks"] and result[3] == prepared[3]
    assert {i["item_id"] for i in after["reading_context"][0]["neighbor_paragraphs"]} == {"p3"}
    assert "p1" not in {i["item_id"] for i in after.get("scope_reading_context", [])}
    assert "b2" in result[3][PAPER]


def test_partial_block_excerpt_cannot_support_a_complete_neighbor(tmp_path):
    current, prepared = prepared_case(tmp_path)
    replace_source(prepared, "b1", current.document.blocks[0].text[:20])
    result = _prune_note_context(current, PAPER, prepared)
    assert "p1" not in {i["item_id"] for i in json.loads(result[4])["reading_context"][0]["neighbor_paragraphs"]}


def test_complete_selected_interval_can_use_a_partial_block_excerpt(tmp_path):
    current, prepared = prepared_case(tmp_path)
    item = next(i for i in current.reading.items if i.id == "p1")
    item.parts = [ReadingPart(block_id="b1", start=9, end=20)]
    replace_source(prepared, "b1", current.document.blocks[0].text[9:20])
    result = _prune_note_context(current, PAPER, prepared)
    assert "p1" in {i["item_id"] for i in json.loads(result[4])["reading_context"][0]["neighbor_paragraphs"]}


def test_utf16_split_and_missing_second_source_are_not_retained(tmp_path):
    current, prepared = prepared_case(tmp_path)
    item = next(i for i in current.reading.items if i.id == "p1")
    item.parts.append(ReadingPart(block_id="not-supplied", end=10))
    result = _prune_note_context(current, PAPER, prepared)
    assert "p1" not in {i["item_id"] for i in json.loads(result[4])["reading_context"][0]["neighbor_paragraphs"]}
    current.document.blocks[0].text = "😀 Original text with a surrogate pair."
    item.parts = [ReadingPart(block_id="b1", start=1, end=8)]
    result = _prune_note_context(current, PAPER, prepared)
    assert "p1" not in {i["item_id"] for i in json.loads(result[4])["reading_context"][0]["neighbor_paragraphs"]}
