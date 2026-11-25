"""The catalogue presents source metadata and persistent reader state."""

import base64
import hashlib

import pytest
from fastapi.testclient import TestClient
from test_workspace_api import report as report_fixture
from test_workspace_api import settings as settings_fixture

from paperlens.bibliography import bibliography
from paperlens.library import LibraryStore
from paperlens.models import ReadingDocument, ReadingItem, SourcePDF
from paperlens.server import create_app
from paperlens.storage import write_json


@pytest.mark.parametrize(
    "text",
    [
        "Alice One1 Bob Two2\n1Example University 2Research Institute",
        "Alice One¹, Bob Two²; ¹Example University; alice@example.org",
    ],
)
def test_author_catalogue_excludes_affiliations_and_removes_markers(report, text):
    report.reading = ReadingDocument(
        source_version="v1",
        items=[],
        information_items={"authors": [ReadingItem(id="authors", kind="paragraph", content=text)]},
    )
    assert bibliography(report)["authors"] == ["Alice One", "Bob Two"]


def test_an_affiliation_alone_is_not_a_catalogue_author(report):
    report.reading = ReadingDocument(
        source_version="v1",
        items=[],
        information_items={
            "authors": [
                ReadingItem(id="authors", kind="paragraph", content="2University of Somewhere")
            ]
        },
    )
    assert bibliography(report)["authors"] == []


@pytest.mark.parametrize("separator", ["\t", "   ", "    "])
def test_author_layout_columns_preserve_multiword_and_hyphenated_names(report, separator):
    text = separator.join(["Jean-Luc de Vries", "Anna van der Meer"])
    report.reading = ReadingDocument(
        source_version="v1", items=[],
        information_items={"authors": [ReadingItem(
            id="authors", kind="paragraph", content=text + "\nExample University",
        )]},
    )
    assert bibliography(report)["authors"] == ["Jean-Luc de Vries", "Anna van der Meer"]


@pytest.mark.parametrize("text, expected", [
    ("Alice One Bob Two", ["Alice One Bob Two"]),
    ("Alice One  Bob Two", ["Alice One Bob Two"]),
    ("Alice    One", ["Alice One"]),
    ("Jean-Luc\tde Vries", ["Jean-Luc de Vries"]),
    ("François\tDupont", ["François Dupont"]),
    ("Alice One    Example University", []),
    ("Alice One, Bob Two    Carol Three", ["Alice One", "Bob Two Carol Three"]),
    ("Alice One and Bob Two", ["Alice One", "Bob Two"]),
])
def test_ambiguous_author_columns_keep_existing_parsing(report, text, expected):
    report.reading = ReadingDocument(
        source_version="v1", items=[],
        information_items={"authors": [ReadingItem(id="authors", kind="paragraph", content=text)]},
    )
    assert bibliography(report)["authors"] == expected


@pytest.fixture
def report():
    return report_fixture.__wrapped__()


@pytest.fixture
def settings(tmp_path):
    return settings_fixture.__wrapped__(tmp_path)


def test_source_title_and_abstract_replace_generated_dashboard_metadata(report):
    report.analysis.title = "An AI generated dashboard"
    report.analysis.summary = "AI claims something"
    report.reading = ReadingDocument(
        version="3",
        source_version="v1",
        items=[ReadingItem(id="abstract", kind="paragraph", content="摘要：真正的原文摘要。")],
        information_items={
            "titles": [ReadingItem(id="title", kind="paragraph", content="真实论文题名")],
            "authors": [ReadingItem(id="authors", kind="paragraph", content="张三, 李四 (某大学)")],
            "citation": [
                ReadingItem(
                    id="citation",
                    kind="paragraph",
                    content="张三. 真实论文题名[J]. 学术期刊, 2020, 1: 2-3.",
                )
            ],
        },
    )
    meta = bibliography(report)
    assert meta["title"] == meta["original_title"] == "真实论文题名"
    assert meta["authors"] == ["张三", "李四"] and meta["abstract"] == "真正的原文摘要。"
    assert meta["venue"] == "学术期刊" and meta["year"] == "2020"


def test_state_survives_restart_and_first_page_thumbnail_is_real(tmp_path, report, settings):
    from reportlab.pdfgen import canvas

    pdf = tmp_path / "paper.pdf"
    writer = canvas.Canvas(str(pdf))
    writer.drawString(100, 700, "FIRST PAGE")
    writer.showPage()
    writer.drawString(100, 700, "SECOND PAGE")
    writer.save()
    report.document.sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()
    report.source_pdf = SourcePDF(
        data_url="data:application/pdf;base64," + base64.b64encode(pdf.read_bytes()).decode(),
        sha256=report.document.sha256,
    )
    write_json(settings.paperlens_output_dir / "paper/report.json", report.model_dump(mode="json"))
    with TestClient(create_app(settings)) as client:
        row = client.get("/api/library").json()["papers"][0]
        assert client.get(row["pdf_url"]).content == pdf.read_bytes()
        image = client.get(row["thumbnail_url"])
        assert image.status_code == 200 and image.headers["content-type"] == "image/webp"
        assert image.content.startswith(b"RIFF")
        saved = client.patch(
            f"/api/library/{row['id']}",
            json={"starred": True, "reading_status": "reading", "opened": True},
        )
        assert saved.status_code == 200 and saved.json()["last_opened"]
    persisted = LibraryStore(settings.paperlens_output_dir).list()["papers"][0]
    assert persisted["starred"] and persisted["reading_status"] == "reading"


def test_english_abstract_heading_and_legacy_items_keep_source_metadata(report):
    report.document.title = "a" * 32
    report.document.source_name = "Source paper.pdf"
    report.analysis.title = "Generated dashboard title"
    report.reading = ReadingDocument(
        version="3",
        source_version="v1",
        items=[
            ReadingItem(id="legacy", kind="heading"),
            ReadingItem(id="title", kind="heading", content="Abstract"),
            ReadingItem(id="abstract", kind="paragraph", content="A source abstract."),
        ],
    )
    meta = bibliography(report)
    assert meta["title"] == "Source paper"
    assert meta["abstract"] == "A source abstract."
