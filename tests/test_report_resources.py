"""Web bytes are lazy and account-scoped; explicit exports stay self-contained."""

from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from test_web_service import client_app, headers, namespace
from test_web_service import config as config
from test_workspace_api import report as report
from test_workspace_api import settings as settings

from paperlens import exporting, library, server
from paperlens.jobs import Job
from paperlens.library import LibraryStore
from paperlens.models import PagePreview, ReadingDocument, ReadingItem, SourcePDF
from paperlens.storage import write_json

PDF = b"%PDF-1.7\nOriginal private PDF body for byte-range testing.\n%%EOF\n"


def picture(color="red"):
    stream = io.BytesIO()
    with Image.new("RGB", (4, 3), color) as image:
        image.save(stream, format="PNG")
    data = stream.getvalue()
    return data, "data:image/png;base64," + base64.b64encode(data).decode()


def with_assets(report, color="red"):
    _, image = picture(color)
    current = report.model_copy(deep=True)
    current.document.sha256 = hashlib.sha256(PDF).hexdigest()
    current.previews = [PagePreview(page=1, width=4, height=3, data_url=image)]
    current.reading = ReadingDocument(
        version="3", source_version="fixture",
        items=[ReadingItem(id="figure", kind="figure", page=1, asset=image, content="Figure 1")],
        references=[ReadingItem(id="reference", kind="reference", page=1, asset=image,
                                content="Author. A study. 2025.")],
        information_items={"authors": [ReadingItem(id="info", kind="paragraph", asset=image,
                                                    content="Author")]},
    )
    current.source_pdf = SourcePDF(
        data_url="data:application/pdf;base64," + base64.b64encode(PDF).decode(),
        sha256=current.document.sha256,
    )
    return current


def seed(root, report, *, source=True):
    folder = root / report.run.id
    write_json(folder / "report.json", report.model_dump(mode="json"))
    if source:
        (folder / "source.pdf").write_bytes(PDF)
    return folder


def test_light_report_only_replaces_asset_slots_and_preserves_full_exports(settings, report, monkeypatch):
    report = with_assets(report)
    _, original_image = picture()
    # A quoted data URL is ordinary source text, not an image slot to externalize.
    report.document.blocks[0].text = report.document.blocks[0].raw_text = original_image
    folder = seed(settings.paperlens_output_dir, report)
    original_json = (folder / "report.json").read_bytes()
    monkeypatch.setattr(exporting, "get_dashboard_template",
                        lambda: '<script id="paperlens-data">__PAPERLENS_DATA__</script>')
    with TestClient(server.create_app(settings)) as client:
        base = f"/api/library/{report.run.id}"
        before = client.get(base + "/report").json()
        web = client.get(base + "/report?view=web").json()
        assert "library.assets" in client.get("/api/health").json()["features"]
        image_url = web["reading"]["items"][0]["asset"]
        assert image_url.startswith(base + "/assets/")
        assert web["previews"][0]["data_url"] == image_url
        assert web["reading"]["references"][0]["asset"] == image_url
        assert web["reading"]["information_items"]["authors"][0]["asset"] == image_url
        assert web["document"]["blocks"][0]["text"] == original_image
        assert web["source_pdf"] == {"data_url": base + "/pdf", "byte_size": len(PDF),
                                      "sha256": report.document.sha256}
        fetched = client.get(image_url)
        assert fetched.status_code == 200 and fetched.content == picture()[0]
        assert fetched.headers["content-type"] == "image/png"
        assert fetched.headers["cache-control"] == "no-store"
        assert fetched.headers["x-content-type-options"] == "nosniff"
        assert client.get(base + "/report").json() == before
        html = client.get(base + "/html").text
        assert report.source_pdf.data_url in html and original_image in html
    assert (folder / "report.json").read_bytes() == original_json
    assert len(list((folder / ".web-assets").iterdir())) == 1


@pytest.mark.parametrize("source", [True, False])
def test_original_and_embedded_only_pdf_support_ranges(settings, report, source):
    report = with_assets(report)
    seed(settings.paperlens_output_dir, report, source=source)
    with TestClient(server.create_app(settings)) as client:
        base = f"/api/library/{report.run.id}"
        web = client.get(base + "/report?view=web").json()
        assert web["source_pdf"]["byte_size"] == len(PDF)
        result = client.get(base + "/pdf", headers={"Range": "bytes=5-13"})
        assert result.status_code == 206 and result.content == PDF[5:14]
        assert result.headers["content-range"] == f"bytes 5-13/{len(PDF)}"
        assert result.headers["content-length"] == "9"
        assert result.headers["accept-ranges"] == "bytes"
        assert result.headers["content-type"] == "application/pdf"
        assert result.headers["content-disposition"].startswith("inline;")
        assert result.headers["cache-control"] == "no-store"
        assert client.get(base + "/pdf").content == PDF
        assert client.get(base + "/pdf", headers={"Range": "bytes=9999-"}).status_code == 416


def test_warm_resources_do_not_reload_or_copy_the_full_report(settings, report, monkeypatch):
    report = with_assets(report)
    seed(settings.paperlens_output_dir, report)
    app = server.create_app(settings)
    with TestClient(app) as client:
        base = f"/api/library/{report.run.id}"
        web = client.get(base + "/report?view=web").json()

        def forbidden(*args, **kwargs):
            pytest.fail("A warm resource request must not parse/deepcopy the full report")

        monkeypatch.setattr(app.state.library, "report", forbidden)
        monkeypatch.setattr(library, "select_pdf", forbidden)
        for _ in range(3):
            assert client.get(web["reading"]["items"][0]["asset"]).content == picture()[0]
            assert client.get(base + "/pdf", headers={"Range": "bytes=0-7"}).content == PDF[:8]
            assert client.get(base + "/report?view=web").json() == web


def test_legacy_exports_restore_missing_embedded_pdf_without_rewriting_report(settings, report, monkeypatch):
    report = with_assets(report)
    report.source_pdf = None
    folder = seed(settings.paperlens_output_dir, report)
    original = (folder / "report.json").read_bytes()
    monkeypatch.setattr(exporting, "get_dashboard_template",
                        lambda: '<script id="paperlens-data">__PAPERLENS_DATA__</script>')
    with TestClient(server.create_app(settings)) as client:
        base = f"/api/library/{report.run.id}"
        web = client.get(base + "/report?view=web").json()
        full = client.get(base + "/report").json()
        assert web["source_pdf"]["byte_size"] == len(PDF)
        embedded = full["source_pdf"]["data_url"]
        assert base64.b64decode(embedded.split(",", 1)[1]) == PDF
        assert embedded in client.get(base + "/html").text
    assert (folder / "report.json").read_bytes() == original


def test_job_html_export_also_restores_the_verified_original_pdf(settings, report, monkeypatch):
    report = with_assets(report)
    report.source_pdf = None
    folder = seed(settings.paperlens_output_dir, report)
    html_path = folder / "dashboard.html"
    html_path.write_text("old HTML without source PDF", encoding="utf-8")
    job = Job(id=report.run.id, status="complete",
              report_relative_path=html_path.relative_to(settings.paperlens_output_dir).as_posix())
    write_json(settings.paperlens_cache_dir / "jobs" / job.id / "job.json", job.model_dump(mode="json"))
    monkeypatch.setattr(exporting, "get_dashboard_template",
                        lambda: '<script id="paperlens-data">__PAPERLENS_DATA__</script>')
    with TestClient(server.create_app(settings)) as client:
        result = client.get(f"/api/jobs/{job.id}/report")
        assert result.status_code == 200
        assert "data:application/pdf;base64," + base64.b64encode(PDF).decode() in result.text
    assert html_path.read_text(encoding="utf-8") == "old HTML without source PDF"


def test_mismatched_saved_pdf_uses_verified_embedded_bytes_for_all_views(settings, report):
    report = with_assets(report)
    folder = seed(settings.paperlens_output_dir, report)
    wrong = b"%PDF-1.7\nAn unrelated document.\n%%EOF"
    (folder / "source.pdf").write_bytes(wrong)
    with TestClient(server.create_app(settings)) as client:
        base = f"/api/library/{report.run.id}"
        web = client.get(base + "/report?view=web").json()
        assert web["source_pdf"]["byte_size"] == len(PDF)
        assert any("来源不一致" in warning for warning in web["quality"]["warnings"])
        sliced = client.get(base + "/pdf", headers={"Range": "bytes=5-25"})
        assert sliced.status_code == 206 and sliced.content == PDF[5:26]
        assert sliced.headers["content-range"] == f"bytes 5-25/{len(PDF)}"
        full = client.get(base + "/report").json()
        assert full["source_pdf"]["data_url"] == report.source_pdf.data_url
    assert (folder / "source.pdf").read_bytes() == wrong


def test_unverifiable_pdf_is_unavailable_while_report_remains_readable(settings, report):
    report = with_assets(report)
    report.source_pdf = None
    folder = seed(settings.paperlens_output_dir, report)
    (folder / "source.pdf").write_bytes(b"%PDF-another-document")
    with TestClient(server.create_app(settings)) as client:
        base = f"/api/library/{report.run.id}"
        for query in ["", "?view=web"]:
            response = client.get(base + "/report" + query)
            assert response.status_code == 200
            payload = response.json()
            assert payload["source_pdf"] is None
            assert payload["reading"]["items"][0]["content"] == "Figure 1"
            assert any("未通过来源校验" in warning for warning in payload["quality"]["warnings"])
        assert client.get(base + "/pdf").status_code == 404


def test_source_revision_invalidates_verified_pdf_selection(settings, report):
    report = with_assets(report)
    folder = seed(settings.paperlens_output_dir, report)
    with TestClient(server.create_app(settings)) as client:
        base = f"/api/library/{report.run.id}"
        assert client.get(base + "/pdf").content == PDF
        (folder / "source.pdf").write_bytes(b"%PDF-replaced-by-different-source")
        # The unchanged embedded source remains authoritative after source.pdf changes.
        assert client.get(base + "/pdf").content == PDF
        web = client.get(base + "/report?view=web").json()
        assert web["source_pdf"]["byte_size"] == len(PDF)
        assert any("来源不一致" in warning for warning in web["quality"]["warnings"])


def test_deleted_derived_image_is_restored_from_unchanged_report(settings, report):
    report = with_assets(report)
    folder = seed(settings.paperlens_output_dir, report)
    original = (folder / "report.json").read_bytes()
    with TestClient(server.create_app(settings)) as client:
        base = f"/api/library/{report.run.id}"
        web = client.get(base + "/report?view=web").json()
        url = web["reading"]["items"][0]["asset"]
        cached = folder / ".web-assets" / url.rsplit("/", 1)[-1]
        cached.unlink()
        assert client.get(url).content == picture()[0]
        assert cached.read_bytes() == picture()[0]
    assert (folder / "report.json").read_bytes() == original


def test_report_source_hash_revision_is_used_without_waiting_for_catalogue_refresh(settings, report):
    report = with_assets(report)
    folder = seed(settings.paperlens_output_dir, report)
    with TestClient(server.create_app(settings)) as client:
        base = f"/api/library/{report.run.id}"
        assert client.get(base + "/pdf").content == PDF
        updated = b"%PDF-1.7\nUpdated original for same saved report.\n%%EOF"
        report.document.sha256 = hashlib.sha256(updated).hexdigest()
        report.source_pdf = None
        (folder / "source.pdf").write_bytes(updated)
        write_json(folder / "report.json", report.model_dump(mode="json"))
        # Calling /pdf directly exercises the cached catalogue's source SHA too.
        assert client.get(base + "/pdf").content == updated
        web = client.get(base + "/report?view=web").json()
        assert web["source_pdf"]["sha256"] == report.document.sha256
        assert web["source_pdf"]["byte_size"] == len(updated)


def test_report_revision_invalidates_urls_and_bounds_derived_cache(settings, report):
    report = with_assets(report)
    folder = seed(settings.paperlens_output_dir, report)
    with TestClient(server.create_app(settings)) as client:
        base = f"/api/library/{report.run.id}"
        before = client.get(base + "/report?view=web").json()
        old_url = before["reading"]["items"][0]["asset"]
        changed = with_assets(report, "blue")
        write_json(folder / "report.json", changed.model_dump(mode="json"))
        after = client.get(base + "/report?view=web").json()
        new_url = after["reading"]["items"][0]["asset"]
        assert new_url != old_url
        assert client.get(old_url).status_code == 404
        assert client.get(new_url).content == picture("blue")[0]
        assert len(list((folder / ".web-assets").iterdir())) == 1
        assert client.delete(base).status_code == 200
        assert client.get(new_url).status_code == 404


def test_binary_resource_cache_is_account_scoped_even_with_the_same_report_id(config, report):
    alice, bob = with_assets(report, "red"), with_assets(report, "blue")
    for user, saved in [("alice", alice), ("bob", bob)]:
        seed(config.root / "workspaces" / namespace(user) / "outputs", saved)
    base = f"/api/library/{report.run.id}"
    with TestClient(client_app(config), base_url=config.origin) as client:
        first = client.get(base + "/report?view=web", headers=headers("alice")).json()
        second = client.get(base + "/report?view=web", headers=headers("bob")).json()
        alice_url = first["reading"]["items"][0]["asset"]
        bob_url = second["reading"]["items"][0]["asset"]
        assert client.get(alice_url, headers=headers("alice")).content == picture("red")[0]
        assert client.get(alice_url, headers=headers("bob")).status_code == 404
        assert client.get(bob_url, headers=headers("bob")).content == picture("blue")[0]
        assert client.get(base + "/pdf", headers=headers("stranger")).status_code == 404
        assert client.get(alice_url, headers={**headers("bob"),
            "x-paperlens-workspace": namespace("alice")}).status_code == 409


@pytest.mark.parametrize("bad", ["https://attacker.invalid/pixel", "data:text/html;base64,PHNjcmlwdD4=",
                                 "data:image/png;base64,@@@", "data:image/svg+xml;base64,PHN2Zz4="])
def test_invalid_or_active_asset_payloads_are_not_exposed(settings, report, bad):
    report = with_assets(report)
    report.reading.items[0].asset = bad
    seed(settings.paperlens_output_dir, report)
    with TestClient(server.create_app(settings)) as client:
        base = f"/api/library/{report.run.id}"
        assert client.get(base + "/report?view=web").json()["reading"]["items"][0]["asset"] == ""
        assert client.get(base + "/report").json()["reading"]["items"][0]["asset"] == bad
        assert client.get(base + "/assets/not-a-hash").status_code == 404


def test_cached_asset_path_escape_is_rejected_without_symlink_privileges(settings, report, monkeypatch):
    report = with_assets(report)
    seed(settings.paperlens_output_dir, report)
    app = server.create_app(settings)
    with TestClient(app) as client:
        base = f"/api/library/{report.run.id}"
        web = client.get(base + "/report?view=web").json()
        url = web["reading"]["items"][0]["asset"]
        resource = app.state.library.image_resource(report.run.id, url.rsplit("/", 1)[-1])
        outside = settings.paperlens_output_dir.parent / "outside-secret"
        outside.write_bytes(b"private")
        resolve = Path.resolve

        def escaped(path, *args, **kwargs):
            return outside if path == resource.path else resolve(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", escaped)
        response = client.get(url)
        assert response.status_code == 404 and b"private" not in response.content


def test_projection_cache_holds_only_two_lightweight_reports(settings, report):
    store = LibraryStore(settings.paperlens_output_dir)
    for letter in "abc":
        current = with_assets(report)
        current.run.id = letter * 32
        seed(settings.paperlens_output_dir, current)
        store.web_report(current.run.id)
    assert len(store._web_cache) == 2
    assert all("data:application/pdf" not in json.dumps(entry.payload)
               and "data:image/png" not in json.dumps(entry.payload) for entry in store._web_cache.values())
