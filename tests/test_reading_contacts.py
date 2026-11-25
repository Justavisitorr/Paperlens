"""Native contact metadata is authoritative without discarding source text."""

from test_reading_projection import b, report

from paperlens.reading import prepare_reading, utf16
from paperlens.reading_contacts import native_contact_content
from paperlens.reading_edition import EditionBatch, EditionBlock, apply_edition


def test_contacts_replace_model_typos_but_keep_other_author_metadata():
    source = "alice@example.org {bob,carol}@university.edu dave@institute.edu"
    model = "Alice One, Bob Two; Example University\nalicex@example.org {bob,carol}@university.edu dave@institute.edu"
    fixed = native_contact_content(model, ["Alice One1", "1Example University", source])
    assert fixed == "Alice One, Bob Two; Example University\n" + source
    assert "alicex@" not in fixed


def test_exact_contacts_are_stable_and_missing_contacts_are_restored():
    source = "E-mail: alice@example.org"
    exact = "Alice One\n" + source
    assert native_contact_content(exact, [source]) == exact
    assert native_contact_content("Alice One\nE-mail:", [source]) == exact
    assert native_contact_content("Alice One", [source]) == exact


def test_mixed_native_prose_is_not_rewritten_or_silently_removed():
    native = "Contact the corresponding author Alice One at alice@example.org for conditions."
    model = "Alice One; a@example.org"
    assert native_contact_content(model, [native]) == model
    assert native_contact_content(model, ["Example University"]) == model


def test_native_contact_ownership_leaves_no_address_prefix_in_body():
    native = "alice@example.org {bob,carol}@university.edu dave@institute.edu"
    value = report([
        b("names", "Alice One1", kind="heading"),
        b("unit", "1Example University", order=1),
        b("contact", native, order=2),
    ])
    original = value.document.model_dump()
    prepare_reading(value)
    apply_edition(value, [EditionBatch(items=[EditionBlock(
        kind="information", page=1, category="authors",
        source_ids=["names", "unit", "contact"],
        content="Alice One1\n1Example University\nalicex@example.org {bob,carol}@university.edu dave@institute.edu",
    )])])
    authors = value.reading.information_items["authors"][0]
    assert native in authors.content and "alicex@" not in authors.content
    assert value.reading.items == []
    assert value.reading.structured_pages == [1]
    contacts = [p for p in authors.parts if p.block_id == "contact"]
    assert len(contacts) == 1 and contacts[0].start == 0 and contacts[0].end == utf16(native)
    assert value.document.model_dump() == original


def test_plain_body_rows_do_not_use_author_contact_repair():
    value = report([b("body", "Write to alice@example.org after reading the conditions.")])
    prepare_reading(value)
    text = "Write to alicex@example.org after reading the conditions."
    apply_edition(value, [EditionBatch(items=[EditionBlock(
        kind="paragraph", page=1, content=text, source_ids=["body"],
    )])])
    assert any(item.content == text for item in value.reading.items)
