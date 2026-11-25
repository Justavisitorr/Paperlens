"""Derived, account-local binary resources for lightweight web report responses.

Stored reports and offline exports retain their embedded data. Only known image
slots are replaced; prose containing a data URL remains original source text.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .errors import InputError
from .models import DashboardReport, SourcePDF

ASSET_ID = re.compile(r"^[a-f0-9]{64}$")
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


def embedded_bytes(value: str, allowed_types: set[str]) -> tuple[str, bytes] | None:
    if not isinstance(value, str) or not value.startswith("data:"):
        return None
    header, separator, encoded = value.partition(",")
    if not separator or not header.endswith(";base64"):
        return None
    media_type = header[5:-7].lower()
    if media_type not in allowed_types:
        return None
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return None
    return (media_type, data) if data else None


def safe_pdf_path(report_path: Path) -> Path | None:
    source = report_path.parent / "source.pdf"
    if (source.is_file() and not source.is_symlink()
            and source.resolve().is_relative_to(report_path.parent.resolve())):
        return source
    return None


@dataclass(frozen=True)
class ReportResource:
    path: Path
    media_type: str
    size: int


@dataclass
class WebReport:
    payload: dict
    images: dict[str, ReportResource]
    pdf: ReportResource | None


@dataclass(frozen=True)
class PDFChoice:
    resource: ReportResource | None
    warning: str = ""


def _cache_directory(report_path: Path) -> Path:
    directory = report_path.parent / ".web-assets"
    if directory.is_symlink() or directory.resolve() != report_path.parent.resolve() / ".web-assets":
        raise InputError("论文资源缓存路径无效。")
    directory.mkdir(exist_ok=True)
    return directory


def _materialize(directory: Path, media_type: str, data: bytes) -> tuple[str, ReportResource]:
    ident = hashlib.sha256(media_type.encode("ascii") + b"\0" + data).hexdigest()
    destination = directory / ident
    if destination.is_symlink() or destination.resolve().parent != directory.resolve():
        raise InputError("论文资源缓存路径无效。")
    # Content-addressed names are shared by repeated uses within one report.
    if (not destination.is_file() or destination.stat().st_size != len(data)
            or destination.read_bytes() != data):
        temporary = directory / (".write-" + uuid.uuid4().hex)
        try:
            temporary.write_bytes(data)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    return ident, ReportResource(destination, media_type, len(data))


def select_pdf(report_path: Path, expected_sha: str,
               load_report: Callable[[], DashboardReport]) -> PDFChoice:
    """Choose verified source bytes once; never join ranges from two PDFs."""
    original = safe_pdf_path(report_path)
    if original:
        before = original.stat()
        digest = hashlib.sha256()
        with original.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        after = original.stat()
        if (digest.hexdigest() == expected_sha
                and (before.st_ino, before.st_mtime_ns, before.st_size)
                == (after.st_ino, after.st_mtime_ns, after.st_size)):
            return PDFChoice(ReportResource(original, "application/pdf", after.st_size))
    report = load_report()
    if report.source_pdf:
        decoded = embedded_bytes(report.source_pdf.data_url, {"application/pdf"})
        if (decoded and decoded[1].startswith(b"%PDF-")
                and hashlib.sha256(decoded[1]).hexdigest() == expected_sha):
            _, resource = _materialize(_cache_directory(report_path), *decoded)
            return PDFChoice(resource, "保存的 PDF 与报告来源不一致，当前使用报告内的原 PDF。" if original else "")
    warning = ("原 PDF 未通过来源校验，暂不能显示或嵌入；正文仍可阅读。"
               if original or report.source_pdf else "此旧报告没有保存原 PDF，正文仍可阅读。")
    return PDFChoice(None, warning)


def hydrate_export(report: DashboardReport, choice: PDFChoice) -> DashboardReport:
    """Restore the verified original PDF on the in-memory export copy only."""
    if choice.resource:
        report.source_pdf = SourcePDF(
            data_url="data:application/pdf;base64," + base64.b64encode(choice.resource.path.read_bytes()).decode(),
            sha256=report.document.sha256,
        )
    else:
        report.source_pdf = None
    if choice.warning and choice.warning not in report.quality.warnings:
        report.quality.warnings.append(choice.warning)
    return report


def web_report(report: DashboardReport, paper_id: str, report_path: Path, choice: PDFChoice) -> WebReport:
    """Build a derived projection once; no original report is changed or saved."""
    payload = report.model_dump(mode="json")
    images: dict[str, ReportResource] = {}
    directory: Path | None = None

    def materialize(media_type: str, data: bytes) -> tuple[str, ReportResource]:
        nonlocal directory
        if directory is None:
            directory = _cache_directory(report_path)
        return _materialize(directory, media_type, data)

    def image(value: str) -> str:
        decoded = embedded_bytes(value, IMAGE_TYPES)
        if not decoded:
            return ""
        ident, resource = materialize(*decoded)
        images[ident] = resource
        return f"/api/library/{paper_id}/assets/{ident}"

    for preview in payload["previews"]:
        preview["data_url"] = image(preview["data_url"])
    reading = payload.get("reading")
    if reading:
        groups = [reading["items"], reading["references"], *reading["information_items"].values()]
        for group in groups:
            for item in group:
                item["asset"] = image(item["asset"]) if item["asset"] else ""

    pdf = choice.resource
    if pdf:
        payload["source_pdf"] = {
            "data_url": f"/api/library/{paper_id}/pdf",
            "sha256": report.document.sha256,
            "byte_size": pdf.size,
        }
    else:
        payload["source_pdf"] = None
    if choice.warning and choice.warning not in payload["quality"]["warnings"]:
        payload["quality"]["warnings"].append(choice.warning)

    # Keep only this report's current derived binary files. Unknown files and
    # links are never removed. The original report and source PDF are untouched.
    if directory is None and (report_path.parent / ".web-assets").exists():
        directory = _cache_directory(report_path)
    if directory:
        retained = {resource.path.name for resource in images.values()}
        if pdf and pdf.path.parent == directory:
            retained.add(pdf.path.name)
        for old in directory.iterdir():
            if (ASSET_ID.fullmatch(old.name) and old.name not in retained
                    and old.is_file() and not old.is_symlink()
                    and old.resolve().parent == directory.resolve()):
                try:
                    old.unlink()
                except OSError:
                    # An active Windows file response may still hold this file.
                    pass
    return WebReport(payload, images, pdf)
