from test_reading_projection import b, report

from paperlens.models import ReadingDocument, ReadingItem, ReadingPart
from paperlens.reading import prepare_reading, utf16
from paperlens.reading_references import clean_references, repair_reference_boundaries


def text(item, blocks):
    return "".join(
        p.join
        + blocks[p.block_id].text.encode("utf-16-le")[p.start * 2 : p.end * 2].decode("utf-16-le")
        for p in item.parts
    )


def test_only_proven_typographic_residue_is_removed_from_old_references():
    raw = b("r", "A. Novikov, N. V˜ u. Alphaevolve. 2025.", kind="reference")
    start, end = raw.text.index("˜"), raw.text.index("˜") + 3
    corrected = ReadingItem(id="edition-r", kind="reference", content="A. Novikov, N. Vũ. Alphaevolve. 2025.", parts=[
        ReadingPart(block_id="r", start=0, end=start),
        ReadingPart(block_id="r", start=end, end=utf16(raw.text)),
    ])
    residue = ReadingItem(id="source-residue", kind="reference", parts=[ReadingPart(block_id="r", start=start, end=end)])
    empty = ReadingItem(id="empty", kind="reference", content=" - \n ")
    label_only = ReadingItem(id="label-only", kind="reference", content="[17]")
    short = ReadingItem(id="short", kind="reference", content="WHO.")
    items = [corrected, residue, empty, label_only, short]
    before = [item.model_dump_json() for item in items]
    assert clean_references(items, {"r": raw}) == [corrected, short]
    assert [item.model_dump_json() for item in items] == before
    assert clean_references([residue], {"r": raw}) == [residue]
    other = corrected.model_copy(update={"content": "A different paper."})
    assert clean_references([other, residue], {"r": raw}) == [other, residue]


def test_alphabetic_markers_do_not_merge_in_new_projection():
    r = report(
        [
            b("h", "References", kind="heading"),
            b("dm", "[DM] First author.\nLong title. 2024.", kind="reference"),
            b("dr", "[DR] Second author. 2025.", kind="reference"),
        ]
    )
    prepare_reading(r)
    assert [i.reference_number for i in r.reading.references] == ["DM", "DR"]
    assert "Long title" in text(r.reading.references[0], {b.id: b for b in r.document.blocks})


def test_old_lump_splits_exact_source_slices_without_touching_prose_or_ids():
    body = b("body", "The interval [x,y] and [DM] are ordinary body text.")
    refs = b(
        "refs",
        "[DM] First 😀 author.\nA multiline title. 2020.\n[DR] Second author. 2021.\n[EGH+] Third. https://example.org/third",
    )
    r = report([body, refs])
    before = r.document.model_dump_json()
    item = ReadingItem(
        id="old-stable",
        kind="reference",
        parts=[ReadingPart(block_id="refs", start=0, end=utf16(refs.text))],
    )
    r.reading = ReadingDocument(source_version="original", items=[], references=[item])
    repair_reference_boundaries(r)
    assert [i.reference_number for i in r.reading.references] == ["DM", "DR", "EGH+"]
    assert r.reading.references[0].id == "old-stable"
    assert r.reading.references[-1].href == "https://example.org/third"
    assert "multiline title" in text(r.reading.references[0], {"refs": refs})
    assert "Second author" not in text(r.reading.references[0], {"refs": refs})
    once = r.reading.model_dump_json()
    repair_reference_boundaries(r)
    assert once == r.reading.model_dump_json() and before == r.document.model_dump_json()


def test_reference_body_brackets_and_intervals_are_not_new_entries():
    refs = b(
        "r", "[Pix26] Author. A discussion of [DM] and interval [x,y].\ncontinued text (2026)."
    )
    r = report([refs])
    r.reading = ReadingDocument(
        source_version="v",
        items=[],
        references=[ReadingItem(id="r", kind="reference", content=refs.text)],
    )
    repair_reference_boundaries(r)
    assert len(r.reading.references) == 1
    assert r.reading.references[0].reference_number == "Pix26"
    assert "[DM]" in r.reading.references[0].content
