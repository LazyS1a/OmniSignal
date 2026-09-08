"""Deterministic project-owned fixture for the processing SDK."""

from __future__ import annotations

import os
import time
from typing import Any

from omnisignal.processing.hooks import hookimpl


class DeterministicTextStatsPlugin:
    @hookimpl
    def process_batch(
        self, records: list[dict[str, Any]], options: dict[str, object]
    ) -> list[dict[str, Any]]:
        mode = options.get("mode", "ok")
        if mode == "crash":
            raise RuntimeError("fixture crash")
        if mode == "hang":
            time.sleep(5)
        outputs: list[dict[str, Any]] = []
        for record in records:
            fields = record.get("fields", {})
            title = fields.get("title") if isinstance(fields, dict) else None
            text = fields.get("text") if isinstance(fields, dict) else None
            values: dict[str, object] = {
                "title_chars": len(title) if isinstance(title, str) else 0,
                "text_chars": len(text) if isinstance(text, str) else 0,
            }
            if mode == "extra_field":
                values["undeclared"] = True
            if mode == "environment_probe":
                values = {"unrelated_env_visible": bool(os.environ.get("OMNISIGNAL_UNRELATED_SECRET"))}
            outputs.append(
                {
                    "normalized_id": record["normalized_id"],
                    "values": values,
                    "quality_status": "accepted",
                    "quality_codes": [],
                }
            )
        return outputs


plugin = DeterministicTextStatsPlugin()
