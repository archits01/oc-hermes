#!/usr/bin/env python3
"""Pin the LMI Unipile MCP runtime to the reviewed Git-controlled source.

The production virtual environment remains the dependency container.  This
script changes only ``mcp_servers.unipile.env.PYTHONPATH`` after validating the
current command, source hashes, dependency manifest, and advertised tool set.
It never reads or prints credential values and never starts an MCP or provider
request.  Service reload and the standalone MCP handshake remain explicit
operator steps after a successful apply.
"""
from __future__ import annotations

import argparse
import ast
import fcntl
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping, MutableMapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError


DEFAULT_REPO_ROOT = Path("/opt/opencomputer-v2")
DEFAULT_DATA_ROOT = Path("/opt/opencomputer-v2-data")
DEFAULT_BACKUP_ROOT = Path("/opt/opencomputer-v2-backups")
DEFAULT_LOCK_PATH = Path("/run/lmi-unipile-mcp-pin.lock")
SOURCE_RELATIVE = Path("optional-mcps/unipile/server/src")
EXPECTED_COMMAND = "/opt/opencomputer-v2-data/mcp-installs/unipile/.venv/bin/python"
EXPECTED_ARGS = ["-m", "mcp_server_unipile_extended.server"]
EXPECTED_TOOL_COUNT = 25
UNSUPPORTED_TOOLS = frozenset({"unipile_delete_post", "unipile_send_connection_request"})
EXPECTED_FILES = {
    "optional-mcps/unipile/server/pyproject.toml": "eb4e2f51203c1e4e0a24d42c3e29891d7e211df3abc962c37ff5e60b4d5f9df1",
    "optional-mcps/unipile/server/src/mcp_server_unipile_extended/__init__.py": "63475cd4bdc1845f188324d4d60addef1de8b2cca41ab282f4bace48ba217374",
    "optional-mcps/unipile/server/src/mcp_server_unipile_extended/server.py": "fc0cb51c97c06dd6add26a365a1c256f9fa670ee512d4455135f3b79983e7690",
    "optional-mcps/unipile/server/src/mcp_server_unipile_extended/unipile_client_extended.py": "a7a33b9e81c2d92f37e0e1b409aac823c8f14e3a947f6db00d3648cd709538c7",
}


class PinBlocked(RuntimeError):
    """The current state does not satisfy the narrow production contract."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def regular_file(path: Path, label: str) -> Path:
    if not path.is_file() or path.is_symlink():
        raise PinBlocked(f"{label} is not a regular file")
    return path


def verify_reviewed_source(repo_root: Path) -> tuple[Path, tuple[str, ...]]:
    source_root = repo_root / SOURCE_RELATIVE
    if not source_root.is_dir() or source_root.is_symlink():
        raise PinBlocked("reviewed Unipile source directory is unavailable")
    for relative, expected in EXPECTED_FILES.items():
        path = regular_file(repo_root / relative, f"reviewed source {relative}")
        if sha256(path) != expected:
            raise PinBlocked(f"reviewed source hash mismatch: {relative}")
        if path.suffix == ".py":
            try:
                compile(path.read_text(encoding="utf-8"), str(path), "exec")
            except (OSError, SyntaxError) as exc:
                raise PinBlocked(f"reviewed source does not compile: {relative}") from exc
    server = source_root / "mcp_server_unipile_extended/server.py"
    tools = advertised_tool_names(server)
    if len(tools) != EXPECTED_TOOL_COUNT:
        raise PinBlocked("reviewed Unipile tool count is unexpected")
    if UNSUPPORTED_TOOLS.intersection(tools):
        raise PinBlocked("reviewed Unipile source advertises an unsupported tool")
    return source_root, tools


def advertised_tool_names(server_path: Path) -> tuple[str, ...]:
    tree = ast.parse(server_path.read_text(encoding="utf-8"), filename=str(server_path))
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "Tool":
            continue
        for keyword in node.keywords:
            if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                if isinstance(keyword.value.value, str):
                    names.append(keyword.value.value)
    if len(names) != len(set(names)):
        raise PinBlocked("reviewed Unipile tool roster contains duplicate names")
    return tuple(sorted(names))


def yaml_round_trip() -> YAML:
    serializer = YAML(typ="rt")
    serializer.preserve_quotes = True
    return serializer


def load_config(config_path: Path) -> MutableMapping[str, Any]:
    regular_file(config_path, "config.yaml")
    try:
        with config_path.open(encoding="utf-8") as handle:
            loaded = yaml_round_trip().load(handle) or {}
    except (OSError, YAMLError) as exc:
        raise PinBlocked("config.yaml could not be parsed") from exc
    if not isinstance(loaded, MutableMapping):
        raise PinBlocked("config.yaml root is not a mapping")
    return loaded


def planned_config(
    config: MutableMapping[str, Any], expected_source: str
) -> tuple[MutableMapping[str, Any], bool]:
    servers = config.get("mcp_servers")
    if not isinstance(servers, MutableMapping):
        raise PinBlocked("config.yaml mcp_servers is not a mapping")
    unipile = servers.get("unipile")
    if not isinstance(unipile, MutableMapping):
        raise PinBlocked("config.yaml has no Unipile MCP mapping")
    if unipile.get("command") != EXPECTED_COMMAND:
        raise PinBlocked("Unipile MCP command is not the reviewed dependency environment")
    if unipile.get("args") != EXPECTED_ARGS:
        raise PinBlocked("Unipile MCP args are not the reviewed module entrypoint")
    env = unipile.get("env")
    if env is None:
        env = {}
        unipile["env"] = env
    if not isinstance(env, MutableMapping):
        raise PinBlocked("Unipile MCP env is not a mapping")
    current = env.get("PYTHONPATH")
    if current not in (None, expected_source):
        raise PinBlocked("Unipile MCP has an unexpected existing PYTHONPATH")
    changed = current != expected_source
    env["PYTHONPATH"] = expected_source
    return config, changed


def verify_dependency_python(data_root: Path) -> None:
    expected = data_root / "mcp-installs/unipile/.venv/bin/python"
    if not expected.is_file():
        raise PinBlocked("Unipile dependency Python is unavailable")


def backup_config(config_path: Path, backup_root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_root / f"lmi-unipile-pin-{stamp}-{os.getpid()}"
    backup.mkdir(mode=0o700, parents=True)
    target = backup / "config.yaml"
    shutil.copy2(config_path, target)
    os.chmod(target, 0o600)
    manifest = {
        "source": str(config_path),
        "sha256": sha256(target),
        "change": "mcp_servers.unipile.env.PYTHONPATH",
    }
    manifest_path = backup / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    os.chmod(manifest_path, 0o600)
    return backup


def atomic_write_config(config_path: Path, config: Mapping[str, Any]) -> None:
    fd, raw_path = tempfile.mkstemp(prefix=".config-unipile-", dir=str(config_path.parent))
    staged = Path(raw_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml_round_trip().dump(config, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(staged, 0o600)
        os.replace(staged, config_path)
    finally:
        staged.unlink(missing_ok=True)


def pin_unipile(
    *,
    repo_root: Path,
    data_root: Path,
    backup_root: Path,
    check_only: bool,
) -> dict[str, Any]:
    source_root, tools = verify_reviewed_source(repo_root)
    verify_dependency_python(data_root)
    config_path = data_root / "config.yaml"
    before_hash = sha256(regular_file(config_path, "config.yaml"))
    config = load_config(config_path)
    config, changed = planned_config(config, str(source_root))
    if check_only:
        if changed:
            raise PinBlocked("Unipile MCP is not pinned to the reviewed Git source")
        return {
            "mode": "check",
            "config_sha256": before_hash,
            "source": str(source_root),
            "tool_count": len(tools),
        }
    if not changed:
        return {
            "mode": "noop",
            "config_sha256": before_hash,
            "source": str(source_root),
            "tool_count": len(tools),
        }
    backup = backup_config(config_path, backup_root)
    atomic_write_config(config_path, config)
    verified = load_config(config_path)
    _verified, remains_changed = planned_config(verified, str(source_root))
    if remains_changed:
        raise PinBlocked("post-write Unipile pin verification failed")
    return {
        "mode": "apply",
        "backup": str(backup),
        "config_sha256": sha256(config_path),
        "source": str(source_root),
        "tool_count": len(tools),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)
    parser.add_argument("--lock-path", type=Path, default=DEFAULT_LOCK_PATH)
    args = parser.parse_args(argv)
    try:
        args.lock_path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        with args.lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = pin_unipile(
                repo_root=args.repo_root,
                data_root=args.data_root,
                backup_root=args.backup_root,
                check_only=args.check,
            )
    except (OSError, PinBlocked) as exc:
        print(f"LMI_UNIPILE_PIN_BLOCKED: {type(exc).__name__}")
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
