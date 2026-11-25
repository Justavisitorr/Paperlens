"""Initial node/definition checks overlap on real HTTP events within one paper cap."""

import json
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from test_analysis_engine import document

from paperlens.analysis import engine
from paperlens.analysis.engine import analyze_document
from paperlens.config import Settings
from paperlens.control import ProcessingPaused, processing_control
from paperlens.errors import ProviderError
from paperlens.provider import JsonProvider, RequestBudget


class ReviewTransport:
    def __init__(self, *, gate=False, repair_gate=False, new_repair_quotes=False):
        self.entered = {name: threading.Event() for name in ("nodes", "glossary")}
        self.release = threading.Event()
        if not gate:
            self.release.set()
        self.full = threading.Event()
        self.nodes_full = threading.Event()
        self.target = 2
        self.lock = threading.Lock()
        self.active = self.peak = 0
        self.calls = []
        self.node_reviews, self.term_reviews = Counter(), Counter()
        self.initial_node_peak = 0
        self.initial_nodes = 0
        self.repair_release = threading.Event()
        if not repair_gate:
            self.repair_release.set()
        self.repairs_full = threading.Event()
        self.repair_target = 2
        self.active_repairs = self.repair_peak = 0
        self.new_repair_quotes = new_repair_quotes
        self.repair_gates = {}
        self.recheck_entered = threading.Event()
        self.recheck_batches = []
        self.review_payloads = []

    def __call__(self, request):
        body = json.loads(request.content)
        payload = json.loads(body["messages"][-1]["content"])
        first_node = (payload.get("nodes") or [{}])[0]
        first_term = (payload.get("terms") or [{}])[0]
        if "blocks" in payload:
            stage = "extraction"
        elif "schema" in payload:
            stage = "synthesis"
        elif "terms" in payload:
            stage = "term_repair" if "issues" in first_term else (
                "glossary" if first_term["explanation"] == "Original definition" else "term_recheck")
        else:
            stage = "node_repair" if "issue" in first_node else (
                "nodes" if first_node["summary"] == "Original claim" else "node_recheck")
        with self.lock:
            self.calls.append(stage)
            self.active += 1
            self.peak = max(self.peak, self.active)
            if stage == "nodes":
                self.initial_nodes += 1
                self.initial_node_peak = max(self.initial_node_peak, self.initial_nodes)
                if self.initial_nodes >= self.target:
                    self.nodes_full.set()
            if stage == "node_repair":
                self.active_repairs += 1
                self.repair_peak = max(self.repair_peak, self.active_repairs)
                if self.active_repairs >= self.repair_target:
                    self.repairs_full.set()
            if stage in {"nodes", "node_recheck"}:
                self.review_payloads.append(payload)
            if self.active >= self.target:
                self.full.set()
        try:
            if stage in self.entered:
                self.entered[stage].set()
                assert self.release.wait(5)
            if stage == "node_repair":
                assert self.repair_release.wait(5)
                gate = self.repair_gates.get(first_node["node_id"])
                if gate is not None:
                    assert gate.wait(5)
            if stage == "extraction":
                answer = {"section_summary": "A definition and claim", "findings": [{
                    "title": "Method", "kind": "concept", "summary": "Original claim",
                    "evidence": [{"block_id": "b1", "quote": payload["blocks"][0]["text"]}],
                }]}
            elif stage == "synthesis":
                evidence = payload["sections"][0]["findings"][0]["evidence_ids"]
                answer = {"title": "Test paper", "summary": "A paper", "research_question": "Why?",
                          "nodes": [{"id": f"n{i}", "title": f"Claim {i}", "kind": "concept",
                                     "summary": "Original claim", "evidence_ids": evidence}
                                    for i in range(24)], "edges": [],
                          "glossary": [{"term": f"Term {i}", "explanation": "Original definition",
                                        "evidence_ids": evidence} for i in range(6)]}
            elif stage in {"glossary", "term_recheck"}:
                with self.lock:
                    self.term_reviews.update(t["term_id"] for t in payload["terms"])
                if stage == "term_recheck":
                    assert all(n["summary"] == "Verified claim" for n in payload["nodes"])
                answer = {"reviews": [{
                    "term_id": t["term_id"],
                    "support": "partial" if stage == "glossary" else "supported",
                    "reason": "Check definition", "node_issues": [{
                        "node_id": "n0", "support": "partial", "reason": "Definition conflict",
                    }] if stage == "glossary" else [],
                } for t in payload["terms"]]}
            elif stage == "term_repair":
                answer = {"terms": [{
                    "term_id": t["term_id"], "explanation": "Verified definition",
                    "evidence": [{"block_id": "b1", "quote": document().blocks[0].text}],
                } for t in payload["terms"]]}
            elif stage == "node_repair":
                for n in payload["nodes"]:
                    if n["node_id"] == "n0":
                        assert "Definition conflict" in n["issue"]
                answer = {"nodes": [{
                    "node_id": n["node_id"], "summary": "Verified claim",
                    "explanation": "Verified explanation", "origin": "author",
                    "evidence": [{"block_id": "b1", "quote": (
                        document().blocks[0].text[int(n["node_id"][1:]):int(n["node_id"][1:]) + 20]
                        if self.new_repair_quotes else document().blocks[0].text
                    )}],
                } for n in payload["nodes"]]}
            else:
                with self.lock:
                    self.node_reviews.update(n["node_id"] for n in payload["nodes"])
                    if stage == "node_recheck":
                        self.recheck_batches.append(first_node["node_id"])
                        self.recheck_entered.set()
                answer = {"reviews": [{
                    "node_id": n["node_id"],
                    # The definition reviewer must still downgrade n0 before
                    # repair selection, despite this initial positive judgment.
                    "support": "partial" if stage == "nodes" and n["node_id"] != "n0"
                    else "supported",
                    "reason": "Check claim",
                } for n in payload["nodes"]]}
            return httpx.Response(200, json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            })
        finally:
            with self.lock:
                self.active -= 1
                if stage == "nodes":
                    self.initial_nodes -= 1
                if stage == "node_repair":
                    self.active_repairs -= 1


def run(settings, transport, workers, *, paused=None, budget=120, checkpoint_hook=None):
    def checkpoint():
        if checkpoint_hook is not None:
            checkpoint_hook()
        if paused and paused.is_set():
            raise ProcessingPaused("Pause requested")

    shared = RequestBudget(budget)
    with processing_control(checkpoint, lambda: workers() if callable(workers) else workers), JsonProvider(
        settings, budget=shared, transport=httpx.MockTransport(transport)
    ) as provider:
        result = analyze_document(document(), provider, settings)
        assert provider.usage.requests == shared.used == len(transport.calls)
        return result


def settings_for(tmp_path):
    return Settings(_env_file=None, llm_api_key="local-test-key", llm_max_retries=0,
                    paperlens_cache_dir=tmp_path)


@pytest.mark.parametrize("workers", range(1, 6))
def test_real_initial_reviews_overlap_without_multiplying_the_selected_cap(tmp_path, workers):
    settings = settings_for(tmp_path)
    transport = ReviewTransport(gate=True)
    transport.target = workers
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run, settings, transport, workers)
        try:
            assert transport.entered["nodes"].wait(3)
            if workers == 1:
                assert not transport.entered["glossary"].is_set()
                assert transport.peak == 1
            else:
                assert transport.entered["glossary"].wait(3)
                assert transport.full.wait(3)
                assert transport.peak == workers
                if workers <= 3:
                    assert transport.initial_node_peak == workers - 1
                else:
                    assert 1 <= transport.initial_node_peak <= workers
            transport.release.set()
            result = future.result(8)
        finally:
            transport.release.set()
    assert transport.peak <= workers
    assert transport.node_reviews == Counter({f"n{i}": 2 for i in range(24)})
    assert transport.term_reviews == Counter({f"t{i:04d}": 2 for i in range(1, 7)})
    assert all(n.support == "supported" for n in result.analysis.nodes)
    assert all(t.support == "supported" for t in result.analysis.glossary)
    assert result.audit["definition_conflicts"]["initial"]
    assert len(result.audit["initial_reviews"]) == 24
    assert result.audit["initial_reviews"][0]["support"] == "supported"
    assert any(r["node_id"] == "n0" for r in result.audit["repairs"])
    # Nothing is skipped when the same validated stage cache resumes.
    resumed_transport = ReviewTransport()
    resumed = run(settings, resumed_transport, workers)
    assert resumed_transport.calls == [] and resumed.analysis == result.analysis


def test_review_budget_exhaustion_raises_instead_of_returning_unverified_success(tmp_path):
    transport = ReviewTransport()
    with pytest.raises(ProviderError, match="请求预算"):
        run(settings_for(tmp_path), transport, 5, budget=2)
    assert transport.calls == ["extraction", "synthesis"]


def test_pause_during_overlapping_initial_reviews_stops_before_repairs_and_can_resume(tmp_path):
    settings = settings_for(tmp_path)
    transport, paused = ReviewTransport(gate=True), threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run, settings, transport, 2, paused=paused)
        try:
            assert all(event.wait(3) for event in transport.entered.values())
            paused.set()
            transport.release.set()
            with pytest.raises(ProcessingPaused):
                future.result(5)
        finally:
            transport.release.set()
    assert "node_repair" not in transport.calls and "term_repair" not in transport.calls
    resumed_transport = ReviewTransport()
    result = run(settings, resumed_transport, 2)
    assert "extraction" not in resumed_transport.calls and "synthesis" not in resumed_transport.calls
    assert all(n.support == "supported" for n in result.analysis.nodes)
    assert all(t.support == "supported" for t in result.analysis.glossary)


def test_parallel_repairs_recheck_independently_and_merge_in_stable_order_without_shared_mutation(
    tmp_path, monkeypatch
):
    registries = []
    original = engine.EvidenceRegistry

    class OwnedEvidence(dict):
        def __init__(self, owner):
            super().__init__()
            self.owner = owner

        def __setitem__(self, key, value):
            assert threading.get_ident() == self.owner
            super().__setitem__(key, value)

        def update(self, *args, **kwargs):
            assert threading.get_ident() == self.owner
            super().update(*args, **kwargs)

    class OwnedRegistry(original):
        def __init__(self, doc):
            super().__init__(doc)
            self.owner = threading.get_ident()
            self.items = OwnedEvidence(self.owner)
            self.resolved = 0
            registries.append(self)

        def resolve(self, *args):
            assert threading.get_ident() == self.owner
            self.resolved += 1
            return super().resolve(*args)

    monkeypatch.setattr(engine, "EvidenceRegistry", OwnedRegistry)
    settings = settings_for(tmp_path)
    transport = ReviewTransport(repair_gate=True, new_repair_quotes=True)
    transport.repair_target = 3
    transport.repair_gates = {f"n{i}": threading.Event() for i in range(0, 24, 3)}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run, settings, transport, 3)
        try:
            assert transport.repairs_full.wait(4)
            assert transport.repair_peak == 3 and not future.done()
            transport.repair_release.set()
            # The third batch completes its own recheck while earlier batches
            # remain blocked. Publication must still follow the original plan.
            transport.repair_gates["n6"].set()
            assert transport.recheck_entered.wait(3)
            assert transport.recheck_batches[0] == "n6"
            assert not transport.repair_gates["n0"].is_set()
            for gate in transport.repair_gates.values():
                gate.set()
            result = future.result(8)
        finally:
            transport.repair_release.set()
            for gate in transport.repair_gates.values():
                gate.set()
    assert transport.peak <= 3
    assert len(registries) == 9  # One coordinator and eight private repair chains.
    assert all(registry.resolved for registry in registries)
    assert [n.id for n in result.analysis.nodes] == [f"n{i}" for i in range(24)]
    assert [record["node_id"] for record in result.audit["repairs"]] == [f"n{i}" for i in range(24)]
    assert len(result.evidence) == 25
    for payload in transport.review_payloads:
        assert len(payload["context_blocks"]) == 1
        assert all(node["context_block_ids"] == ["b1"] for node in payload["nodes"])
        assert all("blocks" not in node for node in payload["nodes"])
    assert all(node.support == "supported" for node in result.analysis.nodes)
    resumed_transport = ReviewTransport(new_repair_quotes=True)
    resumed = run(settings, resumed_transport, 5)
    assert not resumed_transport.calls
    assert resumed.analysis == result.analysis and resumed.evidence == result.evidence
    assert resumed.audit["repairs"] == result.audit["repairs"]


def test_initial_reviews_apply_a_live_boost_while_existing_requests_are_waiting(tmp_path):
    width = [2]
    transport = ReviewTransport(gate=True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run, settings_for(tmp_path), transport, lambda: width[0])
        try:
            assert all(event.wait(3) for event in transport.entered.values())
            assert transport.peak == 2
            transport.target = 5
            transport.full.clear()
            width[0] = 5
            assert transport.full.wait(3)
            assert transport.peak == 5 and not future.done()
            transport.release.set()
            result = future.result(8)
        finally:
            transport.release.set()
    assert transport.peak == 5
    assert all(node.support == "supported" for node in result.analysis.nodes)


def test_wide_node_review_borrows_the_finished_glossary_branch_capacity(tmp_path):
    transport = ReviewTransport(gate=True)
    # Keep all node requests blocked, while definitions can complete immediately.
    transport.entered.pop("glossary")
    transport.target = 4
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run, settings_for(tmp_path), transport, 4)
        try:
            assert transport.nodes_full.wait(4)
            assert transport.initial_node_peak == 4
            assert not future.done()
            transport.release.set()
            result = future.result(8)
        finally:
            transport.release.set()
    assert transport.peak == 4
    assert all(node.support == "supported" for node in result.analysis.nodes)


def test_parallel_review_checkpoints_may_wait_without_reentering_one_shared_context(tmp_path):
    # Real pause callbacks may briefly wait on the job queue's lock or storage.
    # Concurrent children must each enter a distinct inherited Context.
    short_wait = threading.Event()
    result = run(settings_for(tmp_path), ReviewTransport(), 5,
                 checkpoint_hook=lambda: short_wait.wait(0.002))
    assert all(node.support == "supported" for node in result.analysis.nodes)


def test_pause_during_parallel_repairs_reuses_completed_repairs_before_rechecking(tmp_path):
    settings = settings_for(tmp_path)
    transport = ReviewTransport(repair_gate=True, new_repair_quotes=True)
    transport.repair_target = 3
    paused = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run, settings, transport, 3, paused=paused)
        try:
            assert transport.repairs_full.wait(4)
            paused.set()
            transport.repair_release.set()
            with pytest.raises(ProcessingPaused):
                future.result(5)
        finally:
            transport.repair_release.set()
    assert transport.calls.count("node_repair") == 3
    assert "node_recheck" not in transport.calls
    resumed_transport = ReviewTransport(new_repair_quotes=True)
    resumed = run(settings, resumed_transport, 5)
    assert resumed_transport.calls.count("node_repair") == 5
    assert resumed_transport.calls.count("node_recheck") == 8
    assert "extraction" not in resumed_transport.calls and "synthesis" not in resumed_transport.calls
    assert [record["node_id"] for record in resumed.audit["repairs"]] == [f"n{i}" for i in range(24)]
    assert all(node.support == "supported" for node in resumed.analysis.nodes)
