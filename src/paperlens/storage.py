"""Atomic, version-keyed stage storage; interrupted writes never become cache hits."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any


def fingerprint(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                # Windows readers/virus scanners can briefly hold the destination.
                if attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


class StageStore:
    def __init__(self, root: Path, key: str) -> None:
        self.root = root / key
        self.root.mkdir(parents=True, exist_ok=True)
        self.hits = 0

    def load(self, stage: str) -> dict | None:
        path = self.root / f"{stage}.json"
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(value, dict):
            return None
        self.hits += 1
        return value

    def save(self, stage: str, value: dict) -> None:
        write_json(self.root / f"{stage}.json", value)
