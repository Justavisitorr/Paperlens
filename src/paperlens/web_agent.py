"""Outbound-only compute client for the original authenticated PaperLens application."""

from __future__ import annotations

import argparse
import asyncio
import base64
import getpass
import hashlib
import hmac
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .exporting import get_dashboard_template
from .storage import write_json
from .web_credentials import service_access, windows_access
from .web_service import ServiceConfig, initialize, service_app

CHUNK_SIZE = 2 * 1024 * 1024
REQUEST_ID = re.compile(r"\d{13}-[0-9a-f]{32}\Z")


def agent_token(config: ServiceConfig) -> str:
    return hmac.digest(config.gateway_key, b"paperlens-agent-control-v1", "sha256").hex()


def public_configuration(config: ServiceConfig) -> dict:
    private = serialization.load_pem_private_key(
        (config.root / "relay-private.pem").read_bytes(), password=None
    )
    public = private.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return {
        "PAPERLENS_AGENT_TOKEN_SHA256": hashlib.sha256(agent_token(config).encode()).hexdigest(),
        "PAPERLENS_AGENT_PUBLIC_KEY": base64.b64encode(public).decode(),
    }


class RequestEnded(Exception):
    pass


class ComputeAgent:
    def __init__(self, config: ServiceConfig, access: str):
        self.config = config
        self.app = service_app(config)
        self.private = serialization.load_pem_private_key(
            (config.root / "relay-private.pem").read_bytes(), password=None
        )
        self.headers = {
            "OAI-Sites-Authorization": "Bearer " + access,
            "X-PaperLens-Agent-Token": agent_token(config),
        }
        self.client = httpx.AsyncClient(
            base_url=config.origin, headers=self.headers, timeout=40, follow_redirects=False
        )
        self.state = "starting"

    def report(self, state: str, message: str) -> None:
        if state != self.state:
            print(message, flush=True)
        self.state = state
        write_json(
            self.config.root / "status.json",
            {
                "pid": os.getpid(),
                "state": state,
                "message": message,
                "site": self.config.origin,
                "connection": "private-outbound-https",
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )

    async def upload_template(self):
        template = get_dashboard_template().replace("__PAPERLENS_DATA__", "null").encode()
        sha = hashlib.sha256(template).hexdigest()
        current = await self.client.head("/_paperlens/host/template")
        if current.status_code == 200 and current.headers.get("x-template-sha") == sha:
            return
        response = await self.client.put(
            "/_paperlens/host/template",
            content=template,
            headers={"x-template-sha": sha, "content-type": "text/html"},
        )
        response.raise_for_status()
        if not response.json().get("saved"):
            raise RuntimeError("原版前端同步尚未完成。")

    async def execute(self, envelope: dict):
        ident = envelope.get("id", "")
        if not REQUEST_ID.fullmatch(ident) or abs(time.time() * 1000 - int(ident[:13])) > 70000:
            return
        key = self.private.decrypt(
            base64.b64decode(envelope["wrapped"]),
            padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        )
        meta = json.loads(
            AESGCM(key).decrypt(
                base64.b64decode(envelope["iv"]), base64.b64decode(envelope["cipher"]), None
            )
        )
        if meta.get("id") != ident or meta["method"] not in {
            "GET",
            "HEAD",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        }:
            return
        path = meta["path"]
        if not (
            path == "/"
            or path.startswith("/api/")
            or path in {"/_paperlens/ready", "/_paperlens/client.js"}
        ):
            return
        if "\\" in path or "//" in path or re.search(r"%2f|%5c|%00", path, re.I):
            return
        headers = {"x-paperlens-object-key": key.hex()}
        incoming = asyncio.Event()
        input_index = 0
        sent_inline = False
        outbox = asyncio.Queue(maxsize=4)
        response = {"status": 500, "headers": {}}
        streaming = False

        async def receive():
            nonlocal input_index, sent_inline
            if not sent_inline and meta["inline"]:
                sent_inline = True
                return {
                    "type": "http.request",
                    "body": base64.b64decode(meta["inline"]),
                    "more_body": False,
                }
            if input_index < meta["parts"]:
                result = await self.client.get(
                    f"/_paperlens/host/in/{ident}/{input_index}", headers=headers
                )
                result.raise_for_status()
                input_index += 1
                return {
                    "type": "http.request",
                    "body": result.content,
                    "more_body": input_index < meta["parts"],
                }
            if not sent_inline and meta["parts"] == 0:
                sent_inline = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await incoming.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            nonlocal streaming
            if message["type"] == "http.response.start":
                response["status"] = message["status"]
                response["headers"] = {
                    k.decode("latin1"): v.decode("latin1") for k, v in message.get("headers", [])
                }
                kind = response["headers"].get("content-type", "")
                streaming = "ndjson" in kind or "event-stream" in kind
                if streaming:
                    await outbox.put(b"")
            elif message["type"] == "http.response.body":
                body = message.get("body", b"")
                for offset in range(0, len(body), CHUNK_SIZE):
                    await outbox.put(body[offset : offset + CHUNK_SIZE])
                if not message.get("more_body", False):
                    await outbox.put(None)

        async def writer():
            index = 0
            buffer = bytearray()

            async def put(last=False):
                nonlocal index, buffer
                metadata = {**response, "end": last}
                safe_headers = {
                    k: v
                    for k, v in response["headers"].items()
                    if k
                    in {
                        "content-type",
                        "content-disposition",
                        "content-range",
                        "accept-ranges",
                        "cross-origin-resource-policy",
                    }
                }
                metadata["headers"] = safe_headers
                encoded = base64.b64encode(json.dumps(metadata).encode()).decode()
                for attempt in range(3):
                    try:
                        result = await self.client.put(
                            f"/_paperlens/host/out/{ident}/{index}",
                            content=bytes(buffer),
                            headers={**headers, "x-paperlens-response": encoded},
                        )
                        if result.status_code == 410:
                            raise RequestEnded()
                        result.raise_for_status()
                        break
                    except httpx.HTTPError:
                        if attempt == 2:
                            raise
                        await asyncio.sleep(0.5 * (attempt + 1))
                index += 1
                buffer = bytearray()

            while True:
                try:
                    item = await asyncio.wait_for(outbox.get(), timeout=0.7 if streaming else None)
                except TimeoutError:
                    if buffer:
                        await put()
                    continue
                if item is None:
                    await put(last=True)
                    return
                if item == b"" and streaming and index == 0:
                    await put()
                    continue
                if len(buffer) + len(item) > CHUNK_SIZE:
                    await put()
                buffer.extend(item)
                if len(buffer) >= CHUNK_SIZE:
                    await put()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": meta["method"],
            "scheme": "https",
            "path": unquote(path),
            "raw_path": path.encode(),
            "query_string": meta["query"].encode(),
            "root_path": "",
            "headers": [(k.encode(), v.encode("latin1")) for k, v in meta["headers"].items()],
            "client": ("127.0.0.1", 0),
            "server": ("paperlens.internal", 443),
            "paperlens.principal": meta["principal"].encode("ascii"),
        }
        application = asyncio.create_task(self.app(scope, receive, send))
        upload = asyncio.create_task(writer())
        try:
            done, _ = await asyncio.wait({application, upload}, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                task.result()
        finally:
            incoming.set()
            for task in (application, upload):
                if not task.done():
                    task.cancel()
            await asyncio.gather(application, upload, return_exceptions=True)

    async def run(self):
        stop = self.config.root / "stop.request"
        stop.unlink(missing_ok=True)
        active = set()
        last_pulse = 0.0
        template_ready = False
        errors = 0
        try:
            async with self.client, self.app.router.lifespan_context(self.app):
                self.report("connecting", "正在连接私有站点。")
                while not stop.exists():
                    try:
                        if not template_ready:
                            await self.upload_template()
                            template_ready = True
                        pulse = time.monotonic() - last_pulse > 20
                        result = await self.client.get(
                            "/_paperlens/host/poll",
                            params={"slots": 12 - len(active)},
                            headers={"x-paperlens-pulse": "1" if pulse else "0"},
                        )
                        result.raise_for_status()
                        if pulse:
                            last_pulse = time.monotonic()
                        jobs = result.json()["jobs"]
                        if self.state != "online":
                            self.report("online", "原版 PaperLens 已连接，按账户隔离运行。")
                        errors = 0
                        for job in jobs:
                            task = asyncio.create_task(self.execute(job))
                            active.add(task)
                        await asyncio.sleep(0.4 if jobs or active else 1.5)
                        for task in list(active):
                            if task.done():
                                active.remove(task)
                                try:
                                    task.result()
                                except RequestEnded:
                                    pass
                                except Exception as exc:
                                    print("Request failed:", type(exc).__name__, flush=True)
                    except (httpx.HTTPError, ValueError, KeyError, RuntimeError):
                        errors += 1
                        self.report("connecting", "站点连接暂未完成，正在重试。")
                        await asyncio.sleep(min(15, 2 + errors))
        finally:
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)
            self.report("stopped", "网页计算后端已停止。")
            stop.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="PaperLens 私有网页计算客户端")
    parser.add_argument("action", choices=["run", "credential", "public-config"])
    parser.add_argument("--site", default=os.getenv("PAPERLENS_SITE_ORIGIN"))
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--max-active", type=int, default=4)
    args = parser.parse_args()
    if not args.site:
        origin_file = args.data / "site-origin.txt"
        if not origin_file.is_file():
            raise SystemExit("首次启动请用 --site 指定 PaperLens 私有网址。")
        args.site = origin_file.read_text("utf-8").strip()
    if args.action == "credential":
        if os.name != "nt" or not sys.stdin.isatty():
            raise SystemExit("Windows 请在交互终端保存凭据，服务器请挂载服务凭据文件。")
        token = getpass.getpass("Sites service access (hidden): ").strip()
        if not token or any(c.isspace() for c in token):
            raise SystemExit("无效的服务访问凭据。")
        windows_access(args.site, token)
        print("Stored in Windows Credential Manager.")
        return
    initialize(args.data)
    allowed = args.data / "allowed-models.json"
    extra = json.loads(allowed.read_text("utf-8")) if allowed.is_file() else []
    config = ServiceConfig.load(args.data, args.site, extra, args.max_active)
    if args.action == "public-config":
        print(json.dumps(public_configuration(config)))
        return
    cache = Path(os.getenv("PAPERLENS_SHARED_CACHE", ".cache")).resolve()
    for name, relative in {
        "XDG_CACHE_HOME": "xdg",
        "PLAYWRIGHT_BROWSERS_PATH": "browsers",
    }.items():
        os.environ.setdefault(name, str(cache / relative))
    asyncio.run(ComputeAgent(config, service_access(config.origin)).run())


if __name__ == "__main__":
    main()
