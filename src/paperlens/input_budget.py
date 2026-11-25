"""Explicit, in-memory input allowances; never change saved model settings."""

from contextlib import contextmanager
from contextvars import ContextVar

from .errors import InputBudgetExceeded, ValidationError

_allowance: ContextVar[int | None] = ContextVar("paperlens_input_allowance", default=None)


def input_limit(settings) -> int:
    return max(settings.llm_max_input_chars, _allowance.get() or 0)


def approved_limit(payload: dict, pending: dict | None = None) -> int:
    if (not isinstance(payload, dict) or payload.get("confirmed") is not True
            or type(payload.get("limit_chars")) is not int
            or not 2000 <= payload["limit_chars"] <= 1_000_000):
        raise ValidationError("请先确认本次输入上限和可能增加的模型费用。")
    limit = payload["limit_chars"]
    if pending and (pending.get("suggested_chars") is None
                    or limit != pending["suggested_chars"]):
        raise InputBudgetExceeded(pending["limit_chars"], pending["required_chars"], pending["stage"])
    return limit


@contextmanager
def temporary_input_budget(limit: int | None):
    token = _allowance.set(limit)
    try:
        yield
    finally:
        _allowance.reset(token)


def largest_input_budget(value) -> dict | None:
    """Collect bounded stage audits, not prompts or credentials."""
    found = []
    if isinstance(value, dict):
        if isinstance(value.get("input_budget"), dict):
            found.append(value["input_budget"])
        found.extend(filter(None, (largest_input_budget(v) for k, v in value.items()
                                   if k != "input_budget")))
    elif isinstance(value, list):
        found.extend(filter(None, (largest_input_budget(v) for v in value)))
    return max(found, key=lambda item: item["required_chars"], default=None)
