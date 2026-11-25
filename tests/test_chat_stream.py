"""Stream transport and grounding tests using only local fake responses."""

import asyncio
import json
import threading

import httpx
import pytest
import test_conversation as conversation_fixtures

from paperlens import provider_stream
from paperlens.config import ModelRoute
from paperlens.conversation import ConversationService
from paperlens.errors import ProviderError
from paperlens.provider import JsonProvider
from paperlens.provider_stream import answer_prefix

# Keep the shared fixtures registered without importing names shadowed by test arguments.
reports = conversation_fixtures.reports
settings = conversation_fixtures.settings


class SplitStream(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data
        self.closed = False
        self.sent = 0

    async def __aiter__(self):
        # Split both JSON lines and UTF-8 multibyte sequences.
        for index in range(0, len(self.data), 3):
            self.sent += 1
            await asyncio.sleep(0)
            yield self.data[index : index + 3]

    async def aclose(self):
        self.closed = True


def sse(raw, *, finish="stop", done=True):
    events = [
        {"choices": [{"delta": {"content": raw[i : i + 5]}, "finish_reason": None}]}
        for i in range(0, len(raw), 5)
    ]
    events.append(
        {
            "choices": [{"delta": {}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 20},
        }
    )
    result = "".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events)
    return (result + ("data: [DONE]\n\n" if done else "")).encode()


def mock_stream(monkeypatch, raw, **kwargs):
    upstream = SplitStream(sse(raw, **kwargs))
    requests = []

    async def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, stream=upstream)

    real = provider_stream.stream_json

    def fake(provider, system, user, **options):
        return real(provider, system, user, transport=httpx.MockTransport(handle), **options)

    monkeypatch.setattr(provider_stream, "stream_json", fake)
    return upstream, requests


@pytest.mark.parametrize("text", ['中文😀原文\\路径\n下一行"引用"', "a\\", "a\\\\", "a\\u123"])
def test_incremental_answer_decoder_handles_escapes_and_surrogates(text):
    raw = json.dumps({"answer": text, "quotes": []}, ensure_ascii=True)
    seen = ""
    for index in range(1, len(raw) + 1):
        piece = answer_prefix(raw[:index])
        if piece is not None:
            assert text.startswith(piece)
            assert piece.startswith(seen)
            seen = piece
    assert seen == text


def test_stream_delivers_before_upstream_finishes_then_validates_sources(
    tmp_path, reports, settings, monkeypatch
):
    raw = json.dumps(
        {
            "answer": "模型解释中文内容，原文显示 BERT 双向预训练。[1]",
            "quotes": [
                {"paper": "P1", "block_id": "b1", "quote": "BERT 使用掩码语言模型进行双向预训练。"}
            ],
        },
        ensure_ascii=False,
    )
    upstream, requests = mock_stream(monkeypatch, raw)

    async def run():
        frames = []
        async for frame in ConversationService(tmp_path).stream(
            reports, settings, {"question": "?", "papers": ["bert"]}
        ):
            if frame["type"] == "text":
                assert upstream.sent < len(upstream.data) // 3
            frames.append(frame)
        return frames

    frames = asyncio.run(run())
    assert frames[0]["type"] == "text"
    assert frames[-1]["reply"]["grounded"]
    assert frames[-1]["reply"]["quotes"][0]["paper_id"] == "bert"
    assert frames[-1]["reply"]["usage"]["prompt_tokens"] == 40
    assert requests[0]["stream"] is True
    assert requests[0]["model"] == "reader-model"
    assert upstream.closed


@pytest.mark.parametrize(
    "quote,marker",
    [("完全编造且不在论文里面的句子", "[1]"), ("BERT 使用掩码语言模型进行双向预训练。", "[99]")],
)
def test_unmatched_source_or_marker_replaces_provisional_content_with_insufficient(
    tmp_path, reports, settings, monkeypatch, quote, marker
):
    raw = json.dumps(
        {
            "answer": "正在输出长一点的看似合理的解释。" * 3 + marker,
            "quotes": [{"paper": "P1", "block_id": "b1", "quote": quote}],
        },
        ensure_ascii=False,
    )
    mock_stream(monkeypatch, raw)

    async def run():
        return [
            frame
            async for frame in ConversationService(tmp_path).stream(
                reports, settings, {"question": "?", "papers": ["bert"]}
            )
        ]

    frames = asyncio.run(run())
    assert any(frame["type"] == "text" for frame in frames)
    assert not frames[-1]["reply"]["grounded"]
    assert frames[-1]["reply"]["quotes"] == []
    assert "合理的解释" not in frames[-1]["reply"]["answer"]


def test_cancel_closes_upstream_before_complete(tmp_path, reports, settings, monkeypatch):
    raw = json.dumps(
        {"answer": "这是需要逐字生成和核实来源的很长回答。" * 10, "quotes": []}, ensure_ascii=False
    )
    upstream, requests = mock_stream(monkeypatch, raw)

    async def run():
        frames = ConversationService(tmp_path).stream(
            reports, settings, {"question": "?", "papers": ["bert"]}
        )
        assert (await anext(frames))["type"] == "text"
        await frames.aclose()

    asyncio.run(run())
    assert upstream.closed
    assert upstream.sent < len(upstream.data) // 3
    assert len(requests) == 1


@pytest.mark.parametrize("finish,done", [("length", True), ("stop", False)])
def test_truncated_stream_has_no_validated_reply(
    tmp_path, reports, settings, monkeypatch, finish, done
):
    upstream, requests = mock_stream(
        monkeypatch, '{"answer":"这是足够长的未完成回答。' * 3, finish=finish, done=done
    )

    async def run():
        return [
            frame
            async for frame in ConversationService(tmp_path).stream(
                reports, settings, {"question": "?", "papers": ["bert"]}
            )
        ]

    with pytest.raises(ProviderError):
        asyncio.run(run())
    assert upstream.closed and len(requests) == 1


def test_secret_split_between_tokens_never_appears_in_provisional_or_final(
    tmp_path, reports, settings, monkeypatch
):
    secret = settings.llm_api_key.get_secret_value()
    raw = json.dumps(
        {
            "answer": "前文足够长足够长足够长。" * 3 + secret + "后文。[1]",
            "quotes": [
                {"paper": "P1", "block_id": "b1", "quote": "BERT 使用掩码语言模型进行双向预训练。"}
            ],
        },
        ensure_ascii=False,
    )
    mock_stream(monkeypatch, raw)

    async def run():
        return [
            frame
            async for frame in ConversationService(tmp_path).stream(
                reports, settings, {"question": "?", "papers": ["bert"]}
            )
        ]

    frames = asyncio.run(run())
    assert secret not in json.dumps(frames)
    assert "[已隐藏凭据]" in frames[-1]["reply"]["answer"]


@pytest.mark.parametrize("role", ["chat", "conversation"])
def test_chat_route_is_resolved_before_upstream_request(
    tmp_path, reports, settings, monkeypatch, role
):
    raw = json.dumps(
        {
            "answer": "BERT 双向预训练。[1]",
            "quotes": [
                {"paper": "P1", "block_id": "b1", "quote": "BERT 使用掩码语言模型进行双向预训练。"}
            ],
        },
        ensure_ascii=False,
    )
    _, requests = mock_stream(monkeypatch, raw)
    routed = settings.model_copy(
        update={
            "llm_routes": {
                role: ModelRoute(
                    account_id="assigned",
                    model="assigned-chat",
                    base_url="https://api.openai.com/v1",
                    api_key="route-key",
                )
            }
        }
    )

    async def run():
        return [
            frame
            async for frame in ConversationService(tmp_path).stream(
                reports, routed, {"question": "?", "papers": ["bert"], "model_role": role}
            )
        ]

    frames = asyncio.run(run())
    assert requests[0]["model"] == "assigned-chat"
    assert frames[-1]["reply"]["model"] == "assigned-chat"
    assert requests[0]["max_completion_tokens"] == settings.llm_max_output_tokens


def test_http_stream_validates_before_headers_and_returns_verified_final(
    tmp_path,
    reports,
    settings,
    monkeypatch,
):
    from fastapi.testclient import TestClient

    from paperlens.server import create_app
    from paperlens.storage import write_json

    configured = settings.model_copy(
        update={
            "paperlens_output_dir": tmp_path / "outputs",
            "paperlens_catalog_network": False,
        }
    )
    write_json(
        configured.paperlens_output_dir / "bert/report.json",
        reports["bert"].model_dump(mode="json"),
    )
    raw = json.dumps(
        {
            "answer": "BERT 双向预训练。[1]",
            "quotes": [
                {"paper": "P1", "block_id": "b1", "quote": reports["bert"].document.blocks[0].text}
            ],
        }
    )
    upstream, requests = mock_stream(monkeypatch, raw)
    with TestClient(create_app(configured)) as client:
        paper_id = client.get("/api/library").json()["papers"][0]["id"]
        invalid = [
            ({"question": " ", "papers": [paper_id]}, 422),
            ({"question": "?", "papers": []}, 422),
            ({"question": "?", "papers": [paper_id, "missing"]}, 404),
            (
                {
                    "question": "?",
                    "papers": [paper_id],
                    "passages": [
                        {
                            "paper_id": paper_id,
                            "block_id": "b1",
                            "start": 0,
                            "end": 4,
                            "text": "假的引用",
                        }
                    ],
                },
                422,
            ),
        ]
        for body, status in invalid:
            response = client.post("/api/chat/stream", json=body)
            assert response.status_code == status
            assert response.headers["content-type"].startswith("application/json")
        assert requests == []
        response = client.post("/api/chat/stream", json={"question": "?", "papers": [paper_id]})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")
        assert response.headers["cache-control"] == "no-store"
        frames = [json.loads(line) for line in response.iter_lines()]
        assert frames[-1]["type"] == "done"
        assert frames[-1]["reply"]["grounded"]
        assert frames[-1]["reply"]["quotes"][0]["paper_id"] == paper_id
        assert frames[-1]["reply"]["usage"]["completion_tokens"] == 20
    assert len(requests) == 1 and upstream.closed


def test_http_stream_failure_emits_error_without_done(tmp_path, reports, settings, monkeypatch):
    from fastapi.testclient import TestClient

    from paperlens.server import create_app
    from paperlens.storage import write_json

    configured = settings.model_copy(
        update={
            "paperlens_output_dir": tmp_path / "outputs",
            "paperlens_catalog_network": False,
        }
    )
    write_json(
        configured.paperlens_output_dir / "bert/report.json",
        reports["bert"].model_dump(mode="json"),
    )
    upstream, requests = mock_stream(
        monkeypatch, '{"answer":"' + "生成中的暂定文字。" * 10, finish="length"
    )
    with TestClient(create_app(configured)) as client:
        paper_id = client.get("/api/library").json()["papers"][0]["id"]
        response = client.post("/api/chat/stream", json={"question": "?", "papers": [paper_id]})
        frames = [json.loads(line) for line in response.iter_lines()]
        assert any(frame["type"] == "text" for frame in frames)
        assert frames[-1]["type"] == "error"
        assert "输出上限" in frames[-1]["message"]
        assert f"{settings.llm_max_output_tokens:,}" in frames[-1]["message"]
        assert not any(frame["type"] == "done" for frame in frames)
        assert configured.llm_api_key.get_secret_value() not in response.text
    assert upstream.closed and len(requests) == 1


def test_http_stream_auth_error_is_actionable_and_hides_all_route_keys(
    tmp_path, reports, settings, monkeypatch
):
    from fastapi.testclient import TestClient

    from paperlens.server import create_app
    from paperlens.storage import write_json

    configured = settings.model_copy(update={"paperlens_output_dir": tmp_path / "outputs"})
    write_json(
        configured.paperlens_output_dir / "bert/report.json",
        reports["bert"].model_dump(mode="json"),
    )

    async def rejected(self, reports, settings, payload):
        secret = settings.for_role("conversation").llm_api_key.get_secret_value()
        raise ProviderError("API 鉴权失败，请检查所选账户的 API Key。" + secret)
        yield  # async generator

    monkeypatch.setattr(ConversationService, "stream", rejected)
    with TestClient(create_app(configured)) as client:
        account = client.post(
            "/api/connections/accounts",
            json={"provider_id": "openrouter", "api_key": "separate-conversation-secret"},
        )
        assert account.status_code == 201
        account_id = account.json()["account"]["id"]
        assert (
            client.put(
                "/api/connections/routing",
                json={
                    "routing": {"conversation": {"account_id": account_id, "model": "test-model"}}
                },
            ).status_code
            == 200
        )
        paper_id = client.get("/api/library").json()["papers"][0]["id"]
        response = client.post(
            "/api/chat/stream",
            json={"question": "?", "papers": [paper_id], "model_role": "conversation"},
        )
        frames = [json.loads(line) for line in response.iter_lines()]
        assert frames[-1]["type"] == "error" and "鉴权失败" in frames[-1]["message"]
        assert "separate-conversation-secret" not in response.text


def test_anthropic_stream_records_cache_usage_and_payload(settings):
    configured = settings.model_copy(update={"llm_protocol": "anthropic"})
    events = [
        {
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 30,
                    "cache_creation_input_tokens": 5,
                }
            },
        },
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "中文😀"}},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 8},
        },
        {"type": "message_stop"},
    ]
    upstream = SplitStream("".join("data: " + json.dumps(e) + "\n\n" for e in events).encode())
    requests = []

    async def handle(request):
        requests.append(json.loads(request.content))
        assert request.url.path.endswith("/messages")
        return httpx.Response(200, stream=upstream)

    async def run(provider):
        return [
            chunk
            async for chunk in provider_stream.stream_json(
                provider, "system", "user", transport=httpx.MockTransport(handle)
            )
        ]

    with JsonProvider(configured) as provider:
        assert asyncio.run(run(provider)) == ["中文😀"]
        assert provider.usage.prompt_tokens == 45
        assert provider.usage.completion_tokens == 8
        assert provider.usage.prompt_cache_hit_tokens == 30
        assert provider.usage.prompt_cache_miss_tokens == 15
        assert provider._events[-1]["outcome"] == "complete"
    assert "system" in requests[0] and requests[0]["stream"]
    assert "stream_options" not in requests[0] and upstream.closed


def test_cancel_records_one_request_and_releases_network_slot(settings, monkeypatch):
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(provider_stream, "_NETWORK_SLOTS", slots)
    upstream = SplitStream(sse('{"answer":"' + "生成内容。" * 20))

    async def handle(request):
        return httpx.Response(200, stream=upstream)

    async def run(provider):
        chunks = provider_stream.stream_json(
            provider, "s", "u", transport=httpx.MockTransport(handle)
        )
        await anext(chunks)
        await chunks.aclose()

    with JsonProvider(settings) as provider:
        asyncio.run(run(provider))
        assert provider.usage.requests == 1
        assert provider._events[-1]["outcome"] == "cancelled"
        assert provider._events[-1]["usage_reported"] is False
    assert upstream.closed
    assert slots.acquire(blocking=False)
    assert not slots.acquire(blocking=False)
    slots.release()
