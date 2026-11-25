"""Reading aids must stay bounded, cached and separate from evidence offsets."""

import pytest
from test_reading_projection import b, report

from paperlens.reading import prepare_reading
from paperlens.transcription import parse_transcription


def test_mixed_math_transcription_preserves_algorithm_newlines():
    value = parse_transcription(
        {
            "status": "recognized",
            "segments": [
                {"text": "输入 "},
                {"latex": r"\lambda_1, \lambda_2"},
                {"text": "\n1) for k=1:num_epoch\n8) end for"},
            ],
        }
    )
    assert value.segments[1].formula.status == "renderable"
    assert not value.verified
    assert "\n8)" in value.segments[-1].text
    with pytest.raises(ValueError):
        parse_transcription({"status": "recognized", "segments": [{"text": "x", "latex": "x"}]})
    assert parse_transcription({"status": "unreadable"}) is None


def test_literal_inline_index_not_removed_and_projection_preserves_transcript():
    value = report([b("body", "For i=1 the value remains in the source. v i ∈ R p")])
    prepare_reading(value)
    item = value.reading.items[0]
    item.transcription = parse_transcription(
        {"status": "recognized", "segments": [{"text": "For i=1"}]}
    )
    prepare_reading(value)
    assert value.document.blocks[0].text.startswith("For i=1")
    assert value.reading.items[0].transcription.segments[0].text == "For i=1"


def test_algorithm_lines_keep_numbering_and_reject_prose_as_math():
    result = parse_transcription(
        {
            "status": "recognized",
            "lines": [
                [{"text": "1) for "}, {"latex": "k=1"}, {"text": ":num_epoch"}],
                [{"text": "2) end for"}],
            ],
        }
    )
    assert "\n2) end for" in "".join(s.text for s in result.segments)
    with pytest.raises(ValueError, match="Prose"):
        parse_transcription({"status": "recognized", "segments": [{"latex": "取出样本 x_i"}]})
