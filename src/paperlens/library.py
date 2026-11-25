"""Persistent metadata over validated local reports; no browser-local library state."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path

from .bibliography import bibliography
from .control import PDF_LOCK
from .errors import InputError, PaperLensError
from .models import DashboardReport
from .reading import prepare_reading
from .recovery import attach_failures
from .report_resources import (
    ASSET_ID,
    PDFChoice,
    ReportResource,
    WebReport,
    hydrate_export,
    safe_pdf_path,
    select_pdf,
    web_report,
)
from .storage import write_json

PAPER_ID = re.compile(r"^[a-f0-9]{32}$")


class LibraryStore:
    def __init__(self, output_root: Path):
        self.root = output_root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.metadata_path = self.root / ".library.json"
        self._lock = threading.RLock()
        self._records: dict[str, dict] = {}
        self._files: dict[str, tuple[int, int, int, dict]] = {}
        self._last_scan = 0.0
        self._overrides: dict[str, dict] = {}
        self._reading_cache: OrderedDict[tuple, DashboardReport] = OrderedDict()
        # Only small projections and file metadata are held in memory; binary
        # resources stay in the report's own derived cache directory.
        self._web_cache: OrderedDict[tuple, WebReport] = OrderedDict()
        self._pdf_cache: OrderedDict[tuple, PDFChoice] = OrderedDict()
        if self.metadata_path.is_file():
            try:
                raw = json.loads(self.metadata_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._overrides = {
                        k: v
                        for k, v in raw.items()
                        if PAPER_ID.fullmatch(k) and isinstance(v, dict)
                    }
            except (OSError, ValueError):
                pass

    def _path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if path == self.root or not path.is_relative_to(self.root):
            raise InputError("文献路径无效。")
        return path

    def refresh(self, *, force: bool = False) -> None:
        with self._lock:
            if not force and time.monotonic() - self._last_scan < 3:
                return
            seen: set[str] = set()
            records: dict[str, dict] = {}
            for folder, dirs, files in os.walk(self.root, followlinks=False):
                dirs[:] = [
                    d for d in dirs if not d.startswith(".") and not (Path(folder) / d).is_symlink()
                ]
                if "report.json" not in files:
                    continue
                path = Path(folder) / "report.json"
                if path.is_symlink() or not path.resolve().is_relative_to(self.root):
                    continue
                relative = path.relative_to(self.root).as_posix()
                seen.add(relative)
                try:
                    stat = path.stat()
                    cached = self._files.get(relative)
                    if cached and cached[:3] == (stat.st_ino, stat.st_mtime_ns, stat.st_size):
                        record = cached[3]
                    else:
                        report = DashboardReport.model_validate_json(
                            path.read_text(encoding="utf-8")
                        )
                        if report.run.status not in {"complete", "partial"}:
                            self._files.pop(relative, None)
                            continue
                        ident = (
                            report.run.id
                            if PAPER_ID.fullmatch(report.run.id)
                            else hashlib.sha256(relative.encode()).hexdigest()[:32]
                        )
                        record = {
                            "id": ident,
                            **bibliography(report),
                            "source_name": report.document.source_name,
                            "created_at": report.run.created_at,
                            "pages": len(report.document.pages),
                            "paper_type": report.analysis.paper_type,
                            "status": report.run.status,
                            "model": report.run.model,
                            "node_count": len(report.analysis.nodes),
                            "evidence_count": len(report.evidence),
                            "document_sha256": report.document.sha256,
                            "relative_path": relative,
                        }
                        source_meta = path.parent / "source-metadata.json"
                        if source_meta.is_file():
                            try:
                                supplied = json.loads(source_meta.read_text(encoding="utf-8"))
                            except (OSError, ValueError):
                                supplied = {}
                            for field in (
                                "authors",
                                "year",
                                "venue",
                                "abstract",
                                "doi",
                                "source_url",
                            ):
                                if not record.get(field):
                                    record[field] = supplied.get(
                                        "url" if field == "source_url" else field, ""
                                    )
                        self._files[relative] = (stat.st_ino, stat.st_mtime_ns, stat.st_size, record)
                    previous = records.get(record["id"])
                    if previous is None or relative < previous["relative_path"]:
                        records[record["id"]] = record
                except (OSError, ValueError):
                    self._files.pop(relative, None)
                    continue
            for relative in set(self._files) - seen:
                self._files.pop(relative)
            self._records = records
            self._last_scan = time.monotonic()

    def list(self) -> dict:
        self.refresh()
        with self._lock:
            records = [
                r
                for r in self._records.values()
                if not self._overrides.get(r["id"], {}).get("deleted")
            ]
            # Keep distinct analysis runs visible. No result is silently replaced by a newer model.
            return {
                "storage_path": str(self.root),
                "papers": [
                    self._public(r)
                    for r in sorted(records, key=lambda r: r["created_at"], reverse=True)
                ],
            }

    def _public(self, record: dict) -> dict:
        result = {k: v for k, v in record.items() if k != "relative_path"}
        override = self._overrides.get(record["id"], {})
        if override.get("title"):
            result["title"] = override["title"]
        result.update(
            data_url=f"/api/library/{record['id']}/report",
            report_url=f"/api/library/{record['id']}/html",
            thumbnail_url=f"/api/library/{record['id']}/thumbnail",
            pdf_url=f"/api/library/{record['id']}/pdf",
            reading_status=override.get("reading_status", "unread"),
            starred=override.get("starred", False),
            last_opened=override.get("last_opened", ""),
        )
        return result

    def pdf_bytes(self, paper_id: str) -> bytes:
        return self.pdf_resource(paper_id).path.read_bytes()

    def pdf_resource(self, paper_id: str) -> ReportResource:
        resource = self._pdf_choice(paper_id).resource
        if resource is None:
            raise InputError("此报告没有可核验的原 PDF。")
        record = self.get_record(paper_id)
        original = safe_pdf_path(self._path(record["relative_path"]))
        if resource.path != original:
            self._validate_resource_path(paper_id, resource)
        return resource

    def _resource_key(self, paper_id: str) -> tuple[dict, Path, tuple]:
        record = self.get_record(paper_id)
        path = self._path(record["relative_path"])
        stamp = path.stat()
        known = self._files.get(record["relative_path"])
        if known is None or known[:3] != (stamp.st_ino, stamp.st_mtime_ns, stamp.st_size):
            # A retry/repair can replace a report inside the catalogue's scan
            # interval. Its source hash must be refreshed before serving bytes.
            self.refresh(force=True)
            record = self.get_record(paper_id)
            path = self._path(record["relative_path"])
            stamp = path.stat()
        source = safe_pdf_path(path)
        source_stat = source.stat() if source else None
        source_stamp = ((source_stat.st_ino, source_stat.st_mtime_ns, source_stat.st_size)
                        if source_stat else None)
        return record, path, (str(path), stamp.st_ino, stamp.st_mtime_ns, stamp.st_size, source_stamp)

    def _pdf_choice(self, paper_id: str, report: DashboardReport | None = None) -> PDFChoice:
        record, path, key = self._resource_key(paper_id)
        with self._lock:
            cached = self._pdf_cache.get(key)
            if cached and (cached.resource is None or cached.resource.path.is_file()):
                self._pdf_cache.move_to_end(key)
                return cached
            result = select_pdf(path, record["document_sha256"],
                                lambda: report if report is not None else self.report(paper_id))
            for old in list(self._pdf_cache):
                if old[0] == str(path):
                    del self._pdf_cache[old]
            self._pdf_cache[key] = result
            while len(self._pdf_cache) > 2:
                self._pdf_cache.popitem(last=False)
            return result

    def export_report(self, paper_id: str) -> DashboardReport:
        report = self.report(paper_id)
        return hydrate_export(report, self._pdf_choice(paper_id, report))

    def _web_report(self, paper_id: str, *, rebuild: bool = False) -> WebReport:
        _, path, key = self._resource_key(paper_id)
        with self._lock:
            cached = None if rebuild else self._web_cache.get(key)
            if cached:
                self._web_cache.move_to_end(key)
                return cached
            report = self.report(paper_id)
            result = web_report(report, paper_id, path, self._pdf_choice(paper_id, report))
            for old in list(self._web_cache):
                if old[0] == str(path):
                    del self._web_cache[old]
            self._web_cache[key] = result
            while len(self._web_cache) > 2:
                self._web_cache.popitem(last=False)
            return result

    def web_report(self, paper_id: str) -> dict:
        return self._web_report(paper_id).payload

    def image_resource(self, paper_id: str, asset_id: str) -> ReportResource:
        if not ASSET_ID.fullmatch(asset_id):
            raise InputError("找不到该论文资源。")
        resource = self._web_report(paper_id).images.get(asset_id)
        if resource is None:
            raise InputError("找不到该论文资源。")
        if not resource.path.is_file() or resource.path.stat().st_size != resource.size:
            # A derived cache may be cleared independently of the saved report.
            # Rebuild only on a miss; the normal image path never parses JSON.
            resource = self._web_report(paper_id, rebuild=True).images.get(asset_id)
            if resource is None:
                raise InputError("找不到该论文资源。")
        self._validate_resource_path(paper_id, resource)
        return resource

    def _validate_resource_path(self, paper_id: str, resource: ReportResource) -> None:
        report_directory = self._path(self.get_record(paper_id)["relative_path"]).parent
        expected_directory = report_directory / ".web-assets"
        if (resource.path.is_symlink() or not resource.path.is_file()
                or expected_directory.is_symlink()
                or expected_directory.resolve() != report_directory.resolve() / ".web-assets"
                or resource.path.resolve().parent != expected_directory.resolve()):
            raise InputError("论文资源缓存已损坏或移走，请刷新后重试。")

    def thumbnail(self, paper_id: str) -> bytes:
        """One small first-page image per immutable PDF, generated only on demand."""
        import pypdfium2 as pdfium

        record = self.get_record(paper_id)
        key = hashlib.sha256(record["document_sha256"].encode()).hexdigest()
        cache = self.root / ".reader" / "thumbnails" / f"{key}.webp"
        if cache.is_file():
            return cache.read_bytes()
        data = self.pdf_bytes(paper_id)
        with PDF_LOCK, pdfium.PdfDocument(data) as doc:
            page = doc[0]
            bitmap = page.render(scale=240 / page.get_width())
            try:
                with bitmap.to_pil() as image:
                    stream = io.BytesIO()
                    image.save(stream, format="WEBP", quality=80)
                    result = stream.getvalue()
            finally:
                bitmap.close()
                page.close()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(result)
        return result

    def get_record(self, paper_id: str) -> dict:
        if not PAPER_ID.fullmatch(paper_id):
            raise InputError("找不到该论文。")
        self.refresh()
        with self._lock:
            if paper_id not in self._records:
                self.refresh(force=True)
            record = self._records.get(paper_id)
            if record is None or self._overrides.get(paper_id, {}).get("deleted"):
                raise InputError("找不到该论文，或它已被删除。")
            return dict(record)

    def report(self, paper_id: str) -> DashboardReport:
        record = self.get_record(paper_id)
        try:
            path = self._path(record["relative_path"])
            stamp = path.stat()
            cache_key = (str(path), stamp.st_ino, stamp.st_mtime_ns, stamp.st_size)
            with self._lock:
                cached = self._reading_cache.get(cache_key)
                if cached:
                    self._reading_cache.move_to_end(cache_key)
                    return cached.model_copy(deep=True)
            report = DashboardReport.model_validate_json(path.read_text(encoding="utf-8"))
            attach_failures(report, path.parent)
        except (OSError, ValueError) as exc:
            raise InputError("论文报告已损坏或移走，请重新处理。") from exc
        if report.reading is None or report.reading.version not in {"2", "3"}:
            source = path.parent / "source.pdf"
            try:
                with PDF_LOCK:
                    prepare_reading(report, source if source.is_file() else None)
            except (OSError, ValueError, PaperLensError):
                report.quality.warnings.append("旧报告阅读布局未能更新，可继续查看原报告和原 PDF。")
            with self._lock:
                self._reading_cache[cache_key] = report.model_copy(deep=True)
                while len(self._reading_cache) > 2:
                    self._reading_cache.popitem(last=False)
        from .reading_references import repair_reference_boundaries
        from .reading_toc import filter_source_toc

        filter_source_toc(report)
        repair_reference_boundaries(report)
        return report

    def rename(self, paper_id: str, title: str) -> dict:
        return self.update(paper_id, {"title": title})

    def update(self, paper_id: str, values: dict) -> dict:
        self.get_record(paper_id)
        if not values or set(values) - {"title", "starred", "reading_status", "opened"}:
            raise InputError("请选择名称、星标或阅读状态。")
        values = dict(values)
        if "title" in values:
            title = values["title"]
            if (
                not isinstance(title, str)
                or not title.strip()
                or len(title) > 240
                or any(ord(c) < 32 for c in title)
            ):
                raise InputError("论文名称须为 1–240 个可显示字符。")
            values["title"] = title.strip()
        if "reading_status" in values and values["reading_status"] not in {
            "unread",
            "reading",
            "read",
        }:
            raise InputError("请选择未读、在读或已读。")
        if any(
            key in values and not isinstance(values[key], bool) for key in ("starred", "opened")
        ):
            raise InputError("星标和打开状态须为开关值。")
        if values.pop("opened", False):
            values["last_opened"] = datetime.now(UTC).isoformat()
        with self._lock:
            overrides = self._overrides | {paper_id: self._overrides.get(paper_id, {}) | values}
            write_json(self.metadata_path, overrides)
            self._overrides = overrides
            return self._public(self._records[paper_id])

    def delete(self, paper_id: str) -> dict:
        record = self.get_record(paper_id)
        with self._lock:
            # A run may have duplicate copies (initial delivery / rebuilt examples).
            # Delete only its selected generated result directory, not source PDFs elsewhere.
            directory = self._path(record["relative_path"]).parent
            if directory == self.root or not directory.is_relative_to(self.root):
                raise InputError("不能删除文献库根目录。")
            nested_reports = list(directory.rglob("report.json"))
            if any(p.parent != directory for p in nested_reports):
                raise InputError("此结果目录含有其他论文，不能整目录删除。")
            for folder, dirs, files in os.walk(directory, followlinks=False):
                for name in [*dirs, *files]:
                    entry = Path(folder) / name
                    if entry.is_symlink() or not entry.resolve().is_relative_to(directory):
                        raise InputError("结果目录包含外部链接，无法删除。")
            shutil.rmtree(directory)
            self._overrides.setdefault(paper_id, {})["deleted"] = True
            write_json(self.metadata_path, self._overrides)
            self._records.pop(paper_id, None)
            for key in list(self._web_cache):
                if key[0] == str(directory / "report.json"):
                    del self._web_cache[key]
            for key in list(self._pdf_cache):
                if key[0] == str(directory / "report.json"):
                    del self._pdf_cache[key]
            return {"deleted": True, "id": paper_id, "original_source_retained": True}
