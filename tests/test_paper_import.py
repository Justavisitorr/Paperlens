"""Public-source discovery and selected PDF import without model requests."""

import httpx
import pytest

from paperlens.errors import InputError
from paperlens.importing import PaperImporter, public_url


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(
        "paperlens.importing.socket.getaddrinfo", lambda *a: [(2, 1, 6, "", ("93.184.216.34", 443))]
    )


def test_web_page_metadata_relative_pdf_and_real_file_download(tmp_path, public_dns):
    calls = []

    def serve(request):
        calls.append(str(request.url))
        if request.url.path == "/paper":
            return httpx.Response(
                200,
                text="""<meta name="citation_title" content="Source title">
              <meta name="citation_author" content="Alice"><meta name="citation_author" content="Bob">
              <meta name="citation_pdf_url" content="/files/paper.pdf"><meta name="citation_date" content="2024/01/02">
              <meta name="description" content="Site description, not the abstract">
              <div class="acl-abstract"><h2>Abstract</h2><p>Actual <em>paper</em> abstract.</p></div>""",
            )
        return httpx.Response(
            200, content=b"%PDF-1.7\nexample", headers={"content-type": "application/pdf"}
        )

    importer = PaperImporter(tmp_path, transport=httpx.MockTransport(serve))
    found = importer.resolve("https://example.org/paper")["candidates"]
    assert len(calls) == 1  # Finding a candidate does not download/process it.
    assert found[0]["authors"] == ["Alice", "Bob"]
    assert found[0]["abstract"] == "Actual paper abstract."
    assert found[0]["pdf_url"] == "https://example.org/files/paper.pdf"
    path = tmp_path / "job/source.pdf"
    candidate = importer.download(found[0]["id"], path, 1024)
    assert candidate["title"] == "Source title" and path.read_bytes().startswith(b"%PDF-")


def test_title_with_colon_queries_sources_and_never_silently_selects(tmp_path, monkeypatch):
    importer = PaperImporter(tmp_path)
    seen = []

    def search(**kwargs):
        seen.append(kwargs["title"])
        return [
            {"title": "First result", "url": "https://example.org/first"},
            {
                "title": "Second result",
                "url": "https://example.org/second",
                "pdf_url": "https://example.org/second.pdf",
            },
        ]

    monkeypatch.setattr(importer, "_arxiv", search)
    monkeypatch.setattr(importer, "_crossref", search)
    rows = importer.resolve("BERT: Pre-training of Deep Bidirectional Transformers")["candidates"]
    assert len(rows) == 2 and len(seen) == 2
    with pytest.raises(InputError, match="未找到公开 PDF"):
        importer.download(rows[0]["id"], tmp_path / "nothing.pdf", 1024)
    assert not (tmp_path / "nothing.pdf").exists()


def test_arxiv_atom_and_doi_pdf_metadata(tmp_path, public_dns):
    def serve(request):
        if request.url.host == "export.arxiv.org":
            return httpx.Response(
                200,
                text="""<feed xmlns="http://www.w3.org/2005/Atom"><entry>
            <id>http://arxiv.org/abs/1706.03762v7</id><title>Attention Is All You Need</title>
            <published>2017-06-12</published><author><name>A Author</name></author><summary>Paper abstract</summary></entry></feed>""",
            )
        return httpx.Response(
            200,
            json={
                "message": {
                    "DOI": "10.1234/paper",
                    "title": ["A paper"],
                    "author": [{"given": "First", "family": "Last"}],
                    "published": {"date-parts": [[2020]]},
                    "link": [
                        {"content-type": "application/pdf", "URL": "https://example.org/p.pdf"}
                    ],
                }
            },
        )

    importer = PaperImporter(tmp_path, transport=httpx.MockTransport(serve))
    arxiv = importer.resolve("arXiv:1706.03762")["candidates"][0]
    assert arxiv["pdf_url"] == "https://arxiv.org/pdf/1706.03762v7"
    doi = importer.resolve("https://doi.org/10.1234/paper")["candidates"][0]
    assert doi["authors"] == ["First Last"] and doi["year"] == "2020"


@pytest.mark.parametrize(
    "body,limit", [(b"<html>Sign in</html>", 1000), (b"%PDF-" + b"x" * 1000, 100)]
)
def test_invalid_or_oversized_download_never_leaves_source(tmp_path, public_dns, body, limit):
    importer = PaperImporter(
        tmp_path, transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))
    )
    row = importer.resolve("https://example.org/paper.pdf")["candidates"][0]
    target = tmp_path / "job/source.pdf"
    with pytest.raises(InputError):
        importer.download(row["id"], target, limit)
    assert not target.exists()


def test_redirect_cannot_fetch_local_service(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "paperlens.importing.socket.getaddrinfo",
        lambda host, *a: [
            (2, 1, 6, "", ("127.0.0.1" if host == "localhost" else "93.184.216.34", 443))
        ],
    )
    called = []

    def respond(request):
        called.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://localhost/private"})

    with pytest.raises(InputError, match="公开论文"):
        PaperImporter(tmp_path, transport=httpx.MockTransport(respond)).resolve(
            "https://example.org/paper"
        )
    assert called == ["https://example.org/paper"]
    with pytest.raises(InputError):
        public_url("file:///C:/private.pdf")
