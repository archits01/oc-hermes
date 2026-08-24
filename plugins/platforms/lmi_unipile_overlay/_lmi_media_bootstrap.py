"""Explicit deployment bootstrap for the shared LMI media bridge.

The two deployment-owned adapters are discovered independently, but the
reviewed overlay has one global tool namespace and one inbound account
binding.  This module is therefore the only place that wires the overlay:
either adapter's ``register(ctx)`` may call :func:`bootstrap_media_deployment`,
while a process-wide guard makes the first successful call install both
channel tool pairs exactly once.

The bootstrap deliberately fails closed.  It does not register model-visible
media tools, or configure the inbound binder, until all deployment values are
present, the Hermes overlay is importable, the pinned dashboard module has the
manifest hash, and the canonical session database can be opened.  Secret
values are never included in diagnostics.
"""
from __future__ import annotations

import hashlib
import importlib.util
import logging
import os
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

try:
    from ._lmi_media_runtime import MediaOverlayError, configure_media_runtime
except ImportError:  # pragma: no cover - supports standalone source checks
    from _lmi_media_runtime import MediaOverlayError, configure_media_runtime


logger = logging.getLogger(__name__)

# This is the hash recorded by the reviewed oc-hermes overlay manifest.  A
# deployment must point at a file with this exact digest; an arbitrary module
# supplied through the environment is not an acceptable provider boundary.
PINNED_MEDIA_MODULE_SHA256 = (
    "97247849a2fef683f1af912cb54496511b2e0d84e7bf2ce1a6a524c2c2872fdd"
)

_REQUIRED_ENV = (
    "WHATSAPP_UNIPILE_DSN",
    "WHATSAPP_UNIPILE_API_KEY",
    "WHATSAPP_ACCOUNT_ID",
    "INSTAGRAM_UNIPILE_DSN",
    "INSTAGRAM_UNIPILE_API_KEY",
    "INSTAGRAM_ACCOUNT_ID",
    "LMI_CRM_DB",
    "LMI_APPROVED_MEDIA_ROOT",
    "LMI_MEDIA_SESSION_DB",
    "LMI_MEDIA_FOLLOWUP_MODULE_PATH",
)


@dataclass(frozen=True)
class MediaBootstrapResult:
    """Non-secret result of one successful shared installation."""

    registered_tools: Mapping[str, tuple[str, str]]


_bootstrap_lock = threading.RLock()
_bootstrap_result: MediaBootstrapResult | None = None


def _required_env(env: Mapping[str, str], name: str) -> str:
    value = str(env.get(name, "") or "").strip()
    if not value:
        raise MediaOverlayError(f"deployment environment {name} is required")
    return value


def _absolute_env_path(env: Mapping[str, str], name: str) -> str:
    value = _required_env(env, name)
    if not Path(value).is_absolute():
        raise MediaOverlayError(f"deployment environment {name} must be absolute")
    return value


def load_media_deployment_config(
    env: Mapping[str, str] | None = None,
    *,
    config_type: Any | None = None,
) -> Any:
    """Read the complete deployment contract without exposing secret values.

    The channel adapters already use channel-prefixed Unipile variables.  The
    bridge intentionally requires both channel values and rejects a mismatch,
    so one provider tenant cannot be silently split across the shared tools.
    ``config_type`` is injectable only for tests; production obtains the
    reviewed overlay's ``MediaBridgeDeploymentConfig`` lazily.
    """
    values = env if env is not None else os.environ
    missing = [name for name in _REQUIRED_ENV if not str(values.get(name, "") or "").strip()]
    if missing:
        # Variable names are safe diagnostics; values (especially credentials)
        # are intentionally never rendered.
        raise MediaOverlayError(
            "incomplete LMI media deployment environment: " + ", ".join(missing)
        )

    whatsapp_dsn = _required_env(values, "WHATSAPP_UNIPILE_DSN")
    instagram_dsn = _required_env(values, "INSTAGRAM_UNIPILE_DSN")
    whatsapp_key = _required_env(values, "WHATSAPP_UNIPILE_API_KEY")
    instagram_key = _required_env(values, "INSTAGRAM_UNIPILE_API_KEY")
    if whatsapp_dsn != instagram_dsn or whatsapp_key != instagram_key:
        raise MediaOverlayError("WhatsApp and Instagram must use one shared Unipile deployment")

    if config_type is None:
        try:
            from plugins.platforms.lmi_unipile_overlay.deployment import (
                MediaBridgeDeploymentConfig,
            )
        except ImportError as exc:  # pragma: no cover - production import path
            raise MediaOverlayError("reviewed LMI media deployment overlay is unavailable") from exc
        config_type = MediaBridgeDeploymentConfig

    return config_type(
        unipile_dsn=whatsapp_dsn,
        unipile_api_key=whatsapp_key,
        crm_db_path=_absolute_env_path(values, "LMI_CRM_DB"),
        approved_media_root=_absolute_env_path(values, "LMI_APPROVED_MEDIA_ROOT"),
        session_db_path=_absolute_env_path(values, "LMI_MEDIA_SESSION_DB"),
        channel_account_ids={
            "whatsapp": _required_env(values, "WHATSAPP_ACCOUNT_ID"),
            "instagram": _required_env(values, "INSTAGRAM_ACCOUNT_ID"),
        },
        channel_adapter_platform_ids={
            "whatsapp": "whatsapp_unipile",
            "instagram": "instagram",
        },
    )


def load_pinned_media_module(path: str, *, expected_sha256: str = PINNED_MEDIA_MODULE_SHA256) -> ModuleType:
    """Load only the manifest-pinned dashboard source at an absolute path."""
    module_path = Path(path)
    if not module_path.is_absolute() or module_path.name != "unipile_media_followup.py":
        raise MediaOverlayError("deployment media module path is not an absolute reviewed source")
    if not module_path.is_file():
        raise MediaOverlayError("deployment media module path does not exist")
    digest = hashlib.sha256(module_path.read_bytes()).hexdigest()
    if digest != expected_sha256:
        raise MediaOverlayError("deployment media module hash is not the reviewed manifest hash")
    spec = importlib.util.spec_from_file_location("_lmi_pinned_unipile_media_followup", module_path)
    if spec is None or spec.loader is None:
        raise MediaOverlayError("deployment media module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    module_name = spec.name
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    sys.path.insert(0, str(module_path.parent))
    try:
        spec.loader.exec_module(module)
    except Exception:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
        raise
    finally:
        try:
            sys.path.remove(str(module_path.parent))
        except ValueError:  # pragma: no cover - defensive against import hooks
            pass
    return module


def _reviewed_overlay() -> ModuleType:
    try:
        from plugins.platforms.lmi_unipile_overlay import deployment
    except ImportError as exc:  # pragma: no cover - production import path
        raise MediaOverlayError("reviewed LMI media deployment overlay is unavailable") from exc
    return deployment


def _open_session_db(config: Any, session_db_factory: Callable[..., Any] | None) -> Any:
    if session_db_factory is None:
        try:
            from hermes_state import SessionDB
        except ImportError as exc:  # pragma: no cover - production runtime
            raise MediaOverlayError("Hermes SessionDB is unavailable") from exc
        session_db_factory = SessionDB
    try:
        return session_db_factory(db_path=Path(config.session_db_path))
    except Exception as exc:
        # Do not log the exception: SQLite/provider paths can contain sensitive
        # deployment details.  The caller receives only a safe error class.
        raise MediaOverlayError("could not open configured Hermes session database") from exc


def bootstrap_media_deployment(
    ctx: Any,
    *,
    env: Mapping[str, str] | None = None,
    media_module: Any | None = None,
    overlay_deployment: Any | None = None,
    session_db: Any | None = None,
    session_db_factory: Callable[..., Any] | None = None,
    bind_event: Callable[..., Any] | None = None,
) -> MediaBootstrapResult | None:
    """Install the reviewed bridge once, returning ``None`` when blocked.

    ``media_module``, ``overlay_deployment``, and the database/fn injections
    are test seams.  Live callers should provide only ``ctx`` and let the
    explicit environment contract select the reviewed deployment inputs.
    """
    global _bootstrap_result
    with _bootstrap_lock:
        if _bootstrap_result is not None:
            return _bootstrap_result
        try:
            values = env if env is not None else os.environ
            overlay = overlay_deployment or _reviewed_overlay()
            config = load_media_deployment_config(
                values,
                config_type=getattr(overlay, "MediaBridgeDeploymentConfig", None),
            )
            inbound_scopes = overlay.VerifiedInboundMediaScopeRegistry(config)
            if session_db is None:
                session_db = _open_session_db(config, session_db_factory)
            if media_module is None:
                media_module = load_pinned_media_module(
                    _required_env(values, "LMI_MEDIA_FOLLOWUP_MODULE_PATH")
                )
            registered_tools = overlay.install_deployment_media_tools(
                ctx,
                config=config,
                media_module=media_module,
                session_db=session_db,
                inbound_scopes=inbound_scopes,
            )
            binder_fn = bind_event or overlay.bind_verified_adapter_inbound_event

            def binder(**kwargs: Any) -> Any:
                return binder_fn(
                    **kwargs,
                    config=config,
                    inbound_scopes=inbound_scopes,
                )

            # Configure only after tool installation has passed all scope,
            # provider-interface, and session-database checks.
            configure_media_runtime(binder)
            _bootstrap_result = MediaBootstrapResult(
                registered_tools=registered_tools,
            )
            return _bootstrap_result
        except Exception as exc:
            # A blocked bootstrap is deliberately retryable after deployment
            # configuration is corrected.  Never admit events with a partial
            # binder, and never include secrets in logs.
            logger.warning("LMI media bootstrap blocked (%s)", type(exc).__name__)
            return None


def clear_media_bootstrap_for_test() -> None:
    """Reset the process-wide guard for isolated unit tests."""
    global _bootstrap_result
    with _bootstrap_lock:
        _bootstrap_result = None
