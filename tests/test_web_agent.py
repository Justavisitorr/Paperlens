"""Exercise the in-process app and encrypted relay without external services."""

import asyncio
import base64
import json
import secrets
import time

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from paperlens.web_agent import ComputeAgent
from paperlens.web_service import ServiceConfig, initialize


def envelope(agent, path="/api/health"):
    ident = str(int(time.time() * 1000)) + "-" + secrets.token_hex(16)
    key, iv = secrets.token_bytes(32), secrets.token_bytes(12)
    meta = {
        "id": ident,
        "principal": "alice",
        "method": "GET",
        "path": path,
        "query": "",
        "headers": {},
        "parts": 0,
        "inline": "",
        "size": 0,
    }
    wrapped = agent.private.public_key().encrypt(
        key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
    )
    def encode(data):
        return base64.b64encode(data).decode()
    return {
        "id": ident,
        "iv": encode(iv),
        "wrapped": encode(wrapped),
        "cipher": encode(AESGCM(key).encrypt(iv, json.dumps(meta).encode(), None)),
    }


@pytest.mark.asyncio
async def test_original_app_response_survives_a_retried_output_upload(tmp_path):
    initialize(tmp_path)
    agent = ComputeAgent(ServiceConfig.load(tmp_path, "https://research.example"), "test-only")
    await agent.client.aclose()
    uploads = []
    attempts = 0

    async def transport(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503)
        uploads.append(
            (
                request.url.path,
                request.content,
                json.loads(base64.b64decode(request.headers["x-paperlens-response"])),
            )
        )
        return httpx.Response(200, json={"stored": True})

    agent.client = httpx.AsyncClient(
        base_url=agent.config.origin, transport=httpx.MockTransport(transport)
    )
    async with agent.client, agent.app.router.lifespan_context(agent.app):
        await asyncio.wait_for(agent.execute(envelope(agent)), 6)
    assert attempts == 2
    assert len(uploads) == 1
    assert uploads[0][2]["status"] == 200
    assert uploads[0][2]["end"] is True
    from paperlens import __version__
    assert json.loads(uploads[0][1])["version"] == __version__


@pytest.mark.asyncio
async def test_streaming_chunks_arrive_before_the_request_finishes(tmp_path):
    initialize(tmp_path)
    agent = ComputeAgent(ServiceConfig.load(tmp_path, "https://research.example"), "test-only")
    await agent.client.aclose()
    uploads = []
    finished = False

    async def transport(request):
        uploads.append(
            (
                finished,
                request.content,
                json.loads(base64.b64decode(request.headers["x-paperlens-response"])),
            )
        )
        return httpx.Response(200, json={"stored": True})

    async def stream(scope, receive, send):
        nonlocal finished
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/x-ndjson")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"part":1}\n', "more_body": True})
        await asyncio.sleep(0.9)
        finished = True
        await send({"type": "http.response.body", "body": b'{"part":2}\n'})

    agent.client = httpx.AsyncClient(
        base_url=agent.config.origin, transport=httpx.MockTransport(transport)
    )
    agent.app = stream
    async with agent.client:
        await asyncio.wait_for(agent.execute(envelope(agent)), 5)
    assert any(not after and content for after, content, _meta in uploads)
    assert b"".join(item[1] for item in uploads) == b'{"part":1}\n{"part":2}\n'
    assert uploads[-1][2]["end"]
