"""Offline reading-note persistence and immutable-anchor checks."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError as SchemaError
from test_conversation import make_report

from paperlens import server
from paperlens.config import Settings
from paperlens.errors import ValidationError
from paperlens.models import ReadingDocument, ReadingItem, ReadingPart, TableCell, TableGrid
from paperlens.notes import NoteService, anchor_text
from paperlens.storage import write_json

PAPER = "a" * 32
NOTE = "b" * 32


def report():
    result = make_report("论文", "c", ["完整原文段落。", "另一个原文段落。"])
    result.reading = ReadingDocument(
        version="3",
        source_version="edition1",
        items=[
            ReadingItem(
                id="p1",
                kind="paragraph",
                content="完整阅读段落。",
                parts=[
                    ReadingPart(block_id="b1", end=7),
                ],
            ),
            ReadingItem(id="p2", kind="paragraph", content="另一段。"),
        ],
    )
    return result


def note(**changes):
    return {"item_id": "p1", "kind": "note", "text": "我的笔记", **changes}


def test_notes_are_idempotent_persistent_editable_and_separate_from_annotations(tmp_path):
    current = report()
    annotations = tmp_path / "annotations" / "original.json"
    write_json(annotations, {"highlight": "untouched"})
    store = NoteService(tmp_path)
    first = store.save(PAPER, current, NOTE, note())
    store.save(PAPER, current, NOTE, note())
    assert len(store.list(PAPER, current)) == 1
    reloaded = NoteService(tmp_path)
    edited = reloaded.save(PAPER, current, NOTE, note(text="修改后的笔记"))
    assert edited["created_at"] == first["created_at"]
    assert edited["anchor_text"] == "完整阅读段落。"
    assert edited["text"] == "修改后的笔记"
    assert not edited["detached"]
    assert list((tmp_path / "notes" / f"{PAPER}-{current.document.sha256}").glob("*.json"))
    assert annotations.read_text(encoding="utf-8").find("untouched") >= 0
    with pytest.raises(ValidationError):
        store.save(PAPER, current, NOTE, note(item_id="p2"))
    assert store.delete(PAPER, current, NOTE)
    assert not store.delete(PAPER, current, NOTE)
    assert not store.list(PAPER, current)


def test_missing_or_changed_anchor_stays_visible_and_edit_does_not_reanchor(tmp_path):
    current = report()
    store = NoteService(tmp_path)
    original = store.save(PAPER, current, NOTE, note())
    current.reading.items[0].content = "新版不同正文。"
    current.reading.source_version = "edition2"
    [detached] = store.list(PAPER, current)
    assert detached["detached"] and detached["anchor_text"] == original["anchor_text"]
    edited = store.save(PAPER, current, NOTE, note(text="仍然能改笔记"))
    assert edited["detached"] and edited["source_version"] == "edition1"
    current.reading.items = []
    assert store.list(PAPER, current)[0]["text"] == "仍然能改笔记"
    assert store.delete(PAPER, current, NOTE)


def test_source_version_change_keeps_note_visible_but_detached(tmp_path):
    current = report()
    store = NoteService(tmp_path)
    original = store.save(PAPER, current, NOTE, note())
    current.reading.source_version = "new-source"
    [saved] = store.list(PAPER, current)
    assert saved["detached"] and saved["anchor_text"] == original["anchor_text"]
    assert store.save(PAPER, current, NOTE, note(text="可继续编辑"))["source_version"] == "edition1"


def test_reading_marks_persist_without_fabricating_source_offsets(tmp_path):
    current = report()
    store = NoteService(tmp_path)
    mark = {
        "surface": "translation",
        "text": "完整😀阅读段落。",
        "start": 2,
        "end": 6,
        "color": "blue",
        "style": "underline",
    }
    saved = store.save(PAPER, current, NOTE, note(text="", quote="😀阅读", mark=mark))
    assert saved["anchor_text"] == "完整阅读段落。"
    assert NoteService(tmp_path).list(PAPER, current)[0]["mark"] == mark
    edited = store.save(PAPER, current, NOTE, note(quote="😀阅读", mark={**mark, "color": "pink"}))
    assert edited["mark"]["color"] == "pink"
    with pytest.raises((SchemaError, ValidationError)):
        store.save(PAPER, current, "d" * 32, note(quote="不同内容", mark=mark))
    current.reading.items[0].content = "新段落"
    assert store.list(PAPER, current)[0]["detached"]


def test_transcription_anchor_matches_reading_text_without_dollar_wrappers(tmp_path):
    from paperlens.models import FormulaRecord, ReadingTranscription, TranscriptionSegment

    current = report()
    item = current.reading.items[0]
    item.content = None
    item.transcription = ReadingTranscription(
        source="vision",
        segments=[
            TranscriptionSegment(text="公式 "),
            TranscriptionSegment(formula=FormulaRecord(latex=r"x^2+\alpha")),
            TranscriptionSegment(text=" 结尾。"),
        ],
    )
    saved = NoteService(tmp_path).save(PAPER, current, NOTE, note())
    assert saved["anchor_text"] == r"公式 x^2+\alpha 结尾。"
    assert not saved["detached"]


def test_parent_thread_is_checked_and_delete_only_cascades_linked_children(tmp_path):
    current = report()
    store = NoteService(tmp_path)
    store.save(PAPER, current, NOTE, note(kind="question"))
    child, unrelated = "d" * 32, "e" * 32
    saved = store.save(
        PAPER,
        current,
        child,
        note(
            kind="ai",
            parent_id=NOTE,
            reply={"answer": "客户端保存的完整回复"},
        ),
    )
    assert saved["reply"]["answer"] == "客户端保存的完整回复"
    store.save(PAPER, current, unrelated, note(kind="ai"))
    for invalid in ["f" * 32, child]:
        with pytest.raises(ValidationError):
            store.save(PAPER, current, child, note(parent_id=invalid))
    with pytest.raises(ValidationError):
        store.save(PAPER, current, "f" * 32, note(item_id="p2", parent_id=NOTE))
    assert store.delete(PAPER, current, NOTE)
    assert [r["id"] for r in store.list(PAPER, current)] == [unrelated]


def test_formula_grid_references_information_and_unicode_source_parts(tmp_path):
    current = report()
    current.document.blocks[0].text = "x😀数学终点"
    source = ReadingItem(
        id="source",
        kind="paragraph",
        parts=[
            ReadingPart(block_id="b1", start=1, end=5),
        ],
    )
    formula = ReadingItem(id="math", kind="formula", content=r"\sum_{i=1}^n x_i^2")
    table = ReadingItem(
        id="grid",
        kind="table",
        content="caption",
        table=TableGrid(
            rows=1,
            cols=1,
            cells=[TableCell(text="123", row_start=0, row_end=1, col_start=0, col_end=1)],
        ),
    )
    current.reading.items = [source, formula, table]
    current.reading.references = [ReadingItem(id="ref", kind="reference", content="Reference")]
    current.reading.information_items = {
        "authors": [ReadingItem(id="author", kind="paragraph", content="作者")]
    }
    assert anchor_text(current, source) == "😀数学"
    store = NoteService(tmp_path)
    for index, item in enumerate(
        [
            source,
            formula,
            table,
            *current.reading.references,
            *current.reading.information_items["authors"],
        ]
    ):
        saved = store.save(PAPER, current, f"{index:032x}", note(item_id=item.id))
        assert saved["anchor_text"] == anchor_text(current, item)
    table.table.cells[0].text = "456"
    assert next(r for r in store.list(PAPER, current) if r["item_id"] == "grid")["detached"]


def test_bad_ids_fields_and_paths_rejected(tmp_path):
    store = NoteService(tmp_path)
    for identifier in ["../escape", "f" * 31, "F" * 32]:
        with pytest.raises(ValidationError):
            store.save(PAPER, report(), identifier, note())
    for invalid in [{"source_version": "invented"}, {"text": "x" * 12001}, {"kind": "madeup"}]:
        with pytest.raises(SchemaError):
            store.save(PAPER, report(), NOTE, note(**invalid))


def test_table_anchor_matches_browser_semantic_json_and_ignores_geometry(tmp_path):
    from paperlens.models import Box

    current = report()
    item = current.reading.items[0]
    item.kind = "table"
    item.content = "Caption"
    cell = TableCell(
        text="123",
        row_start=0,
        row_end=1,
        col_start=0,
        col_end=1,
        bbox=Box(x0=0.0, y0=1e-7, x1=1.0, y1=0.5),
    )
    item.table = TableGrid(rows=1, cols=1, cells=[cell])
    expected = 'Caption\n{"rows":1,"cols":1,"cells":[{"text":"123","row_start":0,"row_end":1,"col_start":0,"col_end":1,"column_header":false}]}'
    store = NoteService(tmp_path)
    saved = store.save(PAPER, current, NOTE, note())
    assert saved["anchor_text"] == expected
    cell.bbox = Box(x0=0.25, y0=0.3, x1=0.9, y1=0.8)
    assert anchor_text(current, item) == expected
    assert not store.list(PAPER, current)[0]["detached"]
    cell.col_end = 2
    assert store.list(PAPER, current)[0]["detached"]


def test_shortened_original_block_does_not_hide_old_note(tmp_path):
    current = report()
    current.reading.items[0].content = None
    store = NoteService(tmp_path)
    original = store.save(PAPER, current, NOTE, note())
    current.document.blocks[0].text = "短"
    [saved] = store.list(PAPER, current)
    assert saved["detached"] and saved["anchor_text"] == original["anchor_text"]
    assert store.save(PAPER, current, NOTE, note(text="还可以编辑"))["detached"]


def test_note_http_routes_restart_and_features(tmp_path):
    config = Settings(
        _env_file=None,
        paperlens_output_dir=tmp_path / "outputs",
        paperlens_cache_dir=tmp_path / "cache",
    )
    current = report()
    current.run.id = PAPER
    write_json(
        config.paperlens_output_dir / "sample" / "report.json", current.model_dump(mode="json")
    )
    with TestClient(server.create_app(config)) as client:
        assert "notes" in client.get("/api/health").json()["features"]
        address = f"/api/library/{PAPER}/notes/{NOTE}"
        assert client.put(address, json=note()).status_code == 200
        assert len(client.get(f"/api/library/{PAPER}/notes").json()["notes"]) == 1
        assert client.put(address, json=note(extra="no")).status_code == 422
        assert client.get(f"/api/library/{'f' * 32}/notes").status_code == 404
    with TestClient(server.create_app(config)) as client:
        assert client.get(f"/api/library/{PAPER}/notes").json()["notes"][0]["id"] == NOTE
        assert client.delete(address).json() == {"deleted": True}
        assert client.delete(address).status_code == 404
