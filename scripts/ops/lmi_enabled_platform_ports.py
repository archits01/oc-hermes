#!/usr/bin/env python3
"""Resolve health-gate ports from the enabled LMI platform configuration."""
from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


PLATFORM_PORTS = {
    "linkedin": 8643,
    "instagram": 8645,
    "whatsapp_unipile": 8646,
}


class PlatformPortConfigError(ValueError):
    """The platform configuration cannot safely drive a health gate."""


def enabled_platform_ports(config: Mapping[str, Any]) -> tuple[int, ...]:
    """Return ports for configured, explicitly enabled LMI platforms.

    Disabled or absent platforms impose no listener requirement. Malformed
    enabled fields fail closed rather than being coerced from strings.
    """
    if not isinstance(config, Mapping):
        raise PlatformPortConfigError("config must be a mapping")
    platforms = config.get("platforms")
    if not isinstance(platforms, Mapping):
        raise PlatformPortConfigError("config.platforms must be a mapping")
    ports: list[int] = []
    for name, port in PLATFORM_PORTS.items():
        settings = platforms.get(name)
        if settings is None:
            continue
        if not isinstance(settings, Mapping):
            raise PlatformPortConfigError(f"platform {name} configuration is malformed")
        enabled = settings.get("enabled", False)
        if not isinstance(enabled, bool):
            raise PlatformPortConfigError(f"platform {name}.enabled must be boolean")
        if enabled:
            ports.append(port)
    return tuple(ports)


def missing_platform_ports(
    config: Mapping[str, Any], listening_ports: Iterable[int]
) -> tuple[int, ...]:
    """Return enabled-platform ports absent from the observed listeners."""
    listening = {int(port) for port in listening_ports}
    return tuple(port for port in enabled_platform_ports(config) if port not in listening)


def load_config(path: Path) -> Mapping[str, Any]:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - depends on runtime image
        raise PlatformPortConfigError("PyYAML is unavailable") from exc
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise PlatformPortConfigError("platform config could not be read") from exc
    if not isinstance(loaded, Mapping):
        raise PlatformPortConfigError("platform config must be a mapping")
    return loaded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    args = parser.parse_args(argv)
    try:
        print(" ".join(str(port) for port in enabled_platform_ports(load_config(args.config))))
    except PlatformPortConfigError as exc:
        print(f"PLATFORM_PORTS_BLOCKED: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
