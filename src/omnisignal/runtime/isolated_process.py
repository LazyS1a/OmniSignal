"""Bounded JSON-file subprocess protocol reused by connectors and processors."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from enum import StrEnum
from pathlib import Path
from uuid import uuid4


class WorkerErrorKind(StrEnum):
    TIMEOUT = "timeout"
    CRASH = "crash"
    OUTPUT_TOO_LARGE = "output_too_large"
    INVALID_OUTPUT = "invalid_output"


class IsolatedWorkerError(RuntimeError):
    def __init__(self, kind: WorkerErrorKind, message: str, *, return_code: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.return_code = return_code


async def run_json_worker(
    *,
    module_name: str,
    document: dict[str, object],
    workspace: Path,
    timeout_seconds: int,
    max_output_bytes: int,
    environment_overrides: dict[str, str] | None = None,
) -> dict[str, object]:
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_.]*", module_name):
        raise ValueError("worker module name is invalid")
    resolved_workspace = workspace.resolve()
    jobs = (resolved_workspace / "jobs").resolve()
    jobs.mkdir(parents=True, exist_ok=True)
    job_id = uuid4().hex
    job_path = (jobs / f"{job_id}.job.json").resolve()
    result_path = (jobs / f"{job_id}.result.json").resolve()
    if resolved_workspace not in job_path.parents or resolved_workspace not in result_path.parents:
        raise ValueError("worker path escaped workspace")
    temporary = job_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(temporary, job_path)
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            module_name,
            "--job",
            str(job_path),
            "--result",
            str(result_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=_minimal_environment(environment_overrides),
        )
        try:
            await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except TimeoutError as exc:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
            raise IsolatedWorkerError(WorkerErrorKind.TIMEOUT, "worker timed out") from exc
        if process.returncode != 0 or not result_path.is_file():
            raise IsolatedWorkerError(
                WorkerErrorKind.CRASH,
                "worker stopped without a result",
                return_code=process.returncode,
            )
        if result_path.stat().st_size > max_output_bytes:
            raise IsolatedWorkerError(WorkerErrorKind.OUTPUT_TOO_LARGE, "worker output exceeded size limit")
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise IsolatedWorkerError(WorkerErrorKind.INVALID_OUTPUT, "worker output is not valid JSON") from exc
        if not isinstance(result, dict):
            raise IsolatedWorkerError(WorkerErrorKind.INVALID_OUTPUT, "worker output must be an object")
        return result
    finally:
        temporary.unlink(missing_ok=True)
        job_path.unlink(missing_ok=True)
        result_path.unlink(missing_ok=True)


def _minimal_environment(overrides: dict[str, str] | None) -> dict[str, str]:
    source_root = str(Path(__file__).resolve().parents[2])
    environment = {"PYTHONPATH": source_root, "PYTHONUTF8": "1"}
    for name in ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"):
        if value := os.environ.get(name):
            environment[name] = value
    if overrides:
        for name, value in overrides.items():
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", name) or not isinstance(value, str):
                raise ValueError("worker environment override is invalid")
            environment[name] = value
    return environment
