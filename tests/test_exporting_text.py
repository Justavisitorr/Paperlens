from test_reading_projection import b, report

from paperlens.exporting import analysis_markdown
from paperlens.models import Evidence, PaperNode


def test_analysis_export_preserves_multiline_evidence_and_warning_layout():
    block = b("b1", "first\r\n\r\n    second\rthird")
    sample = report([block])
    sample.analysis.nodes = [
        PaperNode(id="claim", title="Claim", kind="theme", summary="", evidence_ids=["e1"])
    ]
    sample.evidence = [
        Evidence(id="e1", block_id=block.id, page=1, quote=block.text, bbox=block.bbox)
    ]
    sample.quality.warnings = ["first warning\r\n\r\ncontinued warning"]
    before = sample.model_dump()
    text = analysis_markdown(sample)
    assert "> first\n> \n>     second\n> third" in text
    assert "- first warning\n  \n  continued warning" in text
    assert sample.model_dump() == before
