"""Settings and code-owned state for one extraction run."""

from dataclasses import dataclass, field
from uuid import uuid4


@dataclass(frozen=True)
class ExtractionSettings:
    model: str
    max_model_calls: int = 40
    max_tool_calls: int = 80
    max_consecutive_tool_errors: int = 3
    max_output_tokens: int = 16000
    api_retries: int = 2
    max_validation_attempts: int = 3
    validation_model: str = ""
    validation_max_output_tokens: int = 8000
    max_tool_output_bytes: int = 4_000_000
    request_timeout_seconds: float = 60.0

    def __post_init__(self):
        if not self.model.strip():
            raise ValueError("An extraction model must be supplied")
        for name in (
            "max_model_calls",
            "max_validation_attempts",
            "validation_max_output_tokens",
            "max_tool_calls",
            "max_consecutive_tool_errors",
            "max_output_tokens",
            "max_tool_output_bytes",
            "request_timeout_seconds",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.api_retries < 0:
            raise ValueError("api_retries must not be negative")


@dataclass
class ExtractionRun:
    run_id: str = field(default_factory=lambda: uuid4().hex)
    validation_reports: list[dict] = field(default_factory=list)
    final_checks: dict | None = None
    state: str = "partial"
    response: dict | None = None
    errors: list[str] = field(default_factory=list)
    tool_errors: list[dict] = field(default_factory=list)
    inventory: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    model_calls: int = 0
    tool_calls: int = 0
    elapsed_seconds: float = 0.0
    last_output_text: str | None = None
