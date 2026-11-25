"""Independent PaperLens login and hosting; no Sites or OpenAI account required."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import secrets
import tempfile
import threading
import zipfile
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

from authlib.integrations.httpx_client import AsyncOAuth2Client
from cryptography.fernet import InvalidToken
from fastapi import FastAPI, HTTPException, Request
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount

from .account_migration import MAX_ARCHIVE, export_bundle, import_bundle, recover_migrations
from .account_store import SYNC_KEYS, SYNC_PREFIXES, AccountStore, email_address
from .standalone_config import StandaloneConfig, send_code
from .web_policy import WorkspacePolicy
from .web_service import WorkspaceHost


class Standalone:
    def __init__(self, config: StandaloneConfig, *, oauth_transport=None, mail_sender=send_code):
        self.config = config
        self.service = config.service()
        self.store = AccountStore(config.root / "accounts.sqlite3", self.service.vault_key, max_users=config.max_users)
        development = Path(__file__).resolve().parents[2] / "frontend/dist/index.html"
        self.host = WorkspaceHost(self.service, global_slots=threading.BoundedSemaphore(config.processing_slots),
                                  idle_cache=8, cloud_sync=True,
                                  template_provider=(lambda: development.read_text("utf-8")) if development.is_file() else None)
        self.oauth_transport, self.mail_sender = oauth_transport, mail_sender
        self.session_cookie = "__Host-paperlens-session" if config.secure else "paperlens-session"
        self.browser_cookie = "__Host-paperlens-browser" if config.secure else "paperlens-browser"
        self.maintenance: set[str] = set()
        self.active: dict[str, int] = {}
        self.migration_slot = asyncio.Lock()

    def policy(self, user_id: str):
        return WorkspacePolicy.for_user(("account/" + user_id).encode(), self.config.origin,
                                        self.service.endpoints, self.service.vault_key)

    def cookie(self, response, name: str, value: str, age: int):
        response.set_cookie(name, value, max_age=age, httponly=True, secure=self.config.secure,
                            samesite="lax", path="/")

    def csrf(self, token: str):
        return self.store.digest("csrf/" + token)

    def user(self, request: Request):
        return self.store.user(request.cookies.get(self.session_cookie))

    def required(self, request: Request):
        user = self.user(request)
        if user is None:
            raise HTTPException(401, "请先登录自己的 PaperLens 账户。")
        return user

    def check_write(self, request: Request, *, authenticated=True):
        if (request.headers.get("origin") != self.config.origin
                or request.headers.get("sec-fetch-site") == "cross-site"):
            raise HTTPException(403, "请从 PaperLens 页面内提交。")
        if authenticated:
            self.required(request)
            expected = self.csrf(request.cookies.get(self.session_cookie, ""))
            if not hmac.compare_digest(request.headers.get("x-paperlens-csrf", ""), expected):
                raise HTTPException(403, "登录验证已变化，请刷新页面。")

    def bound_user(self, request: Request):
        user = self.required(request)
        if request.headers.get("x-paperlens-workspace") != self.policy(user["id"]).workspace_id:
            raise HTTPException(409, "账户已变化，请刷新页面。")
        return user

    async def payload(self, request: Request, limit=8 * 1024**2):
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPException(415, "请使用页面提供的表单提交。")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > limit:
                raise HTTPException(413, "提交内容过大。")
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise HTTPException(400, "提交内容无效。") from exc
        if not isinstance(data, dict):
            raise HTTPException(422, "提交内容应为对象。")
        return data

    def logged_in(self, request, user_id):
        old = request.cookies.get(self.session_cookie)
        if old:
            self.store.logout(old)
        response = JSONResponse({"ok": True, "next": "/"})
        self.cookie(response, self.session_cookie, self.store.session(user_id), 7 * 86400)
        return response

    def page(self, request: Request, mode: str):
        user = self.user(request)
        if mode == "login" and user:
            return RedirectResponse("/", status_code=303)
        if mode == "account" and not user:
            return RedirectResponse("/login", status_code=303)
        browser = request.cookies.get(self.browser_cookie) or secrets.token_urlsafe(32)
        bootstrap = {"page": mode, "github": bool(self.config.github_id),
                     "email": self.config.email_enabled, "test_mail": self.config.dev_mail}
        template = files("paperlens").joinpath("assets/web/account.html").read_text("utf-8")
        template = template.replace("__ACCOUNT_BOOT__", json.dumps(bootstrap).replace("<", "\\u003c"))
        script = (f'<script src="/_paperlens/client.js?workspace={self.policy(user["id"]).workspace_id}"></script>' if user else "")
        response = HTMLResponse(template.replace("__WORKSPACE_BOOT__", script))
        self.cookie(response, self.browser_cookie, browser, 86400)
        return response

    async def serve_workspace(self, scope, receive, send):
        request = Request(scope, receive=receive)
        user = self.user(request)
        if not user:
            response = (RedirectResponse("/login", status_code=303) if scope["path"] == "/"
                        else JSONResponse({"detail": "登录已过期，请重新登录。"}, 401))
            return await response(scope, receive, send)
        try:
            if scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
                self.check_write(request)
            uid = user["id"]
            if uid in self.maintenance:
                raise HTTPException(409, "账户正在导入或导出资料，请稍后刷新。")
            self.active[uid] = self.active.get(uid, 0) + 1
            try:
                await self.host({**scope, "paperlens.principal": ("account/" + uid).encode()}, receive, send)
            finally:
                self.active[uid] -= 1
        except HTTPException as exc:
            await JSONResponse({"detail": exc.detail}, exc.status_code, headers=exc.headers)(scope, receive, send)

    @asynccontextmanager
    async def quiescent(self, user_id: str):
        if self.migration_slot.locked():
            raise HTTPException(503, "服务器正在准备一份资料包，请稍后再试。", headers={"Retry-After":"30"})
        await self.migration_slot.acquire()
        try:
            self.store.limit("migration/" + user_id, 6, 3600)
            self.maintenance.add(user_id)
            for _ in range(100):
                if not self.active.get(user_id):
                    break
                await asyncio.sleep(0.1)
            else:
                raise HTTPException(409, "请等待当前操作完成后再迁移资料。")
            async with self.host.lock:
                workspace_id = self.policy(user_id).workspace_id
                workspace = self.host.workspaces.get(workspace_id)
                if workspace:
                    jobs = workspace.app.state.jobs.health()
                    if jobs["queued_jobs"] or jobs["running_jobs"]:
                        raise HTTPException(409, "请先暂停或完成论文处理，再迁移资料。")
                    await workspace.lifespan.__aexit__(None, None, None)
                    del self.host.workspaces[workspace_id]
            yield
        finally:
            self.maintenance.discard(user_id)
            self.migration_slot.release()


def create_standalone(config: StandaloneConfig, *, oauth_transport=None, mail_sender=send_code) -> FastAPI:
    service = Standalone(config, oauth_transport=oauth_transport, mail_sender=mail_sender)

    @asynccontextmanager
    async def lifespan(app):
        async with service.host.lifespan(app):
            recover_migrations(config.root, service.store)
            yield

    app = FastAPI(title="PaperLens", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.standalone = service
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlsplit(config.origin).hostname])

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update({"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "same-origin", "X-Frame-Options": "DENY",
                                 "Content-Security-Policy": "frame-ancestors 'none'; object-src 'none'; base-uri 'self'"})
        if config.secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.get("/login")
    def login(request: Request):
        return service.page(request, "login")

    @app.get("/account")
    def account(request: Request):
        return service.page(request, "account")

    @app.get("/auth/assets/{name}")
    def asset(name: str):
        if name not in {"account.js", "account.css"}:
            raise HTTPException(404)
        return Response(files("paperlens").joinpath("assets/web", name).read_text("utf-8"),
                        media_type="text/css" if name.endswith("css") else "text/javascript")

    @app.get("/auth/session")
    def session(request: Request):
        user = service.required(request)
        return {"user": user, "identities": service.store.identities(user["id"]),
                "workspace": service.policy(user["id"]).workspace_id,
                "csrf": service.csrf(request.cookies[service.session_cookie]),
                "sync_keys": sorted(SYNC_KEYS), "sync_prefixes": SYNC_PREFIXES}

    @app.post("/auth/email/request")
    async def email_request(request: Request):
        service.check_write(request, authenticated=False)
        if not config.email_enabled:
            raise HTTPException(503, "邮箱登录尚未配置，请使用其他已启用的登录方式。")
        data = await service.payload(request, 4096)
        if not isinstance(data.get("email"), str):
            raise HTTPException(422, "请填写邮箱地址。")
        email = email_address(data["email"])
        browser = request.cookies.get(service.browser_cookie, "")
        if not browser or len(browser) > 128:
            raise HTTPException(400, "请刷新登录页后重试。")
        user = service.user(request)
        if user:
            service.check_write(request)
        peer = request.client.host if request.client else "unknown"
        service.store.limit("mail-ip/" + service.store.digest(peer), 20, 3600)
        service.store.limit("mail-email/" + service.store.digest(email), 5, 3600)
        service.store.limit("mail-cooldown/" + service.store.digest(email), 1, 60)
        ident, code = service.store.challenge(email, browser, user["id"] if user else None)
        try:
            await run_in_threadpool(service.mail_sender, config, ident, email, code)
        except Exception as exc:
            # SMTP exceptions can include account credentials or mail contents.
            raise HTTPException(503, "验证码暂时无法发送，请稍后重试或联系管理员检查邮件配置。") from exc
        return {"challenge": ident, "expires_in": 600}

    @app.post("/auth/email/verify")
    async def email_verify(request: Request):
        service.check_write(request, authenticated=False)
        data = await service.payload(request, 4096)
        ident, code = data.get("challenge", ""), data.get("code", "")
        if not isinstance(ident, str) or len(ident) > 64 or not isinstance(code, str) or len(code) != 6:
            raise HTTPException(400, "验证码格式不正确。")
        user = service.user(request)
        if user:
            service.check_write(request)
        uid = service.store.verify(ident, code, request.cookies.get(service.browser_cookie, ""), user["id"] if user else None)
        return service.logged_in(request, uid)

    @app.post("/auth/logout")
    def logout(request: Request):
        service.check_write(request)
        service.store.logout(request.cookies[service.session_cookie])
        response = JSONResponse({"ok": True})
        response.delete_cookie(service.session_cookie, path="/", secure=config.secure, httponly=True, samesite="lax")
        return response

    def github_client(**kwargs):
        return AsyncOAuth2Client(config.github_id, config.github_secret, scope="read:user", timeout=20,
                                 redirect_uri=config.origin + "/auth/github/callback", code_challenge_method="S256",
                                 transport=service.oauth_transport, **kwargs)

    @app.post("/auth/github/start")
    async def github_start(request: Request):
        service.check_write(request, authenticated=False)
        if not config.github_id:
            raise HTTPException(503, "GitHub 登录尚未配置。")
        browser = request.cookies.get(service.browser_cookie, "")
        if not browser or len(browser) > 128:
            raise HTTPException(400, "请刷新登录页后重试。")
        user = service.user(request)
        if user:
            service.check_write(request)
        peer = request.client.host if request.client else "unknown"
        service.store.limit("oauth/" + service.store.digest(peer), 30, 3600)
        verifier = secrets.token_urlsafe(48)
        state = service.store.oauth_start(browser, verifier, user["id"] if user else None)
        async with github_client() as client:
            uri, _ = client.create_authorization_url("https://github.com/login/oauth/authorize", state=state,
                                                      code_verifier=verifier)
        return {"url": uri}

    @app.get("/auth/github/callback")
    async def github_callback(request: Request):
        if not config.github_id:
            raise HTTPException(503, "GitHub 登录尚未配置。")
        state, code = request.query_params.get("state", ""), request.query_params.get("code", "")
        if not state or not code or len(state) > 128 or len(code) > 512:
            raise HTTPException(400, "GitHub 授权未完成，请返回登录页重试。")
        user = service.user(request)
        saved = service.store.oauth_finish(state, request.cookies.get(service.browser_cookie, ""), user["id"] if user else None)
        try:
            async with github_client(state=state) as client:
                await client.fetch_token("https://github.com/login/oauth/access_token", code=code,
                                         code_verifier=saved["verifier"], headers={"Accept": "application/json"})
                reply = await client.get("https://api.github.com/user", headers={"Accept": "application/vnd.github+json"})
                reply.raise_for_status()
                profile = reply.json()
            if type(profile.get("id")) is not int or profile["id"] <= 0:
                raise ValueError("Missing immutable GitHub identity")
        except Exception as exc:
            raise HTTPException(502, "GitHub 登录暂时无法完成，请重新登录或改用邮箱。") from exc
        uid = service.store.identity("github", str(profile["id"]), str(profile.get("login", "GitHub")), saved["user_id"])
        response = service.logged_in(request, uid)
        response.status_code = 303
        response.headers["Location"] = "/account" if saved["user_id"] else "/"
        return response

    @app.get("/auth/data")
    def read_data(request: Request):
        user = service.bound_user(request)
        return {"items": service.store.snapshot(user["id"], service.policy(user["id"]))}

    @app.post("/auth/data")
    async def sync_data(request: Request):
        service.check_write(request)
        user = service.bound_user(request)
        if user["id"] in service.maintenance:
            raise HTTPException(409, "正在迁移资料，请稍后再同步。")
        data = await service.payload(request)
        return service.store.sync(user["id"], data.get("changes"), service.policy(user["id"]))

    @app.post("/auth/migration/export")
    async def migration_export(request: Request):
        service.check_write(request)
        user = service.bound_user(request)
        data = await service.payload(request, 4096)
        policy = service.policy(user["id"])
        folder = config.root / "transfers"
        folder.mkdir(mode=0o700, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=folder)
        destination = Path(temporary.name) / "paperlens.plbackup"
        try:
            async with service.quiescent(user["id"]):
                browser = json.loads(service.store.export_browser(user["id"], policy))
                await run_in_threadpool(export_bundle, config.root / "workspaces" / policy.workspace_id,
                                        policy, browser, data.get("password"), destination)
        except (ValueError, OSError) as exc:
            temporary.cleanup()
            raise HTTPException(422, "无法导出资料包，请检查密码、工作区状态和磁盘空间。") from exc
        except BaseException:
            temporary.cleanup()
            raise
        return FileResponse(destination, filename="paperlens-workspace.plbackup", media_type="application/octet-stream",
                            background=BackgroundTask(temporary.cleanup))

    @app.post("/auth/migration/import")
    async def migration_import(request: Request):
        service.check_write(request)
        user = service.bound_user(request)
        # Bound the complete multipart stream before Starlette creates spool files.
        original_receive, received = request._receive, 0

        async def bounded_receive():
            nonlocal received
            message = await original_receive()
            received += len(message.get("body", b""))
            if received > MAX_ARCHIVE + 65536:
                raise HTTPException(413, "资料包不能超过 2 GB。")
            return message

        request._receive = bounded_receive
        folder = config.root / "transfers"
        folder.mkdir(mode=0o700, exist_ok=True)
        async with service.quiescent(user["id"]):
            async with request.form(max_files=1, max_fields=1, max_part_size=1024) as form:
                archive, password = form.get("archive"), form.get("password")
                if not hasattr(archive, "read") or not isinstance(password, str):
                    raise HTTPException(422, "请选择资料包并填写密码。")
                with tempfile.TemporaryDirectory(dir=folder) as temp:
                    path = Path(temp) / "incoming.plbackup"
                    with path.open("wb") as destination:
                        while chunk := await archive.read(1024**2):
                            destination.write(chunk)
                    try:
                        return await run_in_threadpool(import_bundle, path, password, config.root, service.store,
                                                       user["id"], service.policy(user["id"]))
                    except (ValueError, KeyError, TypeError, OSError, zipfile.BadZipFile, InvalidToken) as exc:
                        raise HTTPException(422, "无法导入：密码错误、资料包不完整或内容不兼容；原工作区未改动。") from exc

    app.router.routes.append(Mount("/", app=service.serve_workspace))
    return app


def main():
    parser = argparse.ArgumentParser(description="PaperLens 独立账户服务")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--dev-mail", action="store_true", help="验证码仅写入本地 test-mail，限本机验收")
    parser.add_argument("--env-file", type=Path, help="显式加载独立服务配置，不加载原模型 .env")
    args = parser.parse_args()
    if args.dev_mail and args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("测试邮件模式只能监听本机。")
    if args.env_file and not args.env_file.is_file():
        parser.error("独立服务配置文件不存在。")
    config = StandaloneConfig.from_env(args.data, args.origin, args.dev_mail, args.env_file)
    if not config.secure and args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("HTTP 测试模式只能监听本机；正式服务需配置 HTTPS 来源。")
    import uvicorn

    uvicorn.run(create_standalone(config), host=args.host, port=args.port, proxy_headers=True,
                forwarded_allow_ips=config.trusted_proxies,
                access_log=False)


if __name__ == "__main__":
    main()
