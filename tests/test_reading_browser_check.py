"""Browser acceptance checks must distinguish semantic figures from chart pieces."""

from copy import deepcopy

import pytest

from tools.check_reading import assert_chart_ownership, metadata_parts


def charts():
    blocks = {}
    items = []
    for number in range(1, 7):
        parts = []
        for index in range(1 if number == 1 else 2):
            bid = f"fig-{number}-{index}"
            blocks[bid] = {
                "kind": "picture",
                "bbox": {"x0": 0.1 + index * 0.4, "y0": 0.1, "x1": 0.4 + index * 0.4, "y1": 0.4},
            }
            parts.append({"block_id": bid})
        items.append(
            {
                "id": f"fig-{number}",
                "kind": "figure",
                "label": f"Fig. {number} original caption",
                "page": 4 if number == 1 else 5 if number <= 4 else 6,
                "parts": parts,
                "asset": "data:image/webp;base64,asset",
                "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.8, "y1": 0.5},
            }
        )
    return {"items": items}, blocks


def test_grouped_chart_check_retains_later_figures():
    reading, blocks = charts()
    assert_chart_ownership(reading, blocks)
    assert len(reading["items"]) == 6


@pytest.mark.parametrize("damage", ["split", "lost", "crop", "duplicate-label"])
def test_grouped_chart_check_rejects_incomplete_semantic_objects(damage):
    reading, blocks = charts()
    target = reading["items"][1]
    if damage == "split":
        reading["items"].append(deepcopy(target))
    elif damage == "lost":
        target["parts"].pop()
    elif damage == "crop":
        target["bbox"]["x1"] = 0.4
    else:
        target["label"] = "Fig. 1 duplicate"
    with pytest.raises(AssertionError):
        assert_chart_ownership(reading, blocks)


def test_metadata_slices_use_explicit_empty_records_and_utf16_legacy_fallback():
    blocks = {"mixed": {"text": "基金😀正文"}, "author": {"text": "A😀B"}}
    exact = [{"block_id": "mixed", "start": 0, "end": 4, "bbox": {"x0": 0.1}}]
    reading = {
        "information": {"declarations": ["mixed"], "authors": ["author"], "titles": ["mixed"]},
        "information_parts": {"declarations": exact, "titles": [], "footnotes": exact},
    }
    result = metadata_parts(reading, blocks)
    assert result["declarations"] == exact
    assert result["footnotes"] == exact
    assert result["titles"] == []
    assert result["authors"] == [{"block_id": "author", "start": 0, "end": 4}]
