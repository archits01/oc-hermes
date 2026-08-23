"""Tests for the Git-source pin used by the production Unipile MCP."""
from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest
import yaml


REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts/ops/lmi_unipile_mcp_pin.py"


def load_pin_module():
    spec = importlib.util.spec_from_file_location("lmi_unipile_mcp_pin_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture_data(tmp_path: Path):
    pin = load_pin_module()
    data_root = tmp_path / "data"
    dependency_python = data_root / "mcp-installs/unipile/.venv/bin/python"
    dependency_python.parent.mkdir(parents=True)
    dependency_python.write_text("#!/bin/sh\n", encoding="utf-8")
    config = {
        "mcp_servers": {
            "unipile": {
                "command": pin.EXPECTED_COMMAND,
                "args": list(pin.EXPECTED_ARGS),
                "enabled": True,
                "tools": {"include": ["unipile_get_accounts"]},
                "env": {
                    "UNIPILE_API_KEY": "${UNIPILE_API_KEY}",
                    "UNIPILE_DSN": "${UNIPILE_DSN}",
                },
            }
        }
    }
    config_path = data_root / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    config_path.chmod(0o600)
    return pin, data_root, config_path


def test_apply_preserves_credentials_filters_and_creates_recoverable_backup(tmp_path):
    pin, data_root, config_path = fixture_data(tmp_path)
    backup_root = tmp_path / "backups"
    result = pin.pin_unipile(
        repo_root=REPO,
        data_root=data_root,
        backup_root=backup_root,
        check_only=False,
    )
    assert result["mode"] == "apply"
    updated = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    unipile = updated["mcp_servers"]["unipile"]
    assert unipile["enabled"] is True
    assert unipile["tools"] == {"include": ["unipile_get_accounts"]}
    assert unipile["env"]["UNIPILE_API_KEY"] == "${UNIPILE_API_KEY}"
    assert unipile["env"]["UNIPILE_DSN"] == "${UNIPILE_DSN}"
    assert unipile["env"]["PYTHONPATH"] == str(REPO / pin.SOURCE_RELATIVE)
    backup = Path(result["backup"])
    assert (backup / "config.yaml").is_file()
    assert (backup / "MANIFEST.json").is_file()


def test_apply_is_idempotent_and_check_passes(tmp_path):
    pin, data_root, _config_path = fixture_data(tmp_path)
    kwargs = {
        "repo_root": REPO,
        "data_root": data_root,
        "backup_root": tmp_path / "backups",
    }
    pin.pin_unipile(**kwargs, check_only=False)
    assert pin.pin_unipile(**kwargs, check_only=False)["mode"] == "noop"
    assert pin.pin_unipile(**kwargs, check_only=True)["mode"] == "check"


def test_check_fails_when_pin_is_absent(tmp_path):
    pin, data_root, _config_path = fixture_data(tmp_path)
    with pytest.raises(pin.PinBlocked, match="not pinned"):
        pin.pin_unipile(
            repo_root=REPO,
            data_root=data_root,
            backup_root=tmp_path / "backups",
            check_only=True,
        )


@pytest.mark.parametrize("field", ["command", "args"])
def test_unexpected_launch_contract_fails_closed(tmp_path, field):
    pin, data_root, config_path = fixture_data(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["mcp_servers"]["unipile"][field] = "unexpected"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    with pytest.raises(pin.PinBlocked, match=f"MCP {field}"):
        pin.pin_unipile(
            repo_root=REPO,
            data_root=data_root,
            backup_root=tmp_path / "backups",
            check_only=False,
        )


def test_unexpected_existing_pythonpath_fails_closed(tmp_path):
    pin, data_root, config_path = fixture_data(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["mcp_servers"]["unipile"]["env"]["PYTHONPATH"] = "/unexpected"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    with pytest.raises(pin.PinBlocked, match="unexpected existing PYTHONPATH"):
        pin.pin_unipile(
            repo_root=REPO,
            data_root=data_root,
            backup_root=tmp_path / "backups",
            check_only=False,
        )


def test_source_hash_drift_fails_before_config_write(tmp_path):
    pin, data_root, config_path = fixture_data(tmp_path)
    repo_copy = tmp_path / "repo"
    shutil.copytree(REPO / "optional-mcps/unipile", repo_copy / "optional-mcps/unipile")
    before = config_path.read_bytes()
    server = repo_copy / "optional-mcps/unipile/server/src/mcp_server_unipile_extended/server.py"
    server.write_bytes(server.read_bytes() + b"\n# drift\n")
    with pytest.raises(pin.PinBlocked, match="hash mismatch"):
        pin.pin_unipile(
            repo_root=repo_copy,
            data_root=data_root,
            backup_root=tmp_path / "backups",
            check_only=False,
        )
    assert config_path.read_bytes() == before


def test_reviewed_roster_is_exact_and_excludes_unsupported_tools():
    pin = load_pin_module()
    _source, tools = pin.verify_reviewed_source(REPO)
    assert len(tools) == 25
    assert not pin.UNSUPPORTED_TOOLS.intersection(tools)
