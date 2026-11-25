"""Authenticated multi-user host for the original PaperLens application."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import secrets
import tempfile
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, replace
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Mount
from starlette.types import Message, Receive, Scope, Send

from . import __version__
from .config import Settings
from .server import create_app
from .web_policy import WorkspacePolicy, approved_endpoints


class TenantSettings(Settings):
    """A visitor may use only their saved settings and keys, never the host's .env."""

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        return (init_settings,)


@dataclass(frozen=True)
class ServiceConfig:
    root: Path
    origin: str
    gateway_key: bytes
    vault_key: bytes
    endpoints: frozenset[str]
    max_active: int

    @classmethod
    def load(cls, root: Path, origin: str, extra_endpoints=(), max_active=4):
        url = urlsplit(origin)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
        ):
            raise ValueError("请填写网站的完整 HTTPS 来源地址。")
        if not 2 <= max_active <= 32:
            raise ValueError("活跃工作区上限需在 2 到 32 之间。")
        root = root.resolve()
        gateway = (root / "gateway.key").read_bytes().strip()
        vault = base64.urlsafe_b64decode((root / "vault.key").read_bytes().strip())
        if len(gateway) < 32 or len(vault) != 32:
            raise ValueError("服务端密钥文件无效。")
        return cls(
            root,
            origin.rstrip("/"),
            gateway,
            vault,
            approved_endpoints(tuple(extra_endpoints)),
            max_active,
        )


def initialize(root: Path) -> None:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in ("gateway.key", "vault.key"):
        path = root / name
        if path.exists():
            continue
        if (root / "workspaces").exists():
            raise RuntimeError("已有工作区数据，请从备份恢复缺少的密钥。")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(base64.urlsafe_b64encode(secrets.token_bytes(32)))
            handle.flush()
            os.fsync(handle.fileno())
    private = root / "relay-private.pem"
    if not private.exists():
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        data = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        fd = os.open(private, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)


@contextmanager
def single_writer(root: Path):
    """Each original job queue has one owner, including during rolling restarts."""
    with (root / "service.lock").open("a+b") as handle:
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("此数据目录已有 PaperLens 网页服务运行。") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass
class Workspace:
    app: FastAPI
    lifespan: object
    requests: int = 0
    last_used: float = 0.0

    def idle(self) -> bool:
        jobs = self.app.state.jobs.health()
        return self.requests == 0 and not jobs["queued_jobs"] and not jobs["running_jobs"]


@lru_cache(maxsize=1)
def browser_adapter() -> str:
    assets = files("paperlens").joinpath("assets", "web")
    library = assets.joinpath("tweetnacl-1.0.3.min.js").read_text("utf-8")
    return library + "\n;" + assets.joinpath("workspace.js").read_text("utf-8")


class WorkspaceHost:
    def __init__(self, config: ServiceConfig, *, global_slots: threading.BoundedSemaphore | None = None,
                 idle_cache: int | None = None, cloud_sync: bool = False, template_provider=None):
        self.config = config
        self.global_slots = global_slots
        self.idle_cache = idle_cache or config.max_active
        self.cloud_sync = cloud_sync
        self.template_provider = template_provider
        self.transfers: set[str] = set()
        self.workspaces: dict[str, Workspace] = {}
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(self, _app):
        with single_writer(self.config.root):
            yield
            for workspace in self.workspaces.values():
                await workspace.lifespan.__aexit__(None, None, None)
            self.workspaces.clear()

    @asynccontextmanager
    async def acquire(self, policy: WorkspacePolicy) -> AsyncIterator[FastAPI]:
        async with self.lock:
            if policy.workspace_id in self.transfers:
                raise HTTPException(409, "资料正在导出，请稍后重试。")
            workspace = self.workspaces.get(policy.workspace_id)
            if workspace is None:
                if len(self.workspaces) >= self.idle_cache:
                    idle = [
                        (obj.last_used, key) for key, obj in self.workspaces.items() if obj.idle()
                    ]
                    if not idle and len(self.workspaces) >= self.config.max_active:
                        raise HTTPException(
                            503, "当前处理容量已满，请稍后再试。", headers={"Retry-After": "10"}
                        )
                    if idle:
                        _, oldest = min(idle)
                        await self.workspaces.pop(oldest).lifespan.__aexit__(None, None, None)
                root = self.config.root / "workspaces" / policy.workspace_id
                settings = TenantSettings(
                    paperlens_output_dir=root / "outputs",
                    paperlens_cache_dir=root / "cache",
                    paperlens_max_upload_mb=policy.upload_mb,
                    paperlens_max_parallel_jobs=policy.parallel_jobs,
                )
                app = create_app(settings, web_policy=policy, global_slots=self.global_slots,
                                 template_provider=self.template_provider)
                life = app.router.lifespan_context(app)
                await life.__aenter__()
                workspace = self.workspaces[policy.workspace_id] = Workspace(app, life)
            workspace.requests += 1
        try:
            yield workspace.app
        finally:
            workspace.requests -= 1
            workspace.last_used = time.monotonic()

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            return
        headers = dict(scope["headers"])
        path = scope["path"]
        # This ASGI extension is written by the authenticated outbound agent.
        # HTTP headers, paths and request payloads can never supply an identity.
        principal = scope.get("paperlens.principal")
        if not isinstance(principal, bytes):
            return await JSONResponse({"detail": "需要通过已授权的 PaperLens 网站访问。"}, 403)(
                scope, receive, send
            )
        if path == "/_paperlens/ready" and scope["method"] in {"GET", "HEAD"}:
            return await JSONResponse(
                {"service": "PaperLens", "version": __version__, "status": "ready"},
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)
        if not principal or len(principal) > 256 or any(b < 33 or b > 126 for b in principal):
            return await JSONResponse({"detail": "请先登录自己的研究账户。"}, 401)(
                scope, receive, send
            )
        policy = WorkspacePolicy.for_user(
            principal, self.config.origin, self.config.endpoints, self.config.vault_key
        )
        policy = replace(policy, cloud_sync=self.cloud_sync)
        if headers.get(b"sec-fetch-site") == b"cross-site" and not (
            scope["method"] == "GET"
            and headers.get(b"sec-fetch-mode") == b"navigate"
            and path == "/"
        ):
            return await JSONResponse({"detail": "请在 PaperLens 页面内操作。"}, 403)(
                scope, receive, send
            )
        browser_origin = headers.get(b"origin")
        if browser_origin and browser_origin != self.config.origin.encode():
            return await JSONResponse({"detail": "页面来源无效。"}, 403)(scope, receive, send)
        bound = headers.get(b"x-paperlens-workspace")
        write = scope["method"] not in {"GET", "HEAD", "OPTIONS"}
        if (bound is not None or write) and bound != policy.workspace_id.encode():
            return await JSONResponse({"detail": "登录账户已变化，请刷新页面后重试。"}, 409)(
                scope, receive, send
            )
        if not self.cloud_sync and path in {"/_paperlens/migration", "/api/account-export"}:
            return await self.migration(policy, scope, receive, send)
        if path == "/_paperlens/client.js" and scope["method"] == "GET":
            query = parse_qs(scope.get("query_string", b""))
            if query.get(b"workspace") != [policy.workspace_id.encode()]:
                return await JSONResponse({"detail": "账户已变化，请刷新页面。"}, 409)(
                    scope, receive, send
                )
            script = (
                browser_adapter()
                .replace("__WORKSPACE_ID__", policy.workspace_id)
                .replace("__BROWSER_KEY__", policy.browser_key)
            )
            if self.cloud_sync:
                script += "\n;" + files("paperlens").joinpath("assets/web/cloud-sync.js").read_text("utf-8")
            response = Response(
                script,
                media_type="text/javascript",
                headers={
                    "Cache-Control": "private, no-store",
                    "Cross-Origin-Resource-Policy": "same-origin",
                    "X-Content-Type-Options": "nosniff",
                },
            )
            return await response(scope, receive, send)

        origin = urlsplit(self.config.origin)
        internal_headers = [
            (key, value)
            for key, value in scope["headers"]
            if key
            not in {
                b"host",
                b"authorization",
                b"cookie",
                b"x-paperlens-signature",
                b"x-paperlens-principal",
                b"x-paperlens-time",
                b"x-paperlens-nonce",
            }
        ]
        internal_headers.append((b"host", origin.netloc.encode()))
        internal = {
            **scope,
            "scheme": "https",
            "server": (origin.hostname, origin.port or 443),
            "headers": internal_headers,
        }
        # Limit the streamed body before the multipart parser creates temporary files.
        limit = (
            policy.upload_mb * 1024**2 + 65536
            if path in {"/api/jobs", "/api/estimates"}
            else 8 * 1024**2
        )
        try:
            size = int(headers.get(b"content-length", b"0"))
        except ValueError:
            size = -1
        if size < 0 or size > limit:
            return await JSONResponse({"detail": "请求超过服务器大小上限。"}, 413)(
                scope, receive, send
            )
        received = 0

        async def bounded_receive() -> Message:
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > limit:
                raise HTTPException(413, "请求超过服务器大小上限。")
            return message

        async with self.acquire(policy) as app:
            await app(internal, bounded_receive, send)

    async def migration(self, policy, scope, receive, send):
        """Export only the workspace of the identity authenticated by the existing gateway."""
        if scope["path"] == "/_paperlens/migration" and scope["method"] == "GET":
            html = files("paperlens").joinpath("assets/web/migration.html").read_text("utf-8")
            html = html.replace("__WORKSPACE_ID__", policy.workspace_id)
            return await HTMLResponse(html, headers={"Cache-Control": "private, no-store"})(scope, receive, send)
        if scope["path"] != "/api/account-export" or scope["method"] != "POST":
            return await JSONResponse({"detail": "不支持的迁移操作。"}, 405)(scope, receive, send)
        from .account_migration import export_bundle

        temporary = None
        marked = False
        try:
            body = bytearray()
            async for chunk in Request(scope, receive).stream():
                body.extend(chunk)
                if len(body) > 12 * 1024**2:
                    raise HTTPException(413, "浏览器资料过大。")
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError("Invalid request")
            async with self.lock:
                if self.transfers:
                    raise HTTPException(503, "已有资料包正在准备，请稍后再试。")
                workspace = self.workspaces.get(policy.workspace_id)
                if workspace and not workspace.idle():
                    raise HTTPException(409, "请先暂停论文处理、关闭其他操作，再导出。")
                self.transfers.add(policy.workspace_id)
                marked = True
                if workspace:
                    await workspace.lifespan.__aexit__(None, None, None)
                    del self.workspaces[policy.workspace_id]
            folder = self.config.root / "transfers"
            folder.mkdir(mode=0o700, exist_ok=True)
            temporary = tempfile.TemporaryDirectory(dir=folder)
            destination = Path(temporary.name) / "workspace.plbackup"
            await run_in_threadpool(export_bundle, self.config.root / "workspaces" / policy.workspace_id,
                                    policy, data.get("browser", {}), data.get("password"), destination)
            response = FileResponse(destination, filename="paperlens-workspace.plbackup",
                                    background=BackgroundTask(temporary.cleanup))
        except (ValueError, OSError):
            if temporary:
                temporary.cleanup()
            response = JSONResponse({"detail": "无法导出，请检查密码、资料格式和磁盘空间。"}, 422)
        except HTTPException as exc:
            if temporary:
                temporary.cleanup()
            response = JSONResponse({"detail": exc.detail}, exc.status_code)
        finally:
            if marked:
                self.transfers.discard(policy.workspace_id)
        await response(scope, receive, send)


def service_app(config: ServiceConfig) -> Starlette:
    host = WorkspaceHost(config)
    return Starlette(routes=[Mount("/", app=host)], lifespan=host.lifespan)


def main() -> None:
    parser = argparse.ArgumentParser(description="PaperLens 多用户网页后端")
    parser.add_argument("command", choices=["init"])
    parser.add_argument("--data", type=Path, required=True)
    args = parser.parse_args()
    initialize(args.data)


if __name__ == "__main__":
    main()
