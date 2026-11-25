"""Real reading services overlap behind local HTTP barriers; no paid requests."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from test_auto_notes import config, report

from paperlens import auto_notes, pipeline
from paperlens import provider as provider_module
from paperlens.control import (
    ProcessingPaused,
    concurrency_limit,
    processing_concurrency,
    processing_control,
    processing_stage,
)
from paperlens.errors import ProviderError
from paperlens.provider import JsonProvider, RequestBudget
from paperlens.reading_parallel import (
    release_request_permit,
    request_slots,
    try_request_permit,
)


def services(monkeypatch, settings, *, failure=None, gate=False, peak=None):
    entered = {name: threading.Event() for name in ("translation", "auto_notes")}
    if peak is not None:
        entered["peak"] = threading.Event()
        entered["overflow"] = threading.Event()
    release = threading.Event()
    if not gate:
        release.set()
    lock = threading.Lock()
    calls, active = [], [0, 0]

    def respond(request):
        body = json.loads(request.content)
        payload = json.loads(body["messages"][-1]["content"])
        name = "auto_notes" if "question" in payload else "translation"
        with lock:
            calls.append(name)
            active[0] += 1
            active[1] = max(active)
            if peak is not None and active[0] >= peak:
                entered["peak"].set()
                if active[0] > peak:
                    entered["overflow"].set()
        entered[name].set()
        try:
            assert release.wait(5)
            if failure == name:
                return httpx.Response(429)
            if name == "translation":
                answer = {"items": [{"id": i["id"], "zh": "条件内的方法。"}
                                    for i in payload["items"]]}
            else:
                block = payload["blocks"][0]
                answer = {"answer": "条件内的方法。[1]", "quotes": [
                    {"paper": "P1", "block_id": block["block_id"], "quote": block["text"]}
                ]}
            return httpx.Response(200, json={
                "choices": [{"finish_reason": "stop", "message": {
                    "content": json.dumps(answer)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            })
        finally:
            with lock:
                active[0] -= 1

    transport = httpx.MockTransport(respond)
    def factory(settings, budget):
        return JsonProvider(settings, budget=budget, transport=transport)
    monkeypatch.setattr(pipeline, "JsonProvider", factory)
    monkeypatch.setattr(auto_notes, "JsonProvider", factory)
    return entered, release, calls, active


def run(current, settings, budget, root):
    return pipeline._run_postprocessing(
        current, settings, budget, root / "reader", root / "audit", lambda *args: None
    )


def test_real_translation_and_notes_start_before_either_finishes(tmp_path, monkeypatch):
    settings = config(tmp_path, paperlens_auto_translate_enabled=True,
                      paperlens_auto_notes_enabled=True, llm_max_retries=0,
                      paperlens_auto_notes_max_paragraphs=2)
    current, budget = report(), RequestBudget(20)
    entered, release, calls, active = services(monkeypatch, settings, gate=True)
    original = current.model_dump()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run, current, settings, budget, tmp_path)
        try:
            assert all(event.wait(3) for event in entered.values())
            assert not future.done() and active[1] == 2
            # Workers operate on private snapshots until coordinator merge.
            assert current.model_dump() == original
            release.set()
            timings, records = future.result(5)
        finally:
            release.set()
    assert current.translations and calls.count("auto_notes") == 2
    assert budget.used == sum(r["usage"]["requests"] for r in records) == len(calls)
    audit = json.loads((tmp_path / "audit/postprocessing-verification.json").read_text())
    assert audit["parallel"] and audit["wall_seconds"] == timings["postprocessing"]


@pytest.mark.parametrize("failure", ["translation", "auto_notes"])
def test_one_failed_branch_preserves_the_other_and_accounts_attempts(
    tmp_path, monkeypatch, failure
):
    settings = config(tmp_path, paperlens_auto_translate_enabled=True,
                      paperlens_auto_notes_enabled=True, llm_max_retries=0,
                      paperlens_auto_notes_max_paragraphs=1)
    current, budget = report(), RequestBudget(10)
    _, _, calls, _ = services(monkeypatch, settings, failure=failure)
    _, records = run(current, settings, budget, tmp_path)
    assert current.quality.warnings
    assert bool(current.translations) is (failure != "translation")
    notes = auto_notes.AutoNoteService(tmp_path / "reader").notes.list(current.run.id, current)
    assert bool(notes) is (failure != "auto_notes")
    assert budget.used == sum(r["usage"]["requests"] for r in records) == len(calls) == 2


def test_two_request_budget_gives_both_branches_one_attempt(tmp_path, monkeypatch):
    settings = config(tmp_path, paperlens_auto_translate_enabled=True,
                      paperlens_auto_notes_enabled=True, llm_max_retries=0,
                      paperlens_auto_notes_max_paragraphs=3)
    current, budget = report(), RequestBudget(2)
    _, _, calls, _ = services(monkeypatch, settings)
    _, records = run(current, settings, budget, tmp_path)
    assert current.translations and calls.count("auto_notes") == 1
    assert budget.used == sum(r["usage"]["requests"] for r in records) == 2
    assert current.quality.warnings


def test_paused_branches_cache_successes_and_new_run_reuses_notes(tmp_path, monkeypatch):
    settings = config(tmp_path, paperlens_auto_translate_enabled=True,
                      paperlens_auto_notes_enabled=True, llm_max_retries=0,
                      paperlens_auto_notes_max_paragraphs=1)
    entered, release, calls, _ = services(monkeypatch, settings, gate=True)
    paused = threading.Event()

    def check():
        if paused.is_set():
            raise ProcessingPaused("pause")

    def controlled():
        with processing_control(check):
            return run(report(), settings, RequestBudget(10), tmp_path)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(controlled)
        try:
            assert all(event.wait(3) for event in entered.values())
            paused.set()
            release.set()
            with pytest.raises(ProcessingPaused) as raised:
                future.result(5)
        finally:
            release.set()
    assert len(calls) == 2
    assert sum(row["usage"]["requests"] for row in raised.value.usage_records) == 2
    resumed = report()
    resumed.run.id = "b" * 32
    budget = RequestBudget(10)
    _, records = run(resumed, settings, budget, tmp_path)
    assert len(calls) == 2 and budget.used == 0
    assert sum(r["usage"]["requests"] for r in records) == 0
    assert resumed.translations
    notes = auto_notes.AutoNoteService(tmp_path / "reader").notes.list(resumed.run.id, resumed)
    assert len(notes) == 1 and notes[0]["reply"]["quotes"][0]["paper_id"] == resumed.run.id


def test_reserved_budget_wait_does_not_hold_http_slots(tmp_path, monkeypatch):
    settings = config(tmp_path, llm_max_retries=0)
    budget = RequestBudget(1)
    reserved, waiting = budget.reserve(), budget.reserve()
    monkeypatch.setattr(provider_module, "_NETWORK_SLOTS", threading.BoundedSemaphore(1))
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }))
    with JsonProvider(settings, budget=waiting, transport=transport) as waiter:
        with JsonProvider(settings, budget=reserved, transport=transport) as sibling:
            with ThreadPoolExecutor(max_workers=2) as pool:
                future = pool.submit(waiter.json, "test", "{}")
                try:
                    successful = pool.submit(sibling.json, "test", "{}")
                    assert successful.result(3) == {}
                    reserved.close()
                    with pytest.raises(ProviderError):
                        future.result(3)
                finally:
                    reserved.close()
                    waiting.close()
    assert budget.used == 1


def test_no_selected_note_types_does_not_reduce_translation_parallelism(tmp_path, monkeypatch):
    settings = config(tmp_path, paperlens_auto_translate_enabled=True,
                      paperlens_auto_notes_enabled=True,
                      **{f"paperlens_auto_notes_{kind}": False for kind in auto_notes.TYPES})
    observed = []

    def translate(self, current, settings, provider, **kwargs):
        observed.append(concurrency_limit())
        return {"status": "skipped", "remaining": []}

    services(monkeypatch, settings)
    monkeypatch.setattr(pipeline.TranslationService, "generate", translate)
    run(report(), settings, RequestBudget(10), tmp_path)
    assert observed == [2]


def test_optional_audit_write_failure_keeps_generated_results_and_usage(tmp_path, monkeypatch):
    settings = config(tmp_path, paperlens_auto_translate_enabled=True,
                      paperlens_auto_notes_enabled=True, llm_max_retries=0,
                      paperlens_auto_notes_max_paragraphs=1)
    current, budget = report(), RequestBudget(10)
    _, _, calls, _ = services(monkeypatch, settings)
    original = pipeline.write_json

    def unavailable(path, value):
        if path.name.endswith("verification.json"):
            raise OSError("audit directory unavailable")
        return original(path, value)

    monkeypatch.setattr(pipeline, "write_json", unavailable)
    _, records = run(current, settings, budget, tmp_path)
    assert current.translations and budget.used == len(calls) == 2
    assert sum(r["usage"]["requests"] for r in records) == 2
    assert auto_notes.AutoNoteService(tmp_path / "reader").notes.list(current.run.id, current)
    assert current.quality.warnings


@pytest.mark.parametrize("width", [1, 2, 3, 4, 5])
@pytest.mark.parametrize("with_notes", [False, True])
def test_postprocessing_shared_width_is_real_and_never_multiplies_nested_pools(
    tmp_path, monkeypatch, width, with_notes
):
    settings = config(tmp_path, paperlens_auto_translate_enabled=True,
                      paperlens_auto_notes_enabled=with_notes, llm_max_retries=0,
                      paperlens_auto_notes_max_paragraphs=6)
    current = report()
    source = next(item for item in current.reading.items if item.kind == "paragraph")
    current.reading.items.extend(
        source.model_copy(deep=True, update={"id": f"extra-{index}"}) for index in range(80)
    )
    budget = RequestBudget(100)
    entered, release, calls, active = services(monkeypatch, settings, gate=True, peak=width)

    def controlled():
        with processing_concurrency(width):
            return run(current, settings, budget, tmp_path)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(controlled)
        try:
            assert entered["peak"].wait(3)
            # Give all nested workers time to attempt their first HTTP call while
            # the first k requests remain blocked. An extra pool slot must show up.
            assert not entered["overflow"].wait(0.15)
            assert not future.done() and active[1] == width
            if with_notes and width >= 2:
                assert entered["translation"].is_set() and entered["auto_notes"].is_set()
            elif with_notes:
                assert entered["translation"].is_set()
                assert not entered["auto_notes"].wait(0.15)
            release.set()
            _, records = future.result(5)
        finally:
            release.set()
    assert active[1] == width
    assert calls.count("auto_notes") == (6 if with_notes else 0)
    assert calls.count("translation") >= 5
    assert budget.used == sum(record["usage"]["requests"] for record in records) == len(calls)
    assert current.translations


@pytest.mark.parametrize("width", [1, 2, 3, 4, 5])
def test_low_budget_finishes_without_waiting_for_an_unstarted_sibling(
    tmp_path, monkeypatch, width
):
    settings = config(tmp_path, paperlens_auto_translate_enabled=True,
                      paperlens_auto_notes_enabled=True, llm_max_retries=0,
                      paperlens_auto_notes_max_paragraphs=1)
    current = report()
    source = next(item for item in current.reading.items if item.kind == "paragraph")
    current.reading.items.extend(
        source.model_copy(deep=True, update={"id": f"extra-{index}"}) for index in range(80)
    )
    budget, stop = RequestBudget(2), threading.Event()
    _, _, calls, active = services(monkeypatch, settings)

    def check():
        if stop.is_set():
            raise ProcessingPaused("test cleanup after timeout")

    def controlled():
        with processing_control(check, lambda: width):
            return run(current, settings, budget, tmp_path)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(controlled)
        try:
            _, records = future.result(3)
        finally:
            # A reservation deadlock regression fails promptly and still releases
            # waiting workers, instead of hanging the test executor at shutdown.
            stop.set()
    assert budget.used == len(calls) == sum(row["usage"]["requests"] for row in records) == 2
    assert active[1] <= width and current.translations and current.quality.warnings
    if width >= 2:
        assert calls.count("translation") == calls.count("auto_notes") == 1


def test_request_pool_captures_live_job_width_and_reuses_it_inside_narrower_branches():
    width = [3]
    with processing_control(lambda: None, lambda: width[0]), request_slots() as pool:
        with processing_concurrency(1), request_slots() as nested:
            assert nested is pool
            assert all(try_request_permit() for _ in range(3))
            assert not try_request_permit()
            width[0] = 5
            assert all(try_request_permit() for _ in range(2))
            assert not try_request_permit()
            for _ in range(5):
                release_request_permit()
    assert pool.active == 0 and pool.peak == 5


def test_first_request_slot_is_reserved_for_unstarted_sibling_then_borrowed():
    with processing_concurrency(3), request_slots() as pool:
        pool.reserve_branches(["translation", "auto_notes"])
        with processing_stage("translation"):
            assert try_request_permit() and try_request_permit()
            assert not try_request_permit()
        with processing_stage("auto_notes"):
            assert try_request_permit()
            release_request_permit()
        # The initial note request happened: any branch may now borrow the slot.
        with processing_stage("translation"):
            assert try_request_permit()
            for _ in range(3):
                release_request_permit()
    assert pool.active == 0 and pool.peak == 3


def test_ultra_translation_and_many_notes_share_sixteen_real_http_slots(tmp_path, monkeypatch):
    settings = config(tmp_path, paperlens_auto_translate_enabled=True,
                      paperlens_auto_notes_enabled=True, llm_max_retries=0,
                      paperlens_auto_notes_max_paragraphs=20)
    current = report()
    source = next(item for item in current.reading.items if item.kind == "paragraph")
    current.reading.items.extend(
        source.model_copy(deep=True, update={"id": f"extra-{index}"}) for index in range(320)
    )
    monkeypatch.setattr(provider_module, "_CONSERVATIVE_NETWORK_SLOTS", threading.BoundedSemaphore(32))
    budget = RequestBudget(100)
    entered, release, calls, active = services(monkeypatch, settings, gate=True, peak=16)

    def controlled():
        with processing_concurrency(16):
            return run(current, settings, budget, tmp_path)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(controlled)
        try:
            assert entered["peak"].wait(4)
            assert not entered["overflow"].wait(0.2)
            assert entered["translation"].is_set() and entered["auto_notes"].is_set()
            release.set()
            _, records = future.result(15)
        finally:
            release.set()
    assert active[1] == 16 and calls.count("auto_notes") == 20
    assert budget.used == len(calls) == sum(row["usage"]["requests"] for row in records)


def test_cached_note_branch_releases_first_request_slot_for_translation(tmp_path, monkeypatch):
    settings = config(tmp_path, paperlens_auto_translate_enabled=True,
                      paperlens_auto_notes_enabled=True, llm_max_retries=0,
                      paperlens_auto_notes_max_paragraphs=1)
    current = report()
    source = next(item for item in current.reading.items if item.kind == "paragraph")
    current.reading.items.extend(
        source.model_copy(deep=True, update={"id": f"extra-{index}"}) for index in range(80)
    )
    services(monkeypatch, settings)
    auto_notes.AutoNoteService(tmp_path / "reader").generate(
        current.run.id, current, settings, {"scope": "paper"}, resume=True)
    budget = RequestBudget(30)
    entered, release, calls, active = services(monkeypatch, settings, gate=True, peak=2)

    def controlled():
        with processing_concurrency(2):
            return run(current, settings, budget, tmp_path)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(controlled)
        try:
            assert entered["peak"].wait(4)
            release.set()
            _, records = future.result(8)
        finally:
            release.set()
    assert active[1] == 2 and "auto_notes" not in calls
    assert budget.used == len(calls) == sum(row["usage"]["requests"] for row in records)
