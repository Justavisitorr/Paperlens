"""Local workspace API; credentials stay server-side and each job captures its settings."""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__, currency, estimates
from .auto_notes import AutoNoteService
from .catalog import load_catalog
from .concurrency_policy import SpeedMode, resolve_speed_mode
from .config import Settings
from .connections import ConnectionStore
from .conversation import ConversationService
from .errors import ConfigurationError, InputBudgetExceeded, InputError, PaperLensError
from .errors import ValidationError as ContentValidationError
from .importing import PaperImporter
from .jobs import Job as Job
from .jobs import JobManager
from .library import LibraryStore
from .models import DashboardReport
from .notes import NoteService
from .profiles import ProcessingProfile, profile_catalog
from .reader import ReaderService
from .recovery import attach_failures
from .reference_tree_routes import register_reference_tree_routes
from .report_resources import hydrate_export, select_pdf
from .runtime import RuntimeSettings
from .storage import write_json
from .translation import TranslationService
from .web_policy import WorkspacePolicy

MAX_QUEUED_JOBS = 16


def run_pipeline(
    pdf_path: Path,
    settings: Settings,
    *,
    progress=None,
    source_name: str | None = None,
    reader_storage_root: Path | None = None,
):
    """Lazy boundary also lets HTTP tests substitute a bounded fake pipeline."""
    from .pipeline import run_pipeline as run

    result = run(
        pdf_path,
        settings,
        progress=progress,
        source_name=source_name,
        **({"reader_storage_root": reader_storage_root} if reader_storage_root is not None else {}),
    )
    metadata = pdf_path.parent / "source-metadata.json"
    if metadata.is_file():
        write_json(
            result.output_dir / "source-metadata.json",
            json.loads(metadata.read_text(encoding="utf-8")),
        )
    return result


def get_dashboard_template() -> str:
    from .exporting import get_dashboard_template as get_template

    return get_template()


def _message(exc: Exception, settings: Settings) -> str:
    text = (
        str(exc)
        if isinstance(exc, PaperLensError)
        else "处理未完成，请检查 PDF、依赖与本地配置后重试。"
    )
    secrets = {settings.llm_api_key.get_secret_value()}
    secrets.update(route.api_key.get_secret_value() for route in settings.llm_routes.values())
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            text = text.replace(secret, "[已隐藏]")
    return text[:800]


class UploadBodyLimit:
    """Reject excessive bodies before multipart data can fill temporary storage."""

    def __init__(self, app: ASGIApp, limit) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("path") != "/api/jobs"
            or scope.get("method") != "POST"
        ):
            await self.app(scope, receive, send)
            return
        limit = self.limit() if callable(self.limit) else self.limit
        headers = dict(scope.get("headers", []))
        try:
            advertised = int(headers.get(b"content-length", b"0"))
        except ValueError:
            await JSONResponse({"detail": "上传请求长度无效。"}, status_code=400)(
                scope, receive, send
            )
            return
        if advertised < 0 or advertised > limit:
            await JSONResponse({"detail": "上传文件超过当前大小上限。"}, status_code=413)(
                scope, receive, send
            )
            return
        consumed = 0

        async def bounded_receive() -> Message:
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > limit:
                    raise HTTPException(413, "上传文件超过当前大小上限。")
            return message

        await self.app(scope, bounded_receive, send)


def _discard_upload(path: Path) -> None:
    path.unlink(missing_ok=True)
    try:
        path.parent.rmdir()
    except OSError:
        pass


def _display_filename(filename: str | None) -> str:
    """Preserve readable Unicode names while discarding every path component."""
    basename = (filename or "paper.pdf").replace("\\", "/").rsplit("/", 1)[-1]
    basename = "".join(char for char in basename if ord(char) >= 32 and ord(char) != 127)
    basename = basename.strip()[:240]
    return basename if basename and basename not in {".", ".."} else "paper.pdf"


def create_app(settings: Settings | None = None, *, web_policy: WorkspacePolicy | None = None,
               global_slots: threading.BoundedSemaphore | None = None, template_provider=None) -> FastAPI:
    settings = settings or Settings()
    runtime = RuntimeSettings(settings, policy=web_policy)
    connections = ConnectionStore(runtime.snapshot(), policy=web_policy)

    def configured() -> Settings:
        return connections.resolve(runtime.snapshot())

    def connection_action(action, *args):
        try:
            return action(*args)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    catalog_lock = threading.Lock()
    stop_catalog = threading.Event()
    wake_catalog = threading.Event()

    def catalog_refresh(*, force: bool = False) -> dict:
        current = runtime.snapshot()
        with catalog_lock:
            return load_catalog(
                current.paperlens_cache_dir,
                allow_network=current.paperlens_catalog_network,
                force_refresh=force,
                max_age_hours=current.paperlens_catalog_max_age_hours,
            ).model_dump(mode="json")

    def maintain_catalog() -> None:
        while not stop_catalog.is_set():
            current = runtime.snapshot()
            if current.paperlens_catalog_network and current.paperlens_catalog_auto_update:
                try:
                    catalog_refresh()
                except (OSError, ValueError):
                    # Failed refresh leaves the last verified snapshot available.
                    pass
            wake_catalog.wait(60)
            wake_catalog.clear()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.prepare_local_paths()
        manager = JobManager(
            settings,
            runner=run_pipeline,
            settings_provider=configured,
            settings_restorer=lambda data: connections.restore(runtime.snapshot(), data),
            queue_limit=MAX_QUEUED_JOBS,
            global_slots=global_slots,
            worker_threads=web_policy.parallel_jobs if web_policy else 4,
        )
        app.state.jobs = manager
        app.state.runtime = runtime
        app.state.connections = connections
        app.state.library = LibraryStore(settings.paperlens_output_dir)
        app.state.importer = PaperImporter(settings.paperlens_cache_dir, pin_public=bool(web_policy))
        app.state.reader = ReaderService(settings.paperlens_output_dir / ".reader")
        app.state.conversations = ConversationService(settings.paperlens_output_dir / ".reader")
        app.state.notes = NoteService(settings.paperlens_output_dir / ".reader")
        app.state.auto_notes = AutoNoteService(
            settings.paperlens_output_dir / ".reader", app.state.notes
        )
        app.state.translations = TranslationService(
            settings.paperlens_output_dir / ".reader" / "translations"
        )
        catalog_thread = threading.Thread(
            target=maintain_catalog, daemon=True, name="paperlens-catalog"
        )
        catalog_thread.start()
        try:
            yield
        finally:
            stop_catalog.set()
            wake_catalog.set()
            manager.close()
            catalog_thread.join(timeout=1)

    app = FastAPI(title="PaperLens local API", version=__version__, lifespan=lifespan)
    app.add_middleware(
        UploadBodyLimit,
        limit=lambda: runtime.snapshot().paperlens_max_upload_mb * 1024 * 1024 + 65536,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[urlsplit(web_policy.public_origin).hostname] if web_policy else ["localhost", "127.0.0.1", "[::1]", "testserver"],
    )

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            parts = urlsplit(origin) if origin else None
            if request.headers.get("sec-fetch-site") == "cross-site" or (
                parts and (parts.scheme, parts.netloc) != (request.url.scheme, request.url.netloc)
            ):
                return JSONResponse(
                    {"detail": "请从本机 PaperLens 页面提交操作。"}, status_code=403
                )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(RequestValidationError)
    @app.exception_handler(ValidationError)
    async def invalid_input(request: Request, exc: Exception):
        # Pydantic's default errors include input values, possibly including a key.
        return JSONResponse({"detail": "输入格式或字段范围无效，请检查后重试。"}, status_code=422)

    @app.exception_handler(PaperLensError)
    async def known_error(request: Request, exc: PaperLensError):
        status = (
            503
            if isinstance(exc, ConfigurationError)
            else 422
            if isinstance(exc, (InputError, ContentValidationError))
            else 502
        )
        body = {"detail": _message(exc, runtime.snapshot())}
        if isinstance(exc, InputBudgetExceeded):
            body.update(code=exc.code, input_budget=exc.budget)
        return JSONResponse(body, status_code=status)

    def paper(request: Request, paper_id: str, *, for_export: bool = False) -> DashboardReport:
        try:
            store = request.app.state.library
            return store.export_report(paper_id) if for_export else store.report(paper_id)
        except InputError as exc:
            raise HTTPException(404, str(exc)) from exc

    register_reference_tree_routes(
        app,
        paper_loader=paper,
        cache_dir=lambda: runtime.snapshot().paperlens_cache_dir,
        allow_network=lambda: runtime.snapshot().paperlens_reference_network,
    )

    def html_page(template: str, request: Request) -> HTMLResponse:
        if web_policy:
            navigation = request.headers.get("sec-fetch-mode") == "navigate" or (
                request.headers.get("sec-fetch-dest") in {"document", "iframe"}
                or ("sec-fetch-dest" not in request.headers and "text/html" in request.headers.get("accept", ""))
            )
            template = web_policy.live_html(template, navigation)
        return HTMLResponse(template)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        try:
            template = (template_provider or get_dashboard_template)()
        except (OSError, ImportError, PaperLensError) as exc:
            raise HTTPException(
                503, "上传页面资源尚未准备好，请按 README 构建前端后重试。"
            ) from exc
        if "__PAPERLENS_DATA__" not in template:
            raise HTTPException(503, "上传页面资源不完整，请重新构建前端。")
        return html_page(template.replace("__PAPERLENS_DATA__", "null"), request)

    @app.get("/api/health")
    @app.get("/health", include_in_schema=False)
    def health(request: Request) -> dict:
        return request.app.state.jobs.health() | {
            "features": [
                "library",
                "library.assets",
                "imports",
                "jobs.control",
                "jobs.history",
                "jobs.source",
                "references.tree",
                "settings",
                "connections",
                "jobs.accelerate",
                "processing.profiles",
                "explain",
                "annotations",
                "notes",
                "auto_notes",
                "chat",
                "chat.papers",
                "chat.stream",
                "chat.routing",
                "conversations",
                "rename",
                "translate",
                "estimates",
            ],
            "version": __version__,
        }

    @app.get("/api/settings")
    def get_settings() -> dict:
        return runtime.public()

    @app.put("/api/settings")
    def save_settings(request: Request, payload: Annotated[dict, Body()]) -> dict:
        values = payload.get("values")
        if not isinstance(values, dict):
            return {"errors": {"values": "请提交需要修改的设置。"}}
        result = runtime.update(values)
        if "errors" not in result:
            connections.update_environment(runtime.snapshot())
            request.app.state.jobs.notify_settings_changed()
            wake_catalog.set()
        return result

    @app.get("/api/connections")
    def get_connections() -> dict:
        return connections.public()

    @app.post("/api/connections/accounts", status_code=201)
    def add_account(payload: Annotated[dict, Body()]) -> dict:
        return {"account": connection_action(connections.save_account, payload)}

    @app.patch("/api/connections/accounts/{account_id}")
    def edit_account(account_id: str, payload: Annotated[dict, Body()]) -> dict:
        return {"account": connection_action(connections.save_account, payload, account_id)}

    @app.delete("/api/connections/accounts/{account_id}")
    def remove_account(account_id: str) -> dict:
        connection_action(connections.delete_account, account_id)
        return {"deleted": True}

    @app.post("/api/connections/accounts/{account_id}/models/refresh")
    def discover_models(account_id: str) -> dict:
        return connection_action(connections.refresh_models, account_id)

    @app.put("/api/connections/routing")
    def assign_models(payload: Annotated[dict, Body()]) -> dict:
        if not isinstance(payload.get("routing"), dict):
            raise HTTPException(422, "请选择各任务使用的模型。")
        return {"routing": connection_action(connections.set_routing, payload["routing"])}

    @app.get("/api/catalog")
    def get_catalog() -> dict:
        current = runtime.snapshot()
        return load_catalog(
            current.paperlens_cache_dir, max_age_hours=current.paperlens_catalog_max_age_hours
        ).model_dump(mode="json")

    @app.post("/api/catalog/refresh")
    def refresh_catalog() -> dict:
        if not runtime.snapshot().paperlens_catalog_network:
            raise HTTPException(409, "目录联网尚未开启，请先在设置中允许联网更新目录。")
        return catalog_refresh(force=True)

    @app.post("/api/models/refresh")
    def refresh_models() -> dict:
        from .provider import JsonProvider

        current = configured()
        current.require_provider()
        with JsonProvider(current) as provider:
            models = provider.list_models()
        return {"models": models, "base_url": current.llm_base_url, "capabilities_verified": False}

    @app.get("/api/jobs")
    def jobs(request: Request) -> dict:
        return {"jobs": request.app.state.jobs.list()}

    @app.delete("/api/jobs/history")
    def clear_job_history(request: Request) -> dict:
        return request.app.state.jobs.clear_history()

    @app.get("/api/exchange-rates")
    async def exchange_rates() -> dict:
        return await asyncio.to_thread(currency.load_exchange_rates, configured())

    @app.get("/api/currencies")
    def display_currencies() -> dict:
        return {"currencies": currency.currencies()}

    @app.get("/api/processing-profiles")
    def processing_profiles() -> dict:
        return profile_catalog(configured())

    @app.post("/api/estimates")
    async def processing_estimate(
        file: Annotated[UploadFile | None, File()] = None,
        pages: Annotated[int | None, Form(ge=1, le=100000)] = None,
        document_features: Annotated[str | None, Form(max_length=2000)] = None,
        accelerated: Annotated[bool, Form()] = False,
        speed_mode: Annotated[SpeedMode | None, Form()] = None,
    ) -> dict:
        """Usage, cost and time per tier for a paper about to be processed."""
        current = configured()
        try:
            features = estimates.DocumentFeatures.model_validate_json(document_features) if (
                document_features is not None
            ) else estimates.DocumentFeatures()
        except (ValueError, TypeError):
            if file is not None:
                await file.close()
            raise HTTPException(422, "文档特征格式无效，请重新选择文件。") from None
        if file is not None:
            limit = current.paperlens_max_upload_mb * 1024 * 1024
            scratch = current.paperlens_cache_dir / "estimates" / f"{uuid.uuid4().hex}.pdf"
            try:
                scratch.parent.mkdir(parents=True, exist_ok=True)
                size = 0
                with scratch.open("xb") as destination:
                    while chunk := await file.read(1024 * 1024):
                        size += len(chunk)
                        if size > limit:
                            break
                        destination.write(chunk)
                if size > limit:
                    raise HTTPException(413, "上传文件超过当前大小上限。")
                observed_pages, features = await asyncio.to_thread(
                    estimates.inspect_pdf, scratch, current
                )
                pages = observed_pages or pages
            finally:
                await file.close()
                scratch.unlink(missing_ok=True)
        return await asyncio.to_thread(
            estimates.estimate, current, pages=pages, document_features=features,
            accelerated=resolve_speed_mode(speed_mode, accelerated) != "normal",
            speed_mode=speed_mode,
        )

    @app.post("/api/estimates/run")
    def processing_run_cost(payload: Annotated[dict, Body()]) -> dict:
        """Price a report's recorded usage at the current reference prices."""
        run = payload.get("run")
        if not isinstance(run, dict):
            raise HTTPException(422, "请提交处理记录。")
        return estimates.run_cost(run, configured())

    @app.post("/api/imports/resolve")
    def resolve_import(request: Request, payload: Annotated[dict, Body()]) -> dict:
        query = payload.get("query")
        if not isinstance(query, str):
            raise HTTPException(422, "请输入论文标题或来源。")
        return request.app.state.importer.resolve(query)

    @app.post("/api/imports", status_code=202)
    def import_paper(request: Request, payload: Annotated[dict, Body()]) -> dict:
        candidate = payload.get("candidate_id")
        profile = payload.get("profile", "standard")
        accelerated = payload.get("accelerated", False)
        if not isinstance(accelerated, bool):
            raise HTTPException(422, "加速选项必须是开关值。")
        speed_mode = payload.get("speed_mode")
        try:
            mode = resolve_speed_mode(speed_mode, accelerated)
        except (TypeError, ValueError):
            raise HTTPException(422, "请选择普通、加速或极速。") from None
        if not isinstance(candidate, str) or profile not in {"economy", "standard", "detailed"}:
            raise HTTPException(422, "请选择论文和处理档位。")
        current = configured()
        current.require_provider()
        manager: JobManager = request.app.state.jobs
        job_id = uuid.uuid4().hex
        path = manager.root / job_id / f"{job_id}.pdf"
        try:
            meta = request.app.state.importer.download(
                candidate, path, current.paperlens_max_upload_mb * 1024 * 1024
            )
            write_json(path.parent / "source-metadata.json", meta)
            manager.submit(
                job_id, _display_filename(meta["title"] + ".pdf"), profile,
                accelerated=accelerated, speed_mode=mode,
            )
        except (PaperLensError, HTTPException, OSError):
            (path.parent / "source-metadata.json").unlink(missing_ok=True)
            _discard_upload(path)
            raise
        return {"id": job_id}

    @app.post("/api/jobs", status_code=202)
    async def upload(
        request: Request,
        file: Annotated[UploadFile, File()],
        profile: Annotated[ProcessingProfile, Form()] = "standard",
        unlimited_confirmed: Annotated[bool, Form()] = False,
        accelerated: Annotated[bool, Form()] = False,
        speed_mode: Annotated[SpeedMode | None, Form()] = None,
    ) -> dict:
        current = configured()
        try:
            current.require_provider()
        except PaperLensError as exc:
            await file.close()
            raise HTTPException(503, _message(exc, current)) from exc
        max_bytes = current.paperlens_max_upload_mb * 1024 * 1024
        manager: JobManager = request.app.state.jobs
        job_id = uuid.uuid4().hex
        path = manager.root / job_id / f"{job_id}.pdf"
        size = 0
        prefix = b""
        try:
            path.parent.mkdir(parents=True, exist_ok=False)
            with path.open("xb") as destination:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise HTTPException(413, "上传文件超过当前大小上限。")
                    if len(prefix) < 1024:
                        prefix += chunk[: 1024 - len(prefix)]
                    destination.write(chunk)
            if not size or b"%PDF-" not in prefix:
                raise HTTPException(422, "文件不是有效 PDF，或文件为空；请检查后重新上传。")
            manager.submit(
                job_id, _display_filename(file.filename), profile, accelerated=accelerated,
                speed_mode=speed_mode,
            )
        except HTTPException:
            _discard_upload(path)
            raise
        except OSError as exc:
            _discard_upload(path)
            raise HTTPException(500, "无法保存上传文件，请检查磁盘空间和目录权限。") from exc
        finally:
            await file.close()
        return {"id": job_id}

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str, request: Request) -> dict:
        manager: JobManager = request.app.state.jobs
        return manager.public(manager.get(job_id))

    @app.get("/api/jobs/{job_id}/source.pdf")
    def job_source(job_id: str, request: Request):
        manager: JobManager = request.app.state.jobs
        job = manager.get(job_id)
        return FileResponse(
            manager.source_path(job_id), media_type="application/pdf",
            filename=job.source_name, content_disposition_type="inline",
        )

    @app.post("/api/jobs/{job_id}/{action}")
    def job_action(
        job_id: str, action: str, request: Request, payload: Annotated[dict | None, Body()] = None
    ) -> dict:
        if action in {"accelerate", "ultra"} and (not payload or payload.get("confirmed") is not True):
            raise HTTPException(409, "请先确认并行加速的限流、重试与在途请求费用提示。")
        if action == "accelerate" and payload and "speed_mode" in payload:
            if payload["speed_mode"] == "ultra":
                action = "ultra"
            elif payload["speed_mode"] != "fast":
                raise HTTPException(422, "运行中的任务只能提升到加速或极速。")
        if action not in {"pause", "resume", "cancel", "retry", "accelerate", "ultra"}:
            raise HTTPException(404, "找不到该任务操作。")
        try:
            return request.app.state.jobs.action(
                job_id, action, input_budget=(payload or {}).get("input_budget")
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/jobs/{job_id}/report")
    def job_report(job_id: str, request: Request):
        from .exporting import report_html

        manager: JobManager = request.app.state.jobs
        path = manager.report_path(manager.get(job_id))
        data_path = path.parent / "report.json"
        if data_path.is_file() and data_path.resolve().is_relative_to(path.parent):
            try:
                report = DashboardReport.model_validate_json(data_path.read_text(encoding="utf-8"))
                attach_failures(report, data_path.parent)
                hydrate_export(report, select_pdf(data_path, report.document.sha256, lambda: report))
                return html_page(report_html(report), request)
            except (ValueError, OSError):
                pass
        if web_policy:
            return html_page(path.read_text(encoding="utf-8"), request)
        return FileResponse(path, media_type="text/html")

    @app.get("/api/library")
    def library(request: Request) -> dict:
        request.app.state.library.refresh(force=True)
        listing = request.app.state.library.list()
        if web_policy:
            listing["storage_path"] = "个人文献库"
        return listing

    @app.get("/api/library/{paper_id}/report")
    def library_report(paper_id: str, request: Request, view: Literal["full", "web"] = "full") -> dict:
        if view == "web":
            try:
                return request.app.state.library.web_report(paper_id)
            except InputError as exc:
                raise HTTPException(404, str(exc)) from exc
            except OSError as exc:
                raise HTTPException(404, "论文资源已移走，请刷新后重试。") from exc
        return paper(request, paper_id, for_export=True).model_dump(mode="json")

    @app.get("/api/library/{paper_id}/html")
    def library_html(paper_id: str, request: Request):
        from .exporting import report_html

        return html_page(report_html(paper(request, paper_id, for_export=True)), request)

    @app.get("/api/library/{paper_id}/thumbnail")
    def library_thumbnail(paper_id: str, request: Request):
        return Response(
            request.app.state.library.thumbnail(paper_id),
            media_type="image/webp",
            headers={"Cache-Control": "private, max-age=86400"},
        )

    @app.get("/api/library/{paper_id}/pdf")
    def library_pdf(paper_id: str, request: Request):
        try:
            resource = request.app.state.library.pdf_resource(paper_id)
        except InputError as exc:
            raise HTTPException(404, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(404, "原 PDF 已移走，请刷新后重试。") from exc
        return FileResponse(resource.path, media_type=resource.media_type,
                            filename="source.pdf", content_disposition_type="inline")

    @app.get("/api/library/{paper_id}/assets/{asset_id}")
    def library_asset(paper_id: str, asset_id: str, request: Request):
        try:
            resource = request.app.state.library.image_resource(paper_id, asset_id)
            data = resource.path.read_bytes()
        except (InputError, OSError) as exc:
            raise HTTPException(404, "找不到该论文资源。") from exc
        return Response(data, media_type=resource.media_type, headers={"ETag": f'"{asset_id}"'})

    @app.patch("/api/library/{paper_id}")
    def rename_paper(paper_id: str, request: Request, payload: Annotated[dict, Body()]) -> dict:
        paper(request, paper_id)
        return request.app.state.library.update(paper_id, payload)

    @app.delete("/api/library/{paper_id}")
    def delete_paper(paper_id: str, request: Request) -> dict:
        paper(request, paper_id)
        return request.app.state.library.delete(paper_id)

    @app.get("/api/library/{paper_id}/annotations")
    def annotations(paper_id: str, request: Request) -> dict:
        return {
            "annotations": request.app.state.reader.list_annotations(
                paper_id, paper(request, paper_id)
            )
        }

    @app.get("/api/library/{paper_id}/notes")
    def notes(paper_id: str, request: Request) -> dict:
        return {"notes": request.app.state.notes.list(paper_id, paper(request, paper_id))}

    @app.put("/api/library/{paper_id}/notes/{note_id}")
    def save_note(
        paper_id: str, note_id: str, request: Request, payload: Annotated[dict, Body()]
    ) -> dict:
        return request.app.state.notes.save(paper_id, paper(request, paper_id), note_id, payload)

    @app.post("/api/library/{paper_id}/notes/generate")
    def generate_notes(paper_id: str, request: Request, payload: Annotated[dict, Body()]) -> dict:
        return request.app.state.auto_notes.generate(
            paper_id, paper(request, paper_id), configured(), payload
        )

    @app.delete("/api/library/{paper_id}/notes/{note_id}")
    def delete_note(paper_id: str, note_id: str, request: Request) -> dict:
        if not request.app.state.notes.delete(paper_id, paper(request, paper_id), note_id):
            raise HTTPException(404, "找不到该条笔记。")
        return {"deleted": True}

    @app.post("/api/library/{paper_id}/annotations", status_code=201)
    def annotate(paper_id: str, request: Request, payload: Annotated[dict, Body()]) -> dict:
        return request.app.state.reader.create_annotation(
            paper_id, paper(request, paper_id), payload
        )

    @app.patch("/api/library/{paper_id}/annotations/{annotation_id}")
    def update_annotation(
        paper_id: str, annotation_id: str, request: Request, payload: Annotated[dict, Body()]
    ) -> dict:
        return request.app.state.reader.update_annotation(
            paper_id, paper(request, paper_id), annotation_id, payload
        )

    @app.delete("/api/library/{paper_id}/annotations/{annotation_id}")
    def delete_annotation(paper_id: str, annotation_id: str, request: Request) -> dict:
        deleted = request.app.state.reader.delete_annotation(
            paper_id, paper(request, paper_id), annotation_id
        )
        if not deleted:
            raise HTTPException(404, "找不到该条勾画。")
        return {"deleted": True}

    @app.post("/api/library/{paper_id}/explain")
    def explain(paper_id: str, request: Request, payload: Annotated[dict, Body()]) -> dict:
        return request.app.state.reader.explain(
            paper_id, paper(request, paper_id), configured(), payload
        )

    @app.post("/api/library/{paper_id}/chat")
    def chat(paper_id: str, request: Request, payload: Annotated[dict, Body()]) -> dict:
        return request.app.state.reader.chat(
            paper_id, paper(request, paper_id), configured(), payload
        )

    @app.get("/api/library/{paper_id}/translations")
    def translations(paper_id: str, request: Request) -> dict:
        return request.app.state.translations.list(paper_id, paper(request, paper_id))

    @app.post("/api/library/{paper_id}/translate")
    def translate(paper_id: str, request: Request, payload: Annotated[dict, Body()]) -> dict:
        return request.app.state.translations.translate(
            paper_id, paper(request, paper_id), configured(), payload
        )

    # Workspace conversations (conversation.py): several papers, stored threads.
    @app.post("/api/chat")
    def chat_papers(request: Request, payload: Annotated[dict, Body()]) -> dict:
        ids = payload.get("papers") if isinstance(payload, dict) else None
        reports = {
            paper_id: paper(request, paper_id)
            for paper_id in (ids if isinstance(ids, list) else [])[:6]
            if isinstance(paper_id, str)
        }
        return request.app.state.conversations.ask(reports, configured(), payload)

    @app.post("/api/chat/stream")
    def chat_stream(request: Request, payload: Annotated[dict, Body()]):
        ids = payload.get("papers")
        reports = {
            paper_id: paper(request, paper_id)
            for paper_id in (ids if isinstance(ids, list) else [])[:6]
            if isinstance(paper_id, str)
        }
        service = request.app.state.conversations
        settings = configured()
        service._prepare(reports, settings, payload)

        async def events():
            stream = service.stream(reports, settings, payload)
            try:
                async for event in stream:
                    yield json.dumps(event, ensure_ascii=False) + "\n"
            except PaperLensError as exc:
                yield (
                    json.dumps(
                        {"type": "error", "message": _message(exc, settings)},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            finally:
                await stream.aclose()

        return StreamingResponse(
            events(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/conversations")
    def conversations(request: Request) -> dict:
        return {"conversations": request.app.state.conversations.list_all()}

    @app.get("/api/conversations/{conversation_id}")
    def conversation(conversation_id: str, request: Request) -> dict:
        record = request.app.state.conversations.get(conversation_id)
        if record is None:
            raise HTTPException(404, "找不到这段对话。")
        return record

    @app.put("/api/conversations/{conversation_id}")
    def save_conversation(
        conversation_id: str, request: Request, payload: Annotated[dict, Body()]
    ) -> dict:
        return request.app.state.conversations.save(conversation_id, payload)

    @app.delete("/api/conversations/{conversation_id}")
    def delete_conversation(conversation_id: str, request: Request) -> dict:
        if not request.app.state.conversations.delete(conversation_id):
            raise HTTPException(404, "找不到这段对话。")
        return {"deleted": True}

    return app
