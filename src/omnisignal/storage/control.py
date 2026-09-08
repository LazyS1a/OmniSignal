"""Shared operational source guard used before any new connector run."""

from __future__ import annotations

from sqlalchemy.orm import Session

from omnisignal.contracts import ConnectorFailure, ErrorCategory

from .models import SourceControlState


def assert_source_enabled(session: Session, source_id: str) -> None:
    """Fail closed when an operator has disabled this source.

    No row means there is no operational override; registry authorization is
    validated separately by each connector before this guard.
    """

    state = session.get(SourceControlState, source_id)
    if state is not None and not state.enabled:
        raise ConnectorFailure(
            ErrorCategory.POLICY_VIOLATION,
            "source is disabled by operations control",
        )
