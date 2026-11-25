"""Bounded reading requests and a coordinator that drains admitted work on pause."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from threading import Lock
from typing import TypeVar

from .control import (
    MAX_STAGE_CONCURRENCY,
    ProcessingPaused,
    capture_concurrency_limit,
    checkpoint,
    concurrency_limit,
    task_stage,
)

T = TypeVar("T")
R = TypeVar("R")


def note_concurrency() -> int:
    """Keep ordinary mode's small note window; faster modes widen independent work."""
    width = concurrency_limit()
    return max(1, width - 1)


class ReadingRequestSlots:
    """One live ceiling for the translation and note branches of a pipeline.

    Join provider network admission, so budget reservations release this permit
    while waiting just as they release the provider's global network permit.
    Calling the inherited limit afresh also makes an in-progress boost effective.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._limit = capture_concurrency_limit()
        self.active = 0
        self.peak = 0
        self._pending_branches: set[str] = set()

    def reserve_branches(self, names: Iterable[str]) -> None:
        """Let each sibling start once, then lend the whole ceiling dynamically."""
        with self._lock:
            if self._limit() > 1:
                self._pending_branches.update(names)

    def release_branch(self, name: str) -> None:
        with self._lock:
            self._pending_branches.discard(name)

    def try_acquire(self) -> bool:
        with self._lock:
            stage = task_stage()
            reserved_for_siblings = len(self._pending_branches - {stage})
            if self.active >= max(1, self._limit() - reserved_for_siblings):
                return False
            self.active += 1
            self.peak = max(self.peak, self.active)
            self._pending_branches.discard(stage)
            return True

    def release(self) -> None:
        with self._lock:
            self.active -= 1


_request_slots: ContextVar[ReadingRequestSlots | None] = ContextVar("request_slots", default=None)


@contextmanager
def request_slots():
    """Inherit one job's pool through copied contexts, including nested branches."""
    existing = _request_slots.get()
    if existing is not None:
        yield existing
        return
    pool = ReadingRequestSlots()
    token = _request_slots.set(pool)
    try:
        yield pool
    finally:
        _request_slots.reset(token)


def try_request_permit() -> bool:
    pool = _request_slots.get()
    return pool.try_acquire() if pool is not None else True


def release_request_permit() -> None:
    pool = _request_slots.get()
    if pool is not None:
        pool.release()


def coordinated_map(
    function: Callable[[T], R],
    items: Iterable[T],
    *,
    limit: Callable[[], int],
    on_result: Callable[[int, R], None],
    stop_when: Callable[[R], bool],
) -> None:
    """No executor backlog; save every completed response before re-raising pause.

    The coordinator alone publishes results. A provider failure stops new work;
    already submitted tasks remain chargeable and must still be accounted for.
    """
    values = list(items)
    pending: dict[Future[R], int] = {}
    next_index = 0
    stopped = False
    interrupted: BaseException | None = None
    with ThreadPoolExecutor(max_workers=MAX_STAGE_CONCURRENCY,
                            thread_name_prefix="paperlens-note") as executor:
        while pending or (not stopped and next_index < len(values)):
            if not stopped:
                try:
                    checkpoint()
                    while next_index < len(values) and len(pending) < limit():
                        checkpoint()
                        pending[executor.submit(copy_context().run, function,
                                                values[next_index])] = next_index
                        next_index += 1
                except BaseException as exc:
                    interrupted, stopped = exc, True
            if not pending:
                break
            done, _ = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
            for future in sorted(done, key=lambda candidate: pending[candidate]):
                index = pending.pop(future)
                try:
                    result = future.result()
                    on_result(index, result)
                    stopped = stopped or stop_when(result)
                except ProcessingPaused as exc:
                    interrupted, stopped = exc, True
                except BaseException as exc:
                    interrupted, stopped = exc, True
        if interrupted:
            raise interrupted
        checkpoint()
