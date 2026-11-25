"""Exercise submitted budgets with real PDF extraction and mock HTTP providers.

These tests measure orchestration and accounting, not model quality or prices.
Only the local browser rendering boundary is replaced in pipeline tests.
"""

import json
import time
from contextlib import nullcontext

import httpx
import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen.canvas import Canvas

from paperlens import pipeline, server
from paperlens.analysis.engine import analyze_document
from paperlens.config import Settings
from paperlens.errors import ProviderError
from paperlens.profiles import apply_profile, profile_catalog, profile_limits
from paperlens.provider import JsonProvider, ProviderPool, RequestBudget

PROFILES = ["economy", "standard", "detailed"]


def settings_for(tmp_path, **kwargs):
    return Settings(
        _env_file=None,
        llm_api_key="profile-test-secret",
        llm_base_url="https://profile.example/v1",
        llm_model="main-selected",
        llm_max_input_chars=6000,
        llm_max_retries=0,
        paperlens_parser="pdfplumber",
        paperlens_vision_max_pages=4,
        paperlens_vision_allow_unverified=True,
        paperlens_preview_pages=0,
        paperlens_cache_dir=tmp_path / "cache",
        paperlens_output_dir=tmp_path / "outputs",
        **kwargs,
    )


def wait_job(client, job_id, *, error=None):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        result = client.get(f"/api/jobs/{job_id}").json()
        if result["status"] not in {"queued", "running"} and (
            error is None or result.get("error") == error
        ):
            return result
        time.sleep(0.01)
    raise AssertionError("profile test worker did not finish")


def fixture_pdf(path):
    canvas = Canvas(str(path), pagesize=(600, 800), invariant=1)
    for page in range(1, 9):
        canvas.setFont("Helvetica", 10)
        for line in range(16):
            canvas.drawString(
                40,
                740 - line * 15,
                f"Page {page} line {line}: The proposed method improves the benchmark "
                "under the stated evaluation conditions.",
            )
        canvas.drawString(40, 450, f"Figure {page}. Benchmark comparison under stated conditions.")
        canvas.rect(40, 250, 360, 150)
        canvas.showPage()
    canvas.save()


class RecordingTransport:
    def __init__(self):
        self.calls = []

    def __call__(self, request):
        body = json.loads(request.content)
        content = body["messages"][-1]["content"]
        text = content if isinstance(content, str) else content[0]["text"]
        payload = json.JSONDecoder().raw_decode(text[text.index("{"):])[0]
        self.calls.append((body, payload))
        if "requested_pages" in payload and "blocks" not in payload:
            result = {"items": [
                {"kind": "paragraph", "page": page, "box": [0.1, 0.1, 0.9, 0.8],
                 "content": f"Page {page}: The proposed method improves the benchmark under the stated evaluation conditions. " * 4}
                for page in payload["requested_pages"]
            ]}
        elif "requested_pages" in payload:
            result = {
                "items": [
                    {
                        "kind": "paragraph",
                        "page": b["page"],
                        # Budget/routing fixtures must still transcribe the full
                        # source so edition coverage is a meaningful assertion.
                        "content": b["text"],
                        "source_ids": [b["id"]],
                    }
                    for b in payload["blocks"]
                ]
            }
        elif "items" in payload and "image_views" in payload:
            result = {"reviews": [{"id": item["id"], "status": "ok"} for item in payload["items"]]}
        elif "items" in payload and "glossary" in payload:
            result = {"items": [{"id": i["id"], "zh": "本段译文。"} for i in payload["items"]]}
        elif "visuals" in payload:
            result = {
                "items": [
                    {
                        "visual_id": item["visual_id"],
                        "status": "analyzed",
                        "description": "Mock interpretation for budget routing only.",
                        "latex": None,
                        "results": [],
                        "warnings": [],
                    }
                    for item in payload["visuals"]
                ]
            }
        elif "schema" in payload:
            evidence_id = payload["sections"][0]["findings"][0]["evidence_ids"][0]
            result = {
                "title": "Budget fixture",
                "summary": "条件内的基准比较",
                "research_question": "方法在给定条件下的表现如何？",
                "nodes": [
                    {
                        "id": "root",
                        "title": "方法比较",
                        "kind": "theme",
                        "summary": "在给定条件下改善基准",
                        "evidence_ids": [evidence_id],
                    }
                ],
                "edges": [],
                "glossary": [
                    {
                        "term": "benchmark",
                        "explanation": "文中用于方法比较的基准",
                        "evidence_ids": [evidence_id],
                    }
                ],
            }
        elif "terms" in payload:
            result = {
                "reviews": [
                    {
                        "term_id": item["term_id"],
                        "support": "supported",
                        "reason": "mock source check",
                        "node_issues": [],
                    }
                    for item in payload["terms"]
                ]
            }
        elif "blocks" in payload:
            first = payload["blocks"][0]
            result = {
                "section_summary": "条件内的基准比较",
                "findings": [
                    {
                        "title": "方法比较",
                        "kind": "contribution",
                        "summary": "在给定条件下改善基准",
                        "origin": "author",
                        "evidence": [{"block_id": first["block_id"], "quote": first["text"][:150]}],
                    }
                ],
            }
        else:
            result = {
                "reviews": [
                    {"node_id": item["node_id"], "support": "supported", "reason": "mock check"}
                    for item in payload["nodes"]
                ]
            }
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(result)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
        )


def stub_browser(monkeypatch):
    monkeypatch.setattr(pipeline, "get_dashboard_template", lambda: "<html></html>")
    monkeypatch.setattr(pipeline, "browser_session", lambda *_: nullcontext())
    monkeypatch.setattr(pipeline, "report_html", lambda _: "<html>profile test</html>")

    def write_report(report, output_dir, **kwargs):
        destination = output_dir / "dashboard.html"
        destination.write_text("<html>profile test</html>", encoding="utf-8")
        return destination

    monkeypatch.setattr(pipeline, "write_report", write_report)




@pytest.mark.parametrize("profile", PROFILES)
def test_retry_and_restart_preserve_profile_limits_after_settings_change(
    tmp_path, monkeypatch, profile
):
    config = settings_for(tmp_path)
    seen = []

    def runner(path, settings, **kwargs):
        seen.append(settings.model_copy(deep=True))
        raise ProviderError(f"controlled failure attempt {len(seen)}")

    monkeypatch.setattr(server, "run_pipeline", runner)
    with TestClient(server.create_app(config)) as client:
        response = client.post(
            "/api/jobs",
            data={"profile": profile, "unlimited_confirmed": "true"},
            files={"file": ("fixture.pdf", b"%PDF-controlled-boundary")},
        )
        job_id = response.json()["id"]
        assert wait_job(client, job_id, error="controlled failure attempt 1")["status"] == "failed"
        assert (
            client.put(
                "/api/settings",
                json={
                    "values": {
                        "LLM_MAX_REQUESTS": 180,
                        "LLM_MAX_INPUT_CHARS": 20000,
                        "LLM_MAX_OUTPUT_TOKENS": 60000,
                    }
                },
            ).status_code
            == 200
        )
        assert client.post(f"/api/jobs/{job_id}/retry").status_code == 200
        assert wait_job(client, job_id, error="controlled failure attempt 2")["status"] == "failed"
    with TestClient(server.create_app(config)) as client:
        assert client.post(f"/api/jobs/{job_id}/retry").status_code == 200
        assert wait_job(client, job_id, error="controlled failure attempt 3")["status"] == "failed"
    assert len(seen) == 3
    original = apply_profile(config, profile)
    assert all(profile_limits(item) == profile_limits(original) for item in seen)
    assert all(item.paperlens_processing_profile == profile for item in seen)
    assert all(item.llm_model == config.llm_model for item in seen)


@pytest.mark.parametrize("profile", PROFILES)
def test_user_ceilings_and_disabled_vision_are_respected(profile):
    base = Settings(
        _env_file=None,
        llm_max_requests=1,
        llm_max_output_tokens=512,
        paperlens_reading_output_tokens=512,
        paperlens_vision_enabled=False,
    )
    effective = apply_profile(base, profile)
    assert effective.llm_max_requests == 1
    assert effective.llm_max_output_tokens == effective.paperlens_reading_output_tokens == 512
    assert profile_limits(effective)["ai_direct"] is True
    assert profile_limits(effective)["code_blocks"] is True
    assert base.paperlens_processing_profile == "standard"
    assert "api_key" not in json.dumps(profile_catalog(base))






@pytest.mark.parametrize("profile", PROFILES)
def test_retries_share_request_cap_and_never_send_an_extra_attempt(tmp_path, profile):
    settings = apply_profile(settings_for(tmp_path, llm_max_requests=1), profile)
    settings.llm_max_retries = 2
    calls = []

    def unavailable(request):
        calls.append(request)
        return httpx.Response(429)

    budget = RequestBudget(settings.llm_max_requests)
    with JsonProvider(
        settings, budget=budget, transport=httpx.MockTransport(unavailable), sleeper=lambda _: None
    ) as provider:
        with pytest.raises(ProviderError, match="请求预算"):
            provider.json("test", "{}")
        assert provider.usage.requests == budget.used == len(calls) == 1


def test_invalid_profile_rejected_before_submitting(tmp_path):
    with TestClient(server.create_app(settings_for(tmp_path))) as client:
        response = client.post(
            "/api/jobs",
            data={"profile": "cheapest", "unlimited_confirmed": "true"},
            files={"file": ("fixture.pdf", b"%PDF-controlled-boundary")},
        )
        assert response.status_code == 422
        assert client.get("/api/jobs").json()["jobs"] == []


@pytest.mark.parametrize("profile", PROFILES)
def test_unchanged_term_review_reuses_the_initial_check_at_the_request_cap(tmp_path, profile):
    settings = apply_profile(settings_for(tmp_path, llm_max_requests=4), profile)
    settings.llm_max_input_chars = 100000
    source = tmp_path / "fixture.pdf"
    fixture_pdf(source)
    from test_reasoning import sample_document
    document = sample_document("The proposed method improves the benchmark under the stated evaluation conditions.")
    recorded = RecordingTransport()
    with ProviderPool(settings, transport=httpx.MockTransport(recorded)) as provider:
        result = analyze_document(document, provider, settings)
        assert len(recorded.calls) == provider.usage.requests == 4
    assert sum("terms" in payload for _, payload in recorded.calls) == 1
    assert result.audit["glossary_final_review_reused"]
    assert list(settings.paperlens_cache_dir.glob("analysis/*/final-analysis-*.json"))


@pytest.mark.parametrize("profile", PROFILES)
def test_text_tiers_differ_without_vision_and_keep_full_source_and_reviews(tmp_path, profile):
    from test_reasoning import sample_document

    from paperlens.analysis.engine import document_chunks

    document = sample_document("An exact quotation supports the result. " * 400)
    settings = apply_profile(settings_for(
        tmp_path, paperlens_vision_enabled=False, paperlens_math_enabled=False,
    ), profile)
    recorded = RecordingTransport()
    with ProviderPool(settings, transport=httpx.MockTransport(recorded)) as provider:
        result = analyze_document(document, provider, settings)
    extracted = [p for _, payload in recorded.calls for p in payload.get("blocks", [])]
    expected = [p for chunk in document_chunks(document, settings.llm_max_input_chars) for p in chunk]
    assert sorted(p["text"] for p in extracted) == sorted(p["text"] for p in expected)
    assert len(result.audit["reviews"]) == len(result.analysis.nodes)
    assert sum("terms" in p for _, p in recorded.calls) == 1
    assert result.audit["glossary_final_review_reused"]
    synthesis = next(body for body, payload in recorded.calls if "schema" in payload)
    assert synthesis["max_tokens"] == {
        "economy": 16000, "standard": 32000, "detailed": 48000,
    }[profile]
    assert ({"economy": "8–12", "standard": "14–26", "detailed": "24–36"}[profile]
            in synthesis["messages"][0]["content"])
    reading = next(body for body, payload in recorded.calls if "blocks" in payload)
    assert reading["max_tokens"] == (4000 if profile == "economy" else 8000)


def test_tiers_vary_analysis_and_math_review_without_losing_pages_or_raising_caps():
    base = Settings(_env_file=None, llm_max_requests=23, llm_max_output_tokens=3000, paperlens_reading_output_tokens=2000)
    for tier, reading, review in [("economy", 1000, 1), ("standard", 2000, 2), ("detailed", 2000, 4)]:
        effective = apply_profile(base, tier)
        limits = profile_limits(effective)
        assert limits["ai_direct"] and limits["vision_enabled"] and limits["code_blocks"]
        assert limits["reading_output_tokens"] == reading
        assert limits["math_review_batches"] == review
        assert limits["max_requests"] == 23 and limits["output_tokens"] == 3000
        assert not {"vision_pages", "math_regions", "transcription_regions"}.intersection(limits)
        assert effective.llm_max_input_chars == base.llm_max_input_chars
