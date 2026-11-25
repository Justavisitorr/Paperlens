"""Model input projection; original reading/evidence/geometry remain intact."""

from .notes import item_content


def prompt_item_content(report, item):
    content = item_content(report, item)
    if item.table is not None:
        # Compact tuples preserve empty cells, merged spans, headers and exact
        # values. Bounding boxes and repeated JSON field names serve only the UI.
        content["table"] = {
            "rows": item.table.rows, "cols": item.table.cols,
            "cell_fields": ["row_start", "row_end", "col_start", "col_end", "text", "column_header"],
            "cells": [[c.row_start, c.row_end, c.col_start, c.col_end, c.text, c.column_header]
                      for c in item.table.cells],
        }
    return content
