"""Definition review orchestration, using synthetic source blocks.

Scripted reviewers only test evidence boundaries and flow. Their decisions are
not measurements of an actual language model's semantic accuracy.
"""

import json
from pathlib import Path

import pytest

from paperlens.analysis.engine import analyze_document
from paperlens.analysis.evidence import EvidenceRegistry
from paperlens.analysis.glossary import repair_glossary, review_glossary
from paperlens.config import Settings
from paperlens.models import Block, GlossaryTerm, Page, PaperAnalysis, PaperDocument, PaperNode

CASE = json.loads(
    (Path(__file__).parent / "fixtures/sparsity_definition_conflict.json").read_text(
        encoding="utf-8"
    )
)


def example():
    document = PaperDocument(
        title="向量稀疏度示例",
        source_name="sparsity-regression.pdf",
        sha256=CASE["provenance"]["pdf_sha256"],
        parser="regression-excerpt",
        pages=[Page(number=i, width=600, height=800) for i in range(1, 5)],
        blocks=[Block.model_validate(block) for block in CASE["source_blocks"]],
    )
    registry = EvidenceRegistry(document)
    ids = [registry.resolve(block.id, block.text, {block.id}) for block in document.blocks]
    node = PaperNode.model_validate(CASE["correct_node"])
    node.evidence_ids = ids
    node.parent_id = None
    analysis = PaperAnalysis(
        title=document.title,
        summary="指标定义的回归用例",
        research_question="如何衡量稀疏程度",
        nodes=[node],
        edges=[],
        glossary=[
            GlossaryTerm(term="稀疏度", explanation=CASE["wrong_explanation"], evidence_ids=ids[:1])
        ],
    )
    return document, registry, analysis


def judgment(support="supported", *, issues=None):
    return {
        "term_id": "t0001",
        "support": support,
        "reason": "核对本文的分子、分母及变化方向",
        "node_issues": issues or [],
    }


def test_real_definition_context_and_complete_node_explanation_reach_reviewer():
    document, registry, analysis = example()

    def request(stage, model, system, payload):
        term = payload["terms"][0]
        assert len(payload["other_terms"]) == len(analysis.glossary)
        assert term["explanation"] == CASE["wrong_explanation"]
        # The term cites the broken formula alone. Node evidence and neighbors
        # must retrieve the author-specific definition on the same page.
        assert any("零元素个数" in block["text"] for block in term["blocks"])
        assert payload["nodes"][0]["explanation"] == CASE["correct_node"]["explanation"]
        return model.model_validate({"reviews": [judgment("partial")]})

    result = review_glossary(analysis, registry, document, request, stage="test")
    assert analysis.glossary[0].support == "partial"
    assert result.reviews[0]["term"] == "稀疏度"
    assert result.node_issues == []  # The correct metric node need not be rewritten.


@pytest.mark.parametrize("count", [0, 2])
def test_missing_or_duplicate_term_judgments_are_unverified(count):
    document, registry, analysis = example()

    def request(stage, model, system, payload):
        return model.model_validate({"reviews": [judgment()] * count})

    review_glossary(analysis, registry, document, request, stage="test")
    assert analysis.glossary[0].support == "unverified"
    assert "缺失或重复" in analysis.glossary[0].review_notes[0]


def test_model_cannot_certify_a_term_without_matched_evidence():
    document, registry, analysis = example()
    analysis.glossary[0].evidence_ids = []

    def request(stage, model, system, payload):
        return model.model_validate({"reviews": [judgment()]})

    review_glossary(analysis, registry, document, request, stage="test")
    assert analysis.glossary[0].support == "unverified"
    assert any("缺少可匹配原文引用" in n for n in analysis.glossary[0].review_notes)


@pytest.mark.parametrize("valid_quote", [True, False])
def test_definition_repair_requires_a_real_quote_and_preserves_old_sources(valid_quote):
    document, registry, analysis = example()
    term = analysis.glossary[0]
    old_ids = term.evidence_ids[:]
    definition = document.blocks[1]

    def request(stage, model, system, payload):
        return model.model_validate(
            {
                "terms": [
                    {
                        "term_id": "t0001",
                        "explanation": CASE["expected_explanation"],
                        "evidence": [
                            {
                                "block_id": definition.id,
                                "quote": definition.text if valid_quote else "原文不存在这段新定义",
                            }
                        ],
                    }
                ]
            }
        )

    records = repair_glossary(analysis, registry, document, request)
    if valid_quote:
        assert term.explanation == CASE["expected_explanation"]
        assert set(old_ids) < set(term.evidence_ids)
        assert records[0]["before"]["explanation"] == CASE["wrong_explanation"]
        assert term.support == "unverified"  # Only a later review can certify it.
    else:
        assert term.explanation == CASE["wrong_explanation"]
        assert term.evidence_ids == old_ids
        assert records == []


class ScriptedDefinitionProvider:
    def __init__(self, *, persistent_conflict=False):
        self.calls = 0
        self.persistent_conflict = persistent_conflict
        self.supplemental = None

    def json(self, system, user, **kwargs):
        self.calls += 1
        payload = json.loads(user)
        if "blocks" in payload:
            return {
                "section_summary": "本文指标有明确零元素定义",
                "findings": [
                    {
                        "title": "稀疏度定义",
                        "kind": "concept",
                        "summary": "本文 nz 表示零元素数",
                        "evidence": [
                            {"block_id": b["block_id"], "quote": b["text"]}
                            for b in payload["blocks"]
                        ],
                    }
                ],
            }
        if "schema" in payload:
            self.supplemental = payload.get("supplemental_context")
            _, _, analysis = example()
            return analysis.model_dump()
        if "terms" in payload:
            term = payload["terms"][0]
            if "issues" in term:
                source = CASE["source_blocks"][1]
                return {
                    "terms": [
                        {
                            "term_id": term["term_id"],
                            "explanation": CASE["expected_explanation"],
                            "evidence": [{"block_id": source["id"], "quote": source["text"]}],
                        }
                    ]
                }
            support = "partial" if term["explanation"] == CASE["wrong_explanation"] else "supported"
            issues = (
                [{"node_id": "metrics", "support": "partial", "reason": "定义方向仍需核查"}]
                if self.persistent_conflict
                else []
            )
            return {"reviews": [judgment(support, issues=issues)]}
        if "issue" in payload["nodes"][0]:
            # Leave the conflict unresolved to exercise the bounded final pass.
            return {"nodes": []}
        return {
            "reviews": [
                {"node_id": "metrics", "support": "supported", "reason": "节点原文定义一致"}
            ]
        }


def test_full_pipeline_repairs_only_wrong_term_and_resumes_from_cache(tmp_path):
    document, _, _ = example()
    settings = Settings(_env_file=None, paperlens_cache_dir=tmp_path)
    provider = ScriptedDefinitionProvider()
    result = analyze_document(document, provider, settings)
    term = result.analysis.glossary[0]
    assert term.explanation == CASE["expected_explanation"]
    assert term.support == "supported"
    assert result.analysis.nodes[0].summary == CASE["correct_node"]["summary"]
    assert result.analysis.nodes[0].support == "supported"
    assert result.audit["initial_glossary_reviews"][0]["support"] == "partial"
    assert len(result.audit["glossary_repairs"]) == 1
    assert result.audit["repairs"] == []
    assert provider.calls == 6
    resumed_provider = ScriptedDefinitionProvider()
    resumed = analyze_document(document, resumed_provider, settings)
    assert resumed_provider.calls == 0
    assert resumed.analysis == result.analysis


def test_final_cross_node_conflict_is_visible_even_if_node_review_says_supported(tmp_path):
    document, _, _ = example()
    result = analyze_document(
        document,
        ScriptedDefinitionProvider(persistent_conflict=True),
        Settings(_env_file=None, paperlens_cache_dir=tmp_path),
    )
    assert result.analysis.nodes[0].support == "partial"
    assert any("定义方向仍需核查" in w for w in result.analysis.warnings)
    assert result.audit["definition_conflicts"]["initial"]
    assert result.audit["definition_conflicts"]["final"]


def test_supplemental_visual_context_reaches_synthesis_and_changes_cache_key(tmp_path):
    document, _, _ = example()
    settings = Settings(_env_file=None, paperlens_cache_dir=tmp_path)
    first = analyze_document(document, ScriptedDefinitionProvider(), settings)
    provider = ScriptedDefinitionProvider()
    supplemental = {"visuals": [{"page": 4, "description": "图内曲线数值尚未确认"}]}
    second = analyze_document(document, provider, settings, supplemental_context=supplemental)
    assert provider.supplemental == supplemental
    assert second.audit["cache_key"] != first.audit["cache_key"]
    assert provider.calls == 6


def test_unchanged_review_inputs_do_not_make_a_second_glossary_call(tmp_path):
    class AlreadyCorrectProvider(ScriptedDefinitionProvider):
        def json(self, system, user, **kwargs):
            result = super().json(system, user, **kwargs)
            if "schema" in json.loads(user):
                result["glossary"][0]["explanation"] = CASE["expected_explanation"]
            return result

    document, _, _ = example()
    provider = AlreadyCorrectProvider()
    result = analyze_document(
        document, provider, Settings(_env_file=None, paperlens_cache_dir=tmp_path)
    )
    assert result.audit["glossary_final_review_reused"] is True
    assert result.audit["glossary_reviews"] == result.audit["initial_glossary_reviews"]
    assert provider.calls == 4
