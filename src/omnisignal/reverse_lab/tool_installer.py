"""Install selected reverse-lab tools from pinned official GitHub assets."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
from urllib.request import Request, urlopen
import zipfile

import yaml


class ToolInstallError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_extract_zip(archive: Path, destination: Path) -> None:
    destination_root = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            candidate = (destination / member.filename).resolve()
            try:
                candidate.relative_to(destination_root)
            except ValueError as exc:
                raise ToolInstallError("tool archive contains a path traversal entry") from exc
        bundle.extractall(destination)


def _load_tool(lock_path: Path, tool_name: str) -> tuple[Path, dict[str, object]]:
    lock = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    if lock.get("source_policy") != "official_github_releases_only":
        raise ToolInstallError("tool lock source policy is not approved")
    tools = lock.get("tools", {})
    if tool_name not in tools:
        raise ToolInstallError("tool is not present in the lock file")
    spec = tools[tool_name]
    asset = spec.get("asset")
    if not isinstance(asset, dict):
        raise ToolInstallError("select the target platform and lock an asset before installing this tool")
    repository = str(spec.get("repository", ""))
    repository_path = repository.removeprefix("https://github.com/").strip("/")
    asset_url = str(asset.get("url", ""))
    if not repository_path or not asset_url.startswith(f"https://github.com/{repository_path}/releases/download/"):
        raise ToolInstallError("tool asset is not an official locked GitHub release URL")
    expected_hash = str(asset.get("sha256", ""))
    if re.fullmatch(r"[a-f0-9]{64}", expected_hash) is None:
        raise ToolInstallError("tool asset SHA-256 is invalid")
    install_root = Path(str(lock.get("install_root", ""))).resolve()
    if install_root.drive.upper() != "D:":
        raise ToolInstallError("reverse tools must be installed on D drive")
    return install_root, spec


def install_tool(lock_path: Path, tool_name: str, cache_root: Path) -> Path:
    install_root, spec = _load_tool(lock_path, tool_name)
    version = str(spec["version"])
    asset = spec["asset"]
    assert isinstance(asset, dict)
    asset_name = str(asset["name"])
    expected_hash = str(asset["sha256"])
    asset_url = str(asset["url"])
    destination = install_root / tool_name / version
    receipt_path = destination / "installation-receipt.json"
    if receipt_path.is_file():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt.get("asset_sha256") == expected_hash and receipt.get("source_url") == asset_url:
            return destination
        raise ToolInstallError("existing installation receipt does not match the lock; refusing to overwrite")
    if destination.exists():
        raise ToolInstallError("installation directory already exists without a valid receipt")

    cache_root.mkdir(parents=True, exist_ok=True)
    cached_asset = cache_root / asset_name
    if not cached_asset.is_file() or sha256_file(cached_asset) != expected_hash:
        partial = cache_root / f"{asset_name}.part"
        request = Request(asset_url, headers={"User-Agent": "OmniSignal-ReverseLab-Installer/1.0"})
        with urlopen(request, timeout=60) as response, partial.open("wb") as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)
        if sha256_file(partial) != expected_hash:
            partial.unlink(missing_ok=True)
            raise ToolInstallError("downloaded tool asset failed SHA-256 verification")
        partial.replace(cached_asset)

    destination.mkdir(parents=True)
    if asset_name.lower().endswith(".zip"):
        safe_extract_zip(cached_asset, destination)
    else:
        shutil.copy2(cached_asset, destination / asset_name)
    receipt_path.write_text(
        json.dumps(
            {
                "tool": tool_name,
                "version": version,
                "tag": spec["tag"],
                "source_url": asset_url,
                "asset_name": asset_name,
                "asset_sha256": expected_hash,
                "installed_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install a locked reverse-lab tool from official GitHub releases")
    parser.add_argument("tool", choices=("ghidra", "jadx", "apktool", "frida", "mitmproxy"))
    parser.add_argument("--lock", type=Path, default=Path("reverse_lab/tools.lock.yaml"))
    parser.add_argument("--cache", type=Path, default=Path("D:/CodexCache/OmniSignal/reverse-tools"))
    args = parser.parse_args(argv)
    try:
        destination = install_tool(args.lock, args.tool, args.cache)
    except (OSError, ValueError, KeyError, ToolInstallError, zipfile.BadZipFile) as exc:
        print(json.dumps({"status": "rejected", "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps({"status": "installed", "tool": args.tool, "path": str(destination)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
