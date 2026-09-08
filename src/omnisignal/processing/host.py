"""Subprocess host that loads one approved Pluggy implementation."""

from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
from typing import Any

import pluggy

from .contracts import PluginJob, PluginOutputRecord, PluginResult, output_value_matches_type
from .hooks import ProcessingHooks
from .protocol import PROTOCOL_VERSION, SCHEMA_FINGERPRINT


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    try:
        job = PluginJob.model_validate_json(args.job.read_text(encoding="utf-8"))
        if job.protocol_version != PROTOCOL_VERSION or job.schema_fingerprint != SCHEMA_FINGERPRINT:
            return 20
    except Exception:
        return 20
    try:
        module_name, _, attribute_name = job.entrypoint.partition(":")
        plugin = getattr(importlib.import_module(module_name), attribute_name)
        manager = pluggy.PluginManager("omnisignal_processing")
        manager.add_hookspecs(ProcessingHooks)
        manager.register(plugin, name=job.plugin_id)
        raw_outputs = manager.hook.process_batch(
            records=[record.model_dump(mode="json") for record in job.records],
            options=job.options,
        )
    except Exception:
        return 22
    try:
        if not isinstance(raw_outputs, list):
            return 23
        outputs = tuple(PluginOutputRecord.model_validate(value) for value in raw_outputs)
        _validate_outputs(job, outputs)
        result = PluginResult(
            protocol_version=PROTOCOL_VERSION,
            schema_fingerprint=SCHEMA_FINGERPRINT,
            plugin_id=job.plugin_id,
            plugin_version=job.plugin_version,
            output_schema_version=job.output_schema_version,
            outputs=outputs,
        )
        _write_result(args.result, result.model_dump(mode="json"))
        return 0
    except Exception:
        # Parent receives only a classified non-zero exit. Validation details and
        # record content are deliberately not copied to stdout/stderr.
        return 23


def _validate_outputs(job: PluginJob, outputs: tuple[PluginOutputRecord, ...]) -> None:
    input_ids = {record.normalized_id for record in job.records}
    output_ids = [record.normalized_id for record in outputs]
    if len(output_ids) != len(set(output_ids)) or not set(output_ids).issubset(input_ids):
        raise ValueError("plugin output identities are invalid")
    allowed = set(job.output_fields)
    for output in outputs:
        if not set(output.values).issubset(allowed):
            raise ValueError("plugin output contains undeclared fields")
        if any(not output_value_matches_type(value, job.output_types[field]) for field, value in output.values.items()):
            raise ValueError("plugin output value type differs from the declared schema")


def _write_result(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


if __name__ == "__main__":
    raise SystemExit(main())
