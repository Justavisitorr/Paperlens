"""Approval, lossless prompt compaction and recovery without another paid call."""

import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient
from test_ai_direct import batch, make_pdf, native_pages, responses_reply
from test_ai_direct import settings as native_settings
from test_auto_notes import PAPER, config, report, stub
from test_server import fake_result, upload, wait_job

from paperlens import edition_native, server
from paperlens.auto_notes import AutoNoteService
from paperlens.config import Settings
from paperlens.conversation import ConversationService
from paperlens.errors import InputBudgetExceeded
from paperlens.input_budget import input_limit, temporary_input_budget
from paperlens.models import Box, DashboardReport, Evidence, TableCell, TableGrid
from paperlens.notes import item_content
from paperlens.prompt_content import prompt_item_content
from paperlens.provider import JsonProvider


def test_saved_numeric_reference_response_is_recovered_without_resubmission(tmp_path, monkeypatch):
    configured = native_settings(tmp_path)
    source = make_pdf(tmp_path / "paper.pdf", 2)
    calls = []

    def send(request):
        pages = native_pages(json.loads(request.content))
        calls.append(pages)
        raw = batch(pages)
        raw["items"][0].update(kind="reference", reference_number=1)
        return responses_reply(raw)

    validate = edition_native.validate_native
    monkeypatch.setattr(edition_native, "validate_native",
                        lambda *_: (_ for _ in ()).throw(ValueError("old strict label schema")))
    with JsonProvider(configured, transport=httpx.MockTransport(send)) as provider:
        first = edition_native.read_native(source, configured, provider)
    assert first.audit["status"] == "partial" and len(calls) == 1
    monkeypatch.setattr(edition_native, "validate_native", validate)
    with JsonProvider(configured, transport=httpx.MockTransport(send)) as provider:
        second = edition_native.read_native(source, configured, provider)
        assert provider.usage.requests == 0
    assert len(calls) == 1 and second.audit["status"] == "completed"
    assert second.audit["batches"][0]["recovered_raw"]
    assert second.report.reading.references[0].reference_number == "1"
    raw = batch([1])
    raw["items"][0]["reference_number"] = True
    with pytest.raises(ValueError):
        validate(raw, [1])
    with pytest.raises(ValueError, match="全部物理页"):
        validate(batch([1]), [1, 2])


def test_large_table_keeps_all_values_spans_and_headers_within_default_budget(tmp_path):
    current = report()
    item = current.reading.items[1]
    item.kind = "table"
    item.content = "Table 1: exact values and conditions."
    item.table = TableGrid(rows=25, cols=12, cells=[
        TableCell(text=f"value-{i}", row_start=i // 12, row_end=i // 12 + 1,
                  col_start=i % 12, col_end=i % 12 + 1, column_header=i < 12,
                  bbox=Box(x0=.1, y0=.1, x1=.8, y1=.9)) for i in range(300)
    ])
    item.table.cells[0].col_end = 2
    item.table.cells[-1].text = ""
    before = item_content(current, item)
    prepared = AutoNoteService(tmp_path)._prepare(PAPER, current, config(tmp_path), item)
    projected = json.loads(prepared[4])["reading_context"][0]["table"]
    restored = [dict(zip(projected["cell_fields"], cell, strict=True)) for cell in projected["cells"]]
    assert restored == [c.model_dump(exclude={"bbox"}) for c in item.table.cells]
    assert len(prepared[4]) < 18000
    assert len(json.dumps(before)) > 18000 * 2
    assert item_content(current, item) == before
    assert prompt_item_content(current, item)["table"]["rows"] == 25


def test_oversized_note_stops_before_network_and_approval_expires(tmp_path, monkeypatch):
    calls = stub(monkeypatch)
    current, configured = report(), config(tmp_path, llm_max_input_chars=2000)
    target = current.reading.items[1]
    target.content = "Selected complete text " * 300 + "KEEP_THE_END"
    service = AutoNoteService(tmp_path)
    request = {"scope": "paper", "item_id": target.id}
    first = service.generate(PAPER, current, configured, request)
    pending = first["errors"][0]["input_budget"]
    assert calls == [] and first["generated"] == 0
    assert pending["required_chars"] > 2000
    second = service.generate(PAPER, current, configured, request | {
        "input_budget": {"confirmed": True, "limit_chars": pending["suggested_chars"]},
    })
    assert second["generated"] == 1 and len(calls) == 1
    assert calls[0][1]["reading_context"][0]["content"].endswith("KEEP_THE_END")
    assert configured.llm_max_input_chars == input_limit(configured) == 2000
    with pytest.raises(InputBudgetExceeded):
        service._prepare(PAPER, current, configured, target)


def test_job_approval_is_required_and_never_changes_snapshot_or_next_job(tmp_path, monkeypatch):
    configured = config(tmp_path, paperlens_auto_notes_enabled=False,
                        paperlens_auto_translate_enabled=False)
    observed = []

    def runner(path, snapshot, **kwargs):
        observed.append((snapshot.llm_max_input_chars, input_limit(snapshot)))
        result = fake_result(path, snapshot, status="partial" if len(observed) == 1 else "complete")
        result.retryable = len(observed) == 1
        result.input_budget = InputBudgetExceeded(18000, 23000, "自动笔记").budget if result.retryable else None
        return result

    monkeypatch.setattr(server, "run_pipeline", runner)
    with TestClient(server.create_app(configured)) as client:
        ident = upload(client).json()["id"]
        first = wait_job(client, ident)
        pending = first["input_budget"]
        assert first["status"] == "partial" and "retry" in first["actions"]
        assert client.get(first["report_url"]).status_code == 200
        for payload in ({}, {"input_budget": {"confirmed": False, "limit_chars": pending["suggested_chars"]}},
                        {"input_budget": {"confirmed": True, "limit_chars": 1_000_001}}):
            assert client.post(f"/api/jobs/{ident}/retry", json=payload).status_code == 422
        assert len(observed) == 1
        response = client.post(f"/api/jobs/{ident}/retry", json={"input_budget": {
            "confirmed": True, "limit_chars": pending["suggested_chars"],
        }})
        assert response.status_code == 200
        assert wait_job(client, ident)["input_budget"] is None
        next_id = upload(client, b"%PDF-other").json()["id"]
        assert wait_job(client, next_id)["status"] == "complete"
        saved = json.loads((configured.paperlens_cache_dir / "jobs" / ident / "job.json").read_text("utf-8"))
        assert saved["configuration"]["llm_max_input_chars"] == 18000
    assert observed == [(18000, 18000), (18000, pending["suggested_chars"]), (18000, 18000)]


def test_allowance_is_thread_local_and_resets_even_after_failure():
    configured = Settings(_env_file=None, llm_max_input_chars=18000)
    with pytest.raises(RuntimeError), temporary_input_budget(30000):
        assert input_limit(configured) == 30000
        with ThreadPoolExecutor(1) as pool:
            assert pool.submit(input_limit, configured).result() == 18000
        raise RuntimeError("attempt failed")
    assert input_limit(configured) == 18000


def test_question_allowance_is_bound_to_one_payload(tmp_path):
    configured = config(tmp_path, llm_max_input_chars=2000)
    current = report()
    item = current.reading.items[1]
    item.content = "complete selection " * 250
    service = ConversationService(tmp_path)
    payload = {"papers": [PAPER], "question": "Explain", "reading_items": [{
        "paper_id": PAPER, "item_id": item.id, "source_version": current.reading.source_version,
    }]}
    with pytest.raises(InputBudgetExceeded) as first:
        service._prepare({PAPER: current}, configured, payload)
    approved = {"confirmed": True, "limit_chars": first.value.budget["suggested_chars"]}
    prepared = service._prepare({PAPER: current}, configured, payload | {"input_budget": approved})
    assert json.loads(prepared[4])["reading_context"][0]["content"] == item.content
    assert "input_budget" not in json.loads(prepared[4])
    with pytest.raises(InputBudgetExceeded):
        service._prepare({PAPER: current}, configured, payload)


def test_native_page_only_evidence_reopens_without_accepting_wrong_page_or_region():
    current = report()
    block = current.document.blocks[0]
    current.document.parser = "openai-pdf"
    block.source_ref = "ai-direct:unverified-position"
    current.evidence = [Evidence(id="native", block_id=block.id, page=block.page,
                                 bbox=Box(x0=0, y0=0, x1=1, y1=1), quote=block.text)]
    raw = current.model_dump()
    assert DashboardReport.model_validate(raw).evidence[0].bbox.x1 == 1
    raw["evidence"][0]["page"] += 1
    with pytest.raises(ValueError, match="original page"):
        DashboardReport.model_validate(raw)
    raw["evidence"][0]["page"] = block.page
    raw["evidence"][0]["bbox"]["x1"] = .9
    with pytest.raises(ValueError, match="original page"):
        DashboardReport.model_validate(raw)
    raw["evidence"][0]["bbox"]["x1"] = 1
    raw["document"]["parser"] = "pdfplumber"
    with pytest.raises(ValueError, match="original page"):
        DashboardReport.model_validate(raw)
