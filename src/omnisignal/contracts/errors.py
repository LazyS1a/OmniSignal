"""Stable connector error taxonomy and default operational action."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCategory(StrEnum):
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    RATE_LIMIT = "rate_limit"
    TRANSIENT_UPSTREAM = "transient_upstream"
    SCHEMA_DRIFT = "schema_drift"
    POLICY_VIOLATION = "policy_violation"
    INVALID_CONFIG = "invalid_config"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    RUNNER_CRASH = "runner_crash"
    UNKNOWN = "unknown"


class ErrorAction(StrEnum):
    RETRY = "retry"
    PAUSE = "pause"
    QUARANTINE = "quarantine"
    DISABLE = "disable"


_DEFAULT_ACTIONS = {
    ErrorCategory.AUTHENTICATION: ErrorAction.PAUSE,
    ErrorCategory.PERMISSION: ErrorAction.PAUSE,
    ErrorCategory.RATE_LIMIT: ErrorAction.RETRY,
    ErrorCategory.TRANSIENT_UPSTREAM: ErrorAction.RETRY,
    ErrorCategory.SCHEMA_DRIFT: ErrorAction.QUARANTINE,
    ErrorCategory.POLICY_VIOLATION: ErrorAction.DISABLE,
    ErrorCategory.INVALID_CONFIG: ErrorAction.DISABLE,
    ErrorCategory.RESOURCE_EXHAUSTED: ErrorAction.PAUSE,
    ErrorCategory.RUNNER_CRASH: ErrorAction.RETRY,
    ErrorCategory.UNKNOWN: ErrorAction.PAUSE,
}


def default_action(category: ErrorCategory) -> ErrorAction:
    return _DEFAULT_ACTIONS[category]


class ConnectorFailure(Exception):
    """Typed connector failure with deliberately non-sensitive details."""

    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        *,
        retry_after_seconds: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.action = default_action(category)
        self.retry_after_seconds = retry_after_seconds
        self.details = details or {}
