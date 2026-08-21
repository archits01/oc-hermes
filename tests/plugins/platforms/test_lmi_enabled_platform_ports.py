"""Tests for enabled-platform listener health-gate resolution."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "scripts/ops/lmi_enabled_platform_ports.py"


def load_ports_module():
    spec = importlib.util.spec_from_file_location("lmi_enabled_platform_ports_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def config(*, linkedin: bool = False):
    return {
        "platforms": {
            "linkedin": {"enabled": linkedin},
            "instagram": {"enabled": True},
            "whatsapp_unipile": {"enabled": True},
        }
    }


def test_disabled_linkedin_does_not_require_8643():
    ports = load_ports_module()
    assert ports.enabled_platform_ports(config(linkedin=False)) == (8645, 8646)
    assert ports.missing_platform_ports(config(linkedin=False), {8645, 8646}) == ()


def test_enabled_linkedin_missing_listener_fails_closed():
    ports = load_ports_module()
    assert ports.enabled_platform_ports(config(linkedin=True)) == (8643, 8645, 8646)
    assert ports.missing_platform_ports(config(linkedin=True), {8645, 8646}) == (8643,)


def test_malformed_enabled_field_is_rejected():
    ports = load_ports_module()
    malformed = config()
    malformed["platforms"]["instagram"]["enabled"] = "false"
    with pytest.raises(ports.PlatformPortConfigError, match="must be boolean"):
        ports.enabled_platform_ports(malformed)


def test_shell_gates_delegate_enabled_platform_resolution():
    for name in (
        "lmi_opencomputer_v2_update.sh",
        "opencomputer-v2-health-monitor.sh",
        "lmi_opencomputer_v2_finalize_cleanup.sh",
    ):
        source = (SCRIPT.parent / name).read_text(encoding="utf-8")
        assert "lmi_enabled_platform_ports.py" in source
        assert "8643 8645 8646" not in source
