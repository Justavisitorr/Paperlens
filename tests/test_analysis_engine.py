"""Check orchestration invariants with a deterministic transport substitute."""

import json

import pytest

from paperlens.analysis.engine import SectionNotes, _structured, analyze_document
from paperlens.analysis.evidence import EvidenceRegistry
from paperlens.analysis.repair import introduces_source_absence, repair_context
from paperlens.config import Settings
from paperlens.errors import ValidationError
from paperlens.models import Block, Box, Page, PaperDocument, PaperNode
from paperlens.storage import StageStore


def test_invalid_model_structure_retains_diagnostics_without_raw_inputs(tmp_path):
    class InvalidProvider:
        calls = 0

        def json(self, *args, **kwargs):
            self.calls += 1
            return {"section_summary": {"sensitive": "private-paper-content"}, "findings": []}

    provider = InvalidProvider()
    store = StageStore(tmp_path, "case")
    with pytest.raises(ValidationError, match="结构校验"):
        _structured(provider, store, "section", SectionNotes, "test", {"blocks": []})
    assert provider.calls == 3
    diagnostics = list(store.root.glob("section-validation-*.json"))
    assert len(diagnostics) == 3
    for path in diagnostics:
        raw = path.read_text(encoding="utf-8")
        assert "private-paper-content" not in raw
        assert json.loads(raw)["errors"][0]["field"] == "section_summary"


def document():
    passage = "The proposed method improves classification accuracy on this benchmark."
    return PaperDocument(
        title="Test paper",
        source_name="test.pdf",
        sha256="test",
        parser="fixture",
        pages=[Page(number=1, width=600, height=800)],
        blocks=[
            Block(
                id="b1",
                page=1,
                text=passage,
                raw_text=passage,
                order=0,
                bbox=Box(x0=0.1, y0=0.1, x1=0.9, y1=0.3),
            ),
        ],
    )


class ScriptedProvider:
    def __init__(self):
        self.calls = 0

    def json(self, system, user, **kwargs):
        self.calls += 1
        payload = json.loads(user)
        if "blocks" in payload:
            return {
                "section_summary": "有条件的分类提升",
                "findings": [
                    {
                        "title": "分类改进",
                        "kind": "contribution",
                        "summary": "在该基准改善分类准确率",
                        "explanation": "比较范围为文中基准",
                        "origin": "author",
                        "evidence": [{"block_id": "b1", "quote": payload["blocks"][0]["text"]}],
                    }
                ],
            }
        if "schema" in payload:
            eid = payload["sections"][0]["findings"][0]["evidence_ids"][0]
            return {
                "title": "分类改进",
                "summary": "在该基准改善分类准确率",
                "research_question": "能否改善分类",
                "nodes": [
                    {
                        "id": "root",
                        "title": "分类改进",
                        "kind": "theme",
                        "summary": "在该基准改善分类准确率",
                        "evidence_ids": [eid, "EFAKE"],
                        "support": "supported",
                    }
                ],
                "edges": [],
            }
        return {
            "reviews": [
                {"node_id": n["node_id"], "support": "supported", "reason": "条件一致"}
                for n in payload["nodes"]
            ]
        }


def test_synthesis_receives_validated_quotes_once_without_extra_requests(tmp_path):
    class InspectingProvider(ScriptedProvider):
        synthesis_payload = None

        def json(self, system, user, **kwargs):
            payload = json.loads(user)
            if "schema" in payload:
                self.synthesis_payload = payload
            result = super().json(system, user, **kwargs)
            if "blocks" in payload:
                # Multiple findings may cite the same passage; a fabricated
                # extra quote must not enter the synthesis evidence catalog.
                result["findings"].append(result["findings"][0].copy())
                result["findings"][0]["evidence"].append(
                    {"block_id": "b1", "quote": "This invented experimental result is absent."}
                )
            return result

    provider = InspectingProvider()
    result = analyze_document(document(), provider, Settings(_env_file=None, paperlens_cache_dir=tmp_path))
    assert provider.calls == 3
    assert provider.synthesis_payload["evidence"] == [
        {"id": result.evidence[0].id, "page": 1, "quote": document().blocks[0].text}
    ]


def test_full_analysis_filters_invented_evidence_and_resumes_without_api(tmp_path):
    settings = Settings(_env_file=None, paperlens_cache_dir=tmp_path)
    first_provider = ScriptedProvider()
    result = analyze_document(document(), first_provider, settings)
    assert first_provider.calls == 3
    assert len(result.evidence) == 1
    assert result.analysis.nodes[0].evidence_ids == [result.evidence[0].id]
    assert result.analysis.nodes[0].support == "supported"
    assert result.audit["covered_block_ids"] == ["b1"]
    assert any("未知证据" in warning for warning in result.analysis.warnings)
    next_provider = ScriptedProvider()
    renamed = document()
    renamed.source_name = "uploaded-under-a-new-uuid.pdf"
    resumed = analyze_document(renamed, next_provider, settings)
    assert next_provider.calls == 0
    assert resumed.analysis == result.analysis
    assert resumed.audit["cache_hits"] >= 3


def test_changed_document_content_does_not_reuse_old_analysis(tmp_path):
    settings = Settings(_env_file=None, paperlens_cache_dir=tmp_path)
    analyze_document(document(), ScriptedProvider(), settings)
    changed = document()
    changed.blocks[0].text = changed.blocks[0].raw_text = (
        "This separate experiment measures different outcomes."
    )
    provider = ScriptedProvider()
    result = analyze_document(changed, provider, settings)
    assert provider.calls == 3
    assert result.evidence[0].quote == changed.blocks[0].text


def test_repair_keeps_existing_sources_when_adding_a_missing_qualification(tmp_path):
    doc = document()
    qualifier = "The evaluation was conducted only on the development split."
    doc.blocks.append(
        Block(
            id="b2",
            page=1,
            text=qualifier,
            raw_text=qualifier,
            order=1,
            bbox=Box(x0=0.1, y0=0.4, x1=0.9, y1=0.6),
        )
    )

    class RepairProvider(ScriptedProvider):
        def json(self, system, user, **kwargs):
            payload = json.loads(user)
            if "nodes" in payload and "issue" in payload["nodes"][0]:
                return {
                    "nodes": [
                        {
                            "node_id": "root",
                            "title": "开发集上的分类改进",
                            "summary": "该方法在开发集上改善分类准确率",
                            "explanation": "结论限于已测试的开发集",
                            "origin": "author",
                            "evidence": [{"block_id": "b2", "quote": qualifier}],
                        }
                    ]
                }
            if "nodes" in payload:
                sources = payload["nodes"][0]["sources"]
                complete = any(qualifier == source["quote"] for source in sources)
                if complete:
                    assert any("proposed method" in source["quote"] for source in sources)
                    assert payload["nodes"][0]["title"] == "开发集上的分类改进"
                return {
                    "reviews": [
                        {
                            "node_id": "root",
                            "support": "supported" if complete else "partial",
                            "reason": "依据完整" if complete else "需要开发集限定",
                        }
                    ]
                }
            return super().json(system, user, **kwargs)

    result = analyze_document(
        doc, RepairProvider(), Settings(_env_file=None, paperlens_cache_dir=tmp_path)
    )
    assert result.analysis.nodes[0].support == "supported"
    assert result.analysis.nodes[0].title == "开发集上的分类改进"
    assert {e.block_id for e in result.evidence} == {"b1", "b2"}
    assert result.audit["initial_reviews"][0]["support"] == "partial"


@pytest.mark.parametrize("before,after,reject", [
    ("方法在该数据集上有效。", "原文未说明模型版本。", True),
    ("方法在该数据集上有效。", "示例标题未标注Dev/Test。", True),
    ("原文未定义A。", "原文未说明B。", True),
    ("原文未定义A。", "原文未定义A。作者没有提供划分说明。", True),
    ("Original result", "The paper does not specify the version.", True),
    ("Original result", "The version was not defined in the paper.", True),
    ("Original result", "The paper does not explicitly define the version.", True),
    ("原文未定义A。", "原文未定义A。该方法未提高准确率。", False),
    ("错误的正向结论", "该方法在独立测试中未提高准确率。", False),
    ("错误的正向结论", "论文报告该模型未提供更高准确率。", False),
    ("提高10%", "准确率从70%变为80%，提高10个百分点。", False),
    ("提高10%", "差值为10，口径无法判断，保留相对分母的不确定性。", False),
])
def test_source_absence_guard_checks_new_assertions_without_suppressing_negative_results(
    before, after, reject
):
    assert introduces_source_absence(before, after) is reject


@pytest.mark.parametrize("before,after,accepted", [
    ("方法改善准确率。", "原文未说明模型版本。", False),
    ("原文未定义指标A。", "原文未标注模型版本。", False),
    ("方法改善准确率。", "该方法在独立测试中未提高准确率。", True),
    ("方法改善准确率。", "论文报告该模型未提供更高准确率。", True),
    ("准确率提高10%。", "准确率从70%变为80%，提高10个百分点。", True),
    ("准确率提高10%。", "差值为10，口径无法判断，保留相对分母的不确定性。", True),
])
def test_repair_rejects_new_source_absence_but_accepts_grounded_negative_or_numeric_corrections(
    tmp_path, before, after, accepted
):
    doc = document()
    source = "Accuracy was 70% for baseline and 80% for method. No gain appeared on independent tests."
    doc.blocks.append(doc.blocks[0].model_copy(update={"id": "b2", "text": source,
                                                     "raw_text": source, "order": 1}))

    class RepairProvider(ScriptedProvider):
        reviewed = 0

        def json(self, system, user, **kwargs):
            payload = json.loads(user)
            if "schema" in payload:
                result = super().json(system, user, **kwargs)
                result["nodes"][0]["summary"] = before
                return result
            if "nodes" in payload and "issue" in payload["nodes"][0]:
                return {"nodes": [{"node_id": "root", "summary": after,
                    "explanation": "结论限定于已测试的条件。", "origin": "author",
                    "evidence": [{"block_id": "b2", "quote": source}]}]}
            if "nodes" in payload:
                self.reviewed += 1
                assert [block["block_id"] for block in payload["context_blocks"]] == ["b1", "b2"]
                assert payload["nodes"][0]["context_block_ids"] == ["b1", "b2"]
                return {"reviews": [{"node_id": "root", "support": (
                    "partial" if self.reviewed == 1 else "supported"), "reason": "核对条件与数值"}]}
            return super().json(system, user, **kwargs)

    result = analyze_document(doc, RepairProvider(), Settings(_env_file=None,
                                                             paperlens_cache_dir=tmp_path))
    assert result.analysis.nodes[0].summary == (after if accepted else before)
    assert bool(result.audit["repairs"]) is accepted
    assert bool(result.audit["repair_rejections"]) is not accepted
    assert {e.block_id for e in result.evidence} == ({"b1", "b2"} if accepted else {"b1"})


def test_related_chinese_claim_bridges_to_distant_english_definition_and_neighbors():
    doc = document()
    doc.pages.append(Page(number=2, width=600, height=800))
    for index, text in enumerate([
        "POS_VERB is a universal part-of-speech tag.",
        "A verb describes an action, state, or occurrence.",
    ], 2):
        doc.blocks.append(doc.blocks[0].model_copy(update={
            "id": f"b{index}", "page": 2, "order": index,
            "text": text, "raw_text": text,
        }))
    registry = EvidenceRegistry(doc)
    primary = registry.resolve("b1", doc.blocks[0].text, {"b1"})
    distant = registry.resolve("b2", doc.blocks[1].text, {"b2"})
    node = PaperNode(id="n1", title="词性标签编码", kind="concept",
                     summary="通过词性标签理解序列表示", evidence_ids=[primary])
    related = PaperNode(id="n2", title="词性标签的定义", kind="concept",
                        summary="词性标签描述词语的语法类别", evidence_ids=[distant])
    assert "b2" not in {block["block_id"] for block in repair_context(node, registry, doc)}
    context = repair_context(node, registry, doc, related_nodes=[node, related])
    assert [block["block_id"] for block in context] == ["b1", "b2", "b3"]
