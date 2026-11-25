"""Exercise the real batch stages with delayed local responses, never paid APIs."""

import json
import threading
import time

import pytest
from test_edition_math_review import edition
from test_glossary_review import example

from paperlens.analysis.glossary import repair_glossary, review_glossary
from paperlens.config import Settings
from paperlens.control import processing_control
from paperlens.edition_math_review import review_math
from paperlens.provider import RequestBudget


class DelayedProvider:
    def __init__(self, delay=0.06, budget=120):
        self._budget = RequestBudget(budget)
        self.delay = delay
        self.lock = threading.Lock()
        self.active = self.maximum = 0
        self.calls = []

    def json(self, system, user, **kwargs):
        self._budget.take()
        payload = json.JSONDecoder().raw_decode(user)[0]
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            self.calls.append(payload)
        try:
            return self.answer(payload)
        finally:
            with self.lock:
                self.active -= 1

    def answer(self, payload):
        time.sleep(self.delay)
        if "requested_pages" in payload:
            return {
                "items": [
                    {
                        "kind": "paragraph",
                        "page": p,
                        "content": f"Page {p}.",
                        "source_ids": [f"b{p}"],
                    }
                    for p in payload["requested_pages"]
                ]
            }
        if "terms" in payload:
            if "other_terms" not in payload:
                return {
                    "terms": [
                        {
                            "term_id": t["term_id"],
                            "explanation": "Corrected definition.",
                            "evidence": [
                                {
                                    "block_id": t["blocks"][0]["block_id"],
                                    "quote": t["blocks"][0]["text"],
                                }
                            ],
                        }
                        for t in payload["terms"]
                    ]
                }
            return {
                "reviews": [
                    {
                        "term_id": t["term_id"],
                        "support": "supported",
                        "reason": "checked",
                        "node_issues": [],
                    }
                    for t in payload["terms"]
                ]
            }
        return {"patches": []}


def config(tmp_path):
    return Settings(
        _env_file=None,
        paperlens_cache_dir=tmp_path,
        paperlens_vision_allow_unverified=True,
        paperlens_transcription_max_regions=8,
        paperlens_processing_profile="detailed",
    )


@pytest.mark.parametrize("stage", ["math", "glossary", "glossary_repair"])
def test_accelerated_stages_keep_identical_content_requests_and_order(
    tmp_path, monkeypatch, record_property, stage
):
    monkeypatch.setattr("paperlens.reading_edition.page_image", lambda *args: "local-image")
    monkeypatch.setattr(
        "paperlens.edition_math_review.region_images", lambda *args: ["local-image"]
    )
    results, observations = [], []
    for concurrency in [1, 3]:
        s = config(tmp_path / str(concurrency))
        provider = DelayedProvider()
        progress = []
        started = time.monotonic()
        with processing_control(lambda: None, lambda limit=concurrency: limit):
            if stage == "math":
                r = edition([("formula", r"\sum_{i=1}^n x_i")] * 24)
                audit = review_math(
                    r,
                    tmp_path / "paper.pdf",
                    provider,
                    s,
                    progress=lambda a, z, sink=progress: sink.append((a, z)),
                )
                assert audit["reviewed"] == 24 and audit["calls"] == 4
                results.append(r.model_dump())
                assert review_math(r, tmp_path / "paper.pdf", provider, s)["cache_hits"] == 4
            else:
                doc, registry, analysis = example()
                analysis.glossary = [
                    analysis.glossary[0].model_copy(deep=True, update={"term": f"term {i}"})
                    for i in range(12)
                ]

                def request(_stage, model, system, payload, backend=provider):
                    return model.model_validate(backend.json(system, json.dumps(payload)))

                if stage == "glossary_repair":
                    repaired = repair_glossary(analysis, registry, doc, request)
                    assert len(repaired) == 12
                else:
                    review = review_glossary(
                        analysis,
                        registry,
                        doc,
                        request,
                        stage="test",
                        progress=lambda a, z, sink=progress: sink.append((a, z)),
                    )
                    assert len(review.reviews) == 12
                results.append(analysis.model_dump())
        observations.append(
            {
                "parallel": concurrency,
                "seconds": round(time.monotonic() - started, 4),
                "requests": provider._budget.used,
                "max_inflight": provider.maximum,
            }
        )
        assert progress == [(i, len(progress)) for i in range(1, len(progress) + 1)]
        assert provider.maximum == concurrency
    assert results[0] == results[1]
    assert observations[0]["requests"] == observations[1]["requests"]
    record_property(
        "speed_observation",
        json.dumps({"stage": stage, "delay_per_request": 0.06, "runs": observations}),
    )






def test_parallel_math_cannot_exceed_remaining_shared_request_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "paperlens.edition_math_review.region_images", lambda *args: ["local-image"]
    )
    r = edition([("formula", r"\sum_{i=1}^n x_i")] * 24)
    provider = DelayedProvider(budget=2)
    provider._budget.take()  # One request spent by the preceding pipeline.
    with processing_control(lambda: None, lambda: 3):
        audit = review_math(r, tmp_path / "paper.pdf", provider, config(tmp_path))
    assert provider._budget.used == 2 and len(provider.calls) == 1
    assert audit["reviewed"] == 6 and audit["status"] == "partial"
