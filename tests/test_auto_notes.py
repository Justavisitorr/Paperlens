"""Real generation orchestration with a local HTTP stub, never paid calls."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient
from test_conversation import make_report

from paperlens import auto_notes, pipeline, server
from paperlens.auto_notes import AutoNoteService
from paperlens.config import ModelRoute, Settings
from paperlens.control import ProcessingPaused, processing_concurrency, processing_control
from paperlens.errors import ValidationError
from paperlens.models import (
    DashboardReport,
    ReadingDocument,
    ReadingItem,
    ReadingPart,
    TableCell,
    TableGrid,
)
from paperlens.profiles import apply_profile, profile_catalog, profile_limits
from paperlens.provider import JsonProvider, ProviderPool, RequestBudget
from paperlens.runtime import RuntimeSettings
from paperlens.storage import write_json

PAPER = "a" * 32


def report():
    texts = [
        f"Original paragraph {i} explains the method under stated conditions." for i in range(1, 7)
    ]
    current = make_report("Fixture", "c", texts)
    current.run.id = PAPER
    items = []
    for i, block in enumerate(current.document.blocks, 1):
        if i in (1, 5):
            items.append(ReadingItem(id=f"h{i}", kind="heading", content=f"Section {i}"))
        items.append(
            ReadingItem(
                id=f"p{i}",
                kind="paragraph",
                content=f"Reading transcription {i} with symbols.",
                parts=[ReadingPart(block_id=block.id, end=len(block.text))],
            )
        )
    current.reading = ReadingDocument(version="3", source_version="fixture-v1", items=items)
    return current


def config(tmp_path, **updates):
    return Settings(
        _env_file=None,
        llm_api_key="test-secret",
        llm_model="fixture",
        llm_base_url="https://local-test.invalid",
        paperlens_cache_dir=tmp_path / "cache",
        paperlens_output_dir=tmp_path / "outputs",
        **updates,
    )


def stub(monkeypatch, *, invalid=False, fail_item=None, answer_text=None, mislabel=False):
    calls = []

    def respond(request):
        body = json.loads(request.content)
        payload = json.loads(body["messages"][-1]["content"])
        calls.append((body, payload))
        item = payload["reading_context"][0]["item_id"]
        if item == fail_item:
            return httpx.Response(429)
        block = payload["blocks"][0]
        answer = {
            "answer": answer_text or (
                "## 要点摘要\n条件内的方法。[1]\n## 概念与公式解释\n解释关键概念。[1]"
            ),
            "quotes": [
                {
                    "paper": "P1",
                    "block_id": "unknown-source" if mislabel else block["block_id"],
                    "quote": "fabricated unsupported quote" if invalid else block["text"],
                }
            ],
        }
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(answer)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            },
        )

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        auto_notes,
        "JsonProvider",
        lambda settings, budget, **kwargs: JsonProvider(settings, budget=budget, transport=transport, **kwargs),
    )
    return calls


def test_pipeline_resume_cache_invalidates_changed_neighbor_reading_content(tmp_path, monkeypatch):
    calls = stub(monkeypatch)
    service, current = AutoNoteService(tmp_path), report()
    settings = config(tmp_path, paperlens_auto_notes_max_paragraphs=1,
                      paperlens_auto_notes_context="nearby")
    first = service.generate(PAPER, current, settings, {"scope": "paper"}, resume=True)
    changed = current.model_copy(deep=True)
    changed.run.id = "b" * 32
    next(item for item in changed.reading.items if item.id == "p2").content = "Changed neighbor."
    assert current.reading.source_version == changed.reading.source_version
    second = service.generate(changed.run.id, changed, settings, {"scope": "paper"}, resume=True)
    assert first["generated"] == second["generated"] == 1 and len(calls) == 2


def test_plan_is_free_and_bounded_scoped_and_stable(tmp_path, monkeypatch):
    calls = stub(monkeypatch)
    service, current, settings = (
        AutoNoteService(tmp_path),
        report(),
        config(tmp_path, paperlens_auto_notes_max_paragraphs=2),
    )
    plan = service.generate(
        PAPER, current, settings, {"scope": "section", "anchor_id": "p2", "dry_run": True}
    )
    assert plan["eligible"] == 4 and plan["planned"] == plan["max_calls"] == 2
    assert [i["item_id"] for i in plan["items"]] == ["p1", "p4"]
    assert not calls
    result = service.generate(
        PAPER, current, settings, {"scope": "section", "anchor_id": "p2", "item_id": "p4"}
    )
    assert result["generated"] == len(calls) == 1
    assert result["notes"][0]["auto_types"] == ["summary", "explain"]
    assert {b["block_id"] for b in calls[0][1]["blocks"]} <= {"b1", "b2", "b3", "b4"}
    again = service.generate(
        PAPER, current, settings, {"scope": "section", "anchor_id": "p2", "item_id": "p4"}
    )
    assert again["generated"] == 0 and len(calls) == 1
    outside = service.generate(
        PAPER, current, settings, {"scope": "section", "anchor_id": "p2", "item_id": "p2"}
    )
    assert outside["generated"] == 0 and len(calls) == 1
    with pytest.raises(ValidationError):
        service.plan(PAPER, current, settings, {"scope": "section"})
    with pytest.raises(ValidationError):
        service.plan(PAPER, current, settings, {"scope": "paper", "source_version": "old"})


def test_plan_avoids_debris_and_publishing_metadata_before_spreading_selection(tmp_path):
    current = report()
    template = current.reading.items[1]
    noise = ["x", "7", "Introduction", "© 2025 All rights reserved.",
             "ISBN 978-1-23456-789-0", "contact@example.org", "Published by Example Press",
             "1https://github.com/example/a-research-project", "[12] https://example.org/project"]
    current.reading.items = [
        template.model_copy(deep=True, update={"id": f"noise-{i}", "content": text})
        for i, text in enumerate(noise)
    ] + current.reading.items
    plan = AutoNoteService(tmp_path).plan(PAPER, current,
        config(tmp_path, paperlens_auto_notes_max_paragraphs=6), {"scope": "paper"})
    assert plan["eligible"] == 6
    assert [item["item_id"] for item in plan["items"]] == [f"p{i}" for i in range(1, 7)]


def many_paragraphs(count):
    current = report()
    source = current.reading.items[1]
    current.reading.items = [source.model_copy(deep=True, update={
        "id": f"p{i}", "content": f"Reading paragraph {i} describes a distinct assumption."
    }) for i in range(1, count + 1)]
    return current


def concurrent_stub(monkeypatch, *, width, invalid=None, gated=True):
    """Each returned usage is unique; barriers prove requests really overlap."""
    from paperlens import provider as provider_module

    calls, active = [], [0, 0]
    lock, peak, release = threading.Lock(), threading.Event(), threading.Event()
    if not gated:
        release.set()
    # Isolate the scheduler from the conservative cap for unknown endpoints.
    monkeypatch.setattr(provider_module, "_CONSERVATIVE_NETWORK_SLOTS", threading.BoundedSemaphore(32))

    def respond(request):
        payload = json.loads(json.loads(request.content)["messages"][-1]["content"])
        identifier = payload["reading_context"][0]["item_id"]
        with lock:
            calls.append(identifier)
            active[0] += 1
            active[1] = max(active)
            if active[0] >= width:
                peak.set()
        try:
            assert release.wait(5)
            block = payload["blocks"][0]
            number = int(identifier[1:])
            answer = {"answer": f"条件 {number} 下的方法。[1]", "quotes": [{
                "paper": "P1", "block_id": block["block_id"],
                "quote": "unsupported fabricated quote" if identifier == invalid else block["text"],
            }]}
            return httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps(answer)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": number * 100, "completion_tokens": number * 10,
                          "prompt_cache_hit_tokens": number * 20,
                          "prompt_cache_miss_tokens": number * 80},
            })
        finally:
            with lock:
                active[0] -= 1

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(auto_notes, "JsonProvider", lambda settings, budget: JsonProvider(
        settings, budget=budget, transport=transport))
    return calls, active, peak, release


@pytest.mark.parametrize("stage_width,note_width", [(3, 2), (5, 4), (16, 15)])
def test_independent_notes_really_overlap_with_stable_ids_and_exact_usage(
    tmp_path, monkeypatch, stage_width, note_width
):
    current, settings = many_paragraphs(20), config(tmp_path, paperlens_auto_notes_max_paragraphs=20)
    service, budget = AutoNoteService(tmp_path), RequestBudget(20)
    calls, active, peak, release = concurrent_stub(monkeypatch, width=note_width)

    def generate():
        with processing_concurrency(stage_width):
            return service.generate(PAPER, current, settings, {"scope": "paper"}, budget=budget)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(generate)
        try:
            assert peak.wait(4)
            assert not future.done() and active[1] == note_width
            release.set()
            result = future.result(10)
        finally:
            release.set()
    assert len(calls) == budget.used == result["generated"] == 20
    assert active[1] == note_width
    assert [note["item_id"] for note in result["notes"]] == [f"p{i}" for i in range(1, 21)]
    assert result["usage"]["requests"] == 20
    assert result["usage"]["prompt_tokens"] == 21000
    assert result["usage"]["prompt_cache_hit_tokens"] == 4200
    for index, note in enumerate(result["notes"], 1):
        item = current.reading.items[index - 1]
        assert note["id"] == service._identity(current, item, settings)
        assert note["reply"]["usage"]["prompt_tokens"] == index * 100
        assert note["reply"]["usage"]["completion_tokens"] == index * 10
        assert note["reply"]["usage"]["requests"] == 1
        assert note["reply"]["grounded"] and note["reply"]["quotes"]


def test_parallel_rejected_note_still_bills_its_own_exact_usage(tmp_path, monkeypatch):
    calls, _, _, _ = concurrent_stub(monkeypatch, width=4, invalid="p2", gated=False)
    with processing_concurrency(5):
        result = AutoNoteService(tmp_path).generate(PAPER, report(), config(tmp_path), {"scope": "paper"})
    assert len(calls) == result["usage"]["requests"] == 6
    assert result["usage"]["prompt_tokens"] == 2100
    assert [note["item_id"] for note in result["notes"]] == ["p1", "p3", "p4", "p5", "p6"]
    assert result["errors"][0]["item_id"] == "p2"
    for note in result["notes"]:
        assert note["reply"]["usage"]["prompt_tokens"] == int(note["item_id"][1:]) * 100


def test_parallel_pause_drains_and_caches_admitted_notes_with_billed_usage(tmp_path, monkeypatch):
    current, settings, service = report(), config(tmp_path), AutoNoteService(tmp_path)
    calls, _, peak, release = concurrent_stub(monkeypatch, width=4)
    paused = threading.Event()

    def checkpoint():
        if paused.is_set():
            raise ProcessingPaused("paused")

    def generate():
        with processing_control(checkpoint, lambda: 5):
            return service.generate(PAPER, current, settings, {"scope": "paper"}, resume=True)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(generate)
        try:
            assert peak.wait(4)
            paused.set()
            release.set()
            with pytest.raises(ProcessingPaused) as raised:
                future.result(5)
        finally:
            release.set()
    assert len(calls) == len(service.notes.list(PAPER, current)) == 4
    assert sum(row["usage"]["requests"] for row in raised.value.usage_records) == 4
    resumed = current.model_copy(deep=True)
    resumed.run.id = "b" * 32
    with processing_concurrency(5):
        result = service.generate(resumed.run.id, resumed, settings, {"scope": "paper"}, resume=True)
    assert result["generated"] == 2 and len(calls) == 6
    assert len(service.notes.list(resumed.run.id, resumed)) == 6


def test_parallel_requests_never_exceed_shared_budget(tmp_path, monkeypatch):
    calls, _, _, _ = concurrent_stub(monkeypatch, width=4, gated=False)
    budget = RequestBudget(2)
    with processing_concurrency(5):
        result = AutoNoteService(tmp_path).generate(PAPER, report(), config(tmp_path),
                                                  {"scope": "paper"}, budget=budget)
    assert len(calls) == budget.used == result["usage"]["requests"] == result["generated"] == 2
    assert result["errors"]


def test_provider_failure_cancels_siblings_waiting_for_a_network_permit(tmp_path, monkeypatch):
    from paperlens import provider as provider_module

    service = AutoNoteService(tmp_path)
    prepared, lock = [], threading.Lock()
    ready, release = threading.Event(), threading.Event()
    original = service._prepare
    calls = []

    def prepare(*args):
        result = original(*args)
        with lock:
            prepared.append(args[-1].id)
            if len(prepared) == 4:
                ready.set()
        return result

    def respond(request):
        calls.append(request)
        assert release.wait(5)
        return httpx.Response(429)

    monkeypatch.setattr(provider_module, "_NETWORK_SLOTS", threading.BoundedSemaphore(1))
    monkeypatch.setattr(service, "_prepare", prepare)
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(auto_notes, "JsonProvider", lambda settings, budget: JsonProvider(
        settings, budget=budget, transport=transport))

    def generate():
        with processing_concurrency(5):
            return service.generate(PAPER, report(), config(tmp_path), {"scope": "paper"})

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(generate)
        try:
            assert ready.wait(4)
            release.set()
            result = future.result(5)
        finally:
            release.set()
    assert len(prepared) == 4
    assert len(calls) == result["usage"]["requests"] == 1
    assert result["generated"] == 0 and result["errors"]


@pytest.mark.parametrize(
    "scope, expected",
    [
        ("nearby", {"b1", "b2", "b3"}),
        ("section", {"b1", "b2", "b3", "b4"}),
        ("paper", {"b1", "b2", "b3", "b4", "b5", "b6"}),
    ],
)
def test_context_scope_changes_actual_original_and_reading_context(tmp_path, scope, expected):
    current, settings = report(), config(tmp_path, paperlens_auto_notes_context=scope)
    prepared = AutoNoteService(tmp_path)._prepare(
        PAPER, current, settings, current.reading.items[2]
    )
    payload = json.loads(prepared[4])
    assert {b["block_id"] for b in payload["blocks"]} == expected
    assert payload["reading_context"][0]["content"] == "Reading transcription 2 with symbols."
    assert all("Original paragraph" in b["text"] for b in payload["blocks"])
    assert "scope_reading_context" in payload
    assert len(prepared[4]) <= settings.llm_max_input_chars
    assert bool(payload["papers"][0]["analysis_summary"]) is (scope == "paper")


def test_unmatched_quote_does_not_save_ai_note(tmp_path, monkeypatch):
    calls = stub(monkeypatch, invalid=True)
    service = AutoNoteService(tmp_path)
    result = service.generate(PAPER, report(), config(tmp_path), {"scope": "paper"})
    assert len(calls) == 6 and result["generated"] == 0 and len(result["errors"]) == 6
    assert result["usage"]["requests"] == 6
    assert not service.notes.list(PAPER, report())


def test_note_generation_recovers_unique_supplied_source_without_a_second_request(tmp_path, monkeypatch):
    calls = stub(monkeypatch, mislabel=True)
    current = report()
    result = AutoNoteService(tmp_path).generate(
        PAPER, current, config(tmp_path), {"scope": "paper", "item_id": "p2"})
    assert result["generated"] == len(calls) == result["usage"]["requests"] == 1
    assert not result["errors"]
    supplied = calls[0][1]["blocks"][0]
    quote = result["notes"][0]["reply"]["quotes"][0]
    assert quote["block_id"] == supplied["block_id"]
    assert quote["page"] == supplied["page"] and quote["quote"] == supplied["text"]


def figure_report():
    current = report()
    current.document.pages.extend(
        current.document.pages[0].model_copy(update={"number": n, "label": str(4901 + n)})
        for n in (2, 3)
    )
    target = next(i for i in current.reading.items if i.id == "p4")
    target.page, target.content = 3, "Figure 1 illustrates a test matrix; templates are defined here."
    current.document.blocks[3].page = 3
    caption = current.document.blocks[0].model_copy(update={
        "id": "figure-one", "kind": "caption", "page": 2, "order": 7,
        "text": "Figure 1: A matrix organizes capabilities as rows and test types as columns.",
    })
    current.document.blocks.append(caption)
    return current, target


def test_nearby_context_keeps_section_framing_and_referenced_original_caption(tmp_path):
    current, target = figure_report()
    prepared = AutoNoteService(tmp_path)._prepare(
        PAPER, current, config(tmp_path, paperlens_auto_notes_context="nearby"), target
    )
    body = json.loads(prepared[4])
    originals = {block["block_id"]: block for block in body["blocks"]}
    assert {"b1", "b3", "b4", "b5", "figure-one"} == originals.keys()
    assert originals["figure-one"]["page"] == 2
    assert originals["b4"]["page"] == body["reading_context"][0]["page"] == 3
    optional_ids = [i["item_id"] for i in body["scope_reading_context"]]
    nearby_ids = [i["item_id"] for i in body["reading_context"][0]["neighbor_paragraphs"]]
    assert set(optional_ids + nearby_ids) >= {"p1", "p3"}
    assert not set(optional_ids) & set(nearby_ids)
    assert {p["block_id"] for p in body["passages"]} >= {"b1", "figure-one"}
    assert "不能为填栏目制造质疑" in body["question"]
    assert "不同层级概念" in body["question"]
    assert len(prepared[4]) <= config(tmp_path).llm_max_input_chars
    assert current.document.blocks[0].text == originals["b1"]["text"]


@pytest.mark.parametrize("answer", [
    "对照 PDF 第3页 Figure 1 与模板定义。[1]",
    "Figure 1 位于 PDF物理第3页。[1]",
    "图1在物理第3页。[1]",
])
def test_wrong_explicit_figure_physical_page_is_rejected_and_billed(tmp_path, monkeypatch, answer):
    current, target = figure_report()
    calls = stub(monkeypatch, answer_text=answer)
    service = AutoNoteService(tmp_path)
    result = service.generate(PAPER, current, config(tmp_path),
                              {"scope": "paper", "item_id": target.id})
    assert len(calls) == result["usage"]["requests"] == 1
    assert result["usage"]["prompt_tokens"] == 100
    assert result["generated"] == 0 and "图表物理页" in result["errors"][0]["message"]
    assert not service.notes.list(PAPER, current)


@pytest.mark.parametrize("answer", [
    "对照 PDF 第2页 Figure 1。[1]",
    "Figure 1位于物理第2页。[1]",
    "图1在PDF第2页。[1]",
    "PDF第3页提到 Figure 1。[1]",
    "见印刷第4903页 Figure 1。[1]",
    "Figure 1 的方法和第3节的模板有关。[1]",
])
def test_physical_page_guard_preserves_correct_or_non_location_mentions(answer):
    current, _ = figure_report()
    auto_notes._validate_visual_pages(current, answer)


def test_ambiguous_caption_does_not_guess_a_page_or_expand_nearby_context(tmp_path):
    current, target = figure_report()
    current.document.blocks.append(current.document.blocks[-1].model_copy(update={
        "id": "ambiguous-figure-one", "page": 3,
    }))
    prepared = AutoNoteService(tmp_path)._prepare(
        PAPER, current, config(tmp_path, paperlens_auto_notes_context="nearby"), target
    )
    assert not {"figure-one", "ambiguous-figure-one"} & set(prepared[3][PAPER])
    auto_notes._validate_visual_pages(current, "Figure 1位于物理第3页。[1]")


@pytest.mark.parametrize("input_chars", [3000, 3500])
def test_long_optional_caption_does_not_displace_target_source(tmp_path, input_chars):
    current, target = figure_report()
    current.document.blocks[-1].text += " A matrix compares dimensions and evaluation methods." * 200
    prepared = AutoNoteService(tmp_path)._prepare(
        PAPER, current, config(tmp_path, paperlens_auto_notes_context="nearby",
                               llm_max_input_chars=input_chars), target
    )
    assert "b4" in prepared[3][PAPER]
    assert len(prepared[4]) <= input_chars
    # This stress case requires local fallback, not a second model request.
    assert not json.loads(prepared[4])["passages"]


def test_oversize_selected_table_is_not_silently_replaced_by_other_sources(tmp_path, monkeypatch):
    current, target = figure_report()
    target.kind, target.content = "table", "A long selected table cell with conditions. " * 200
    calls = stub(monkeypatch)
    result = AutoNoteService(tmp_path).generate(
        PAPER, current, config(tmp_path, llm_max_input_chars=2000),
        {"scope": "paper", "item_id": target.id},
    )
    assert not calls and result["usage"]["requests"] == result["generated"] == 0
    assert result["errors"]


def test_reasoning_notes_use_configured_output_allowance_without_hidden_retries(
    tmp_path, monkeypatch
):
    calls = []

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        context = json.loads(body["messages"][-1]["content"])
        # Reproduce a reasoning response that needs more than the old 2,400 cap.
        enough = body["max_tokens"] >= 6000
        block = context["blocks"][0]
        answer = {
            "answer": "## 要点摘要\n方法在所述条件下成立。[1]",
            "quotes": [{"paper": "P1", "block_id": block["block_id"], "quote": block["text"]}],
        }
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "reasoning_content": "fixture reasoning",
                            "content": json.dumps(answer) if enough else "",
                        },
                        "finish_reason": "stop" if enough else "length",
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 4000 if enough else body["max_tokens"],
                },
            },
        )

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        auto_notes,
        "JsonProvider",
        lambda settings, budget, **kwargs: JsonProvider(settings, budget=budget, transport=transport, **kwargs),
    )
    service = AutoNoteService(tmp_path)
    result = service.generate(
        PAPER,
        report(),
        config(tmp_path, llm_max_output_tokens=8000),
        {"scope": "paper", "item_id": "p1"},
    )
    assert result["generated"] == 1 and not result["errors"]
    assert len(calls) == 1 and calls[0]["max_tokens"] == 8000
    failed = service.generate(
        PAPER,
        report(),
        config(tmp_path, llm_max_output_tokens=1024, llm_max_retries=4),
        {"scope": "paper", "item_id": "p2"},
    )
    assert failed["generated"] == 0 and len(calls) == 2
    assert "输出上限" in failed["errors"][0]["message"]
    assert "模型配置" not in failed["errors"][0]["message"]
    assert failed["usage"]["requests"] == 1


def test_shared_budget_stops_between_objects_and_retains_failure_usage(tmp_path, monkeypatch):
    calls = stub(monkeypatch, fail_item="p2")
    service, current = AutoNoteService(tmp_path), report()
    budget = RequestBudget(3)
    budget.take()
    result = service.generate(
        PAPER, current, config(tmp_path, llm_max_retries=5), {"scope": "paper"}, budget=budget
    )
    assert len(calls) == result["usage"]["requests"] == 2 and budget.used == 3
    assert result["generated"] == 1 and result["errors"]
    assert result["usage_records"][0]["usage"]["requests"] == 2
    assert len(service.notes.list(PAPER, current)) == 1


def test_manual_notes_remain_untouched_and_simultaneous_clicks_deduplicate(tmp_path, monkeypatch):
    calls = stub(monkeypatch)
    service, current, settings = AutoNoteService(tmp_path), report(), config(tmp_path)
    manual = service.notes.save(
        PAPER,
        current,
        "f" * 32,
        {"item_id": "p1", "kind": "note", "text": "My personal observation"},
    )
    payload = {"scope": "paper", "item_id": "p1"}
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(lambda _: service.generate(PAPER, current, settings, payload), range(2))
        )
    assert len(calls) == sum(r["generated"] for r in results) == 1
    assert (
        next(n for n in service.notes.list(PAPER, current) if n["id"] == manual["id"])["text"]
        == manual["text"]
    )
    current.reading.source_version = "fixture-v2"
    assert service.plan(PAPER, current, settings, payload)["planned"] == 1
    assert all(n["detached"] for n in service.notes.list(PAPER, current))


def test_http_feature_plan_execution_and_restart(tmp_path, monkeypatch):
    calls = stub(monkeypatch)
    settings, current = config(tmp_path), report()
    write_json(
        settings.paperlens_output_dir / "sample" / "report.json", current.model_dump(mode="json")
    )
    address = f"/api/library/{PAPER}/notes/generate"
    with TestClient(server.create_app(settings)) as client:
        assert "auto_notes" in client.get("/api/health").json()["features"]
        plan = client.post(address, json={"scope": "paper", "dry_run": True}).json()
        assert not calls and plan["types"] == ["summary", "explain"]
        result = client.post(
            address,
            json={
                "scope": "paper",
                "item_id": plan["items"][0]["item_id"],
                "source_version": plan["source_version"],
            },
        )
        assert result.status_code == 200 and result.json()["generated"] == 1
        assert client.post(address, json={"scope": "section"}).status_code == 422
    with TestClient(server.create_app(settings)) as client:
        assert len(client.get(f"/api/library/{PAPER}/notes").json()["notes"]) == 1


def test_auto_note_settings_persist_and_do_not_scale_with_tiers(tmp_path):
    settings = config(tmp_path)
    manager = RuntimeSettings(settings)
    result = manager.update(
        {
            "PAPERLENS_AUTO_NOTES_ENABLED": True,
            "PAPERLENS_AUTO_NOTES_MAX_PARAGRAPHS": 9,
            "PAPERLENS_AUTO_NOTES_CONTEXT": "paper",
            "PAPERLENS_AUTO_NOTES_CONNECTIONS": True,
        }
    )
    assert "errors" not in result
    saved = RuntimeSettings(settings).snapshot()
    for tier in ("economy", "standard", "detailed"):
        effective = apply_profile(saved, tier)
        assert profile_limits(effective)["auto_note_paragraphs"] == 9
        assert effective.paperlens_reading_output_tokens == settings.paperlens_reading_output_tokens
    catalog = profile_catalog(saved)["auto_notes"]
    assert catalog == {
        "enabled": True,
        "max_paragraphs": 9,
        "context_scope": "paper",
        "types": ["summary", "explain", "connections"],
    }
    fields = {f["key"]: f for g in manager.public()["groups"] for f in g["fields"]}
    assert "tiers" not in fields["PAPERLENS_READING_OUTPUT_TOKENS"]
    assert fields["PAPERLENS_READING_OUTPUT_TOKENS"]["label"] == "分析长度"


def test_chat_role_route_is_used_for_notes(tmp_path, monkeypatch):
    calls = stub(monkeypatch)
    settings = config(
        tmp_path,
        llm_routes={
            "chat": ModelRoute(
                account_id="local",
                model="reading-helper",
                base_url="https://local-test.invalid",
                api_key="role-secret",
            )
        },
    )
    AutoNoteService(tmp_path).generate(
        PAPER, report(), settings, {"scope": "paper", "item_id": "p1"}
    )
    assert calls[0][0]["model"] == "reading-helper"


def test_all_types_disabled_does_not_call_provider(tmp_path, monkeypatch):
    calls = stub(monkeypatch)
    settings = config(
        tmp_path, paperlens_auto_notes_summary=False, paperlens_auto_notes_explain=False
    )
    result = AutoNoteService(tmp_path).generate(PAPER, report(), settings, {"scope": "paper"})
    assert result["planned"] == 0 and not result["errors"] and not calls


def test_formula_table_and_all_selected_types_reach_real_request(tmp_path, monkeypatch):
    calls = stub(monkeypatch)
    current = report()
    formula, table = current.reading.items[1:3]
    formula.kind, formula.content = "formula", r"\sum_{i=1}^n x_i^2"
    table.kind, table.content = "table", "Table caption"
    table.table = TableGrid(
        rows=1,
        cols=1,
        cells=[
            TableCell(
                text="42 under stated conditions",
                row_start=0,
                row_end=1,
                col_start=0,
                col_end=1,
            )
        ],
    )
    settings = config(
        tmp_path,
        paperlens_auto_notes_connections=True,
        paperlens_auto_notes_questions=True,
        paperlens_auto_notes_check=True,
    )
    service = AutoNoteService(tmp_path)
    for item in (formula, table):
        result = service.generate(PAPER, current, settings, {"scope": "paper", "item_id": item.id})
        assert result["generated"] == 1
        assert result["notes"][0]["auto_types"] == [
            "summary",
            "explain",
            "connections",
            "questions",
            "check",
        ]
    contexts = [payload["reading_context"][0] for _, payload in calls]
    assert contexts[0]["content"] == formula.content
    grid = contexts[1]["table"]
    assert dict(zip(grid["cell_fields"], grid["cells"][0], strict=True))["text"] == "42 under stated conditions"
    assert "Original paragraph 1" in current.document.blocks[0].text
    for _, payload in calls:
        assert all(
            title in payload["question"]
            for title in [
                "要点摘要",
                "概念与公式解释",
                "前后联系",
                "值得追问的问题",
                "疑点与核对提示",
            ]
        )


def test_preview_call_count_also_caps_per_item_generation(tmp_path, monkeypatch):
    calls = stub(monkeypatch)
    service, current, settings = (
        AutoNoteService(tmp_path),
        report(),
        config(tmp_path, llm_max_requests=1),
    )
    plan = service.plan(PAPER, current, settings, {"scope": "paper"})
    assert plan["planned"] == plan["max_calls"] == 1
    for entry in plan["items"]:
        service.generate(PAPER, current, settings, {"scope": "paper", "item_id": entry["item_id"]})
    assert len(calls) == 1


@pytest.mark.parametrize("optional_failure", [False, True])
@pytest.mark.parametrize("entry", ["upload", "import"])
def test_new_paper_pipeline_generates_notes_with_shared_budget_and_failure_accounting(
    tmp_path, monkeypatch, optional_failure, entry
):
    from test_processing_profiles import (
        RecordingTransport,
        fixture_pdf,
        settings_for,
        stub_browser,
        wait_job,
    )

    from paperlens.importing import PaperImporter

    settings = settings_for(
        tmp_path,
        paperlens_auto_notes_enabled=True,
        paperlens_auto_notes_max_paragraphs=3,
        paperlens_auto_translate_enabled=False,
        paperlens_text_review_enabled=False,
        paperlens_reference_network=False,
        llm_routes={
            "chat": ModelRoute(
                account_id="chat-local",
                model="reading-helper",
                base_url="https://local-test.invalid",
                api_key="test-secret",
            )
        },
    )
    source, reader_root = tmp_path / "fixture.pdf", settings.paperlens_output_dir / ".reader"
    fixture_pdf(source)
    recorded, note_calls = RecordingTransport(), []

    def respond(request):
        body = json.loads(request.content)
        content = body["messages"][-1]["content"]
        text = content if isinstance(content, str) else content[0]["text"]
        payload = json.JSONDecoder().raw_decode(text[text.index("{"):])[0]
        if "question" not in payload:
            return recorded(request)
        note_calls.append((body, payload))
        if optional_failure:
            return httpx.Response(429)
        block = payload["blocks"][0]
        answer = {
            "answer": "## 要点摘要\n条件内的方法。[1]",
            "quotes": [
                {"paper": "P1", "block_id": block["block_id"], "quote": block["text"][:150]}
            ],
        }
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(answer)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            },
        )

    transport = httpx.MockTransport(respond)
    stub_browser(monkeypatch)
    monkeypatch.setattr(
        pipeline,
        "ProviderPool",
        lambda settings, budget: ProviderPool(settings, budget=budget, transport=transport),
    )
    monkeypatch.setattr(
        pipeline,
        "JsonProvider",
        lambda settings, budget, **kwargs: JsonProvider(settings, budget=budget, transport=transport, **kwargs),
    )
    monkeypatch.setattr(
        auto_notes,
        "JsonProvider",
        lambda settings, budget, **kwargs: JsonProvider(settings, budget=budget, transport=transport, **kwargs),
    )
    # Keep the real service wrapper and job queue; only external HTTP and browser
    # rendering are fixtures. Replacing server.run_pipeline hid a broken signature.
    app = server.create_app(settings)
    with TestClient(app) as client:
        account = client.post(
            "/api/connections/accounts",
            json={
                "provider_id": "custom",
                "name": "reading helper",
                "base_url": "https://local-test.invalid",
                "api_key": "test-secret",
            },
        ).json()["account"]
        assert (
            client.put(
                "/api/connections/routing",
                json={
                    "routing": {
                        "main": {"account_id": "env", "model": settings.llm_model, "vision_confirmed": True},
                        "chat": {"account_id": account["id"], "model": "reading-helper"},
                    }
                },
            ).status_code
            == 200
        )
        if entry == "upload":
            response = client.post(
                "/api/jobs", files={"file": ("fixture.pdf", source.read_bytes(), "application/pdf")}
            , data={"unlimited_confirmed": "true"})
        else:
            monkeypatch.setattr(
                "paperlens.importing.socket.getaddrinfo",
                lambda *a: [(2, 1, 6, "", ("93.184.216.34", 443))],
            )

            def source_response(request):
                if request.url.path == "/paper.pdf":
                    return httpx.Response(
                        200,
                        content=source.read_bytes(),
                        headers={"content-type": "application/pdf"},
                    )
                return httpx.Response(
                    200,
                    text='<meta name="citation_title" content="Imported paper"><meta name="citation_pdf_url" content="https://papers.example/paper.pdf">',
                    headers={"content-type": "text/html"},
                )

            app.state.importer = PaperImporter(
                settings.paperlens_cache_dir, transport=httpx.MockTransport(source_response)
            )
            found = client.post(
                "/api/imports/resolve", json={"query": "https://papers.example/paper"}
            ).json()
            assert not recorded.calls and not note_calls
            response = client.post(
                "/api/imports",
                json={"candidate_id": found["candidates"][0]["id"], "profile": "standard", "unlimited_confirmed": True},
            )
        assert response.status_code == 202, response.text
        job_id = response.json()["id"]
        job = wait_job(client, job_id)
        assert job["status"] == ("partial" if optional_failure else "complete"), job
        assert ("retry" in job["actions"]) is optional_failure
        report_path = app.state.jobs.report_path(app.state.jobs.get(job_id))
        current = DashboardReport.model_validate_json(
            (report_path.parent / "report.json").read_bytes()
        )
        visible = client.get(f"/api/library/{current.run.id}/notes").json()["notes"]
        assert len(visible) == (0 if optional_failure else 3)
        if entry == "import":
            assert (
                json.loads((report_path.parent / "source-metadata.json").read_text("utf-8"))[
                    "title"
                ]
                == "Imported paper"
            )
    audit = json.loads(
        (report_path.parent / "auto-notes-verification.json").read_text(encoding="utf-8")
    )
    assert len(note_calls) == audit["usage"]["requests"] == (1 if optional_failure else 3)
    assert all(body["model"] == "reading-helper" for body, _ in note_calls)
    assert current.run.usage.requests == len(recorded.calls) + len(note_calls)
    assert current.run.usage.requests <= settings.llm_max_requests
    notes = AutoNoteService(reader_root).notes.list(current.run.id, current)
    assert len(notes) == audit["generated"] == (0 if optional_failure else 3)
    assert bool(audit["errors"]) is optional_failure
    assert report_path.is_file()
    if not optional_failure:
        assert all(note["reply"]["usage"]["requests"] == 1 for note in notes)
