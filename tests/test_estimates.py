"""Pre-import estimates use local history and budgets only, never a model call."""

import io
import json
import runpy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen.canvas import Canvas

from paperlens import estimates, server
from paperlens.config import ModelRoute, Settings
from paperlens.estimate_timing import request_seconds
from paperlens.estimate_workload import Work


def settings_for(tmp_path, **kwargs):
    values = dict(
        _env_file=None,
        llm_api_key="estimate-test-secret",
        llm_base_url="https://api.deepseek.com",
        llm_model="deepseek-flash",
        llm_max_requests=40,
        llm_max_input_chars=10000,
        llm_max_output_tokens=4000,
        paperlens_output_dir=tmp_path / "outputs",
        paperlens_cache_dir=tmp_path / "cache",
        paperlens_catalog_network=False,
    )
    values.update(kwargs)
    return Settings(**values)


def write_run(settings, name, *, pages, requests, prompt, completion, seconds, **run):
    path = settings.paperlens_output_dir / name / f"{name}_run" / "report.json"
    path.parent.mkdir(parents=True)
    body = {
        "quality": {"total_pages": pages},
        "run": {
            "id": name,
            "status": "complete",
            "processing_profile": "standard",
            "usage": {
                "requests": requests,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
            },
            "timings": {"parsing": seconds / 2, "analysis": seconds / 2},
            **run,
        },
    }
    path.write_text(json.dumps(body), encoding="utf-8")


def pdf_bytes(pages):
    buffer = io.BytesIO()
    canvas = Canvas(buffer)
    for index in range(pages):
        canvas.drawString(72, 720, f"page {index + 1}")
        canvas.showPage()
    canvas.save()
    return buffer.getvalue()


def by_id(result):
    return {item["profile"]: item for item in result["profiles"]}


def test_native_workload_covers_all_pages_without_legacy_vision_or_ocr(tmp_path):
    from paperlens.estimate_workload import workload

    settings = settings_for(
        tmp_path, paperlens_reading_mode="ai_direct", llm_max_requests=120,
        paperlens_vision_enabled=False, paperlens_vision_max_pages=1,
        paperlens_transcription_max_regions=1, paperlens_math_enabled=True,
    )
    works, local, _ = workload(settings, 5, estimates.DocumentFeatures(), None, "typical", 8)
    stages = {work.stage: work for work in works}
    assert stages["native_reading"].requests == 3
    assert stages["native_reading"].concurrency == 2
    assert stages["math_review"].concurrency == stages["text_review"].concurrency == 2
    assert {"extraction", "synthesis", "math_review", "text_review"} <= stages.keys()
    assert not {"visuals", "chart_refinement", "reading_edition"} & stages.keys()
    inactive = settings.model_copy(update={
        "paperlens_parser": "pdfplumber", "paperlens_parse_timeout_seconds": 20,
        "paperlens_vision_enabled": True, "paperlens_vision_max_pages": 40,
        "paperlens_transcription_max_regions": 40, "paperlens_math_max_regions": 100,
        "paperlens_math_timeout_seconds": 600, "paperlens_inline_math_max_regions": 200,
        "paperlens_code_mode": "off",
    })
    other, other_local, _ = workload(inactive, 5, estimates.DocumentFeatures(), None, "typical", 8)
    assert [(w.stage, w.requests, w.input_per_request, w.output_per_request) for w in works] == [
        (w.stage, w.requests, w.input_per_request, w.output_per_request) for w in other
    ]
    assert local == other_local


def test_native_estimate_keeps_shared_budget_and_has_no_local_parser_preview(tmp_path, monkeypatch):
    import importlib.util

    from paperlens.profiles import profile_catalog

    assert importlib.util.find_spec("paperlens.parsing.lightweight") is None
    settings = settings_for(tmp_path, paperlens_reading_mode="ai_direct", llm_max_requests=2,
                            paperlens_vision_enabled=False, paperlens_code_mode="off")
    pdf = tmp_path / "native-estimate.pdf"
    pdf.write_bytes(pdf_bytes(5))
    pages, features = estimates.inspect_pdf(pdf, settings)
    assert pages == 5
    assert features.structure_source == "unavailable" and not features.native_chunk_chars
    result = by_id(estimates.estimate(settings, pages=5))["standard"]
    assert result["budget_limited"]
    assert result["workload"][0]["stage"] == "native_reading"
    assert result["typical"]["requests"]["typical"] == 2
    for profile in profile_catalog(settings)["profiles"]:
        limits = profile["limits"]
        assert limits["ai_direct"] and limits["vision_enabled"] and limits["code_blocks"]
        assert not {"math_regions", "math_seconds", "inline_math_regions"}.intersection(limits)
        assert profile["unlimited_limits"] == []
        assert limits["math_review_batches"] == {"economy": 1, "standard": 2, "detailed": 4}[profile["id"]]


def test_workload_is_available_without_history_and_not_derived_from_old_reports(tmp_path):
    settings = settings_for(tmp_path)
    before = estimates.estimate(settings)
    standard = by_id(before)["standard"]
    assert before["pages"] == estimates.DEFAULT_PAGES
    assert standard["time_basis"] == "heuristic"
    assert standard["basis"] == "feature_workload_heuristic"
    assert 0 < standard["typical"]["seconds"]["low"] < standard["typical"]["seconds"]["high"]
    assert 0 < standard["typical"]["completion_tokens"]["typical"] < standard["ceiling"]["completion_tokens"]
    write_run(settings, "old", pages=999, requests=2, prompt=999999, completion=1, seconds=1)
    assert estimates.estimate(settings) == before


def test_expected_tokens_depend_on_settings_and_document_features_not_upper_caps(tmp_path):
    settings = settings_for(tmp_path, paperlens_vision_enabled=False,
                            llm_max_output_tokens=50000, llm_max_requests=120)
    def standard(config, **kwargs):
        return by_id(estimates.estimate(config, pages=8, **kwargs))["standard"]
    base = standard(settings)
    higher_cap = standard(settings.model_copy(update={"llm_max_output_tokens": 100000}))
    assert higher_cap["typical"] == base["typical"]
    # Text depth now differs even with images disabled; all source chunks remain.
    tiers = by_id(estimates.estimate(settings, pages=8))
    assert tiers["economy"]["typical"]["completion_tokens"]["typical"] < tiers["standard"]["typical"]["completion_tokens"]["typical"]
    extracts = [next(w for w in p["workload"] if w["stage"] == "extraction")
                for p in tiers.values()]
    assert len({w["requests"] for w in extracts}) == 1
    assert len({w["prompt_tokens"] for w in extracts}) == 1
    features = estimates.DocumentFeatures(chars_per_page=7000, source="pdf_sample", sampled_pages=4)
    dense = standard(settings, document_features=features)
    assert dense["typical"]["prompt_tokens"]["typical"] > base["typical"]["prompt_tokens"]["typical"]
    translated = standard(settings.model_copy(update={"paperlens_auto_translate_enabled": True}))
    assert translated["typical"]["completion_tokens"]["typical"] > base["typical"]["completion_tokens"]["typical"]
    assert "translation" in {w["stage"] for w in translated["workload"]}
    notes = standard(settings.model_copy(update={"paperlens_auto_notes_enabled": True}))
    assert "auto_notes" in {w["stage"] for w in notes["workload"]}
    assert notes["typical"]["seconds"]["typical"] > base["typical"]["seconds"]["typical"]


def test_request_and_output_estimates_respect_shared_budget(tmp_path):
    settings = settings_for(tmp_path, llm_max_requests=5)
    result = by_id(estimates.estimate(settings, pages=100))["standard"]
    assert result["budget_limited"] is True
    assert result["ceiling"]["requests"] == 5
    for point in ("low", "typical", "high"):
        assert result["typical"]["requests"][point] <= 5
        assert result["typical"]["completion_tokens"][point] <= 5 * settings.llm_max_output_tokens
    assert result["ceiling"]["prompt_tokens_kind"].startswith("approximate")


def test_official_prices_give_a_range_and_other_endpoints_stay_unknown(tmp_path):
    official = settings_for(tmp_path)
    write_run(official, "a", pages=2, requests=2, prompt=2000, completion=1000, seconds=4)
    cost = by_id(estimates.estimate(official, pages=2))["standard"]["typical"]["cost"]["typical"]
    assert cost["status"] == "range"
    assert 0 < cost["off_peak"][0] <= cost["off_peak"][1]
    other = settings_for(tmp_path, llm_base_url="https://openrouter.ai/api/v1")
    cost = by_id(estimates.estimate(other, pages=2))["standard"]["typical"]["cost"]["typical"]
    assert cost == {"status": "unknown", "reason": "model_or_provider_price_unknown"}


def test_endpoint_counts_pages_of_a_staged_pdf_and_keeps_nothing(tmp_path):
    settings = settings_for(tmp_path)
    with TestClient(server.create_app(settings)) as client:
        assert "estimates" in client.get("/api/health").json()["features"]
        result = client.post(
            "/api/estimates", files={"file": ("p.pdf", pdf_bytes(3), "application/pdf")}
        ).json()
        assert result["pages"] == 3 and result["pages_assumed"] is False
        assert result["document_features"]["source"] == "pdf_sample"
        assert result["document_features"]["sampled_pages"] == 3
        assert client.post("/api/estimates", data={"pages": "7"}).json()["pages"] == 7
        assert client.post("/api/estimates").json()["pages_assumed"] is True
    assert not list((settings.paperlens_cache_dir / "estimates").glob("*"))


def timing_sample(settings, **updates):
    return {
        "endpoint": settings.llm_base_url,
        "model": settings.llm_model,
        "protocol": settings.llm_protocol,
        "reasoning_effort": settings.llm_reasoning_effort,
        "effective_reasoning_effort": "none",
        "input_tokens": 2000,
        "output_tokens": 1200,
        "duration_seconds": 5,
        "recorded_at": datetime.now(UTC).isoformat(),
        "success": True,
        "status": "complete",
        "concurrency": 1,
        **updates,
    }


def write_timing(settings, rows):
    path = settings.paperlens_cache_dir / "telemetry" / "llm.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_recent_comparable_requests_only_calibrate_speed_not_token_demand(tmp_path):
    settings = settings_for(tmp_path, paperlens_vision_enabled=False, llm_max_requests=120)
    before = by_id(estimates.estimate(settings, pages=12))["standard"]
    write_timing(settings, [
        timing_sample(settings, effective_reasoning_effort=effort)
        for effort in ("none", "low")
    ])
    after = by_id(estimates.estimate(settings, pages=12))["standard"]
    assert after["time_basis"] == "recent_api_requests"
    assert after["samples"] == 1
    assert before["typical"]["prompt_tokens"] == after["typical"]["prompt_tokens"]
    assert before["typical"]["completion_tokens"] == after["typical"]["completion_tokens"]
    assert after["typical"]["seconds"]["typical"] < before["typical"]["seconds"]["typical"]


@pytest.mark.parametrize("overrides", [
    {"model": "another-model"}, {"endpoint": "https://another-provider.example/v1"},
    {"protocol": "anthropic"}, {"effective_reasoning_effort": "high"},
    {"concurrency": 3}, {"execution_mode": "codex_session"},
    {"status": "failed"}, {"success": False}, {"stream": True},
    {"recorded_at": "2020-01-01T00:00:00+00:00"},
    {"recorded_at": "not-a-date"}, {"duration_seconds": float("nan")},
    {"output_tokens": 0},
])
def test_unrelated_or_unreliable_timing_does_not_change_estimate(tmp_path, overrides):
    settings = settings_for(tmp_path, paperlens_vision_enabled=False)
    before = estimates.estimate(settings, pages=3)
    write_timing(settings, [timing_sample(settings, **overrides)])
    assert estimates.estimate(settings, pages=3) == before


@pytest.mark.parametrize("endpoint,effort,effective", [
    ("https://api.deepseek.com", "default", "provider_default"),
    ("https://api.deepseek.com", "none", "none"),
    ("https://api.openai.com/v1", "high", "high"),
])
def test_timing_matches_actual_json_reasoning_behavior(tmp_path, endpoint, effort, effective):
    settings = settings_for(tmp_path, llm_base_url=endpoint, llm_reasoning_effort=effort)
    work = Work("test", "reading", settings, 1, 2000, 1200, effort)
    row = timing_sample(settings, effective_reasoning_effort=effective)
    seconds, count = request_seconds(work, [row], "typical")
    assert count == 1 and seconds == pytest.approx(5)


def test_timing_prefers_same_concurrent_load_without_mixing_it_into_serial_calls(tmp_path):
    settings = settings_for(tmp_path)
    serial = Work("test", "reading", settings, 1, 2000, 1200, "none")
    parallel = Work("test", "reading", settings, 4, 2000, 1200, "none", concurrency=2)
    rows = [timing_sample(settings), timing_sample(settings, concurrency=2, duration_seconds=9)]
    serial_seconds, serial_count = request_seconds(serial, rows, "typical")
    assert serial_seconds == pytest.approx(5) and serial_count == 1
    seconds, count = request_seconds(parallel, rows, "typical")
    assert seconds == pytest.approx(36) and count == 1
    from paperlens.estimate_timing import scheduled_seconds
    assert scheduled_seconds(parallel, seconds) == pytest.approx(18)


def test_estimate_overlaps_translation_and_notes_after_core_work(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    works = [
        Work("extraction", "reading", settings, 4, 2000, 1200, "none", concurrency=2),
        Work("synthesis", "main", settings, 1, 2000, 1200, "low"),
        Work("translation", "chat", settings, 3, 2000, 1200, "low"),
        Work("auto_notes", "chat", settings, 2, 2000, 1200, "low"),
    ]
    monkeypatch.setattr(estimates, "workload", lambda *args: (works, 10, False))
    monkeypatch.setattr(estimates, "request_seconds", lambda work, *_: (work.requests * 20, 0))
    result = estimates.estimate_profile(settings, "standard", 12, [], None)
    # Two extraction waves (40) + synthesis (20) + max(translation 60, notes 40) + local 10.
    assert result["typical"]["seconds"]["typical"] == 130
    assert result["typical"]["requests"]["typical"] == 10


def test_public_model_speed_is_a_fallback_and_price_uses_actual_source(tmp_path, monkeypatch):
    from paperlens.model_metrics import EndpointMetrics, ModelMetrics

    settings = settings_for(tmp_path, llm_model="vendor/model", llm_base_url="https://openrouter.ai/api/v1",
                            paperlens_vision_enabled=False)
    checked = datetime.now(UTC).isoformat()
    metric = ModelMetrics("vendor/model", settings.llm_base_url, checked, "https://openrouter.ai/api/v1/models/vendor/model/endpoints",
                          True, (EndpointMetrics("fixture", 0.000001, 0.000002, None, 0.01, 50, 2),))
    metrics = {(settings.llm_base_url, settings.llm_model): metric}
    monkeypatch.setattr(estimates, "load_model_metrics", lambda _: metrics)
    # An unrelated old DeepSeek catalog must not mark fresh OpenRouter prices stale.
    original_load = estimates.load_catalog
    monkeypatch.setattr(estimates, "load_catalog", lambda *a, **kw: original_load(*a, **kw).model_copy(update={"stale": True}))
    result = estimates.estimate(settings, pages=2)
    assert result["price_checked_at"] == checked and result["price_stale"] is False
    assert by_id(result)["standard"]["time_basis"] == "openrouter_reference"
    assert by_id(result)["standard"]["typical"]["cost"]["typical"]["status"] == "range"
    work = Work("test", "reading", settings, 1, 2000, 1200, "none")
    assert request_seconds(work, [], "typical", metrics) == (26, -1)
    local = timing_sample(settings, duration_seconds=7, effective_reasoning_effort="provider_default")
    measured, count = request_seconds(work, [local], "typical", metrics)
    assert measured == pytest.approx(7) and count == 1


def test_worker_plans_preserve_cost_and_obey_join_dependencies_through_ultra(tmp_path):
    settings = settings_for(tmp_path, paperlens_vision_enabled=False, llm_max_requests=200,
                            paperlens_auto_translate_enabled=True, paperlens_auto_notes_enabled=True)
    results = [by_id(estimates.estimate(settings, pages=30, concurrency=k))["standard"]
               for k in range(1, 17)]
    assert len({json.dumps(r["typical"]["cost"], sort_keys=True) for r in results}) == 1
    times = [r["typical"]["seconds"]["typical"] for r in results]
    assert times == sorted(times, reverse=True)
    for k, result in enumerate(results, 1):
        work = {w["stage"]: w for w in result["workload"]}
        assert work["extraction"]["concurrency"] == k
        assert work["node_review"]["concurrency"] == (k if k >= 4 else max(1, k - 1))
        assert work["glossary_review"]["concurrency"] == (k if k >= 4 else 1)
        assert work["translation"]["concurrency"] == k
        assert work["auto_notes"]["concurrency"] == max(1, k - 1)
        groups = [row["stages"] for row in result["schedule"]]
        assert ["node_review", "glossary_review"] in groups
        assert ["translation", "auto_notes"] in groups
        assert abs(sum(s["seconds"] for s in result["schedule"]) - times[k - 1]) < 1


def test_accelerated_api_uses_same_concurrency_policy_as_job_submission(tmp_path, monkeypatch):
    settings = settings_for(tmp_path, paperlens_vision_enabled=False)
    monkeypatch.setattr(estimates, "job_execution_concurrency", lambda _, accelerated, *, speed_mode: {
        "normal": 3, "fast": 5, "ultra": 16,
    }[speed_mode])
    assert estimates.estimate(settings)["concurrency"] == 3
    boosted = estimates.estimate(settings, accelerated=True)
    assert boosted["concurrency"] == 5 and boosted["accelerated"] is True
    ultra = estimates.estimate(settings, speed_mode="ultra")
    assert ultra["concurrency"] == 16 and ultra["speed_mode"] == "ultra"
    assert ultra["accelerated"] is True
    standard = estimates.estimate(settings, accelerated=True, speed_mode="normal")
    assert standard["concurrency"] == 3 and standard["accelerated"] is False
    assert estimates.estimate(settings, concurrency=2500)["concurrency"] == 16


def test_ultra_request_history_is_calibrated_at_its_actual_concurrency(tmp_path):
    settings = settings_for(tmp_path)
    sample = timing_sample(settings, concurrency=16, effective_reasoning_effort="provider_default")
    write_timing(settings, [sample, timing_sample(settings, concurrency=2500)])
    samples = estimates.history_samples(settings.paperlens_cache_dir)
    assert samples == [sample]
    work = Work("test", "reading", settings, 1, 2000, 1200, "default", concurrency=16)
    assert request_seconds(work, samples, "typical")[1] == 1
    work.concurrency = 5
    assert request_seconds(work, samples, "typical")[1] == 0


def test_economy_keeps_requested_translation_and_notes_work(tmp_path):
    settings = settings_for(tmp_path, paperlens_vision_enabled=False,
                            paperlens_math_enabled=False, llm_max_requests=200,
                            paperlens_auto_translate_enabled=True,
                            paperlens_auto_notes_enabled=True)
    tiers = by_id(estimates.estimate(settings, pages=12))
    for stage in ("translation", "auto_notes"):
        rows = [next(w for w in item["workload"] if w["stage"] == stage) for item in tiers.values()]
        assert len({w["requests"] for w in rows}) == 1
        assert len({w["completion_tokens"] for w in rows}) == 1


def test_vision_tiers_and_code_follow_actual_enabled_routes(tmp_path):
    settings = settings_for(
        tmp_path, llm_max_requests=120, llm_max_output_tokens=50000,
        llm_max_input_chars=30000, paperlens_vision_allow_unverified=True,
        paperlens_vision_max_pages=4, paperlens_transcription_max_regions=2,
    )
    features = estimates.DocumentFeatures(code_fraction=0.8, visual_pages_fraction=1,
                                          math_density=4)
    tiers = by_id(estimates.estimate(settings, pages=20, document_features=features))
    econ, detailed = tiers["economy"], tiers["detailed"]
    assert detailed["typical"]["completion_tokens"]["typical"] > econ["typical"]["completion_tokens"]["typical"]
    assert "native_reading" in {w["stage"] for w in detailed["workload"]}
    disabled = settings.model_copy(update={"paperlens_math_enabled": False, "paperlens_text_review_enabled": False})
    stages = by_id(estimates.estimate(disabled, pages=20))["standard"]["workload"]
    assert {w["stage"] for w in stages if w["role"] == "vision"} == {"native_reading"}


def test_optional_translation_is_language_sensitive_and_note_types_can_disable_calls(tmp_path):
    settings = settings_for(tmp_path, paperlens_auto_translate_enabled=True,
                            paperlens_auto_notes_enabled=True,
                            paperlens_auto_notes_summary=False,
                            paperlens_auto_notes_explain=False)
    features = estimates.DocumentFeatures(latin_fraction=0.1)
    stages = by_id(estimates.estimate(settings, pages=8, document_features=features))["standard"]["workload"]
    assert not {"translation", "auto_notes"} & {w["stage"] for w in stages}


def test_each_work_role_is_priced_using_its_actual_route(tmp_path):
    settings = settings_for(tmp_path, llm_max_requests=120)
    unknown = ModelRoute(account_id="other", model="some-model",
                         base_url="https://provider.example/v1")
    routed = settings.model_copy(update={"llm_routes": {"reading": unknown}})
    result = by_id(estimates.estimate(routed, pages=5))["standard"]
    cost = result["typical"]["cost"]["typical"]
    assert cost["status"] == "partial"
    assert cost["unpriced_models"] == ["some-model"]
    reading = next(w for w in result["workload"] if w["stage"] == "extraction")
    assert reading["model"] == "some-model"
    assert next(w for w in result["workload"] if w["stage"] == "synthesis")["model"] == settings.llm_model


def test_pdf_sampling_is_bounded_and_document_features_round_trip(tmp_path):
    path = tmp_path / "many.pdf"
    path.write_bytes(pdf_bytes(30))
    pages, features = estimates.inspect_pdf(path)
    assert pages == 30
    assert features.sampled_pages == 8
    assert 0 < features.chars_per_page < 30
    settings = settings_for(tmp_path)
    with TestClient(server.create_app(settings)) as client:
        first = client.post("/api/estimates", files={"file": ("p.pdf", path.read_bytes())}).json()
        second = client.post("/api/estimates", data={
            "pages": first["pages"], "document_features": json.dumps(first["document_features"]),
        }).json()
        assert first == second
        for invalid in ("not-json", '{"chars_per_page": -1}', '{"chars_per_page": NaN}', '{"text": "abc"}'):
            assert client.post("/api/estimates", data={"document_features": invalid}).status_code == 422


def test_estimate_endpoint_uses_saved_current_settings_without_model_calls(tmp_path):
    settings = settings_for(tmp_path, llm_max_requests=120)
    with TestClient(server.create_app(settings)) as client:
        before = client.post("/api/estimates", data={"pages": 6}).json()
        saved = client.put("/api/settings", json={"values": {
            "PAPERLENS_AUTO_TRANSLATE_ENABLED": True, "PAPERLENS_MAX_PARALLEL_JOBS": 2,
        }})
        assert saved.status_code == 200 and "errors" not in saved.json()
        after = client.post("/api/estimates", data={"pages": 6}).json()
    assert after["parallel_jobs"] == 2
    assert by_id(after)["standard"]["typical"]["seconds"]["typical"] > by_id(before)["standard"]["typical"]["seconds"]["typical"]
    assert settings.llm_api_key.get_secret_value() not in json.dumps(after)




def test_only_recent_timestamped_timing_is_accepted(tmp_path):
    settings = settings_for(tmp_path)
    stale = timing_sample(settings, recorded_at=(datetime.now(UTC) - timedelta(days=31)).isoformat())
    write_timing(settings, [stale, timing_sample(settings)])
    assert len(estimates.history_samples(settings.paperlens_cache_dir)) == 1


def test_run_cost_prices_each_model_and_flags_unpriced_ones(tmp_path):
    settings = settings_for(tmp_path)
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 500,
        "prompt_cache_hit_tokens": 200,
        "prompt_cache_miss_tokens": 800,
    }
    run = {
        "usage_by_provider": [
            {
                "model": "deepseek-flash",
                "provider_base_url": "https://api.deepseek.com",
                "usage": usage,
            },
            {
                "model": "deepseek-flash",
                "provider_base_url": "https://api.deepseek.com",
                "usage": usage,
            },
            {"model": "other", "provider_base_url": "https://openrouter.ai/api/v1", "usage": usage},
        ]
    }
    result = estimates.run_cost(run, settings)
    assert result["status"] == "partial"
    priced, unpriced = result["models"]
    # A known cache breakdown gives one exact figure per period.
    assert priced["status"] == "estimated" and priced["off_peak"][0] == priced["off_peak"][1]
    assert unpriced == {"model": "other", "status": "unknown"}
    assert result["off_peak"] == priced["off_peak"]
    single = estimates.run_cost(
        {
            "model": "deepseek-flash",
            "provider_base_url": "https://api.deepseek.com",
            "usage": usage,
        },
        settings,
    )
    assert single["off_peak"][0] * 2 == pytest.approx(result["off_peak"][0], abs=1e-4)
    assert estimates.run_cost({"usage": {"requests": 0}}, settings)["status"] == "none"


def test_run_cost_endpoint_needs_a_run(tmp_path):
    with TestClient(server.create_app(settings_for(tmp_path))) as client:
        assert client.post("/api/estimates/run", json={}).status_code == 422
        body = client.post("/api/estimates/run", json={"run": {"usage": {}}}).json()
        assert body["status"] == "none"


def test_reasoning_demand_uses_stage_and_effort_without_double_counting(tmp_path):
    from paperlens.estimate_timing import calibrate_usage

    settings = settings_for(tmp_path)
    rows = [timing_sample(settings, effective_reasoning_effort="low", task_stage=stage,
                          output_tokens=total, reasoning_tokens=total - 100)
            for stage, total in (("node_review", 1000), ("synthesis", 200)) for _ in range(3)]
    for stage, expected in (("node_review", 1000), ("synthesis", 200)):
        work = Work(stage, "verification", settings, 1, 1000, 200, "low",
                    visible_output_per_request=100, output_cap=4000)
        calibrate_usage(work, rows, "typical")
        assert work.output_per_request == expected
        assert work.usage_calibration == "recent_stage_reasoning"
        assert work.usage_samples == 3
    unrelated = Work("synthesis", "main", settings, 1, 1000, 100, "none",
                     visible_output_per_request=100, output_cap=4000)
    calibrate_usage(unrelated, rows, "typical")
    assert unrelated.output_per_request == 100 and unrelated.usage_samples == 0
    missing = Work("synthesis", "main", settings, 1, 1000, 200, "low",
                   visible_output_per_request=100, output_cap=4000)
    calibrate_usage(missing, [{k: v for k, v in row.items() if k != "reasoning_tokens"}
                              for row in rows], "typical")
    assert missing.output_per_request == 200
    assert missing.usage_calibration == "insufficient_reasoning_samples"


def test_retry_is_sequential_overhead_even_when_spare_workers_exist(tmp_path):
    from paperlens.estimate_timing import scheduled_seconds

    settings = settings_for(tmp_path)
    work = Work("synthesis", "main", settings, 1.2, 1000, 1000, "low", concurrency=16,
                retry_multiplier=1.2)
    assert scheduled_seconds(work, 120) == pytest.approx(120)
    work.requests = 4.8
    work.concurrency = 2
    assert scheduled_seconds(work, 480) == pytest.approx(240)


def test_shared_branch_schedule_respects_global_capacity_and_repair_chains(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    works = [Work(stage, "verification", settings, 3, 1000, 1000, "low", concurrency=4)
             for stage in ("node_review", "glossary_review", "node_repair", "node_recheck")]
    monkeypatch.setattr(estimates, "workload", lambda *args: (works, 0, False))
    monkeypatch.setattr(estimates, "request_seconds", lambda work, *_: (work.requests * 10, 0))
    result = estimates.estimate_profile(settings, "standard", 12, [], None, concurrency=4)
    groups = {tuple(row["stages"]): row["seconds"] for row in result["schedule"]}
    # Six independent review batches need two waves under four total permits.
    assert groups[("node_review", "glossary_review")] == 20
    # Each repair batch still waits for its own recheck despite spare workers.
    assert groups[("node_repair", "node_recheck")] == 20






def test_history_cutoff_does_not_admit_future_measurements(tmp_path):
    settings = settings_for(tmp_path)
    cutoff = datetime.now(UTC)
    row = timing_sample(settings, recorded_at=(cutoff + timedelta(seconds=1)).isoformat())
    write_timing(settings, [row])
    assert estimates.history_samples(settings.paperlens_cache_dir, as_of=cutoff) == []


def test_archived_import_preserves_time_usage_and_real_overlap_without_touching_input():
    importer = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools/import_request_telemetry.py"))
    rows = [{
        "started_at": f"2026-01-01T00:00:0{start}+00:00",
        "finished_at": f"2026-01-01T00:00:0{end}+00:00",
        "duration_seconds": end - start,
        "model_requested": "fixture", "reasoning_effort": "low", "stage": "node_review",
        "status": 200, "finish_reason": "stop",
        "usage": {"prompt_tokens": 100, "completion_tokens": 500,
                  "completion_tokens_details": {"reasoning_tokens": 400}},
    } for start, end in ((0, 3), (1, 2), (4, 5))]
    before = json.dumps(rows)
    result = importer["convert"](rows, endpoint="https://example.com", protocol="openai")
    assert json.dumps(rows) == before
    assert [row["concurrency"] for row in result] == [2, 2, 1]
    assert result[0]["recorded_at"] == rows[1]["finished_at"]
    assert result[0]["output_tokens"] == 500 and result[0]["visible_output_tokens"] == 100
    rows[0]["stage"] = "unknown"
    with pytest.raises(ValueError, match="audited stage"):
        importer["convert"](rows, endpoint="https://example.com", protocol="openai")
