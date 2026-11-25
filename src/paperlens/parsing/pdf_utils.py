"""PDF validation and displayed-page coordinates; no text parser or OCR."""
from pathlib import Path

from paperlens.errors import InputError


def validate_path(path: Path, max_pages: int) -> Path:
    path = Path(path).resolve()
    if max_pages < 1:
        raise InputError("最大页数必须大于零。")
    if not path.is_file():
        raise InputError("找不到输入 PDF 文件，请检查路径。")
    if path.stat().st_size > 150 * 1024 * 1024:
        raise InputError("PDF 超过 150 MB，请先压缩文件。")
    try:
        with path.open("rb") as stream:
            header = stream.read(1024)
    except OSError as exc:
        raise InputError("无法读取输入文件，请检查文件权限。") from exc
    if b"%PDF-" not in header:
        raise InputError("输入文件不是有效 PDF，或文件已经损坏。")
    return path

def _display_page(page):
    """pdfplumber defaults to MediaBox; browser/PDFium display the CropBox."""
    media, crop = page.mediabox, page.cropbox
    # pdfplumber normalizes boxes by axis orientation but leaves the 180/270
    # CropBox origin unreflected. Glyph coordinates already include rotation.
    if page.rotation in (180, 270):
        local = (crop[0] - media[0], crop[1] - media[1], crop[2] - media[0], crop[3] - media[1])
        x0, y0, x1, y1 = rotate_box(local, page.width, page.height, 180)
        crop = (x0 + media[0], y0 + media[1], x1 + media[0], y1 + media[1])
    bounds = (
        max(media[0], crop[0]),
        max(media[1], crop[1]),
        min(media[2], crop[2]),
        min(media[3], crop[3]),
    )
    if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
        raise ValueError("Empty displayed page")
    return page.crop(bounds, strict=False) if bounds != page.bbox else page


def rotate_box(
    box: tuple[float, float, float, float],
    width: float,
    height: float,
    rotation: int,
) -> tuple[float, float, float, float]:
    """Rotate a top-left box clockwise inside its current page extent."""
    x0, y0, x1, y1 = box
    if rotation == 90:
        return height - y1, x0, height - y0, x1
    if rotation == 180:
        return width - x1, height - y1, width - x0, height - y0
    if rotation == 270:
        return y0, width - x1, y1, width - x0
    return box
