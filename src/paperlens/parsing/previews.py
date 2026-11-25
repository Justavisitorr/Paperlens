"""Offline evidence previews with the same displayed-page coordinates."""

from __future__ import annotations

import base64
import io
from pathlib import Path

import pypdfium2 as pdfium

from paperlens.errors import InputError
from paperlens.models import PagePreview
from paperlens.parsing.pdf_utils import validate_path


def render_page_previews(
    path: Path,
    page_numbers: list[int],
    max_width: int = 1000,
) -> list[PagePreview]:
    """Render physical pages, deduplicated in request order, into JPEG data URLs.

    PDFium applies intrinsic page rotation and CropBox. No network resource,
    filesystem URL or temporary image is referenced in the exported payload.
    """
    path = validate_path(path, 1)
    if not 100 <= max_width <= 4000:
        raise InputError("预览宽度必须在 100–4000 像素之间。")
    requested = list(dict.fromkeys(page_numbers))
    if any(
        not isinstance(number, int) or isinstance(number, bool) or number < 1
        for number in requested
    ):
        raise InputError("预览页码必须为从 1 开始的整数。")
    if not requested:
        return []
    try:
        pdf = pdfium.PdfDocument(path)
    except Exception as exc:
        raise InputError("无法打开 PDF 以生成证据预览。") from exc
    previews: list[PagePreview] = []
    with pdf:
        if any(number > len(pdf) for number in requested):
            raise InputError("预览页码超出 PDF 实际页数。")
        for number in requested:
            page = None
            bitmap = None
            try:
                page = pdf[number - 1]
                width, height = page.get_size()
                if width <= 0 or height <= 0:
                    raise ValueError("Invalid page extent")
                # Both dimensions are bounded for adversarial page sizes.
                scale = min(max_width / width, 6000 / height)
                bitmap = page.render(scale=scale)
                with bitmap.to_pil() as image:
                    rgb = image.convert("RGB")
                    try:
                        output = io.BytesIO()
                        rgb.save(output, format="JPEG", quality=84, optimize=True)
                        previews.append(
                            PagePreview(
                                page=number,
                                width=rgb.width,
                                height=rgb.height,
                                data_url="data:image/jpeg;base64,"
                                + base64.b64encode(output.getvalue()).decode("ascii"),
                            )
                        )
                    finally:
                        rgb.close()
            except Exception as exc:
                raise InputError(
                    f"第 {number} 页的证据预览生成失败（{type(exc).__name__}）。"
                ) from exc
            finally:
                if bitmap is not None:
                    bitmap.close()
                if page is not None:
                    page.close()
    return previews
