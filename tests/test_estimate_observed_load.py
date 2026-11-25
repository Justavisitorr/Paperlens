"""Observed API load depends on ready independent requests, not spare capacity."""

import pytest

from paperlens.config import Settings
from paperlens.estimate_timing import request_seconds, scheduled_seconds
from paperlens.estimate_workload import Work


def row(settings, stage, load, seconds):
    return {
        "endpoint": settings.llm_base_url, "model": settings.llm_model,
        "protocol": settings.llm_protocol, "effective_reasoning_effort": "none",
        "input_tokens": 2000, "output_tokens": 1200, "duration_seconds": seconds,
        "concurrency": load, "task_stage": stage,
    }


@pytest.mark.parametrize("stage", [
    "extraction", "reading_edition", "math_review", "glossary_repair", "glossary_recheck",
])
def test_independent_batch_matches_real_ready_load_and_sequential_retries(tmp_path, stage):
    settings = Settings(_env_file=None, llm_base_url="https://api.deepseek.com",
                        paperlens_cache_dir=tmp_path)
    work = Work(stage, "reading", settings, 2.4, 2000, 1200, "none", concurrency=16,
                retry_multiplier=1.2)
    samples = [row(settings, stage, 2, 9) for _ in range(3)]
    # An unrelated stage actually using all sixteen slots cannot replace the
    # exact ready-load history of the current independent two-task batch.
    samples += [row(settings, "node_review", 16, 100) for _ in range(3)]
    service, count = request_seconds(work, samples, "typical")
    assert count == 3 and service == pytest.approx(21.6)
    assert scheduled_seconds(work, service) == pytest.approx(10.8)


@pytest.mark.parametrize("stage", [
    "node_review", "glossary_review", "translation", "auto_notes", "node_repair", "node_recheck",
])
def test_paired_branches_keep_global_peak_load_definition(tmp_path, stage):
    settings = Settings(_env_file=None, llm_base_url="https://api.deepseek.com",
                        paperlens_cache_dir=tmp_path)
    work = Work(stage, "verification", settings, 2, 2000, 1200, "none", concurrency=16)
    samples = [row(settings, stage, 2, 9) for _ in range(3)]
    samples += [row(settings, stage, 1, 7)]
    service, count = request_seconds(work, samples, "typical")
    # Two jobs in this branch do not imply a global load of two while a sibling
    # branch can also be active. Only the existing serial fallback is valid.
    assert count == 1 and service == pytest.approx(14)
