"""Pluggy hookspec shared by all processing plugins."""

from __future__ import annotations

from typing import Any

import pluggy


hookspec = pluggy.HookspecMarker("omnisignal_processing")
hookimpl = pluggy.HookimplMarker("omnisignal_processing")


class ProcessingHooks:
    @hookspec(firstresult=True)
    def process_batch(
        self, records: list[dict[str, Any]], options: dict[str, object]
    ) -> list[dict[str, Any]]:
        """Transform a bounded batch without mutating core normalized records."""
