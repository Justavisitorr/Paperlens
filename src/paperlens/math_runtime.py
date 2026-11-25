"""Shared local math models and cancellable queues; never downloads resources."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock, RLock
from typing import Literal

from .control import checkpoint

DeviceMode = Literal["auto", "cpu", "cuda"]
EventCallback = Callable[[str, dict], None]
RUNTIME_VERSION = "2-fp32-eager-greedy"


class MathUnavailable(RuntimeError):
    """A local recognizer needs prepared resources or a usable execution device."""


def event(callback: EventCallback | None, name: str, **data) -> None:
    if callback is not None:
        callback(name, data)


@contextmanager
def waiting_lock(lock, callback: EventCallback | None = None, **data) -> Iterator[None]:
    started = time.monotonic()
    checkpoint()
    acquired = lock.acquire(blocking=False)
    try:
        if not acquired:
            event(callback, "queued", **data)
        while not acquired:
            checkpoint()
            acquired = lock.acquire(timeout=0.1)
        checkpoint()
        event(callback, "queue_done", seconds=round(time.monotonic() - started, 3), **data)
        yield
    finally:
        if acquired:
            lock.release()


_keys_guard = Lock()
_key_locks: dict[str, tuple[Lock, int]] = {}


@contextmanager
def recognition_key_lock(
    key: str, on_event: EventCallback | None = None,
) -> Iterator[None]:
    """Acquire before re-reading a cache entry; no successes or failures are stored."""
    with _keys_guard:
        lock, users = _key_locks.get(key, (Lock(), 0))
        _key_locks[key] = (lock, users + 1)
    try:
        with waiting_lock(lock, on_event, queue="cache"):
            yield
    finally:
        with _keys_guard:
            _, users = _key_locks[key]
            if users == 1:
                del _key_locks[key]
            else:
                _key_locks[key] = (lock, users - 1)


@dataclass
class SharedMathModel:
    processor: object
    model: object
    device: str
    lock: RLock = field(default_factory=RLock)


_models_guard = RLock()
_models: dict[tuple[str, str], SharedMathModel] = {}
# A failed GPU stays off for automatic work until process restart. Explicit CUDA
# remains retryable, and a successful explicit attempt re-enables automatic use.
_auto_cpu_fallbacks: set[str] = set()
_precision_lock = RLock()


@contextmanager
def strict_fp32(device: str, callback: EventCallback | None = None) -> Iterator[None]:
    """Serialize this runtime's TF32 changes; other Torch users share these globals."""
    if device != "cuda":
        yield
        return
    import torch

    with waiting_lock(_precision_lock, callback, queue="precision", device=device):
        matmul, cudnn = torch.backends.cuda.matmul, torch.backends.cudnn
        if hasattr(matmul, "fp32_precision"):
            targets = [(matmul, "fp32_precision"), (cudnn, "fp32_precision")]
            for name in ("conv", "rnn"):
                backend = getattr(cudnn, name, None)
                if backend is not None and hasattr(backend, "fp32_precision"):
                    targets.append((backend, "fp32_precision"))
            desired = "ieee"
        else:
            targets = [(matmul, "allow_tf32"), (cudnn, "allow_tf32")]
            desired = False
        previous = [(backend, name, getattr(backend, name)) for backend, name in targets]
        try:
            for backend, name in targets:
                setattr(backend, name, desired)
            yield
        finally:
            for backend, name, value in reversed(previous):
                setattr(backend, name, value)


def selected_device(key: str, mode: DeviceMode) -> str:
    if mode not in {"auto", "cpu", "cuda"}:
        raise ValueError("Math device must be auto, cpu or cuda")
    if mode == "cpu" or (mode == "auto" and key in _auto_cpu_fallbacks):
        return "cpu"
    try:
        import torch

        available = torch.cuda.is_available()
    except (ImportError, OSError, RuntimeError) as exc:
        if mode == "cuda":
            raise MathUnavailable("CUDA 无法加载；请改用自动选择或 CPU。") from exc
        return "cpu"
    if available:
        return "cuda"
    if mode == "cuda":
        raise MathUnavailable("当前 PyTorch 或设备不支持 CUDA；请改用自动选择或 CPU。")
    return "cpu"


def _load_backend(model_dir: Path) -> tuple[object, object]:
    import torch
    from transformers import AutoModelForImageTextToText, AutoProcessor

    processor = AutoProcessor.from_pretrained(model_dir, local_files_only=True)
    model = AutoModelForImageTextToText.from_pretrained(
        model_dir, local_files_only=True, dtype=torch.float32, attn_implementation="eager",
    )
    model.eval()
    return processor, model


def shared_model(
    key: str, model_dir: Path, mode: DeviceMode, on_event: EventCallback | None = None,
) -> SharedMathModel:
    device = selected_device(key, mode)
    with waiting_lock(_models_guard, on_event, queue="loading", device=device):
        # Another automatic caller may have fallen back while we queued.
        device = selected_device(key, mode)
        existing = _models.get((key, device))
        if existing is not None:
            event(on_event, "loaded", device=existing.device, shared=True, seconds=0.0)
            return existing
        started = time.monotonic()
        event(on_event, "loading", device=device)
        try:
            processor, model = _load_backend(model_dir)
        except (ImportError, OSError, ValueError, RuntimeError) as exc:
            raise MathUnavailable(
                f"本地数学识别依赖无法加载（{type(exc).__name__}）；保留原页公式。"
            ) from exc
        try:
            model = model.to(device)
        except (RuntimeError, AssertionError) as exc:
            if mode != "auto" or device != "cuda":
                raise MathUnavailable("本地数学模型无法放入所选设备；保留原页公式。") from exc
            event(on_event, "device_fallback", device="cpu", previous_device="cuda",
                  reason=type(exc).__name__)
            try:
                model = model.to("cpu")
            except (RuntimeError, AssertionError) as fallback_exc:
                raise MathUnavailable("GPU 与 CPU 数学模型均无法加载；保留原页公式。") from fallback_exc
            device = "cpu"
            _auto_cpu_fallbacks.add(key)
            existing = _models.get((key, "cpu"))
            if existing is not None:
                event(on_event, "loaded", device="cpu", shared=True,
                      seconds=round(time.monotonic() - started, 3))
                return existing
        shared = SharedMathModel(processor, model, device)
        # Bound retained idle models. Existing recognizers keep their own shared
        # references, so eviction cannot mutate an active inference.
        while len(_models) >= 2:
            del _models[next(iter(_models))]
        _models[(key, device)] = shared
        if device == "cuda":
            _auto_cpu_fallbacks.discard(key)
        event(on_event, "loaded", device=device, shared=False,
              seconds=round(time.monotonic() - started, 3))
        return shared


def fallback_to_cpu(
    key: str, shared: SharedMathModel, on_event: EventCallback | None,
) -> SharedMathModel:
    """Called while this model's inference lock is held, after an automatic GPU failure."""
    event(on_event, "device_fallback", device="cpu", previous_device="cuda",
          reason="cuda_execution_failed")
    with waiting_lock(_models_guard, on_event, queue="fallback", device="cpu"):
        existing = _models.get((key, "cpu"))
        if existing is not None and existing is not shared:
            # A CPU caller may already be using that instance. Retire the failed
            # GPU object; its queued callers must select the shared CPU queue.
            shared.device = "retired"
            shared.model = None
            shared.processor = None
            _auto_cpu_fallbacks.add(key)
            _models.pop((key, "cuda"), None)
            return existing
        try:
            shared.model = shared.model.to("cpu")
        except (RuntimeError, AssertionError) as exc:
            raise MathUnavailable("GPU 回退到 CPU 失败；保留原页公式。") from exc
        shared.device = "cpu"
        _auto_cpu_fallbacks.add(key)
        _models.pop((key, "cuda"), None)
        _models[(key, "cpu")] = shared
    return shared
