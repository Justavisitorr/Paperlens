"""Public, credential-free errors that CLI and web UI can explain."""


class PaperLensError(Exception):
    code = "processing_error"


class InputError(PaperLensError):
    code = "invalid_pdf"


class ConfigurationError(PaperLensError):
    code = "configuration_error"


class ProviderError(PaperLensError):
    code = "provider_error"


class ProviderRejected(ProviderError):
    """A terminal HTTP rejection, with bounded credential-free diagnostics."""

    code = "provider_rejected"

    def __init__(self, message: str, details: dict):
        self.details = details
        super().__init__(message)


class ProviderOutcomeUnknown(ProviderError):
    """A request may have been submitted, but no terminal result was received."""

    code = "provider_outcome_unknown"


class ValidationError(PaperLensError):
    code = "validation_error"


class InputBudgetExceeded(ValidationError):
    """Local preflight failure: nothing has been submitted for this request."""

    code = "input_budget_exceeded"

    def __init__(self, limit: int, required: int, stage: str):
        import math

        suggested = min(1_000_000, math.ceil(max(required * 1.1, limit + 2000) / 1000) * 1000)
        self.budget = {
            "limit_chars": limit, "required_chars": required,
            "suggested_chars": suggested if required <= 1_000_000 else None,
            "maximum_chars": 1_000_000, "stage": stage,
        }
        super().__init__(
            f"{stage}超过单次输入长度上限：当前 {limit:,} 字符，至少需要 {required:,} 字符。"
            "本次超限输入未发送，已完成结果保留。"
        )
