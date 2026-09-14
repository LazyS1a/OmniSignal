from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_windows_docker_launchers_delegate_to_bounded_powershell_scripts() -> None:
    open_cmd = (ROOT / "用Docker打开OmniSignal总控台.cmd").read_text(encoding="utf-8")
    close_cmd = (ROOT / "关闭Docker版OmniSignal总控台.cmd").read_text(encoding="utf-8")

    assert "scripts\\start-docker-console.ps1" in open_cmd
    assert "scripts\\stop-docker-console.ps1" in close_cmd
    assert "ExecutionPolicy RemoteSigned" in open_cmd
    assert "ExecutionPolicy RemoteSigned" in close_cmd


def test_docker_start_script_keeps_secrets_private_and_scheduler_off() -> None:
    script = (ROOT / "scripts" / "start-docker-console.ps1").read_text(encoding="utf-8")

    assert "artifacts\\private\\docker-console" in script
    assert "OMNISIGNAL_DB_PASSWORD" in script
    assert "OMNISIGNAL_LOCAL_CONSOLE_TOKEN" in script
    assert "OMNISIGNAL_CONTROL_PRINCIPALS_JSON" in script
    assert "OMNISIGNAL_DATA_DIR" in script
    assert "OMNISIGNAL_APP_DATA_DIR" in script
    assert "Join-Path $privateRoot 'postgres'" in script
    assert "Join-Path $privateRoot 'app'" in script
    assert "docker compose up -d --build --wait" in script
    assert "docker compose ps --status running --services" in script
    assert "Get-NetTCPConnection -State Listen" in script
    assert "OMNISIGNAL_SCHEDULER_ENABLED" not in script
    assert "[int]$UiPort = 8501" in script
    assert '"http://127.0.0.1:$UiPort"' in script


def test_docker_start_script_supports_windows_powershell_51_crypto() -> None:
    script = (ROOT / "scripts" / "start-docker-console.ps1").read_text(encoding="utf-8")

    assert "RandomNumberGenerator]::Create()" in script
    assert ".GetBytes($bytes)" in script
    assert "SHA256]::Create()" in script
    assert ".ComputeHash($bytes)" in script
    assert "[BitConverter]::ToString" in script
    assert "::Fill(" not in script
    assert "::HashData(" not in script
    assert "[Convert]::ToHexString" not in script


def test_docker_stop_script_preserves_data_and_volumes() -> None:
    script = (ROOT / "scripts" / "stop-docker-console.ps1").read_text(encoding="utf-8")

    assert "docker compose down --remove-orphans" in script
    assert " down -v" not in script
    assert "Remove-Item" not in script


def test_local_stop_script_reads_chinese_paths_as_utf8_in_windows_powershell() -> None:
    script = (ROOT / "scripts" / "stop-console.ps1").read_text(encoding="utf-8")

    assert "Get-Content -LiteralPath $statePath -Raw -Encoding utf8" in script
