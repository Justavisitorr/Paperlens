"""Read printed labels from the page itself; never infer a journal offset."""

from __future__ import annotations

import re


def printed_page_label(words: list[dict], width: float, height: float) -> str:
    candidates: list[tuple[int, str]] = []
    for word in words:
        if word["bottom"] > height * 0.09 and word["top"] < height * 0.90:
            continue
        text = word["text"].strip()
        decorated = re.fullmatch(r"[·•.\-–—]?\s*(\d{1,5}|[ivxlcdm]{1,10})\s*[·•.\-–—]?", text, re.I)
        if decorated is None:
            continue
        number = decorated.group(1)
        if text != number and text[0] in "·•.-–—" and text[-1] in "·•.-–—":
            candidates.append((3, number))
            continue
        # A bare numeral is a page label only when it stands on its own line.
        # This excludes years, volume/issue numbers and article identifiers.
        neighbors = [
            other
            for other in words
            if other is not word
            and abs(other["top"] - word["top"]) < max(3, word["bottom"] - word["top"])
        ]
        # Scientific journals often paint the two middle dots separately.
        # Require decoration on both sides close to the numeral; volume/issue
        # and year numbers share the header but do not satisfy this pattern.
        left_marks = [
            other
            for other in neighbors
            if re.fullmatch(r"[·•.\-–—]", other["text"].strip())
            and 0 <= word["x0"] - other["x1"] < max(12, word["bottom"] - word["top"])
        ]
        right_marks = [
            other
            for other in neighbors
            if re.fullmatch(r"[·•.\-–—]", other["text"].strip())
            and 0 <= other["x0"] - word["x1"] < max(12, word["bottom"] - word["top"])
        ]
        if left_marks and right_marks:
            candidates.append((3, number))
            continue
        if not neighbors:
            candidates.append((2, number))
        elif all(
            min(abs(other["x0"] - word["x1"]), abs(word["x0"] - other["x1"])) > width * 0.12
            for other in neighbors
        ) and (word["x0"] < width * 0.15 or word["x1"] > width * 0.85):
            candidates.append((1, number))
    if not candidates:
        return ""
    best = max(score for score, _ in candidates)
    labels = {label for score, label in candidates if score == best}
    return labels.pop() if len(labels) == 1 else ""
