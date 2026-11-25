"""Bounded, resumable local jobs with immutable per-submission settings."""

from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
import traceback
import uuid
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .concurrency_policy import SpeedMode, job_execution_concurrency, resolve_speed_mode
from .config import Settings
from .control import ProcessingPaused, processing_control
from .errors import InputBudgetExceeded, PaperLensError
from .input_budget import approved_limit, temporary_input_budget
from .models import StageFailure
from .profiles import PROFILE_LABELS, ProcessingProfile, apply_profile
from .recovery import failure_detail, stage_failures
from .storage import write_json

JOB_ID = re.compile(r"^[0-9a-f]{32}$")
ACTIVE = {"queued", "running"}
SUCCESS = {"complete", "partial"}
FINISHED = SUCCESS | {"failed", "cancelled"}
logger = logging.getLogger(__name__)


def _created_time(job: Job) -> float:
    try:
        value = datetime.fromisoformat(job.created_at)
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).timestamp()
    except (ValueError, OSError, OverflowError):
        return 0.0


class Job(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    source_name: str = Field(default="paper.pdf", min_length=1, max_length=240)
    source_sha256: str = ""
    status: Literal["queued", "running", "paused", "cancelled", "complete", "partial", "failed"] = (
        "queued"
    )
    stage: str = "queued"
    message: str = "文件已接收，等待处理。"
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    error: str | None = None
    retryable: bool = False
    input_budget: dict | None = None
    failures: list[StageFailure] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    report_relative_path: str | None = None
    paper_id: str | None = None
    configuration: dict = Field(default_factory=dict)
    acceleration_enabled: bool = False
    normal_concurrency: int = Field(default=2, ge=1, le=5)
    accelerated_concurrency: int = Field(default=3, ge=1, le=5)
    speed_mode: SpeedMode | None = None
    ultra_mode: bool = False
    ultra_concurrency: int = Field(default=3, ge=1, le=16)

    def effective_speed_mode(self) -> SpeedMode:
        if self.speed_mode is not None:
            return self.speed_mode
        return "ultra" if self.ultra_mode else resolve_speed_mode(None, self.acceleration_enabled)

    def worker_limit(self) -> int:
        mode = self.effective_speed_mode()
        if mode == "ultra":
            return max(self.accelerated_concurrency, self.ultra_concurrency)
        return self.accelerated_concurrency if mode == "fast" else self.normal_concurrency

    def public(self) -> dict:
        actions = {
            "queued": ["pause", "cancel"],
            "running": ["pause", "cancel"],
            "paused": ["resume", "cancel"],
            "failed": ["retry"],
            "cancelled": ["retry"],
        }.get(self.status, [])
        if self.status == "partial" and self.retryable:
            actions.append("retry")
        if self.status in ACTIVE and self.effective_speed_mode() == "normal":
            actions.insert(0, "accelerate")
        if (self.status in ACTIVE and self.effective_speed_mode() != "ultra"
                and self.ultra_concurrency > self.accelerated_concurrency):
            actions.insert(1 if actions and actions[0] == "accelerate" else 0, "ultra")
        if self.stage in {"pausing", "cancelling"}:
            actions = []
        return {
            key: getattr(self, key)
            for key in (
                "id",
                "source_name",
                "source_sha256",
                "status",
                "stage",
                "message",
                "progress",
                "error",
                "input_budget",
                "created_at",
                "paper_id",
            )
        } | {
            "processing_profile": self.configuration.get(
                "paperlens_processing_profile", "standard"
            ),
            "profile_label": PROFILE_LABELS.get(
                self.configuration.get("paperlens_processing_profile", "standard"), "标准"
            ),
            "model": self.configuration.get("llm_model", ""),
            "report_url": f"/api/jobs/{self.id}/report" if self.status in SUCCESS else None,
            "actions": actions,
            "failures": [failure.model_dump(mode="json") for failure in self.failures],
            "speed_mode": self.effective_speed_mode(),
            "acceleration": {
                "enabled": self.effective_speed_mode() != "normal",
                "mode": self.effective_speed_mode(),
                "concurrency": self.worker_limit(),
            },
        }


class JobManager:
    def __init__(
        self,
        settings: Settings,
        *,
        runner: Callable,
        settings_provider: Callable[[], Settings] | None = None,
        settings_restorer: Callable[[dict], Settings] | None = None,
        queue_limit: int = 16,
        global_slots: threading.BoundedSemaphore | None = None,
        worker_threads: int = 4,
    ):
        self.settings = settings
        self.root = settings.paperlens_cache_dir.resolve() / "jobs"
        self.output_root = settings.paperlens_output_dir.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.output_root.mkdir(parents=True, exist_ok=True)
        self._runner = runner
        self._settings_provider = settings_provider or (lambda: settings.model_copy(deep=True))
        self._settings_restorer = settings_restorer
        self._queue_limit = queue_limit
        self._global_slots = global_slots
        self._jobs: dict[str, Job] = {}
        self._snapshots: dict[str, Settings] = {}
        # Approval expires when this attempt ends, pauses or the service exits.
        self._input_allowances: dict[str, int] = {}
        self._pending: deque[str] = deque()
        self._requested: dict[str, str] = {}
        self._working: set[str] = set()
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._stopping = threading.Event()
        self._load()
        self._prune_history()
        self._threads = [
            threading.Thread(
                target=self._worker, args=(i,), name=f"paperlens-worker-{i}", daemon=True
            )
            for i in range(worker_threads)
        ]
        for thread in self._threads:
            thread.start()

    def _save(self, job: Job) -> None:
        write_json(self.root / job.id / "job.json", job.model_dump(mode="json"))

    def _source_hash(self, job_id: str) -> str:
        source = self.root / job_id / f"{job_id}.pdf"
        try:
            if not source.resolve().is_relative_to(self.root / job_id):
                return ""
            digest = hashlib.sha256()
            with source.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        except OSError:
            return ""

    def _restore_source_hash(self, job: Job, path: Path) -> None:
        if job.source_sha256:
            return
        job.source_sha256 = self._source_hash(job.id)
        if job.source_sha256:
            try:
                write_json(path, job.model_dump(mode="json"))
            except OSError:
                logger.warning("Could not persist source identity for task %s", job.id)

    def _load(self) -> None:
        for path in self.root.glob("*/job.json"):
            if not JOB_ID.fullmatch(path.parent.name) or not path.resolve().is_relative_to(
                self.root
            ):
                continue
            try:
                job = Job.model_validate_json(path.read_text(encoding="utf-8"))
                if job.id != path.parent.name:
                    continue
                self._restore_source_hash(job, path)
                if job.status == "partial" and job.report_relative_path and not job.failures:
                    directory = (self.output_root / job.report_relative_path).resolve().parent
                    if directory.is_relative_to(self.output_root / job.id):
                        job.failures = stage_failures(
                            directory, job.configuration.get("llm_model", ""),
                            job.configuration.get("llm_routes", {}),
                        )
                if job.status in ACTIVE:
                    job.status = "failed"
                    job.stage = "interrupted"
                    job.error = "上次服务关闭时处理被中断；可重试，文件已移走时需重新上传。"
                    job.message = job.error
                    self._save(job)
                self._jobs[job.id] = job
            except (OSError, ValueError, ValidationError):
                continue

    def get(self, job_id: str) -> Job:
        with self._lock:
            if not JOB_ID.fullmatch(job_id):
                raise HTTPException(404, "找不到该处理任务。")
            if job_id in self._jobs:
                return self._jobs[job_id].model_copy(deep=True)
            # Archived records stay off the hot list, but existing report links work.
            path = self.root / job_id / "archived-job.json"
            try:
                if not path.resolve().is_relative_to(self.root):
                    raise ValueError("Invalid archive path")
                job = Job.model_validate_json(path.read_text(encoding="utf-8"))
                if job.id != job_id:
                    raise ValueError("Invalid archive id")
                self._restore_source_hash(job, path)
                return job
            except (OSError, ValueError) as exc:
                raise HTTPException(404, "找不到该处理任务。") from exc

    def clear_history(self, *, keep: int = 0) -> dict:
        with self._lock:
            finished = sorted(
                (j for j in self._jobs.values() if j.status in FINISHED),
                key=_created_time,
                reverse=True,
            )
            removed = []
            failed = 0
            for job in finished[keep:]:
                if job.id in self._working:
                    continue
                folder = (self.root / job.id).resolve()
                if not folder.is_relative_to(self.root):
                    failed += 1
                    continue
                try:
                    (folder / "job.json").replace(folder / "archived-job.json")
                except OSError:
                    failed += 1
                    logger.warning("Could not archive task %s", job.id)
                    continue
                self._jobs.pop(job.id)
                self._snapshots.pop(job.id, None)
                self._requested.pop(job.id, None)
                removed.append(job.id)
            return {"removed_ids": removed, "failed": failed}

    def _prune_history(self) -> None:
        limit = self._settings_provider().paperlens_job_history_limit
        if limit:
            self.clear_history(keep=limit)

    def list(self) -> list[dict]:
        with self._lock:
            return [
                self.public(job)
                for job in sorted(self._jobs.values(), key=_created_time, reverse=True)
            ]

    def source_path(self, job_id: str) -> Path:
        """Only a known task's own upload is readable, including after failure."""
        self.get(job_id)
        directory = self.root / job_id
        path = directory / f"{job_id}.pdf"
        try:
            # Compare with the unresolved expected directory: a directory junction
            # or file symlink must not turn a task id into another file's access.
            if not path.resolve().is_relative_to(directory) or not path.is_file():
                raise OSError("Missing or relocated task source")
        except OSError as exc:
            raise HTTPException(404, "此任务的原始 PDF 已移走或不存在，请重新上传。") from exc
        return path

    def public(self, job: Job) -> dict:
        try:
            self.source_path(job.id)
            source_url = f"/api/jobs/{job.id}/source.pdf"
        except HTTPException:
            source_url = None
        return job.public() | {"source_url": source_url}

    def submit(
        self, job_id: str, source_name: str, profile: ProcessingProfile = "standard", *,
        accelerated: bool = False,
        speed_mode: SpeedMode | None = None,
    ) -> Job:
        with self._condition:
            if self._stopping.is_set():
                raise HTTPException(503, "服务正在关闭，请重新启动后重试。")
            if len(self._pending) >= self._queue_limit:
                raise HTTPException(503, "等待队列已满，请等待已有论文处理完成后重试。")
            settings = apply_profile(self._settings_provider(), profile)
            # A job's shared stage ceiling is safe for every provider it can use.
            normal = job_execution_concurrency(settings, False)
            fast = job_execution_concurrency(settings, True)
            ultra = job_execution_concurrency(settings, speed_mode="ultra")
            mode = resolve_speed_mode(speed_mode, accelerated)
            job = Job(
                id=job_id,
                source_name=source_name,
                configuration=settings.public_configuration(),
                acceleration_enabled=mode != "normal",
                normal_concurrency=normal,
                accelerated_concurrency=max(normal, fast),
                ultra_concurrency=max(normal, fast, ultra),
                speed_mode=mode,
                ultra_mode=mode == "ultra",
            )
            job.source_sha256 = self._source_hash(job.id)
            self._save(job)
            self._jobs[job_id] = job
            self._snapshots[job_id] = settings
            self._pending.append(job_id)
            self._condition.notify_all()
            return job

    def notify_settings_changed(self) -> None:
        with self._condition:
            self._prune_history()
            self._condition.notify_all()

    def action(self, job_id: str, action: str, *, input_budget: dict | None = None) -> dict:
        with self._condition:
            current = self.get(job_id)
            if action not in current.public()["actions"]:
                raise HTTPException(409, "当前状态不能执行此操作。")
            allowance = None
            if action in {"retry", "resume"} and current.input_budget:
                if input_budget is None:
                    pending = current.input_budget
                    raise InputBudgetExceeded(pending["limit_chars"], pending["required_chars"],
                                              pending["stage"])
                allowance = approved_limit(input_budget, current.input_budget)
            elif input_budget is not None:
                raise HTTPException(409, "该任务当前没有待确认的输入预算申请，请刷新后重试。")
            previous_pending = self._pending.copy()
            previous_requested = self._requested.copy()
            previous_snapshots = self._snapshots.copy()
            previous_allowances = self._input_allowances.copy()
            was_visible = job_id in self._jobs
            try:
                job = self._jobs.setdefault(job_id, current.model_copy(deep=True))
                if action in {"accelerate", "ultra"}:
                    job.acceleration_enabled = True
                    job.speed_mode = "ultra" if action == "ultra" else "fast"
                    job.ultra_mode = action == "ultra"
                    job.message = (
                        f"已开启{'极速' if job.ultra_mode else '加速'}：独立阅读和核查步骤最多并行 {job.worker_limit()} 个，"
                        "原请求预算不变。"
                    )
                elif action in {"pause", "cancel"}:
                    target = "paused" if action == "pause" else "cancelled"
                    if job.status == "running":
                        self._requested[job_id] = target
                        job.stage = "pausing" if action == "pause" else "cancelling"
                        job.message = (
                            "将在当前步骤完成后暂停；已经发出的模型请求可能计费。"
                            if action == "pause"
                            else "将在当前步骤完成后取消；已经发出的模型请求可能计费。"
                        )
                    else:
                        self._pending = deque(i for i in self._pending if i != job_id)
                        self._input_allowances.pop(job_id, None)
                        job.status = target
                        job.stage = target
                        job.message = (
                            "已暂停，可以继续。"
                            if action == "pause"
                            else "已取消，已完成阶段仍保留。"
                        )
                else:
                    if len(self._pending) >= self._queue_limit:
                        raise HTTPException(503, "等待队列已满，请稍后重试。")
                    source = self.root / job_id / f"{job_id}.pdf"
                    if not source.is_file() or not source.resolve().is_relative_to(
                        self.root / job_id
                    ):
                        raise HTTPException(409, "原始上传文件已移走，请重新上传。")
                    if job_id not in self._snapshots:
                        if self._settings_restorer is not None:
                            self._snapshots[job_id] = self._settings_restorer(job.configuration)
                        else:
                            settings = self._settings_provider()
                            if (
                                job.configuration
                                and job.configuration.get("llm_base_url") != settings.llm_base_url
                            ):
                                raise HTTPException(
                                    409,
                                    "该任务使用其他模型服务，请先切换回原服务再重试，避免密钥发往错误地址。",
                                )
                            self._snapshots[job_id] = Settings(
                                _env_file=None, **(settings.model_dump() | job.configuration)
                            )
                    job.status = "queued"
                    job.stage = "queued"
                    job.error = None
                    job.failures = []
                    job.message = "等待继续，已完成阶段将复用。"
                    self._requested.pop(job_id, None)
                    if allowance is not None:
                        self._input_allowances[job_id] = allowance
                    self._pending.append(job_id)
                self._save(job)
            except (OSError, HTTPException) as exc:
                if was_visible:
                    self._jobs[job_id] = current
                else:
                    self._jobs.pop(job_id, None)
                self._pending = previous_pending
                self._requested = previous_requested
                self._snapshots = previous_snapshots
                self._input_allowances = previous_allowances
                if isinstance(exc, HTTPException):
                    raise
                raise HTTPException(503, "任务状态无法保存，请检查磁盘空间后重试。") from exc
            self._condition.notify_all()
            self._prune_history()
            return self.public(job)

    def _checkpoint(self, job_id: str) -> None:
        with self._lock:
            if self._stopping.is_set() or job_id in self._requested:
                raise ProcessingPaused("处理已在阶段边界停止。")

    def _concurrency(self, job_id: str) -> int:
        with self._lock:
            job = self._jobs[job_id]
            return job.worker_limit()

    def _update(self, job_id: str, **updates) -> None:
        with self._lock:
            if self._stopping.is_set():
                return
            if updates.get("status") in SUCCESS or updates.get("status") == "running":
                self._checkpoint(job_id)
            job = self._jobs[job_id]
            for key, value in updates.items():
                setattr(job, key, value)
            self._save(job)

    def _process(self, job_id: str) -> None:
        allowance = self._input_allowances.pop(job_id, None)
        self._checkpoint(job_id)
        settings = self._snapshots[job_id].model_copy(
            update={"paperlens_output_dir": self.output_root / job_id}
        )
        self._update(job_id, status="running", stage="starting", message="正在准备论文处理。")

        def progress(stage: str, fraction: float, message: str) -> None:
            self._checkpoint(job_id)
            amount = min(1.0, max(0.0, fraction)) if math.isfinite(fraction) else 0.0
            secret = settings.llm_api_key.get_secret_value()
            self._update(
                job_id,
                stage=stage.replace(secret, "[已隐藏]")[:80] if secret else stage[:80],
                message=message.replace(secret, "[已隐藏]")[:800] if secret else message[:800],
                progress=max(self.get(job_id).progress, amount),
            )

        with temporary_input_budget(allowance), processing_control(
            lambda: self._checkpoint(job_id), lambda: self._concurrency(job_id)
        ):
            result = self._runner(
                self.root / job_id / f"{job_id}.pdf",
                settings,
                progress=progress,
                source_name=self.get(job_id).source_name,
                **(
                    {"reader_storage_root": self.output_root / ".reader"}
                    if settings.paperlens_auto_notes_enabled
                    or settings.paperlens_auto_translate_enabled
                    else {}
                ),
            )
            self._checkpoint(job_id)
        path = result.report_path.resolve()
        if (
            not path.is_relative_to(self.output_root / job_id)
            or path.suffix.lower() != ".html"
            or not path.is_file()
        ):
            raise ValueError("Pipeline output escaped assigned directory")
        status = result.report.run.status
        if status not in SUCCESS:
            raise ValueError("Unsupported run status")
        retryable = status == "partial" and bool(getattr(result, "retryable", False))
        message = (
            "阅读看板已生成，可以离线阅读。"
            if status == "complete"
            else "已生成部分结果，请查看质量提示。"
        )
        if retryable:
            message = (
                "已保留可读结果，部分模型请求未完成。可点击重试补处理，成功批次将复用；"
                "若已提交的请求未返回结果，该次可能已计费，重试可能新增费用。"
            )
        input_budget = getattr(result, "input_budget", None)
        failures = getattr(result.report.run, "failures", [])
        if failures:
            names = "、".join(dict.fromkeys(failure.label for failure in failures))
            message = f"已保留可读结果；{names}尚未完成。展开下方详情查看原因和处理方法。"
        if input_budget:
            input_budget = {**input_budget, "request_id": uuid.uuid4().hex}
            message = (
                "已保留可读结果，部分步骤超过单次输入长度上限，超限输入尚未发送。"
                "可确认临时提高上限后继续；成功批次将复用。"
            )
        self._update(
            job_id,
            status=status,
            stage=status,
            progress=1.0,
            error=None,
            retryable=retryable,
            input_budget=input_budget,
            failures=failures,
            message=message,
            report_relative_path=path.relative_to(self.output_root).as_posix(),
            paper_id=getattr(result.report.run, "id", job_id),
        )

    def _worker(self, index: int) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: (
                        self._stopping.is_set()
                        or (
                            self._pending
                            and index < self._settings_provider().paperlens_max_parallel_jobs
                            and sum(j.status == "running" for j in self._jobs.values())
                            < self._settings_provider().paperlens_max_parallel_jobs
                        )
                    )
                )
                if self._stopping.is_set():
                    return
                if self._global_slots and not self._global_slots.acquire(blocking=False):
                    self._condition.wait(timeout=0.2)
                    continue
                job_id = self._pending.popleft()
                if self._jobs[job_id].status != "queued":
                    if self._global_slots:
                        self._global_slots.release()
                    continue
                self._jobs[job_id].status = "running"
                self._working.add(job_id)
            try:
                self._process(job_id)
            except ProcessingPaused:
                with self._lock:
                    target = self._requested.pop(job_id, "paused")
                try:
                    self._update(
                        job_id,
                        status=target,
                        stage=target,
                        message="已暂停，可以继续。"
                        if target == "paused"
                        else "已取消，已完成阶段仍保留。",
                    )
                except OSError:
                    # Keep the worker available even when pause persistence fails.
                    # In memory the job has already stopped; restart recovery will
                    # see its last durable state and offer a retry.
                    with self._lock:
                        self._jobs[job_id].error = "任务已停止，但状态无法保存，请检查磁盘空间。"
            except Exception as exc:
                settings = self._snapshots.get(job_id, self.settings)
                failed_stage = self.get(job_id).stage
                secrets = [settings.llm_api_key.get_secret_value()] + [
                    route.api_key.get_secret_value() for route in settings.llm_routes.values()
                ]
                detail = traceback.format_exc()
                for secret in filter(None, secrets):
                    detail = detail.replace(secret, "[已隐藏]")
                diagnostic_saved = False
                try:
                    write_json(
                        self.root / job_id / "failure.json",
                        {
                            "job_id": job_id,
                            "stage": failed_stage,
                            "exception_type": type(exc).__name__,
                            "traceback": detail,
                        },
                    )
                    diagnostic_saved = True
                except OSError:
                    pass
                message = (
                    str(exc)
                    if isinstance(exc, PaperLensError)
                    else f"处理程序出现错误（{type(exc).__name__}）。"
                    + ("诊断记录已保存到本机。" if diagnostic_saved else "请检查本机服务日志。")
                )
                for secret in filter(None, secrets):
                    message = message.replace(secret, "[已隐藏]")
                try:
                    self._update(
                        job_id,
                        status="failed",
                        stage="failed",
                        error=message[:800],
                        message=message[:800],
                        report_relative_path=None,
                        retryable=True,
                        input_budget=({**exc.budget, "request_id": uuid.uuid4().hex}
                                      if isinstance(exc, InputBudgetExceeded) else None),
                        failures=[failure_detail(failed_stage, settings.llm_model, {
                            "message": message[:800], "error_code": getattr(exc, "code", "processing_error"),
                            "provider_error": getattr(exc, "details", {}),
                            "input_budget": getattr(exc, "budget", None),
                        })],
                    )
                except OSError:
                    pass

            finally:
                if self._global_slots:
                    self._global_slots.release()
                with self._condition:
                    self._working.discard(job_id)
                    self._prune_history()
                    self._condition.notify_all()

    def report_path(self, job: Job) -> Path:
        if job.status not in SUCCESS or not job.report_relative_path:
            raise HTTPException(409, "该任务尚未生成可阅读的看板，请查看任务状态。")
        path = (self.output_root / job.report_relative_path).resolve()
        if (
            not path.is_relative_to(self.output_root / job.id)
            or path.suffix.lower() != ".html"
            or not path.is_file()
        ):
            raise HTTPException(404, "看板文件不存在或已经移走，请重新处理该论文。")
        return path

    def health(self) -> dict:
        with self._lock:
            return {
                "ok": all(t.is_alive() for t in self._threads) and not self._stopping.is_set(),
                "queued_jobs": sum(j.status == "queued" for j in self._jobs.values()),
                "running_jobs": sum(j.status == "running" for j in self._jobs.values()),
                "paused_jobs": sum(j.status == "paused" for j in self._jobs.values()),
                "parallel_limit": self._settings_provider().paperlens_max_parallel_jobs,
            }

    def close(self) -> None:
        self._stopping.set()
        with self._condition:
            self._condition.notify_all()
            for job in self._jobs.values():
                if job.status in ACTIVE:
                    job.status = "failed"
                    job.stage = "interrupted"
                    job.error = "服务已停止；可重试，文件已移走时需重新上传。"
                    job.message = job.error
                    try:
                        self._save(job)
                    except OSError:
                        pass
        for thread in self._threads:
            thread.join(timeout=1)
