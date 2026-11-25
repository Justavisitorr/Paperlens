"""Reading-item notes, separate from immutable source-block annotations.

Replies may be client-saved or generated with source checks by AutoNoteService.
Saved text remains editable, not certified model output. Anchors are captured
by the server once and kept when a reading edition changes.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic import ValidationError as SchemaError

from .errors import ValidationError
from .models import DashboardReport, ReadingItem
from .reader import _utf16_index
from .storage import write_json

_ID = re.compile(r"[0-9a-f]{32}")
_SHA = re.compile(r"[0-9a-f]{64}")
NOTE_BYTES = 2_000_000


def reading_items(report: DashboardReport) -> dict[str, ReadingItem]:
    if report.reading is None:
        return {}
    return {
        item.id: item
        for item in [
            *report.reading.items,
            *report.reading.references,
            *(i for group in report.reading.information_items.values() for i in group),
        ]
    }


def item_content(report: DashboardReport, item: ReadingItem) -> dict[str, Any]:
    """Complete visible text/TeX and table cells, without images or MathML."""
    if item.content is not None:
        text = item.content
    elif item.transcription is not None:
        text = "".join(
            s.formula.latex if s.formula and s.formula.latex else s.text
            for s in item.transcription.segments
        )
    else:
        blocks = {b.id: b for b in report.document.blocks}
        text = "".join(
            part.join
            + blocks[part.block_id].text[
                _utf16_index(blocks[part.block_id].text, part.start) : _utf16_index(
                    blocks[part.block_id].text, part.end
                )
            ]
            for part in item.parts
            if part.block_id in blocks
        )
    content = {"kind": item.kind, "content": text, "label": item.label, "page": item.page}
    if item.table is not None:
        content["table"] = item.table.model_dump(mode="json")
    return content


def anchor_text(report: DashboardReport, item: ReadingItem) -> str:
    content = item_content(report, item)
    # Text remains convenient to inspect/copy; grids also affect anchor identity.
    text = content["content"]
    if item.table is not None:
        # Match the browser's field order; geometry and float spelling are not text anchors.
        table = {
            "rows": item.table.rows,
            "cols": item.table.cols,
            "cells": [
                {
                    "text": cell.text,
                    "row_start": cell.row_start,
                    "row_end": cell.row_end,
                    "col_start": cell.col_start,
                    "col_end": cell.col_end,
                    "column_header": cell.column_header,
                }
                for cell in item.table.cells
            ],
        }
        text += "\n" + json.dumps(table, ensure_ascii=False, separators=(",", ":"))
    return text


class ReadingMark(BaseModel):
    """Selection in a displayed paragraph, not PDF/source-block coordinates."""

    model_config = ConfigDict(extra="forbid")
    surface: Literal["original", "translation"]
    text: str = Field(min_length=1, max_length=50000)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    color: Literal["yellow", "green", "blue", "pink"] = "yellow"
    style: Literal["highlight", "underline"] = "highlight"


class NoteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str = Field(min_length=1, max_length=200)
    kind: Literal["note", "question", "ai", "check"]
    text: str = Field(max_length=12000)
    quote: str = Field(default="", max_length=4000)
    parent_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    question: str = Field(default="", max_length=4000)
    reply: dict | None = None
    mark: ReadingMark | None = None
    auto_types: list[Literal["summary", "explain", "connections", "questions", "check"]] = Field(
        default_factory=list, max_length=5
    )

    @model_validator(mode="after")
    def validate_mark(self):
        if self.mark:
            mark = self.mark
            if self.kind != "note" or mark.end <= mark.start:
                raise ValueError("勾画必须属于个人笔记且选区有效。")
            start, end = _utf16_index(mark.text, mark.start), _utf16_index(mark.text, mark.end)
            if not self.quote or mark.text[start:end] != self.quote:
                raise ValueError("勾画内容与所选文字不一致。")
        return self


class ReadingNote(NoteInput):
    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    created_at: str
    updated_at: str
    source_version: str
    anchor_text: str
    page: int
    detached: bool = False


class NoteService:
    def __init__(self, storage_root: Path) -> None:
        self.root = (Path(storage_root) / "notes").resolve()
        self._lock = RLock()

    def _directory(self, paper_id: str, report: DashboardReport) -> Path:
        if not _ID.fullmatch(paper_id) or not _SHA.fullmatch(report.document.sha256):
            raise ValidationError("笔记所属论文编号无效。")
        directory = self.root / f"{paper_id}-{report.document.sha256}"
        if not directory.resolve().is_relative_to(self.root):
            raise ValidationError("笔记存储目录无效。")
        return directory

    def _path(self, paper_id, report, note_id):
        if not _ID.fullmatch(note_id):
            raise ValidationError("笔记编号无效。")
        path = self._directory(paper_id, report) / f"{note_id}.json"
        if path.is_symlink():
            raise ValidationError("笔记存储文件无效。")
        return path

    def _read(self, path: Path) -> dict | None:
        if path.is_symlink() or not _ID.fullmatch(path.stem):
            return None
        try:
            record = ReadingNote.model_validate_json(path.read_text(encoding="utf-8"))
            return record.model_dump() if record.id == path.stem else None
        except (OSError, ValueError, SchemaError):
            return None

    def _visible(self, record, report):
        item = reading_items(report).get(record["item_id"])
        try:
            detached = (
                report.reading is None
                or report.reading.source_version != record["source_version"]
                or item is None
                or anchor_text(report, item) != record["anchor_text"]
            )
        except ValidationError:
            # An old projection can point beyond newly shortened source text.
            detached = True
        return {**record, "detached": detached}

    def list(self, paper_id: str, report: DashboardReport) -> list[dict]:
        with self._lock:
            records = (
                self._read(path) for path in self._directory(paper_id, report).glob("*.json")
            )
            return sorted(
                (self._visible(r, report) for r in records if r),
                key=lambda r: (r["created_at"], r["id"]),
            )

    def save(self, paper_id: str, report: DashboardReport, note_id: str, payload: dict) -> dict:
        incoming = NoteInput.model_validate(payload).model_dump()
        with self._lock:
            path = self._path(paper_id, report, note_id)
            existing = self._read(path) if path.is_file() else None
            if path.is_file() and existing is None:
                raise ValidationError("现有笔记无法读取，请检查文件后重试。")
            if existing and (
                incoming["item_id"] != existing["item_id"]
                or incoming["parent_id"] != existing["parent_id"]
            ):
                raise ValidationError("已有笔记不能改绑段落或所属问题，请新建笔记。")
            if incoming["parent_id"]:
                parent = self._read(self._path(paper_id, report, incoming["parent_id"]))
                if (
                    incoming["parent_id"] == note_id
                    or parent is None
                    or parent["item_id"] != incoming["item_id"]
                ):
                    raise ValidationError("回复必须关联同一段落中已经保存的笔记。")
            now = datetime.now(UTC).isoformat()
            if existing is None:
                item = reading_items(report).get(incoming["item_id"])
                if item is None or report.reading is None:
                    raise ValidationError("找不到该阅读段落，请刷新后重试。")
                existing = {
                    "id": note_id,
                    "created_at": now,
                    "source_version": report.reading.source_version,
                    "anchor_text": anchor_text(report, item),
                    "page": item.page,
                }
            record = ReadingNote.model_validate(
                {**existing, **incoming, "updated_at": now, "detached": False}
            ).model_dump()
            if len(json.dumps(record, ensure_ascii=False).encode("utf-8")) > NOTE_BYTES:
                raise ValidationError("这条笔记太长，无法保存，请缩短回答。")
            write_json(path, record)
            return self._visible(record, report)

    def delete(self, paper_id: str, report: DashboardReport, note_id: str) -> bool:
        with self._lock:
            path = self._path(paper_id, report, note_id)
            if not path.is_file():
                return False
            records = self.list(paper_id, report)
            removed = {note_id}
            while children := {r["id"] for r in records if r["parent_id"] in removed} - removed:
                removed.update(children)
            for identifier in removed:
                self._path(paper_id, report, identifier).unlink(missing_ok=True)
            return True
