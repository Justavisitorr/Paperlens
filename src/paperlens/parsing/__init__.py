"""PDF mechanics for AI reading. The only document parser is edition_native."""
from .pdf_utils import validate_path
from .previews import render_page_previews

__all__ = ["render_page_previews", "validate_path"]
