"""Cooperative checkpoints and bounded, dynamically accelerated independent work."""

from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from threading import RLock
from typing import TypeVar

from .errors import PaperLensError

PDF_LOCK = RLock()
_checkpoint: ContextVar[Callable[[], None] | None] = ContextVar("checkpoint", default=None)
_concurrency: ContextVar[Callable[[], int] | None] = ContextVar("concurrency", default=None)
_task_stage: ContextVar[str] = ContextVar("task_stage", default="unknown")
T = TypeVar("T")
R = TypeVar("R")
MAX_STAGE_CONCURRENCY = 16


class ProcessingPaused(PaperLensError):
    """Completed work stays cached; in-flight requests may still be billed."""


def checkpoint() -> None:
    callback = _checkpoint.get()
    if callback is not None:
        callback()


def concurrency_limit() -> int:
    callback = _concurrency.get()
    return max(1, min(MAX_STAGE_CONCURRENCY, callback())) if callback else 2


def capture_concurrency_limit() -> Callable[[], int]:
    """Retain the job's live ceiling across narrower nested worker contexts."""
    callback = _concurrency.get()
    return lambda: max(1, min(MAX_STAGE_CONCURRENCY, callback())) if callback else 2


def task_stage() -> str:
    return _task_stage.get()


@contextmanager
def processing_stage(name: str) -> Iterator[None]:
    token = _task_stage.set(name)
    try:
        yield
    finally:
        _task_stage.reset(token)


@contextmanager
def processing_concurrency(limit: int) -> Iterator[None]:
    """Limit a nested branch without replacing its inherited pause callback."""
    token = _concurrency.set(lambda: limit)
    try:
        yield
    finally:
        _concurrency.reset(token)


@contextmanager
def processing_control(
    callback: Callable[[], None], concurrency: Callable[[], int] | None = None
) -> Iterator[None]:
    token = _checkpoint.set(callback)
    parallel_token = _concurrency.set(concurrency)
    try:
        yield
    finally:
        _concurrency.reset(parallel_token)
        _checkpoint.reset(token)


def ordered_parallel_map(
    function: Callable[[T], R],
    items: Iterable[T],
    *,
    on_complete: Callable[[int, int], None] | None = None,
    stop_when: Callable[[R], bool] | None = None,
) -> list[R]:
    """Run independent stages; merge in input order and keep in-flight cache writes.

    No executor backlog is created: a boost takes effect while a request is
    waiting, and pause stops new launches. Exiting waits for already submitted
    stages to finish, allowing their validated responses to reach the cache.
    """
    values = list(items)
    results: dict[int, R] = {}
    pending: dict[Future[R], int] = {}
    next_index = 0
    stopped = False
    with ThreadPoolExecutor(max_workers=MAX_STAGE_CONCURRENCY, thread_name_prefix="paperlens-stage") as executor:
        while (not stopped and next_index < len(values)) or pending:
            checkpoint()
            limit = concurrency_limit()
            while not stopped and next_index < len(values) and len(pending) < limit:
                checkpoint()
                context = copy_context()
                future = executor.submit(context.run, function, values[next_index])
                pending[future] = next_index
                next_index += 1
            done, _ = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
            for future in done:
                index = pending.pop(future)
                results[index] = future.result()
                if stop_when and stop_when(results[index]):
                    stopped = True
                if on_complete:
                    on_complete(index, len(results))
        checkpoint()
    return [results[index] for index in sorted(results)]
