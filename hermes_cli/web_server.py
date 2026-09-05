"""Hermes Agent — Web UI server: FastAPI app assembly, auth/host middleware, ``start_server``.

Route handlers live in ``web_routers/``; their helpers live in the sibling
``web_server_<concern>`` modules and are re-imported here so ``web_server.<name>``
stays the single late-binding seam tests monkeypatch (``web_deps.late``).
Usage: ``python -m hermes_cli.main web [--port 8080]``.
"""

from contextlib import asynccontextmanager

import asyncio
from collections import deque
import hmac
import logging
import os
import re
import secrets
import subprocess
import sys
import sysconfig
import threading
import time
import urllib.parse

from hermes_cli.install_identity import get_install_id as _shared_get_install_id
from hermes_cli.pty_session import run_reaper
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from hermes_cli import __version__
from hermes_cli.config import load_config

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
except ImportError:
    # First try lazy-installing the dashboard extras. Only the user actually
    # running `hermes dashboard` needs fastapi+uvicorn; lazy install keeps
    # them out of every other install path. After install, re-import.
    try:
        from tools.lazy_deps import ensure as _lazy_ensure
        _lazy_ensure("tool.dashboard", prompt=False)
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import JSONResponse
    except Exception:
        raise SystemExit(
            "Web UI requires fastapi and uvicorn.\n"
            f"Install with: {sys.executable} -m pip install 'fastapi' 'uvicorn[standard]'"
        )

WEB_DIST = Path(os.environ["HERMES_WEB_DIST"]) if "HERMES_WEB_DIST" in os.environ else Path(__file__).parent / "web_dist"
_log = logging.getLogger(__name__)


from hermes_cli.web_server_lifecycle import (  # noqa: E402
    PORT_IN_USE_EXIT_CODE,
    _dashboard_forwarded_allow_ips,
    _eager_reconcile_own_session_db,
    _maybe_open_browser,
    _port_bind_conflict,
    _read_bound_port,
    _report_port_in_use,
    _start_parent_death_watchdog,
    _warm_gateway_module,
    _write_dashboard_ready_file,
    _write_machine_sentinel_line,
)


def _start_desktop_cron_ticker(stop_event: "threading.Event", interval: int = 60) -> None:
    """Tick the cron scheduler from inside the desktop dashboard backend.

    The desktop spawns a ``hermes dashboard`` backend, not a gateway, so without
    this a cron created in the app would never fire (no live adapters; delivery
    falls back to the per-platform send path). The primary backend outlives the
    per-profile pool (reaped after ~10 idle minutes), so it ticks EVERY local
    profile's store like a multiplex gateway; external providers keep the
    single-store behavior (registries are not profile-scoped). Cross-process
    safe: the built-in tick takes the per-store ``cron/.tick.lock``.

    Every local profile's store is ticked, not just this backend's own (#69377's desktop sibling): the
    desktop pools per-profile backends and reaps them after ~10 idle minutes, so a secondary profile's
    ticker dies with its backend and that profile's jobs silently stop firing until the user next opens it
    ("tasks on the sleeping profile could be idle" — community report, Aug 2026).
    """
    from cron.scheduler_provider import InProcessCronScheduler, resolve_cron_scheduler

    provider = resolve_cron_scheduler()

    start_kwargs: dict = {"interval": interval}
    if isinstance(provider, InProcessCronScheduler):
        try:
            from hermes_cli.profiles import profiles_to_serve

            profile_homes = list(profiles_to_serve(multiplex=True))
            if len(profile_homes) > 1:
                start_kwargs["profile_homes"] = profile_homes
                # Stand down, per tick, for a profile whose OWN gateway runs:
                # it ticks with live adapters, and the tick-lock race would
                # otherwise deliver through the standalone path (#100489).
                from hermes_cli.profiles import _check_gateway_running

                start_kwargs["profile_gate"] = lambda _name, home: not _check_gateway_running(Path(home))
                from hermes_logging import enable_profile_log_routing

                enable_profile_log_routing(profile_homes)
                _log.info(
                    "Desktop cron scheduler will tick %d profile(s): %s",
                    len(profile_homes),
                    [name for name, _home in profile_homes],
                )
        except Exception:
            # Fail open to the single-store ticker so the active profile keeps firing.
            _log.exception("Desktop cron: profile enumeration failed; ticking active profile only")

    _log.info("Desktop cron scheduler started (provider=%s, interval=%ds)", provider.name, interval)
    provider.start(stop_event, **start_kwargs)


# Desktop `serve` only (start_server(start_mcp_discovery_after_bind=True)):
# seconds after the READY sentinel before the MCP discovery thread starts.
_DESKTOP_MCP_DISCOVERY_DELAY_S = 1.0


@asynccontextmanager
async def _lifespan(app: "FastAPI"):
    app.state.event_channels = {}  # dict[str, set]
    app.state.event_lock = asyncio.Lock()
    app.state.pty_active_session_files = {}  # dict[str, Path]
    # Serializes chat-argv resolution so concurrent /api/pty connections don't
    # overlap ``npm install`` / ``npm run build``. Locks live on app.state (not
    # module globals) so they bind to the running loop, not the import-time one.
    app.state.chat_argv_lock = asyncio.Lock()

    # Bring state.db schema current BEFORE the first session-list poll
    # (#79531/#80037): a store left behind by `hermes update` otherwise 500s
    # every poll while the read-probe heal loses to sibling lock contention.
    # Daemon thread so a locked store never delays the socket (Desktop
    # ready-probe times out at 10s, GH-73083).
    threading.Thread(
        target=_eager_reconcile_own_session_db,
        daemon=True,
        name="statedb-eager-reconcile",
    ).start()

    # Import hermes_cli.gateway *before* the yield: on Windows + 3.11 the
    # import holds the GIL, so run_in_executor still froze the loop 15-22s and
    # the Desktop's 10s ready-probe timed out (GH-73083).
    _warm_gateway_module()

    # Snapshot the checkout revision so lazy-import paths (model picker) can
    # refuse with "restart required" after `hermes update` replaced the code
    # (#86207); the update flow does not reliably restart the dashboard.
    from gateway.code_skew import record_boot_fingerprint

    record_boot_fingerprint()

    # Hosted Bot rooms belong to the backend process. Recovery may need a
    # contended state.db migration, so keep it off the pre-yield path: Group
    # Chat must degrade on its own rather than block every Desktop feature.
    from tui_gateway import methods_groups as _hosted_groups
    import tui_gateway.server  # noqa: F401

    hosted_room_start_cancel = threading.Event()

    def _start_hosted_rooms() -> None:
        try:
            _hosted_groups.start_hosted_room_service()
        except Exception:
            _log.exception("Hosted Group Chat recovery failed during backend startup")
        finally:
            if hosted_room_start_cancel.is_set():
                _hosted_groups.stop_hosted_room_service(timeout=1.0)

    hosted_room_start_thread = threading.Thread(
        target=_start_hosted_rooms,
        daemon=True,
        name="hosted-room-startup",
    )
    hosted_room_start_thread.start()

    # Desktop-spawned backends (HERMES_DESKTOP=1) fire cron jobs themselves,
    # since the app has no gateway running the scheduler. Server `hermes
    # dashboard` is unaffected — it relies on its own gateway.
    cron_stop: "threading.Event | None" = None
    cron_thread: "threading.Thread | None" = None
    if os.getenv("HERMES_DESKTOP") == "1":
        # Reap an orphaned gateway from an abnormal previous exit (reparented to
        # launchd, still holding the platform WebSocket) before forking a fresh
        # one that would race the same credential (#77276). Runs
        # unconditionally; protection of a healthy standalone gateway lives
        # INSIDE the reaper (registration probed with cleanup_stale=False).
        try:
            from hermes_cli.gateway import _reap_unsupervised_gateway_orphans

            _reap_unsupervised_gateway_orphans()
        except Exception:
            _log.exception("Desktop startup: orphan gateway reap failed")

        cron_stop = threading.Event()
        cron_thread = threading.Thread(
            target=_start_desktop_cron_ticker,
            args=(cron_stop,),
            daemon=True,
            name="desktop-cron-ticker",
        )
        cron_thread.start()

    # Reap idle/dead keep-alive PTY sessions (30-min TTL).
    pty_reaper_task = asyncio.create_task(run_reaper(PTY_REGISTRY))
    # Periodic authenticated self-test feeding the ``dashboard`` component on /api/status.
    selftest_task = asyncio.create_task(_dashboard_selftest_loop())
    # Live auto-archive timer, independent of list requests.
    auto_archive_task = asyncio.create_task(_auto_archive_ticker_loop())

    # Managed local runtime (local_runtime.enabled): bring llama-server back so a
    # restart doesn't strand a llamacpp main model. Off-thread and best-effort;
    # failure falls back to cloud providers like a cold start. Server only —
    # models load on first inference (an empty router holds no VRAM).
    def _boot_local_runtime():
        try:
            from hermes_cli.config import load_config
            from hermes_cli.local_runtime.bootstrap import ensure_local_runtime

            ensure_local_runtime(load_config())
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning("local runtime boot failed: %s", exc)

    threading.Thread(target=_boot_local_runtime, daemon=True, name="local-runtime-boot").start()

    try:
        yield
    finally:
        hosted_room_start_cancel.set()
        _hosted_groups.stop_hosted_room_service(timeout=5.0)
        hosted_room_start_thread.join(timeout=1.0)
        if cron_stop is not None:
            cron_stop.set()
        pty_reaper_task.cancel()
        selftest_task.cancel()
        auto_archive_task.cancel()
        await PTY_REGISTRY.close_all()
        # Stop the managed llama-server with its parent (an orphan pins VRAM).
        try:
            from hermes_cli.local_runtime.bootstrap import shutdown_local_runtime

            shutdown_local_runtime()
        except Exception:  # noqa: BLE001
            pass
        if os.getenv("HERMES_DESKTOP") == "1":
            _terminate_desktop_managed_gateway()


def _app_state_default(app: "FastAPI", name: str, factory):
    """Return ``app.state.<name>``, lazily creating it for non-``with`` TestClient usages.

    The lifespan normally initialises these on the running event loop (an
    asyncio.Lock created at import time binds to whatever loop was active then).
    """
    try:
        return getattr(app.state, name)
    except AttributeError:
        value = factory()
        setattr(app.state, name, value)
        return value


def _get_chat_argv_lock(app: "FastAPI") -> asyncio.Lock:
    return _app_state_default(app, "chat_argv_lock", asyncio.Lock)


def _get_pty_active_session_files(app: "FastAPI") -> dict[str, Path]:
    return _app_state_default(app, "pty_active_session_files", dict)


app = FastAPI(title="Hermes Agent", version=__version__, lifespan=_lifespan)


# Memory-provider OAuth connect routes live in the memory layer, not here.
from hermes_cli.memory_oauth import router as _memory_oauth_router  # noqa: E402

app.include_router(_memory_oauth_router)

# Session token for sensitive endpoints. The desktop shell mints it via
# HERMES_DASHBOARD_SESSION_TOKEN; otherwise fresh per server start. It dies with
# the process and is injected into the SPA HTML so only the web UI can use it.
def _resolve_session_token() -> str:
    return os.environ.get("HERMES_DASHBOARD_SESSION_TOKEN") or secrets.token_urlsafe(32)


_SESSION_TOKEN = _resolve_session_token()
_SESSION_HEADER_NAME = "X-Hermes-Session-Token"
_SSH_OWNER_NONCE: Optional[str] = None
_SSH_RUNTIME_PURELIB: Optional[Tuple[str, int, int]] = None
_SSH_RUNTIME_MARKER: Optional[str] = None


def _apply_ssh_session_token(token: str) -> None:
    global _SESSION_TOKEN
    if token:
        _SESSION_TOKEN = token


def _apply_ssh_owner_nonce(nonce: Optional[str]) -> None:
    global _SSH_OWNER_NONCE, _SSH_RUNTIME_PURELIB, _SSH_RUNTIME_MARKER
    _SSH_OWNER_NONCE = nonce
    _SSH_RUNTIME_PURELIB = None
    _SSH_RUNTIME_MARKER = None
    if nonce:
        try:
            purelib = sysconfig.get_paths()["purelib"]
        except (KeyError, OSError):
            return
        # Primary identity: a marker FILE in site-packages. A replaced venv
        # loses it deterministically; pip installs leave it. A bare (dev, ino)
        # snapshot alone is NOT enough: ext4 reuses directory inodes at once,
        # so `rm -rf venv && uv venv` can land on the same inode undetected.
        try:
            marker = os.path.join(purelib, f".hermes-ssh-runtime-{nonce}")
            with open(marker, "w", encoding="utf-8") as fh:
                fh.write(f"pid={os.getpid()}\n")
            _SSH_RUNTIME_MARKER = marker
        except OSError:
            pass  # read-only site-packages — fall back to the stat snapshot
        try:
            st = os.stat(purelib)
            _SSH_RUNTIME_PURELIB = (purelib, st.st_dev, st.st_ino)
        except OSError:
            pass


def _ssh_runtime_intact() -> bool:
    if _SSH_RUNTIME_MARKER is not None:
        return os.path.isfile(_SSH_RUNTIME_MARKER)
    # Fallback (read-only site-packages): directory identity snapshot — weaker
    # (inode reuse) but catches cross-device moves and version-bump paths.
    if _SSH_RUNTIME_PURELIB is None:
        return True
    purelib, device, inode = _SSH_RUNTIME_PURELIB
    try:
        st = os.stat(purelib)
    except OSError:
        return False
    return (st.st_dev, st.st_ino) == (device, inode)


# In-browser Chat tab (/chat, /api/pty, /api/ws): always enabled. A module
# constant (not an inlined True) so the WS endpoints and SPA token injection
# share one testable seam.
_DASHBOARD_EMBEDDED_CHAT_ENABLED = True

# Desktop file.attach sends a whole base64 data URL in one JSON-RPC frame;
# uvicorn's 16 MiB default rejects files under the 256 MiB raw attach cap.
_DESKTOP_ATTACHMENT_WS_MAX_BYTES = 384 * 1024 * 1024


# CORS: localhost origins only — allow_origins=["*"] on 0.0.0.0 would let any
# website read/modify config and secrets.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)

# Endpoints that do NOT require the session token; everything else under /api/
# is gated below. Shared with the OAuth gate so the two allowlists cannot
# drift (/api/status once 401'd under the OAuth gate, breaking the portal probe).
from hermes_cli.dashboard_auth.public_paths import PUBLIC_API_PATHS as _PUBLIC_API_PATHS


def _has_valid_session_token(request: Request) -> bool:
    """True if the request carries a valid dashboard session token.

    The dedicated header avoids collisions with reverse proxies that already use
    ``Authorization`` (Caddy ``basic_auth``); the legacy Bearer path stays for
    older dashboard bundles.
    """
    session_header = request.headers.get(_SESSION_HEADER_NAME, "")
    if session_header and hmac.compare_digest(session_header.encode(), _SESSION_TOKEN.encode()):
        return True
    auth = request.headers.get("authorization", "")
    return hmac.compare_digest(auth.encode(), f"Bearer {_SESSION_TOKEN}".encode())


# Routes that may also authenticate via ``?token=`` (download links opened by
# the OS shell / a new tab, where no header can be set). Kept narrow.
_QUERY_TOKEN_API_PATHS: frozenset[str] = frozenset({"/api/files/download"})


def _has_valid_query_token(request: Request, path: str) -> bool:
    if path not in _QUERY_TOKEN_API_PATHS:
        return False
    token = request.query_params.get("token", "")
    return bool(token) and hmac.compare_digest(token.encode(), _SESSION_TOKEN.encode())


def _require_token(request: Request) -> None:
    """Authorize a sensitive endpoint, raising 401 if the caller isn't allowed.

    Loopback mode (``auth_required`` False): validate the SPA-injected
    ``_SESSION_TOKEN``. Gated mode: the token is NOT injected (cookie auth), and
    ``gated_auth_middleware`` already 401'd anything without a verified
    ``request.state.session`` — requiring the absent token here would make every
    ``_require_token`` endpoint unreachable behind the gate, so defer to it.
    """
    if getattr(request.app.state, "auth_required", False):
        ok = getattr(request.state, "session", None) is not None
    else:
        ok = _has_valid_session_token(request)
    if not ok:
        raise HTTPException(status_code=401, detail="Unauthorized")


# Accepted Host values for loopback binds. DNS rebinding TTL-flips an attacker
# hostname to 127.0.0.1 so the browser treats it as same-origin; validating Host
# at the app layer rejects it. See GHSA-ppp5-vxwm-4cf7.
_LOOPBACK_HOST_VALUES: frozenset = frozenset({"localhost", "127.0.0.1", "::1"})


def _dashboard_public_hosts() -> frozenset[str]:
    """Return the exact hostname declared by ``dashboard.public_url``.

    One source of truth for OAuth redirects, Host and WS Origin validation.
    Malformed or unset values fail closed as an empty set.
    """
    from hermes_cli.dashboard_auth.prefix import resolve_public_url

    public_url = resolve_public_url()
    try:
        hostname = urllib.parse.urlparse(public_url).hostname if public_url else None
    except ValueError:
        hostname = None
    return frozenset({hostname.lower()}) if hostname else frozenset()


def should_require_auth(host: str, allow_public: bool = False) -> bool:
    """True iff the auth gate must be active: any non-loopback bind.

    RFC1918 / CGNAT / link-local are deliberately PUBLIC — a hostile LAN device
    is the threat model. ``allow_public`` (legacy ``--insecure``) is accepted for
    old launch scripts but IGNORED since the June 2026 hermes-0day campaign.
    """
    return host not in _LOOPBACK_HOST_VALUES


def should_require_dashboard_auth(
    host: str,
    trusted_public_hosts: Optional[frozenset[str]] = None,
) -> bool:
    """Gate required for a non-loopback bind OR a non-loopback ``dashboard.public_url``.

    Callers may pass the already-resolved host set so startup and request
    validation share one snapshot.
    """
    if trusted_public_hosts is None:
        trusted_public_hosts = _dashboard_public_hosts()
    return should_require_auth(host) or any(h not in _LOOPBACK_HOST_VALUES for h in trusted_public_hosts)


def _desktop_loopback_auth_exempt(
    host: str,
    ssh_session_token: Optional[str] = None,
    ssh_owner_nonce: Optional[str] = None,
) -> bool:
    """True for a Desktop-owned loopback backend (#96490).

    A non-loopback ``dashboard.public_url`` would otherwise engage the
    ticket-only gate for the private loopback backends Desktop spawns, whose
    per-spawn session token the gate's WS path refuses — Desktop could not boot.
    The public dashboard is a separate non-loopback process that stays gated, so
    this never opens the public surface. Requires ALL of: loopback bind,
    ``HERMES_DESKTOP=1``, and an operator-minted credential (env token, SSH
    session token, or owner nonce).
    """
    return (
        host in _LOOPBACK_HOST_VALUES
        and os.environ.get("HERMES_DESKTOP") == "1"
        and bool(os.environ.get("HERMES_DASHBOARD_SESSION_TOKEN") or ssh_session_token or ssh_owner_nonce)
    )


def _host_header_hostname(host_header: str) -> str:
    """Return a normalized hostname from a valid HTTP Host authority.

    Host headers are authorities, not full URLs. Reject ambiguous ports,
    malformed IPv6 brackets, and URL syntax so validation always fails closed.
    """
    value = (host_header or "").strip()
    if not value or "://" in value or any(c in value for c in '"\'<> \n\r\t/?#@'):
        return ""

    if value.startswith("["):
        close = value.find("]")
        if close == -1:
            return ""
        hostname = value[1:close]
        # Bracket notation is reserved for IPv6 literals.
        if ":" not in hostname:
            return ""
        suffix = value[close + 1:]
        if suffix and not re.fullmatch(r":\d+", suffix):
            return ""
        return hostname.lower()

    # Unbracketed IPv6 authorities are ambiguous with a port separator.
    if value.count(":") > 1:
        return ""
    if ":" in value:
        hostname, port = value.rsplit(":", 1)
        if not hostname or not port.isdigit():
            return ""
        return hostname.lower()
    return value.lower()


def _is_accepted_host(
    host_header: str,
    bound_host: str,
    trusted_public_hosts: frozenset[str] = frozenset(),
) -> bool:
    """True if the Host header targets the interface we bound to.

    Accepts:
    - Exact bound host (with or without port suffix)
    - Loopback aliases when bound to loopback
    - Exact operator-declared public hosts (with or without port suffix)
    - Any host when bound to 0.0.0.0 (explicit opt-in to non-loopback,
      no protection possible at this layer)
    """
    host_only = _host_header_hostname(host_header)
    if not host_only:
        return False
    # All-interfaces bind: no Host-layer defence is possible; rely on operator
    # network controls.
    if host_only in trusted_public_hosts or bound_host in {"0.0.0.0", "::"}:
        return True
    bound_lc = bound_host.lower()
    if bound_lc in _LOOPBACK_HOST_VALUES:
        return host_only in _LOOPBACK_HOST_VALUES
    return host_only == bound_lc


@app.middleware("http")
async def host_header_middleware(request: Request, call_next):
    """Reject requests whose Host header doesn't match the bound interface (DNS rebinding, GHSA-ppp5-vxwm-4cf7)."""
    # app.state.bound_host is set by start_server() at listen time.
    bound_host = getattr(app.state, "bound_host", None)
    if bound_host and not _is_accepted_host(
        request.headers.get("host", ""), bound_host, getattr(app.state, "trusted_public_hosts", frozenset())
    ):
        return JSONResponse(
            status_code=400,
            content={
                "detail": (
                    "Invalid Host header. Dashboard requests must use the "
                    "bound hostname or the configured public hostname."
                ),
            },
        )
    return await call_next(request)


@app.middleware("http")
async def _plugin_api_runtime_gate(request: Request, call_next):
    """Block requests to disabled plugin API routes at request time.

    :func:`_mount_plugin_api_routes` gates at import time; a plugin disabled
    while running keeps its router mounted until restart, so enforce on every
    ``/api/plugins/{name}/...`` request. Registered BEFORE the auth middlewares
    (runs AFTER them): an unauthenticated caller must get auth's 401, never this
    404, or the status code becomes a plugin-name oracle.
    """
    path = request.url.path
    # parts: ['', 'api', 'plugins', '<name>', ...]
    parts = path.split("/")
    plugin_name = parts[3] if path.startswith("/api/plugins/") and len(parts) >= 4 else ""
    # Only gate authenticated requests. Unauthenticated ones fall through so
    # auth_middleware / the OAuth gate return 401 first and this route can't
    # be used as a plugin-name oracle.
    if plugin_name and (
        getattr(request.state, "token_authenticated", False)
        or getattr(request.app.state, "auth_required", False)
        or _has_valid_session_token(request)
        or _has_valid_query_token(request, path)
    ):
        try:
            # Gate: only serve user plugins that are in plugins.enabled and not in plugins.disabled. This
            # prevents the frontend from loading JS/CSS from plugins the user has not explicitly activated.
            # (#46435)
            from hermes_cli.plugins_cmd import _get_enabled_set, _get_disabled_set
            enabled_set = _get_enabled_set()
            disabled_set = _get_disabled_set()
        except Exception:
            enabled_set = set()
            disabled_set = set()
        # Source from the cached plugin list; unknown => user plugin (safe default — blocks).
        plugin = next((p for p in _get_dashboard_plugins() if p.get("name") == plugin_name), None)
        source = plugin.get("source") if plugin else "user"
        blocked = plugin_name in disabled_set or (source == "user" and plugin_name not in enabled_set)
        if blocked and source in ("user", "bundled"):
            return JSONResponse(status_code=404, content={"detail": "Plugin not found"})
    return await call_next(request)


@app.middleware("http")
async def _dashboard_auth_gate(request: Request, call_next):
    """OAuth gate — active only when start_server flags ``auth_required``; pass-through on loopback.

    Registered between host_header and auth_middleware: host check → cookie auth → token auth.
    """
    from hermes_cli.dashboard_auth.middleware import gated_auth_middleware
    return await gated_auth_middleware(request, call_next)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Require the session token on all /api/ routes except the public list.

    Skipped for requests the token-auth seam already authenticated
    (``token_authenticated``) and when the OAuth gate is active — cookie auth is
    then authoritative and the loopback-only token path must not override it.
    """
    path = request.url.path
    if (
        not getattr(request.state, "token_authenticated", False)
        and not getattr(request.app.state, "auth_required", False)
        and path.startswith("/api/")
        and path not in _PUBLIC_API_PATHS
        and not path.startswith("/api/mcp/oauth/callback/")
        and not _has_valid_session_token(request)
        and not _has_valid_query_token(request, path)
    ):
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return await call_next(request)


@app.middleware("http")
async def _token_auth_seam(request: Request, call_next):
    """Outermost auth seam: bearer-token auth for opted-in routes (registered LAST = runs FIRST).

    A registered token route is owned here — authenticate, attach the principal
    + ``token_authenticated`` so downstream gates skip enforcement. Non-token
    routes pass through untouched.
    """
    from hermes_cli.dashboard_auth.token_auth import token_auth_middleware
    return await token_auth_middleware(request, call_next)


_DASHBOARD_HEALTH_WINDOW_SECONDS = 300.0


class DashboardHealth:
    """Dashboard-process health: rolling unhandled-error/5xx window + periodic self-test result.

    Feeds ``components`` on the PUBLIC ``/api/status``, so :meth:`snapshot`
    exports counts and enums only — never ``last_error_type``/``last_error_path``.
    """

    def __init__(self, window_seconds: float = _DASHBOARD_HEALTH_WINDOW_SECONDS) -> None:
        self.window_seconds = window_seconds
        self._error_times: "deque[float]" = deque(maxlen=256)
        self.last_error_type: Optional[str] = None
        self.last_error_path: Optional[str] = None  # internal-only, never serialized
        self.last_error_at: Optional[float] = None
        self.selftest_status: str = "unknown"  # unknown | ok | failing
        self.selftest_http_status: Optional[int] = None
        self.selftest_at: Optional[float] = None

    def record_error(self, exc_type: str, path: str) -> None:
        now = time.time()
        self._error_times.append(now)
        self.last_error_type = exc_type
        self.last_error_path = path
        self.last_error_at = now

    def record_selftest(self, passed: bool, http_status: Optional[int]) -> None:
        self.selftest_status = "ok" if passed else "failing"
        self.selftest_http_status = http_status
        self.selftest_at = time.time()

    def recent_error_count(self) -> int:
        cutoff = time.time() - self.window_seconds
        while self._error_times and self._error_times[0] < cutoff:
            self._error_times.popleft()
        return len(self._error_times)

    def snapshot(self) -> Dict[str, Any]:
        """Public component payload: status enum + counts + timestamps only."""
        errors = self.recent_error_count()
        status = "degraded" if (errors or self.selftest_status == "failing") else "ok"
        return {
            "status": status,
            "recent_unhandled_errors": errors,
            "last_error_at": self.last_error_at,
            "selftest": self.selftest_status,
        }


DASHBOARD_HEALTH = DashboardHealth()


@app.middleware("http")
async def _dashboard_health_middleware(request: Request, call_next):
    """Outermost middleware (registered last): count unhandled exceptions and 5xx; re-raises, never alters."""
    try:
        response = await call_next(request)
    except Exception as exc:
        DASHBOARD_HEALTH.record_error(type(exc).__name__, request.url.path)
        raise
    if response.status_code >= 500:
        DASHBOARD_HEALTH.record_error(f"http_{response.status_code}", request.url.path)
    return response


# Authenticated-route self-test: one in-process request per minute against a
# cheap DB-touching route, catching "liveness fine but every authed request 500s".
_DASHBOARD_SELFTEST_INTERVAL_SECONDS = 60.0
_DASHBOARD_SELFTEST_ROUTE = "/api/sessions?limit=1"


async def _dashboard_selftest_once() -> None:
    """Run one authenticated in-process self-test request and record it."""
    try:
        import httpx
    except ImportError:
        return  # optional dependency — leave status "unknown"
    try:
        # Loopback base_url so the Host-header middleware accepts the request.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            resp = await client.get(_DASHBOARD_SELFTEST_ROUTE, headers={_SESSION_HEADER_NAME: _SESSION_TOKEN})
        DASHBOARD_HEALTH.record_selftest(resp.status_code == 200, resp.status_code)
    except Exception:
        DASHBOARD_HEALTH.record_selftest(False, None)


async def _dashboard_selftest_loop() -> None:
    """Periodic self-test driver started from the lifespan."""
    try:
        import httpx  # noqa: F401
    except ImportError:
        _log.debug("httpx unavailable — dashboard self-test disabled")
        return
    while True:
        await asyncio.sleep(_DASHBOARD_SELFTEST_INTERVAL_SECONDS)
        # OAuth-gated binds don't honour the session token; the probe would false-alarm 401.
        if getattr(app.state, "auth_required", False):
            continue
        await _dashboard_selftest_once()




# Action registries/spawner are owned by web_server_gateway; routers and tests reach them
# there, so this module reads them through the module too (one patch seam).
from hermes_cli import web_server_gateway as _gateway_mod  # noqa: E402
from hermes_cli.web_server_gateway import _ACTION_LOG_FILES, _terminate_desktop_managed_gateway  # noqa: E402
from hermes_cli.web_server_sessions import _auto_archive_ticker_loop  # noqa: E402
from hermes_cli.web_server_chat import PTY_REGISTRY  # noqa: E402
from hermes_cli.web_server_dashboard import (  # noqa: E402
    _discover_dashboard_plugins, _mount_plugin_api_routes, mount_spa,
)


_GATEWAY_HEALTH_URL = os.getenv("GATEWAY_HEALTH_URL")
_GATEWAY_HEALTH_TIMEOUT_MAX = 1.0
try:
    _GATEWAY_HEALTH_TIMEOUT = float(os.getenv("GATEWAY_HEALTH_TIMEOUT", "1"))
except (ValueError, TypeError):
    _log.warning(
        "Invalid GATEWAY_HEALTH_TIMEOUT value %r — using default 1.0s",
        os.getenv("GATEWAY_HEALTH_TIMEOUT"),
    )
    _GATEWAY_HEALTH_TIMEOUT = 1.0
if _GATEWAY_HEALTH_TIMEOUT <= 0:
    _log.warning(
        "Invalid non-positive GATEWAY_HEALTH_TIMEOUT value %.3fs — using default 1.0s",
        _GATEWAY_HEALTH_TIMEOUT,
    )
    _GATEWAY_HEALTH_TIMEOUT = 1.0
elif _GATEWAY_HEALTH_TIMEOUT > _GATEWAY_HEALTH_TIMEOUT_MAX:
    _log.warning(
        "Capping GATEWAY_HEALTH_TIMEOUT %.3fs to %.3fs for dashboard liveness probes",
        _GATEWAY_HEALTH_TIMEOUT,
        _GATEWAY_HEALTH_TIMEOUT_MAX,
    )
    _GATEWAY_HEALTH_TIMEOUT = _GATEWAY_HEALTH_TIMEOUT_MAX


_MANAGED_FILE_MAX_BYTES = 100 * 1024 * 1024
_FS_DATA_URL_MAX_BYTES = 16 * 1024 * 1024
# Multipart uploads stream to a temp file in fixed chunks and rename into
# place: constant memory, no base64 inflation, no proxy body-size 502s (NS-501).
_UPLOAD_CHUNK_BYTES = 1024 * 1024

# Stable install identity for /api/status: one uuid4 hex per physical install,
# persisted under the ROOT Hermes home (not the profile HERMES_HOME) so every
# profile reports the same id and the desktop can collapse duplicate roster rows
# for one backend. Must never change across restarts, so cached per process.
_INSTALL_ID_CACHE: Dict[str, Optional[str]] = {"root": None, "value": None}


def get_install_id() -> Optional[str]:
    """Process-lifetime-cached stable install id."""
    return _shared_get_install_id(cache=_INSTALL_ID_CACHE)


# Serializes config.yaml read-modify-write cycles for handlers on worker threads
# (asyncio.to_thread): config.py's _CONFIG_LOCK covers each load/save call, not
# the span between them, so two off-loop updates could drop each other's writes.
# RLock so nested helpers that also take it can't self-deadlock.
_CONFIG_MUTATION_LOCK = threading.RLock()

# A finished ``gateway-restart`` child does not mean the gateway is back (it
# exits once the restart is handed off), so in-flight reuse stops coalescing
# exactly when a stale frontend re-fires every few seconds (#89034: 77 restarts,
# state.db corrupted mid-FTS5-write). MAINTAINER DECISION: a fixed window, not
# "until healthy" — a gateway that never returns must not leave the action
# inert. 10s is above the ~3.5s storm spacing and below an operator's retry.
GATEWAY_RESTART_COOLDOWN_SECONDS = 10.0

# ``(monotonic spawn time, Popen, command)`` of the last restart. Deliberately
# NOT read from ``_ACTION_PROCS``: entries there vanish when the child exits.
_LAST_GATEWAY_RESTART: Optional[Tuple[float, subprocess.Popen, Tuple[str, ...]]] = None


def _spawn_gateway_restart(profile: Optional[str] = None) -> Tuple[subprocess.Popen, bool]:
    """Spawn ``hermes gateway restart``, reusing an in-flight or recent restart.

    Concurrent children race each other on the kill-and-start path, so a live
    child is reused; requests within ``GATEWAY_RESTART_COOLDOWN_SECONDS`` for the
    same profile coalesce onto the last spawn too (#89034). Orphaned gateways
    are reaped first so the fresh one doesn't stack a duplicate (#77276).
    Returns ``(proc, reused)``.
    """
    try:
        from hermes_cli.gateway import _reap_unsupervised_gateway_orphans

        _reap_unsupervised_gateway_orphans()
    except Exception:
        pass  # best-effort — don't block the restart on a reap failure

    global _LAST_GATEWAY_RESTART

    subcommand = _gateway_mod._gateway_subcommand(profile, "restart")
    existing = _gateway_mod._ACTION_PROCS.get("gateway-restart")
    if existing is not None and existing.poll() is None:
        existing_command = _gateway_mod._ACTION_COMMANDS.get("gateway-restart")
        if existing_command is None or existing_command == tuple(subcommand):
            return existing, True
        raise RuntimeError("gateway restart already in progress for another profile")

    recent = _LAST_GATEWAY_RESTART
    if recent is not None:
        spawned_at, recent_proc, recent_command = recent
        age = time.monotonic() - spawned_at if recent_command == tuple(subcommand) else None
        if age is not None and age < GATEWAY_RESTART_COOLDOWN_SECONDS:
            _log.info(
                "Coalescing gateway restart: one was started %.1fs ago "
                "(pid %s) and the gateway may still be coming back; not "
                "spawning another (#89034).",
                age,
                getattr(recent_proc, "pid", "?"),
            )
            return recent_proc, True

    proc = _gateway_mod._spawn_hermes_action(subcommand, "gateway-restart")
    _LAST_GATEWAY_RESTART = (time.monotonic(), proc, tuple(subcommand))
    return proc, False


# Collapses repeated identical ElevenLabs voice-list failures (the desktop
# re-polls on every settings focus) to one log line; re-arms on success or a
# changed signature.
_voice_list_last_error: Optional[str] = None


def _voice_list_error_logged_once(signature: Optional[str]) -> bool:
    """True if ``signature`` is new and should be logged now; ``None`` clears the latch."""
    global _voice_list_last_error
    if signature is None:
        _voice_list_last_error = None
        return False
    if signature == _voice_list_last_error:
        return False
    _voice_list_last_error = signature
    return True


<<<<<<< HEAD
@app.get("/api/audio/elevenlabs/voices")
async def get_elevenlabs_voices(profile: Optional[str] = None):
    """Return ElevenLabs voices when an API key is configured.

    The desktop UI uses this for the ``tts.elevenlabs.voice_id`` dropdown.
    Only non-secret voice metadata is returned; the API key stays server-side.
    """
    # Config-only scope (await-safe): the key lookup reads the requested
    # profile's .env, matching the profile the settings UI writes to.
    with _config_profile_scope(profile):
        api_key = (load_env().get("ELEVENLABS_API_KEY") or "").strip()
    if not api_key:
        # Fallback for env-only deployments — scope-aware (Slack pattern):
        # under multiplex os.environ may hold another profile's key, so
        # honor the installed scope's verdict before touching the env.
        try:
            from agent.secret_scope import UnscopedSecretError, get_secret

            try:
                api_key = (get_secret("ELEVENLABS_API_KEY") or "").strip()
            except UnscopedSecretError:
                api_key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
        except Exception:
            api_key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    if not api_key:
        return {"available": False, "voices": []}

    request = urllib.request.Request(
        "https://api.elevenlabs.io/v1/voices",
        headers={
            "Accept": "application/json",
            "xi-api-key": api_key,
        },
    )

    try:
        loop = asyncio.get_running_loop()

        def _fetch() -> Dict[str, Any]:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))

        payload = await loop.run_in_executor(None, _fetch)
    except urllib.error.HTTPError as exc:
        # An auth failure (bad/expired/scoped key) is a persistent,
        # user-fixable state, not a transient blip — the desktop polls this on
        # every settings open/focus, so a per-poll WARNING floods the log
        # (#voice-list-401-spam). Treat 401/403 as "integration unavailable":
        # report it to the UI with a 200 and log at most once until the error
        # signature changes (see _voice_list_error_logged_once).
        if exc.code in (401, 403):
            if _voice_list_error_logged_once(f"http-{exc.code}"):
                _log.info(
                    "ElevenLabs voices unavailable: %s — check ELEVENLABS_API_KEY", exc
                )
            return {"available": False, "voices": [], "error": "unauthorized"}
        if _voice_list_error_logged_once(f"http-{exc.code}"):
            _log.warning("ElevenLabs voice list failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not load ElevenLabs voices")
    except Exception as exc:
        if _voice_list_error_logged_once(str(exc)):
            _log.warning("ElevenLabs voice list failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not load ElevenLabs voices")
    _voice_list_error_logged_once(None)  # success — re-arm logging for next failure

    voices = []
    for voice in payload.get("voices") or []:
        if not isinstance(voice, dict):
            continue

        voice_id = str(voice.get("voice_id") or "").strip()
        if not voice_id:
            continue

        voices.append({
            "voice_id": voice_id,
            "name": str(voice.get("name") or voice_id),
            "label": _elevenlabs_voice_label(voice),
        })

    voices.sort(key=lambda item: str(item.get("label") or "").lower())
    return {"available": True, "voices": voices}


@app.post("/api/audio/speak")
async def speak_text(payload: TTSSpeakRequest, profile: Optional[str] = None):
    """Synthesize speech and return audio as base64 data URL.

    Used by the desktop voice-conversation mode to play back assistant
    responses without exposing the on-disk file path. Reuses the
    existing TTS provider chain (Edge / OpenAI / ElevenLabs / etc.)
    configured in ``~/.hermes/config.yaml`` under ``tts.``.
    """
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text is required")

    try:
        from tools.tts_tool import text_to_speech_tool

        def _speak_scoped():
            # Home-only scope (contextvar), NOT _profile_scope: synthesis
            # blocks for the provider round-trip and only needs config/.env
            # resolution, so the task-local override inside this worker
            # thread is sufficient (same reasoning as the MCP probe scope).
            with _config_profile_scope(profile):
                return text_to_speech_tool(text)

        loop = asyncio.get_running_loop()
        result_json = await loop.run_in_executor(None, _speak_scoped)
    except HTTPException:
        # _config_profile_scope raises 400/404 for a bad profile — pass it
        # through instead of masking it as a 500 synthesis failure.
        raise
    except Exception as exc:
        _log.exception("Desktop voice TTS failed")
        raise HTTPException(status_code=500, detail=f"Speech synthesis failed: {exc}")

    try:
        result = json.loads(result_json) if isinstance(result_json, str) else result_json
    except Exception:
        raise HTTPException(status_code=500, detail="Invalid TTS response")

    if not result.get("success"):
        raise HTTPException(
            status_code=400,
            detail=result.get("error") or "Speech synthesis failed",
        )

    file_path = result.get("file_path")
    if not file_path or not os.path.isfile(file_path):
        raise HTTPException(status_code=500, detail="Audio file missing")

    ext = os.path.splitext(file_path)[1].lower()
    mime_type = {
        ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg",
        ".opus": "audio/ogg",
        ".wav": "audio/wav",
        ".flac": "audio/flac",
    }.get(ext, "audio/mpeg")

    try:
        with open(file_path, "rb") as fh:
            audio_bytes = fh.read()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not read audio: {exc}")
    finally:
        try:
            os.unlink(file_path)
        except OSError:
            pass

    encoded = base64.b64encode(audio_bytes).decode("ascii")
    return {
        "ok": True,
        "data_url": f"data:{mime_type};base64,{encoded}",
        "mime_type": mime_type,
        "provider": result.get("provider"),
    }


def _split_text_for_speak_stream(text: str, cap: int) -> list:
    """Split *text* into provider-cap-sized pieces on sentence boundaries.

    Deliberately NOT unified with gateway.platforms.helpers'
    split_text_fence_aware: this splitter reflows whitespace (sentences are
    re-joined with single spaces) and has no fence/markdown semantics, so
    expressing it as knobs on the fence-aware core would change behavior.
    """
    from tools.tts_streaming import SENTENCE_BOUNDARY_RE as _SENTENCE_BOUNDARY_RE

    cap = cap if cap and cap > 0 else 4000
    pieces, buf = [], ""
    for sentence in filter(str.strip, _SENTENCE_BOUNDARY_RE.split(text)):
        while len(sentence) > cap:
            pieces.append(sentence[:cap])
            sentence = sentence[cap:]
        if buf and len(buf) + len(sentence) + 1 > cap:
            pieces.append(buf)
            buf = sentence
        else:
            buf = f"{buf} {sentence}" if buf else sentence
    if buf:
        pieces.append(buf)
    return pieces


@app.websocket("/api/audio/speak-stream")
async def speak_stream_ws(ws: "WebSocket") -> None:
    """Streaming TTS for the desktop: text in, raw int16 PCM frames out.

    The socket is a per-reply speech *session*: the client feeds text
    incrementally as LLM deltas arrive, the server cuts sentences
    (``SentenceChunker`` — same cutter as the CLI/TUI speaker pipeline) and
    streams each one's PCM the moment it's ready. Speech overlaps generation,
    exactly like the token→sentence→TTS pipelining the realtime-voice
    literature converges on.

    Protocol:
      client → ``{"text": "..."}`` frames (incremental; may combine with done),
               ``{"done": true}`` when the reply is complete,
               ``{"stop": true}`` or disconnect = barge-in
      server → ``{"type": "start", "sample_rate": N, "channels": 1}``,
               binary PCM frames, then ``{"type": "end"}``
      server → ``{"type": "fallback"}`` when the configured provider has no
               chunked API — the client uses the POST endpoint instead.
    """
    if not _ws_auth_ok(ws):
        await ws.close(code=4401)
        return
    if not _ws_request_is_allowed(ws):
        await ws.close(code=4403)
        return
    await ws.accept()

    # Profile via query param, like /api/pty and /api/console: the provider
    # chain + API keys must resolve from the requesting profile's config, not
    # the dashboard's own. The streamer captures its config at resolve time,
    # so scoping resolution scopes the whole session.
    profile = (ws.query_params.get("profile") or "").strip() or None

    loop = asyncio.get_running_loop()

    def _resolve():
        from tools.tts_streaming import resolve_streaming_provider
        from tools.tts_tool import _get_provider, _load_tts_config, _resolve_max_text_length

        with _config_profile_scope(profile):
            cfg = _load_tts_config()
            streamer = resolve_streaming_provider(cfg)
            cap = _resolve_max_text_length(_get_provider(cfg), cfg) if streamer else 0
        return streamer, cap

    try:
        streamer, cap = await loop.run_in_executor(None, _resolve)
    except Exception:
        _log.exception("speak-stream provider resolution failed")
        streamer, cap = None, 0
    if streamer is None:
        with contextlib.suppress(Exception):
            await ws.send_json({"type": "fallback"})
            await ws.close()
        return

    await ws.send_json(
        {"type": "start", "sample_rate": streamer.sample_rate, "channels": streamer.channels}
    )

    stop = threading.Event()
    text_q: queue.Queue = queue.Queue()  # str deltas; None = end-of-text
    chunks: asyncio.Queue = asyncio.Queue()  # PCM out; None = synthesis done

    def _produce():
        from tools.tts_streaming import SentenceChunker
        from tools.tts_tool import _strip_markdown_for_tts

        chunker = SentenceChunker()

        # The session stays open for a whole agent turn, and the client only
        # sends `done` when the turn ends. During tool execution no text
        # arrives, so without an idle flush a narration line with no trailing
        # whitespace ("Let me check.") sits in the chunker until end-of-turn
        # and is spoken long after the tool already finished. Mirror the CLI
        # speaker pipeline: poll with a timeout and flush the buffer when the
        # producer goes idle — immediately when the buffer ends on sentence
        # punctuation, after a longer quiet spell otherwise.
        idle_poll_seconds = 0.5
        idle_polls_before_force_flush = 4  # ~2s of silence

        def _sentences():
            idle_polls = 0
            while not stop.is_set():
                try:
                    delta = text_q.get(timeout=idle_poll_seconds)
                except queue.Empty:
                    idle_polls += 1
                    buffered = chunker.buf.strip()
                    if not buffered or ("<think" in chunker.buf and "</think>" not in chunker.buf):
                        continue
                    if buffered.endswith((".", "!", "?", "…", ":")) or idle_polls >= idle_polls_before_force_flush:
                        yield from chunker.flush()
                    continue
                idle_polls = 0
                if delta is None:
                    yield from chunker.flush()
                    return
                yield from chunker.feed(delta)

        try:
            for sentence in _sentences():
                cleaned = _strip_markdown_for_tts(sentence)
                if not cleaned:
                    continue
                for piece in _split_text_for_speak_stream(cleaned, cap):
                    for chunk in streamer.stream(piece):
                        if stop.is_set():
                            return
                        loop.call_soon_threadsafe(chunks.put_nowait, chunk)
        except Exception as exc:
            _log.warning("speak-stream synthesis failed: %s", exc)
        finally:
            loop.call_soon_threadsafe(chunks.put_nowait, None)

    threading.Thread(target=_produce, daemon=True).start()

    async def _pump_client():
        # Text frames feed synthesis; done ends the text; stop/disconnect
        # (or any unparseable frame) is barge-in.
        try:
            while True:
                frame = json.loads(await ws.receive_text())
                if frame.get("text"):
                    text_q.put(str(frame["text"]))
                if frame.get("stop"):
                    break
                if frame.get("done"):
                    text_q.put(None)
        except Exception:
            pass
        stop.set()
        text_q.put(None)  # unblock the producer

    pump = asyncio.ensure_future(_pump_client())
    try:
        while True:
            chunk = await chunks.get()
            if chunk is None:
                break
            await ws.send_bytes(chunk)
        if not stop.is_set():
            await ws.send_json({"type": "end"})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        stop.set()
        text_q.put(None)
        pump.cancel()
        with contextlib.suppress(Exception):
            await ws.close()


@app.get("/api/actions/{name}/status")
async def get_action_status(name: str, lines: int = 200):
    """Tail an action log and report whether the process is still running."""
    log_file_name = _ACTION_LOG_FILES.get(name)
    if log_file_name is None:
        raise HTTPException(status_code=404, detail=f"Unknown action: {name}")

    log_path = _ACTION_LOG_DIR / log_file_name
    requested_lines = min(max(lines, 1), 2000)
    tail = _tail_lines(log_path, requested_lines)

    durable_update_action_id = None
    update_receipt_summary = None
    if name == "hermes-update":
        durable_lines = _tail_lines(_ACTION_LOG_DIR / "update.log", 2000)
        durable_update_action_id = _durable_completed_update_action_id(durable_lines)
        if durable_update_action_id:
            marker = f"=== hermes-update completed {durable_update_action_id} ==="
            if marker not in tail:
                tail = [*tail, marker][-requested_lines:]
        # Phase-1 bullet 3 (#91277): the update receipt is the durable,
        # structured truth about the last update — written by every run
        # including refused/failed ones, and it survives the dashboard
        # restarting itself mid-action. Surface its summary alongside the
        # log-marker recovery so clients (Desktop, dashboard) READ the
        # outcome instead of inferring it from liveness probes
        # (#81193/#87359 class).
        update_receipt_summary = _latest_update_receipt_summary()

    proc = _ACTION_PROCS.get(name)
    if proc is None:
        result = _ACTION_RESULTS.get(name)
        running = False
        exit_code = result.get("exit_code") if result else None
        pid = result.get("pid") if result else None
        if result is None and durable_update_action_id:
            exit_code = 0
        if (
            result is None
            and exit_code is None
            and update_receipt_summary is not None
            and update_receipt_summary.get("outcome") in ("success", "partial")
        ):
            # No in-memory result and no log marker (e.g. log rotated), but
            # the receipt proves a completed run: report its outcome rather
            # than a null that clients time out on. ``partial`` maps to
            # exit 1 exactly like the CLI run itself did.
            exit_code = 0 if update_receipt_summary["outcome"] == "success" else 1
    else:
        exit_code = proc.poll()
        running = exit_code is None
        pid = proc.pid
        if exit_code is not None:
            try:
                proc.wait(timeout=1)
            except Exception:
                pass
            _ACTION_RESULTS[name] = {"exit_code": exit_code, "pid": pid}
            _ACTION_PROCS.pop(name, None)
            _ACTION_COMMANDS.pop(name, None)
            _ACTION_IDS.pop(name, None)

    response = {
        "name": name,
        "running": running,
        "exit_code": exit_code,
        "pid": pid,
        "lines": tail,
    }
    if durable_update_action_id:
        response["action_id"] = durable_update_action_id
    if update_receipt_summary is not None:
        response["receipt"] = update_receipt_summary
    return response


def _latest_update_receipt_summary() -> Optional[Dict[str, Any]]:
    """Compact summary of the most recent update receipt, or None.

    Phase-1 bullet 3 (#91277): the receipt (written by EVERY ``hermes
    update`` run since #91283, including refused and failed ones, with a
    ``latest.json`` pointer) is the durable success signal the Desktop and
    dashboard should read instead of inferring outcomes from liveness
    probes across the update's stop/start gap (#81193, #87359). Summary
    only — steps and skips stay in the full receipt endpoint.
    Never raises.
    """
    try:
        from hermes_cli.update_receipt import read_latest_receipt

        receipt = read_latest_receipt()
        if not receipt:
            return None
        fleet = receipt.get("fleet") or []
        return {
            "outcome": receipt.get("outcome"),
            "started_at": receipt.get("started_at"),
            "finished_at": receipt.get("finished_at"),
            "pre_sha": (receipt.get("pre_update") or {}).get("sha"),
            "post_sha": (receipt.get("post_update") or {}).get("sha"),
            "post_version": (receipt.get("post_update") or {}).get("version"),
            "fleet_states": sorted(
                {str(e.get("state")) for e in fleet if isinstance(e, dict)}
            ),
        }
    except Exception:
        return None


@app.get("/api/hermes/update/receipt")
async def get_update_receipt():
    """The most recent update receipt — the durable update-outcome record.

    Phase-1 bullet 3 (#91277): dashboards and the Desktop read this instead
    of inferring update success from backend liveness (the inference misread
    the update's own restart gap as 'Backend update failed' / 'boot failed'
    — #81193, #87359). Returns the FULL receipt (steps, skips, gateway
    restart outcome, fleet matrix) plus a compact ``summary``; 404 when no
    update has run since receipts landed.
    """
    try:
        from hermes_cli.update_receipt import read_latest_receipt

        receipt = read_latest_receipt()
    except Exception:
        receipt = None
    if not receipt:
        raise HTTPException(
            status_code=404,
            detail="No update receipt found (no `hermes update` run recorded).",
        )
    return {"receipt": receipt, "summary": _latest_update_receipt_summary()}


# Per-row fields that no session LIST consumer reads but that dominate the
# payload. ``system_prompt`` is the fully rendered prompt — tens of KB per
# row — and made a 21-row /api/sessions response 528KB (96% dead weight),
# re-fetched by the desktop sidebar on every refresh. The desktop's
# SessionInfo type doesn't declare either field and the web UI never touches
# them; ``GET /api/sessions/{id}`` detail reads stay complete. List callers
# that genuinely need the full rows can pass ``?full=1``.
_SESSION_LIST_HEAVY_FIELDS = ("system_prompt", "model_config")


def _strip_session_list_rows(sessions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for s in sessions:
        for key in _SESSION_LIST_HEAVY_FIELDS:
            s.pop(key, None)
    return sessions


from hermes_cli.web_routers import sessions as _sessions_routes  # noqa: E402

app.include_router(_sessions_routes.list_router)
from hermes_cli.web_routers.sessions import (  # noqa: E402,F401 — legacy re-exports; tests call these via web_server.<name>
    get_sessions,
)


from hermes_cli.web_routers import profiles as _profiles_routes  # noqa: E402

app.include_router(_profiles_routes.sessions_router)
from hermes_cli.web_routers.profiles import (  # noqa: E402,F401 — legacy re-exports; tests call these via web_server.<name>
    get_profiles_sessions,
    get_profiles_sessions_sidebar,
)




app.include_router(_sessions_routes.search_router)
from hermes_cli.web_routers.sessions import (  # noqa: E402,F401 — legacy re-exports; tests call these via web_server.<name>
    search_sessions,
)


def _normalize_config_for_web(config: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize config for the web UI.

    Hermes supports ``model`` as either a bare string (``"anthropic/claude-sonnet-4"``)
    or a dict (``{default: ..., provider: ..., base_url: ...}``).  The schema is built
    from DEFAULT_CONFIG where ``model`` is a string, but user configs often have the
    dict form.  Normalize to the string form so the frontend schema matches.

    Also surfaces ``model_context_length`` as a top-level field so the web UI can
    display and edit it.  A value of 0 means "auto-detect".
    """
    config = dict(config)  # shallow copy
    model_val = config.get("model")
    if isinstance(model_val, dict):
        # Extract context_length before flattening the dict
        ctx_len = model_val.get("context_length", 0)
        config["model"] = model_val.get("default", model_val.get("name", ""))
        config["model_context_length"] = ctx_len if isinstance(ctx_len, int) else 0
    else:
        config["model_context_length"] = 0
    return config


# ── Memory provider config: one generic GET/PUT pair, dispatching on storage ──


def _provider_field_entry(field: ProviderField) -> Dict[str, Any]:
    """Static, storage-independent shape of one field for the UI payload."""

    return {
        "key": field.key,
        "label": field.label,
        "kind": field.kind,
        "description": field.description,
        "info": field.info,
        "placeholder": field.placeholder,
        "inline": field.inline,
        "group": field.group,
        "options": [
            {"value": opt.value, "label": opt.label, "description": opt.description}
            for opt in field.options
        ],
    }


# Sentinel: remove this key so it falls back to the host or built-in default.
_UNSET: Any = object()


def _coerce_field_value(field: ProviderField, raw: str) -> Any:
    """Coerce a submitted non-secret value to its native JSON type.

    Values arrive as strings over the API; this converts them to the type the
    Honcho resolver expects (bool/number/list/dict), so e.g. a boolean is stored
    as a JSON ``false`` rather than the string ``"false"`` (which would read as
    truthy). Returns ``_UNSET`` when the field should be removed. Raises
    ``ValueError`` on malformed input.
    """

    value = (raw or "").strip()
    kind = field.kind

    if kind == "select":
        if not value:
            value = field.default
        if value not in field.allowed_values():
            raise ValueError(f"Invalid value for '{field.key}'")
        return value

    if kind == "bool":
        from utils import is_truthy_value

        return is_truthy_value(value)

    if kind == "number":
        if not value:
            return _UNSET
        try:
            number = float(value)
        except ValueError as exc:
            raise ValueError(f"Invalid number for '{field.key}'") from exc
        return int(number) if number.is_integer() else number

    if kind == "json":
        if not value:
            return _UNSET
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Invalid JSON for '{field.key}'") from exc
        if not isinstance(parsed, (dict, list)):
            raise ValueError(f"'{field.key}' must be a JSON object or array")
        return parsed

    # text / secret — blank clears the key so it falls back to host/default.
    return value if value else _UNSET


def _serialize_field_value(field: ProviderField, value: Any) -> str:
    """Render a stored native value as the string the generic UI edits.

    ``None`` (key absent) yields the field's declared default. Bools become
    ``"true"``/``"false"``, JSON objects/arrays are re-encoded, numbers are
    stringified — so the renderer's per-kind controls always get the shape they
    expect regardless of how the value sits on disk.
    """

    if value is None:
        return field.default
    if field.kind == "bool":
        from utils import is_truthy_value

        return "true" if is_truthy_value(value) else "false"
    if field.kind == "json":
        if isinstance(value, (dict, list)):
            return json.dumps(value)
        return str(value)
    return str(value)


# — flat-json backend (default; reusable for simple providers) —


def _flat_json_path(provider: ProviderConfigSchema) -> Path:
    return get_hermes_home() / provider.name / "config.json"


def _read_flat_json(provider: ProviderConfigSchema) -> Dict[str, Any]:
    path = _flat_json_path(provider)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        _log.warning("Failed to read memory provider config from %s", path, exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def _read_field(field: ProviderField, sources: tuple, env: Dict[str, str]) -> Any:
    """Return the stored native value from the first source holding it, or ``None``.

    Presence (``key in source``) decides, not truthiness, so a stored ``False``
    or ``0`` survives instead of being mistaken for "unset".
    """

    for source in sources:
        for source_key in (field.key, *field.aliases):
            if source_key in source and source[source_key] is not None:
                return source[source_key]
    for env_key in field.env_fallbacks:
        value = env.get(env_key)
        if value:
            return value
    return None


def _declared_field_is_set(field: ProviderField, sources: tuple, env: Dict[str, str]) -> bool:
    for env_key in (field.env_key, *field.env_fallbacks):
        if env_key and env.get(env_key):
            return True
    return any(source.get(k) for source in sources for k in (field.key, *field.aliases))


# — honcho host-block backend —


def _honcho_resolvers():
    """Lazily import the Honcho plugin's resolvers (optional plugin)."""

    from plugins.memory.honcho.client import _host_block, resolve_active_host, resolve_config_path

    return resolve_active_host, resolve_config_path, _host_block


def _honcho_read_sources() -> tuple[Dict[str, Any], str, Dict[str, Any]]:
    """Return (root config, active host key, host block) for the current profile."""

    resolve_active_host, resolve_config_path, host_block_of = _honcho_resolvers()
    host = resolve_active_host()
    path = resolve_config_path()
    raw: Dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            raw = loaded if isinstance(loaded, dict) else {}
        except Exception:
            _log.warning("Failed to read Honcho config from %s", path, exc_info=True)
    return raw, host, host_block_of(raw, host)


def _declared_provider_payload(provider: ProviderConfigSchema) -> Dict[str, Any]:
    fields: List[Dict[str, Any]] = []
    env = load_env()
    is_honcho = provider.storage == STORAGE_HONCHO_HOST_BLOCK

    if is_honcho:
        raw, host, host_block = _honcho_read_sources()

        def sources_for(field: ProviderField) -> tuple:
            return (host_block, raw) if field.scope == "host" else (raw,)
    else:
        host = ""
        data = _read_flat_json(provider)

        def sources_for(field: ProviderField) -> tuple:
            return (data,)

    for field in provider.fields:
        entry = _provider_field_entry(field)
        sources = sources_for(field)

        if field.is_secret:
            entry["value"] = ""  # secrets are write-only over the API
            entry["is_set"] = _declared_field_is_set(field, sources, env)
            fields.append(entry)
            continue

        native = _read_field(field, sources, env)
        if is_honcho and not field.placeholder and field.key in {"workspace", "aiPeer"}:
            # Blank fields surface the resolved host Honcho will actually use.
            entry["placeholder"] = host

        value = _serialize_field_value(field, native)
        if field.kind == "select" and value not in field.allowed_values():
            value = field.default
        entry["value"] = value
        # Presence, not truthiness — a stored False/0 is still "set".
        entry["is_set"] = native is not None if is_honcho else bool(value)
        fields.append(entry)

    return {"name": provider.name, "label": provider.label, "docs_url": provider.docs_url, "fields": fields}


def _apply_field_values(provider: ProviderConfigSchema, values: Dict[str, str], target_for) -> None:
    """Apply submitted non-secret fields to their backend dict, in place.

    Only keys present in ``values`` are touched, so a partial save never
    clobbers fields owned by another surface. ``_UNSET`` clears the key (and
    its aliases) so it falls back to the host/default mapping.
    """

    for field in provider.fields:
        if field.is_secret or field.key not in values:
            continue
        target = target_for(field)
        coerced = _coerce_field_value(field, values[field.key])
        if coerced is _UNSET:
            target.pop(field.key, None)
            for alias in field.aliases:
                target.pop(alias, None)
        else:
            target[field.key] = coerced


def _write_provider_flat(provider: ProviderConfigSchema, values: Dict[str, str]) -> None:
    from utils import atomic_json_write

    existing = _read_flat_json(provider)

    for field in provider.fields:
        if field.is_secret:
            submitted = (values.get(field.key) or "").strip()
            if submitted and field.env_key:
                save_env_value(field.env_key, submitted)

    _apply_field_values(provider, values, lambda field: existing)

    path = _flat_json_path(provider)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(path, existing, mode=0o600)


def _write_provider_honcho(provider: ProviderConfigSchema, values: Dict[str, str]) -> None:
    """Persist submitted fields to Honcho's real config for the active host.

    Only keys present in ``values`` are touched, so a partial save (e.g. the
    inline panel) never clobbers fields owned by the full-config editor. Blank
    text clears a key so it falls back to the host/default mapping.
    """

    from plugins.memory.honcho.oauth import ACCESS_TOKEN_PREFIX, _config_refresh_lock
    from utils import atomic_json_write

    resolve_active_host, resolve_config_path, host_block_of = _honcho_resolvers()
    host = resolve_active_host()
    # Write the file reads resolve, or a save shadows it with a sparse copy.
    path = resolve_config_path()

    # OAuth rotation is single-use; an unlocked RMW here can revoke the grant.
    with _config_refresh_lock(path):
        cfg: Dict[str, Any] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                cfg = loaded if isinstance(loaded, dict) else {}
            except Exception:
                _log.warning("Failed to read Honcho config from %s", path, exc_info=True)

        hosts = cfg.get("hosts")
        cfg["hosts"] = hosts = hosts if isinstance(hosts, dict) else {}
        # Update the block reads resolve (legacy dot-form included), never shadow it.
        existing = host_block_of(cfg, host)
        host_key = next((k for k, v in hosts.items() if v is existing), host) if existing else host
        host_block = hosts.setdefault(host_key, existing)

        for field in provider.fields:
            if not field.is_secret:
                continue
            submitted = (values.get(field.key) or "").strip()
            if not submitted:
                continue
            if field.env_key:
                save_env_value(field.env_key, submitted)
            # Persist where the client reads first; an OAuth token owns that slot.
            stored = host_block.get(field.key)
            if not (isinstance(stored, str) and stored.startswith(ACCESS_TOKEN_PREFIX)):
                host_block[field.key] = submitted

        _apply_field_values(provider, values, lambda field: host_block if field.scope == "host" else cfg)

        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, cfg, mode=0o600)


def _stringify_submitted_values(values: Dict[str, Any]) -> Dict[str, str]:
    """The declared-schema path edits strings; the dashboard may send natives."""

    out: Dict[str, str] = {}
    for key, value in values.items():
        if value is None:
            out[key] = ""
        elif isinstance(value, str):
            out[key] = value
        elif isinstance(value, bool):
            out[key] = "true" if value else "false"
        elif isinstance(value, (dict, list)):
            out[key] = json.dumps(value)
        else:
            out[key] = str(value)
    return out


def _update_memory_provider_config(provider: ProviderConfigSchema, values: Dict[str, str]) -> None:
    if provider.storage == STORAGE_HONCHO_HOST_BLOCK:
        _write_provider_honcho(provider, values)
    else:
        _write_provider_flat(provider, values)

    config = load_config()
    memory_config = config.get("memory")
    if not isinstance(memory_config, dict):
        memory_config = {}
        config["memory"] = memory_config
    if memory_config.get("provider") != provider.name:
        memory_config["provider"] = provider.name
        save_config(config)


def _memory_provider_label(name: str) -> str:
    return name.replace("_", " ").replace("-", " ").title()


def _normalize_memory_provider_name(name: Any) -> str:
    provider = str(name or "").strip()
    if provider.lower() in {"built-in", "builtin", "none"}:
        return ""
    return provider


def _load_memory_provider(name: str):
    try:
        from plugins.memory import load_memory_provider

        return load_memory_provider(name)
    except Exception:
        _log.debug("Failed to load memory provider %s", name, exc_info=True)
        return None


def _memory_provider_manifest(name: str) -> Dict[str, Any]:
    try:
        from plugins.memory import find_provider_dir

        provider_dir = find_provider_dir(name)
        if provider_dir is None:
            return {}
        manifest_path = provider_dir / "plugin.yaml"
        if not manifest_path.exists():
            return {}
        with manifest_path.open(encoding="utf-8-sig") as handle:
            manifest = yaml.safe_load(handle) or {}
        return manifest if isinstance(manifest, dict) else {}
    except Exception:
        _log.debug("Failed to read memory provider manifest for %s", name, exc_info=True)
        return {}


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _memory_provider_setup_manifest(name: str) -> Dict[str, Any]:
    manifest = _memory_provider_manifest(name)
    external_dependencies: List[Dict[str, str]] = []
    for raw in manifest.get("external_dependencies") or []:
        if not isinstance(raw, dict):
            continue
        dep = {
            "name": str(raw.get("name") or "").strip(),
            "install": str(raw.get("install") or "").strip(),
            "check": str(raw.get("check") or "").strip(),
        }
        if dep["name"] or dep["install"] or dep["check"]:
            external_dependencies.append(dep)

    return {
        "pip_dependencies": _string_list(manifest.get("pip_dependencies")),
        "external_dependencies": external_dependencies,
        "required_env": _string_list(manifest.get("requires_env")),
    }


def _memory_provider_setup_info(name: str) -> Dict[str, Any]:
    setup = _memory_provider_setup_manifest(name)
    setup["dependencies_installed"] = _memory_provider_dependencies_installed(setup)
    return setup


_MEMORY_PROVIDER_IMPORT_NAMES = {
    "honcho-ai": "honcho",
    "mem0ai": "mem0",
    "hindsight-client": "hindsight_client",
    "hindsight-all": "hindsight",
}


def _memory_provider_dependency_package(dep: str) -> str:
    return re.split(r"[\[<>=!~;]", dep, maxsplit=1)[0].strip()


def _memory_provider_import_name(dep: str) -> str:
    package = _memory_provider_dependency_package(dep)
    return _MEMORY_PROVIDER_IMPORT_NAMES.get(package, package.replace("-", "_"))


def _dependency_importable(dep: str) -> bool:
    import_name = _memory_provider_import_name(dep)
    if not import_name:
        return False
    try:
        __import__(import_name)
        return True
    except ImportError:
        return False


def _trim_setup_output(value: Optional[str], limit: int = 4000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n... truncated ..."


def _memory_provider_setup_env() -> Dict[str, str]:
    # External package-manager child (npm/uv/pip): exact env preservation —
    # scrubbing or HOME rewriting could break user tool auth/config.
    from tools.environments.local import build_subprocess_env
    env = build_subprocess_env(scrub_secrets=False, inherit_profile_home=False)
    home = Path.home()
    extra_bins = [
        home / ".brv-cli" / "bin",
        home / ".local" / "bin",
        home / ".npm-global" / "bin",
        Path("/usr/local/bin"),
    ]
    existing_path = env.get("PATH", "")
    prefix = os.pathsep.join(str(path) for path in extra_bins if path.exists())
    if prefix:
        env["PATH"] = prefix + os.pathsep + existing_path
    return env


def _command_result(
    *,
    kind: str,
    name: str,
    status: str,
    command: str = "",
    completed: Optional[subprocess.CompletedProcess] = None,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "kind": kind,
        "name": name,
        "status": status,
        "command": command,
        "returncode": None if completed is None else completed.returncode,
        "stdout": "" if completed is None else _trim_setup_output(completed.stdout),
        "stderr": _trim_setup_output(error or ("" if completed is None else completed.stderr)),
    }


def _run_setup_command(
    command: Any,
    *,
    display: str,
    shell: bool = False,
    timeout: int = 180,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        shell=shell,
        executable="/bin/bash" if shell else None,
        env=_memory_provider_setup_env(),
        capture_output=True,
        text=True,
        # Lossy UTF-8 decode — setup tools emit UTF-8; never let a
        # locale-mismatched byte raise in the reader thread (#52649).
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _memory_provider_dependencies_installed(setup: Dict[str, Any]) -> bool:
    pip_dependencies = _string_list(setup.get("pip_dependencies"))
    external_dependencies = setup.get("external_dependencies") or []

    pip_ok = all(_dependency_importable(dep) for dep in pip_dependencies)
    external_ok = True
    for dep in external_dependencies:
        if not isinstance(dep, dict):
            continue
        check_cmd = str(dep.get("check") or "").strip()
        install_cmd = str(dep.get("install") or "").strip()
        if not check_cmd:
            if install_cmd:
                external_ok = False
            continue
        try:
            completed = _run_setup_command(
                shlex.split(check_cmd),
                display=check_cmd,
                timeout=20,
            )
        except Exception:
            external_ok = False
            continue
        if completed.returncode != 0:
            external_ok = False

    return pip_ok and external_ok


def _install_memory_provider_pip_dependencies(dependencies: List[str]) -> List[Dict[str, Any]]:
    missing = [dep for dep in dependencies if not _dependency_importable(dep)]
    if not dependencies:
        return []
    if not missing:
        return [
            _command_result(kind="pip", name=", ".join(dependencies), status="already_installed")
        ]

    # Route through the lazy-install pipeline (tools.lazy_deps.install_specs)
    # instead of shelling out to pip against sys.executable directly. That
    # pipeline is environment-aware: on hosted/immutable images the agent venv
    # under /opt/hermes is sealed read-only, and installs must be redirected
    # to the writable durable target on the data volume
    # (HERMES_LAZY_INSTALL_TARGET, e.g. /opt/data/lazy-packages) — the same
    # path every lazy backend already uses. A direct `pip install --python
    # sys.executable` on those images fails with a permission error (NS-605).
    # install_specs also activates the target on sys.path post-install so the
    # availability recheck below sees the new packages without a restart.
    try:
        from tools.lazy_deps import install_specs

        outcome = install_specs(missing, timeout=240)
    except Exception as exc:
        return [
            _command_result(
                kind="pip",
                name=", ".join(missing),
                status="failed",
                error=str(exc),
            )
        ]

    if outcome.blocked:
        return [
            _command_result(
                kind="pip",
                name=", ".join(missing),
                status="failed",
                command=outcome.command,
                error=outcome.reason,
            )
        ]

    return [
        _command_result(
            kind="pip",
            name=", ".join(missing),
            status="installed" if outcome.ok else "failed",
            command=outcome.command,
            completed=subprocess.CompletedProcess(
                args=outcome.command,
                returncode=0 if outcome.ok else 1,
                stdout=outcome.stdout,
                stderr=outcome.stderr,
            ),
        )
    ]


def _install_memory_provider_external_dependencies(
    dependencies: List[Dict[str, str]],
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for dep in dependencies:
        name = dep.get("name") or "dependency"
        check_cmd = dep.get("check") or ""
        install_cmd = dep.get("install") or ""

        if check_cmd:
            try:
                check = _run_setup_command(
                    shlex.split(check_cmd),
                    display=check_cmd,
                    timeout=20,
                )
            except Exception as exc:
                results.append(
                    _command_result(
                        kind="external_check",
                        name=name,
                        status="missing" if install_cmd else "failed",
                        command=check_cmd,
                        error=str(exc),
                    )
                )
            else:
                if check.returncode == 0:
                    results.append(
                        _command_result(
                            kind="external_check",
                            name=name,
                            status="already_installed",
                            command=check_cmd,
                            completed=check,
                        )
                    )
                    continue
                results.append(
                    _command_result(
                        kind="external_check",
                        name=name,
                        status="missing" if install_cmd else "failed",
                        command=check_cmd,
                        completed=check,
                    )
                )

            if not install_cmd:
                continue

        if install_cmd:
            try:
                install = _run_setup_command(
                    install_cmd,
                    display=install_cmd,
                    shell=True,
                    timeout=300,
                )
            except Exception as exc:
                results.append(
                    _command_result(
                        kind="external_install",
                        name=name,
                        status="failed",
                        command=install_cmd,
                        error=str(exc),
                    )
                )
                continue

            results.append(
                _command_result(
                    kind="external_install",
                    name=name,
                    status="installed" if install.returncode == 0 else "failed",
                    command=install_cmd,
                    completed=install,
                )
            )

            if check_cmd and install.returncode == 0:
                try:
                    post_check = _run_setup_command(
                        shlex.split(check_cmd),
                        display=check_cmd,
                        timeout=20,
                    )
                    results.append(
                        _command_result(
                            kind="external_check",
                            name=name,
                            status="verified" if post_check.returncode == 0 else "failed",
                            command=check_cmd,
                            completed=post_check,
                        )
                    )
                except Exception as exc:
                    results.append(
                        _command_result(
                            kind="external_check",
                            name=name,
                            status="failed",
                            command=check_cmd,
                            error=str(exc),
                        )
                    )

    return results


def _install_memory_provider_setup(name: str) -> Dict[str, Any]:
    provider = _load_memory_provider(name)
    manifest = _memory_provider_manifest(name)
    if provider is None and not manifest:
        raise HTTPException(status_code=404, detail=f"Unknown memory provider: {name}")

    setup = _memory_provider_setup_manifest(name)
    results = []
    results.extend(_install_memory_provider_pip_dependencies(setup["pip_dependencies"]))
    results.extend(
        _install_memory_provider_external_dependencies(setup["external_dependencies"])
    )

    if not results:
        results.append(
            _command_result(
                kind="setup",
                name=name,
                status="no_declared_steps",
            )
        )

    ok = all(result["status"] not in {"failed"} for result in results)
    statuses = {row["name"]: row for row in _discover_memory_provider_statuses()}
    return {
        "ok": ok,
        "provider": name,
        "results": results,
        "status": statuses.get(name),
    }


def _normalize_memory_provider_schema(name: str, provider: Any) -> List[Dict[str, Any]]:
    raw_schema: List[Dict[str, Any]] = []
    if provider is not None and hasattr(provider, "get_config_schema"):
        try:
            raw = provider.get_config_schema()
            if isinstance(raw, list):
                raw_schema = [field for field in raw if isinstance(field, dict)]
        except Exception:
            _log.warning("Failed to read memory provider schema for %s", name, exc_info=True)

    fields: List[Dict[str, Any]] = []
    for raw in raw_schema:
        key = str(raw.get("key") or "").strip()
        if not key:
            continue

        choices = raw.get("choices") or raw.get("options") or []
        if not isinstance(choices, list):
            choices = []

        explicit_kind = str(raw.get("kind") or raw.get("type") or "").strip().lower()
        if raw.get("secret"):
            kind = "secret"
        elif choices:
            kind = "select"
        elif explicit_kind in {"bool", "boolean"} or isinstance(raw.get("default"), bool):
            kind = "boolean"
        elif explicit_kind in {"int", "integer"} or (
            isinstance(raw.get("default"), int) and not isinstance(raw.get("default"), bool)
        ):
            kind = "integer"
        elif explicit_kind in {"float", "number"} or isinstance(raw.get("default"), float):
            kind = "number"
        else:
            kind = "text"

        options = []
        for choice in choices:
            value = str(choice)
            options.append({"value": value, "label": value, "description": ""})

        description = str(raw.get("description") or "")
        fields.append({
            "key": key,
            "label": str(raw.get("label") or key.replace("_", " ").title()),
            "kind": kind,
            "description": description,
            "placeholder": str(raw.get("placeholder") or ""),
            "required": bool(raw.get("required", False)),
            "default": raw.get("default", ""),
            "options": options,
            "url": str(raw.get("url") or ""),
            "when": raw.get("when") if isinstance(raw.get("when"), dict) else None,
            "minimum": raw.get("minimum"),
            "maximum": raw.get("maximum"),
            "step": raw.get("step"),
            "_env_key": str(raw.get("env_var") or "") or None,
        })

    return fields


def _read_json_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        _log.debug("Failed to read JSON config from %s", path, exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def _read_memory_provider_existing_values(name: str) -> Dict[str, Any]:
    """Best-effort read of existing provider config across legacy/native stores."""

    hermes_home = get_hermes_home()
    values: Dict[str, Any] = {}

    # Common native provider stores.
    for path in (
        hermes_home / f"{name}.json",
        hermes_home / name / "config.json",
    ):
        values.update(_read_json_file(path))

    try:
        cfg = load_config()
    except Exception:
        cfg = {}

    memory_cfg = cfg.get("memory") if isinstance(cfg, dict) else {}
    if isinstance(memory_cfg, dict):
        provider_cfg = memory_cfg.get(name)
        if isinstance(provider_cfg, dict):
            values.update(provider_cfg)
        legacy_cfg = memory_cfg.get("provider_config")
        if isinstance(legacy_cfg, dict):
            values = {**legacy_cfg, **values}

    # Holographic stores under plugins.hermes-memory-store.
    plugins_cfg = cfg.get("plugins") if isinstance(cfg, dict) else {}
    if name == "holographic" and isinstance(plugins_cfg, dict):
        holographic_cfg = plugins_cfg.get("hermes-memory-store")
        if isinstance(holographic_cfg, dict):
            values.update(holographic_cfg)

    return values


def _env_lookup(env_key: Optional[str]) -> str:
    if not env_key:
        return ""
    env_on_disk = load_env()
    return str(env_on_disk.get(env_key) or os.environ.get(env_key) or "")


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value}")


def _field_default(field: Dict[str, Any]) -> Any:
    default = field.get("default", "")
    if field["kind"] == "boolean":
        return _coerce_bool(default, default=False)
    return default


def _field_value(field: Dict[str, Any], data: Dict[str, Any]) -> Any:
    if field["kind"] == "secret":
        return ""

    value = data.get(field["key"])
    if value in (None, ""):
        value = _env_lookup(field.get("_env_key"))
    if value in (None, ""):
        value = _field_default(field)

    if field["kind"] == "select":
        allowed = {opt["value"] for opt in field.get("options", [])}
        value = str(value)
        return value if value in allowed else str(_field_default(field))
    if field["kind"] == "boolean":
        return _coerce_bool(value, default=_coerce_bool(_field_default(field), default=False))
    return str(value)


def _field_is_set(field: Dict[str, Any], data: Dict[str, Any]) -> bool:
    if field["kind"] == "secret":
        return bool(_env_lookup(field.get("_env_key")) or data.get(field["key"]))
    value = _field_value(field, data)
    return value not in (None, "")


def _field_visible(
    field: Dict[str, Any],
    data: Dict[str, Any],
    fields_by_key: Optional[Dict[str, Dict[str, Any]]] = None,
) -> bool:
    when = field.get("when")
    if not isinstance(when, dict) or not when:
        return True
    for dep_key, expected in when.items():
        dep_field = (fields_by_key or {}).get(str(dep_key)) or {
            "key": str(dep_key),
            "kind": "text",
            "default": "",
            "_env_key": None,
        }
        actual = _field_value(dep_field, data)
        if str(actual) != str(expected):
            return False
    return True


def _public_memory_provider_field(field: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
    entry = {
        "key": field["key"],
        "label": field["label"],
        "kind": field["kind"],
        "description": field["description"],
        "placeholder": field["placeholder"],
        "required": field["required"],
        "value": "" if field["kind"] == "secret" else _field_value(field, data),
        "is_set": _field_is_set(field, data),
        "options": field.get("options", []),
        "url": field.get("url", ""),
        "when": field.get("when"),
        "minimum": field.get("minimum"),
        "maximum": field.get("maximum"),
        "step": field.get("step"),
    }
    return entry


def _memory_provider_payload(name: str, provider: Any) -> Dict[str, Any]:
    data = _read_memory_provider_existing_values(name)
    fields = [
        _public_memory_provider_field(field, data)
        for field in _normalize_memory_provider_schema(name, provider)
    ]
    return {
        "name": name,
        "label": _memory_provider_label(name),
        "fields": fields,
        "setup": _memory_provider_setup_info(name),
    }


def _coerce_schema_field(field: Dict[str, Any], raw: Any) -> Any:
    if field["kind"] == "boolean":
        return _coerce_bool(raw, default=_coerce_bool(_field_default(field), default=False))

    if field["kind"] in {"integer", "number"}:
        value = raw if raw is not None and raw != "" else _field_default(field)
        try:
            if isinstance(value, bool):
                raise ValueError
            parsed = float(value)
            if not math.isfinite(parsed):
                raise ValueError
            if field["kind"] == "integer":
                if not parsed.is_integer():
                    raise ValueError
                result: int | float = int(parsed)
            else:
                result = parsed
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"Invalid numeric value for '{field['key']}'") from exc

        minimum = field.get("minimum")
        maximum = field.get("maximum")
        if minimum is not None and result < minimum:
            raise ValueError(f"'{field['key']}' must be at least {minimum}")
        if maximum is not None and result > maximum:
            raise ValueError(f"'{field['key']}' must be at most {maximum}")
        return result

    value = str(raw if raw is not None else "").strip()
    if field["kind"] == "select":
        if not value:
            value = str(_field_default(field))
        allowed = {opt["value"] for opt in field.get("options", [])}
        if value not in allowed:
            raise ValueError(f"Invalid value for '{field['key']}'")
        return value

    return value or _field_default(field)


def _save_memory_provider_native_config(name: str, provider: Any, values: Dict[str, Any]) -> None:
    if provider is not None and hasattr(provider, "save_config"):
        try:
            from agent.memory_provider import MemoryProvider as _BaseMemoryProvider
        except Exception:
            provider.save_config(values, str(get_hermes_home()))
            return
        if type(provider).save_config is not _BaseMemoryProvider.save_config:
            provider.save_config(values, str(get_hermes_home()))
            return

    cfg = load_config()
    memory_cfg = cfg.get("memory")
    if not isinstance(memory_cfg, dict):
        memory_cfg = {}
        cfg["memory"] = memory_cfg
    current = memory_cfg.get(name)
    if not isinstance(current, dict):
        current = {}
    current.update(values)
    memory_cfg[name] = current
    save_config(cfg)


def _memory_provider_is_configured(name: str, provider: Any) -> bool:
    data = _read_memory_provider_existing_values(name)
    fields = _normalize_memory_provider_schema(name, provider)
    fields_by_key = {field["key"]: field for field in fields}
    visible_fields = [
        field for field in fields if _field_visible(field, data, fields_by_key)
    ]
    required_fields = [field for field in visible_fields if field.get("required")]
    if not required_fields:
        return True
    return all(_field_is_set(field, data) for field in required_fields)


def _discover_memory_provider_statuses() -> List[Dict[str, Any]]:
    discovered: Dict[str, Dict[str, Any]] = {}
    try:
        from plugins.memory import discover_memory_providers

        for name, description, available in discover_memory_providers():
            discovered[str(name)] = {
                "name": str(name),
                "description": str(description or ""),
                "available": bool(available),
                "missing": False,
            }
    except Exception:
        _log.exception("discover_memory_providers failed")

    cfg = load_config()
    active = ""
    mem = cfg.get("memory")
    if isinstance(mem, dict):
        active = _normalize_memory_provider_name(mem.get("provider"))
    if active and active not in discovered:
        discovered[active] = {
            "name": active,
            "description": "Configured provider was not found.",
            "available": False,
            "missing": True,
        }

    providers: List[Dict[str, Any]] = []
    for name in sorted(discovered):
        row = discovered[name]
        provider = None if row["missing"] else _load_memory_provider(name)
        setup = _memory_provider_setup_info(name)
        configured = False if row["missing"] else _memory_provider_is_configured(name, provider)
        schema_fields = [] if row["missing"] else _normalize_memory_provider_schema(name, provider)
        if row["missing"]:
            status = "missing"
        elif not row["available"] and not setup.get("dependencies_installed", True):
            status = "unavailable"
        elif not configured:
            status = "needs_config"
        elif not row["available"] and schema_fields:
            status = "needs_config"
        elif not row["available"]:
            status = "unavailable"
        else:
            status = "ready"
        providers.append({
            "name": name,
            "description": row["description"],
            "available": row["available"],
            "configured": configured,
            "status": status,
            "setup": setup,
        })
    return providers


def _require_memory_provider_ready(name: str) -> None:
    if not name:
        return
    statuses = {row["name"]: row for row in _discover_memory_provider_statuses()}
    row = statuses.get(name)
    if row is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown memory provider '{name}'.",
        )
    if row["status"] != "ready":
        raise HTTPException(
            status_code=400,
            detail=(
                f"Memory provider '{name}' is not ready "
                f"({row['status'].replace('_', ' ')}). Configure it in the dashboard first."
            ),
        )


def _write_memory_provider_config_values(
    name: str,
    provider: Any,
    values: Dict[str, Any],
) -> None:
    existing = _read_memory_provider_existing_values(name)
    fields = _normalize_memory_provider_schema(name, provider)
    fields_by_key = {field["key"]: field for field in fields}
    config_values: Dict[str, Any] = {}
    secrets: Dict[str, str] = {}

    for field in fields:
        if not _field_visible(field, {**existing, **config_values}, fields_by_key):
            continue

        if field["kind"] == "secret":
            submitted = str(values.get(field["key"]) or "").strip()
            if submitted and field.get("_env_key"):
                secrets[str(field["_env_key"])] = submitted
            continue

        raw = (
            values[field["key"]]
            if field["key"] in values
            else existing.get(field["key"], _field_default(field))
        )
        config_values[field["key"]] = _coerce_schema_field(field, raw)

    _save_memory_provider_native_config(name, provider, config_values)

    for env_key, secret in secrets.items():
        save_env_value(env_key, secret)


_MEMORY_PROVIDER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _require_valid_memory_provider_name(name: str) -> None:
    """Reject provider names that could traverse outside the plugin dirs.

    ``name`` is interpolated into filesystem paths by ``find_provider_dir()``
    and gates which plugin manifest's setup commands run. A strict charset
    allowlist (no path separators, no dots) makes traversal impossible
    regardless of how the downstream lookup evolves.
    """
    if not _MEMORY_PROVIDER_NAME_RE.fullmatch(name or ""):
        raise HTTPException(status_code=404, detail=f"Unknown memory provider: {name}")


@app.get("/api/memory/providers/{name}/config")
async def get_memory_provider_config(name: str, surface: Optional[str] = None, profile: Optional[str] = None):
    _require_valid_memory_provider_name(name)

    def _run():
        with _profile_scope(profile):
            if surface == "declared":
                declared = get_provider_config_schema(name)
                if declared is None:
                    # Undeclared providers (e.g. builtin) have no desktop
                    # config surface; the generic panel renders nothing.
                    return {"name": name, "label": name, "docs_url": "", "fields": []}
                return _declared_provider_payload(declared)

            provider = _load_memory_provider(name)
            if provider is None:
                # Undeclared providers (e.g. builtin) have no config surface. Return an
                # empty schema so the generic panel simply renders nothing.
                return {"name": name, "label": name, "fields": [], "setup": _memory_provider_setup_info(name)}
            return _memory_provider_payload(name, provider)

    return await asyncio.to_thread(_run)

@app.post("/api/memory/providers/{name}/setup")
async def setup_memory_provider(name: str, body: MemoryProviderSetupRequest):
    _require_valid_memory_provider_name(name)
    provider = _load_memory_provider(name)
    if provider is None and not _memory_provider_manifest(name):
        # No discoverable plugin directory → nothing whose manifest could
        # legitimately declare setup commands. Refuse before the
        # command-running path. (provider may be None with a manifest present
        # when its pip deps aren't installed yet — that's the setup use case.)
        raise HTTPException(status_code=404, detail=f"Unknown memory provider: {name}")
    if provider is not None and body.values:
        try:
            _write_memory_provider_config_values(name, provider, body.values)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception:
            _log.exception("Failed to persist memory provider setup values for %s", name)
            raise HTTPException(status_code=500, detail="Internal server error")
    _invalidate_plugins_hub_cache()
    return _install_memory_provider_setup(name)


@app.put("/api/memory/providers/{name}/config")
async def update_memory_provider_config(
    name: str, body: MemoryProviderConfigUpdate, surface: Optional[str] = None, profile: Optional[str] = None
):
    _require_valid_memory_provider_name(name)
    values = body.values or {}

    def _run():
        with _profile_scope(profile):
            if surface == "declared":
                declared = get_provider_config_schema(name)
                if declared is None:
                    raise HTTPException(status_code=404, detail=f"Unknown memory provider: {name}")
                _update_memory_provider_config(declared, _stringify_submitted_values(values))
                _invalidate_plugins_hub_cache()
                return {"ok": True}

            provider = _load_memory_provider(name)
            if provider is None:
                raise HTTPException(status_code=404, detail=f"Unknown memory provider: {name}")
            _write_memory_provider_config_values(name, provider, values)
            _require_memory_provider_ready(name)
            config = load_config()
            memory_config = config.get("memory")
            if not isinstance(memory_config, dict):
                memory_config = {}
                config["memory"] = memory_config
            memory_config["provider"] = name
            save_config(config)
            _invalidate_plugins_hub_cache()
            return {"ok": True, "active": name}

    try:
        return await asyncio.to_thread(_run)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        _log.exception("PUT /api/memory/providers/%s/config failed", name)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/config")
async def get_config(profile: Optional[str] = None):
    # _profile_scope blocks on the process-wide _SKILLS_PROFILE_LOCK and
    # load_config() reads from disk; on the event loop a slow lock-holder
    # froze the whole gateway for >1s (observed via the loop watchdog).
    # asyncio.to_thread copies the contextvar context, so the profile
    # override stays scoped to the worker thread.
    def _run():
        with _profile_scope(profile):
            return _normalize_config_for_web(load_config())

    config = await asyncio.to_thread(_run)
    # Strip internal keys that the frontend shouldn't see or send back
    return {k: v for k, v in config.items() if not k.startswith("_")}


@app.get("/api/config/defaults")
async def get_defaults():
    return DEFAULT_CONFIG


@app.get("/api/config/schema")
async def get_schema(profile: Optional[str] = None):
    # Discovery-driven provider options (voice command providers + memory
    # provider plugins) are merged per-request so providers added after server
    # start still show up, scoped to the requested profile's config.
    with _config_profile_scope(profile):
        fields = _schema_with_dynamic_provider_options()
    return {"fields": fields, "category_order": _CATEGORY_ORDER}


@app.get("/api/egress/status")
async def get_egress_status():
    """Dashboard/Desktop-readable egress proxy status and remediation text."""
    from hermes_cli.proxy_cli import format_status_text

    return {"text": format_status_text()}


_EMPTY_MODEL_INFO: dict = {
    "model": "",
    "provider": "",
    "auto_context_length": 0,
    "config_context_length": 0,
    "effective_context_length": 0,
    "capabilities": {},
}


@app.get("/api/model/info")
def get_model_info(profile: Optional[str] = None):
    """Return resolved model metadata for the currently configured model.

    Calls the same context-length resolution chain the agent uses, so the
    frontend can display "Auto-detected: 200K" alongside the override field.
    Also returns model capabilities (vision, reasoning, tools) when available.
    """
    try:
        with _profile_scope(profile):
            cfg = load_config()
        model_cfg = cfg.get("model", "")

        # Extract model name and provider from the config
        if isinstance(model_cfg, dict):
            model_name = model_cfg.get("default", model_cfg.get("name", ""))
            provider = model_cfg.get("provider", "")
            base_url = model_cfg.get("base_url", "")
            config_ctx = model_cfg.get("context_length")
        else:
            model_name = str(model_cfg) if model_cfg else ""
            provider = ""
            base_url = ""
            config_ctx = None

        if not model_name:
            return dict(_EMPTY_MODEL_INFO, provider=provider)

        # Resolve auto-detected context length (pass config_ctx=None to get
        # purely auto-detected value, then separately report the override)
        try:
            from agent.model_metadata import get_model_context_length
            auto_ctx = get_model_context_length(
                model=model_name,
                base_url=base_url,
                provider=provider,
                config_context_length=None,  # ignore override — we want auto value
            )
        except Exception:
            auto_ctx = 0

        config_ctx_int = 0
        if isinstance(config_ctx, int) and config_ctx > 0:
            config_ctx_int = config_ctx

        # Effective is what the agent actually uses
        effective_ctx = config_ctx_int if config_ctx_int > 0 else auto_ctx

        # Try to get model capabilities from models.dev
        caps = {}
        try:
            from agent.models_dev import get_model_capabilities
            mc = get_model_capabilities(provider=provider, model=model_name)
            if mc is not None:
                caps = {
                    "supports_tools": mc.supports_tools,
                    "supports_vision": mc.supports_vision,
                    "supports_reasoning": mc.supports_reasoning,
                    "context_window": mc.context_window,
                    "max_output_tokens": mc.max_output_tokens,
                    "model_family": mc.model_family,
                }
        except Exception:
            pass

        return {
            "model": model_name,
            "provider": provider,
            "auto_context_length": auto_ctx,
            "config_context_length": config_ctx_int,
            "effective_context_length": effective_ctx,
            "capabilities": caps,
        }
    except HTTPException:
        # Unknown/invalid profile must surface as 404, not degrade into a
        # 200 with empty model info (which would render as "no model set").
        raise
    except Exception:
        _log.exception("GET /api/model/info failed")
        return dict(_EMPTY_MODEL_INFO)


# ---------------------------------------------------------------------------
# Model assignment — pick provider+model for main slot or auxiliary slots.
# Mirrors the model.options JSON-RPC from tui_gateway but uses REST so the
# Models page (which has no chat PTY open) can drive it.
# ---------------------------------------------------------------------------

# Canonical auxiliary task slots. Keep in sync with DEFAULT_CONFIG["auxiliary"]
# in hermes_cli/config.py — listed here for deterministic ordering in the UI.
_AUX_TASK_SLOTS: Tuple[str, ...] = (
    "vision",
    "web_extract",
    "compression",
    "skills_hub",
    "approval",
    "mcp",
    "title_generation",
    "review",
    "triage_specifier",
    "kanban_decomposer",
    "profile_describer",
    "curator",
)


def _dashboard_code_skew_guard() -> Optional[str]:
    """Return a clear \"restart required\" message when the dashboard runs stale code.

    The dashboard is a long-lived process; its ``sys.modules`` is frozen at
    boot.  When ``hermes update`` (or a manual ``git pull``) replaces the
    checkout underneath it, a first-time lazy import on a new code path can
    resolve a freshly-pulled consumer module against a stale cached dependency
    -> ImportError — e.g. ``/api/model/options`` 500 after the update added
    ``agent.model_metadata.is_grok_46_family`` while the running process kept
    serving the pre-update module (#86207).  Mirror the gateway's
    ``_model_switch_skew_guard``: refuse the risky call with an actionable
    message instead of crashing with a cryptic import error.

    Returns None when no drift is detectable (fresh process, or a non-git
    install where the boot fingerprint could not be read — never a false
    positive).
    """
    from gateway.code_skew import detect_code_skew

    skew = detect_code_skew()
    if not skew:
        return None
    boot_rev, disk_rev = skew
    return (
        f"This dashboard is running code from {boot_rev} but the checkout on "
        f"disk is now {disk_rev}. The model picker would risk a stale-module "
        f"crash — restart the dashboard to load the new code "
        f"(managed system service: systemctl restart hermes-dashboard.service; "
        f"user service: systemctl --user restart hermes-dashboard.service; "
        f"or hermes dashboard --port <port>)"
    )


@app.get("/api/model/options")
async def get_model_options(
    profile: Optional[str] = None,
    refresh: bool = False,
    include_unconfigured: bool = False,
    explicit_only: bool = False,
):
    """Return authenticated providers + their curated model lists.

    REST equivalent of the ``model.options`` JSON-RPC on tui_gateway, so the
    dashboard Models page can render the picker without a live chat session.
    The response shape matches ``model.options`` 1:1 so ``ModelPickerDialog``
    can share the same types.

    ``profile`` scopes the picker context (current model/provider, custom
    providers from config, per-profile .env auth state) so the Models page
    reads the SAME profile /api/model/set writes.

    ``refresh`` busts the per-provider model-id disk cache so every row
    re-fetches its live catalog — used by the picker's explicit "Refresh
    Models" control. Normal opens leave it false to stay on the 1h cache.
    """
    try:
        skew_msg = _dashboard_code_skew_guard()
        if skew_msg:
            _log.warning("GET /api/model/options refused: %s", skew_msg)
            raise HTTPException(
                status_code=503, detail=f"Restart required: {skew_msg}"
            )

        from hermes_cli.inventory import build_model_options_payload, load_picker_context

        def _build_payload_scoped() -> dict:
            # Keep the profile override inside the worker thread so the full
            # sync picker build (config load, pricing, refresh probes) runs
            # off the event loop under the requested profile.
            with _profile_scope(profile):
                return build_model_options_payload(
                    load_picker_context(),
                    explicit_only=bool(explicit_only),
                    include_unconfigured=bool(include_unconfigured),
                    refresh=bool(refresh),
                )

        return await run_in_threadpool(_build_payload_scoped)
    except HTTPException:
        raise
    except Exception:
        _log.exception("GET /api/model/options failed")
        raise HTTPException(status_code=500, detail="Failed to list model options")


@app.get("/api/model/recommended-default")
def get_recommended_default_model(provider: str = ""):
    """Return the recommended default model for a freshly-authenticated provider.

    Mirrors the model-curation `hermes model` does so GUI onboarding lands on a
    sensible default instead of blindly taking the first curated entry. For
    Nous this honors the user's free/paid tier: free users get a free model,
    paid users get the full curated default. For any other provider it falls
    back to the first curated model (same as before).

    Response: {"provider": str, "model": str, "free_tier": bool | None}
    where free_tier is True/False for Nous and None otherwise. `model` may be
    empty if nothing could be resolved (caller degrades gracefully).
    """
    slug = (provider or "").strip().lower()

    if slug == "nous":
        try:
            from hermes_cli.models import (
                get_curated_nous_model_ids,
                get_pricing_for_provider,
                check_nous_free_tier,
                partition_nous_models_by_tier,
                pick_silent_default_model,
                union_with_portal_free_recommendations,
                union_with_portal_paid_recommendations,
            )
            from hermes_cli.auth import get_provider_auth_state

            model_ids = get_curated_nous_model_ids()
            pricing = get_pricing_for_provider("nous") or {}
            free_tier = check_nous_free_tier(force_fresh=True)

            portal_url = ""
            try:
                state = get_provider_auth_state("nous") or {}
                portal_url = state.get("portal_base_url", "") or ""
            except Exception:
                portal_url = ""

            if free_tier:
                model_ids, pricing = union_with_portal_free_recommendations(
                    model_ids, pricing, portal_url
                )
                model_ids, _unavailable = partition_nous_models_by_tier(
                    model_ids, pricing, free_tier=True
                )
            else:
                model_ids, pricing = union_with_portal_paid_recommendations(
                    model_ids, pricing, portal_url
                )

            model = pick_silent_default_model(model_ids, provider="nous")
            return {"provider": "nous", "model": model, "free_tier": bool(free_tier)}
        except Exception:
            _log.exception("GET /api/model/recommended-default (nous) failed")
            return {"provider": "nous", "model": "", "free_tier": None}

    # Non-Nous: preferred silent default when the provider's curated list
    # carries it, else the first curated model. Aggregator lists lead with the
    # priciest Anthropic flagship (claude-fable-5), which must never be the
    # model a user lands on without explicitly picking it.
    try:
        from hermes_cli.inventory import build_models_payload, load_picker_context
        from hermes_cli.models import pick_silent_default_model

        payload = build_models_payload(load_picker_context())
        for row in payload.get("providers", []):
            if str(row.get("slug", "")).lower() == slug:
                models = [str(m) for m in (row.get("models") or [])]
                return {"provider": slug, "model": pick_silent_default_model(models, provider=slug), "free_tier": None}
        return {"provider": slug, "model": "", "free_tier": None}
    except Exception:
        _log.exception("GET /api/model/recommended-default failed")
        return {"provider": slug, "model": "", "free_tier": None}


@app.get("/api/model/auxiliary")
def get_auxiliary_models(profile: Optional[str] = None):
    """Return current auxiliary task assignments.

    Shape:
      {
        "tasks": [
          {"task": "vision", "provider": "auto", "model": "", "base_url": ""},
          ...
        ],
        "main": {"provider": "openrouter", "model": "anthropic/claude-opus-4.7"},
      }

    ``profile`` scopes the read — without it, the Models page would show
    the dashboard profile's auxiliary pins while /api/model/set wrote the
    selected profile's (read/write asymmetry).
    """
    try:
        with _profile_scope(profile):
            cfg = load_config()
        aux_cfg = cfg.get("auxiliary", {})
        if not isinstance(aux_cfg, dict):
            aux_cfg = {}

        tasks = []
        for slot in _AUX_TASK_SLOTS:
            slot_cfg = aux_cfg.get(slot, {}) if isinstance(aux_cfg.get(slot), dict) else {}
            tasks.append({
                "task": slot,
                "provider": str(slot_cfg.get("provider", "auto") or "auto"),
                "model": str(slot_cfg.get("model", "") or ""),
                "base_url": str(slot_cfg.get("base_url", "") or ""),
            })

        model_cfg = cfg.get("model", {})
        if isinstance(model_cfg, dict):
            main = {
                "provider": str(model_cfg.get("provider", "") or ""),
                "model": str(model_cfg.get("default", model_cfg.get("name", "")) or ""),
            }
        else:
            main = {"provider": "", "model": str(model_cfg) if model_cfg else ""}

        return {"tasks": tasks, "main": main}
    except HTTPException:
        raise
    except Exception:
        _log.exception("GET /api/model/auxiliary failed")
        raise HTTPException(status_code=500, detail="Failed to read auxiliary config")


@app.get("/api/model/moa")
def get_moa_models(profile: Optional[str] = None):
    """Return the configured Mixture-of-Agents provider/model slots."""
    try:
        from hermes_cli.moa_config import normalize_moa_config

        with _profile_scope(profile):
            cfg = load_config()
            return normalize_moa_config(cfg.get("moa") if isinstance(cfg, dict) else {})
    except HTTPException:
        raise
    except Exception:
        _log.exception("GET /api/model/moa failed")
        raise HTTPException(status_code=500, detail="Failed to read MoA config")


@app.put("/api/model/moa")
def set_moa_models(body: MoaConfigPayload, profile: Optional[str] = None):
    """Persist the Mixture-of-Agents provider/model slots."""
    try:
        from hermes_cli.moa_config import normalize_moa_config, validate_moa_payload

        def _slot_dict(slot: MoaModelSlot) -> dict:
            # Drop unset optionals so saved slots stay minimal ({provider, model}).
            return {k: v for k, v in slot.dict().items() if v is not None}

        def _preset_dict(preset: MoaPresetPayload) -> dict:
            return {
                "reference_models": [_slot_dict(slot) for slot in preset.reference_models],
                "aggregator": _slot_dict(preset.aggregator),
                "reference_temperature": preset.reference_temperature,
                "aggregator_temperature": preset.aggregator_temperature,
                "reference_timeout": preset.reference_timeout,
                "degraded_reference_policy": preset.degraded_reference_policy,
                "max_tokens": preset.max_tokens,
                "reference_max_tokens": preset.reference_max_tokens,
                "fanout": preset.fanout,
                "enabled": preset.enabled,
            }

        with _profile_scope(body.profile or profile):
            cfg = load_config()
            if body.presets:
                raw = {
                    "default_preset": body.default_preset,
                    "active_preset": body.active_preset,
                    "presets": {name: _preset_dict(preset) for name, preset in body.presets.items()},
                }
            else:
                raw = _preset_dict(
                    MoaPresetPayload(
                        reference_models=body.reference_models,
                        aggregator=body.aggregator,
                        reference_temperature=body.reference_temperature,
                        aggregator_temperature=body.aggregator_temperature,
                        reference_timeout=body.reference_timeout,
                        degraded_reference_policy=body.degraded_reference_policy,
                        max_tokens=body.max_tokens,
                        reference_max_tokens=body.reference_max_tokens,
                        fanout=body.fanout,
                        enabled=body.enabled,
                    )
                )

            # Reject-don't-repair: normalize_moa_config() silently swaps any
            # preset containing incomplete slots for the hardcoded defaults —
            # correct tolerance for hand-edited configs at READ time, silent
            # data loss at WRITE time (#64156: desktop autosave of a
            # half-filled slot replaced the user's whole preset). Refuse the
            # save loudly so no client can corrupt config through this route.
            problems = validate_moa_payload(raw)
            if problems:
                raise HTTPException(
                    status_code=422,
                    detail="Invalid MoA config: " + "; ".join(problems),
                )
            normalized = normalize_moa_config(raw)
            # Merge instead of overwrite so that hand-edited keys not declared
            # in MoaConfigPayload (e.g. save_traces, trace_dir) survive a GUI
            # save.  See issue #58819.
            cfg.setdefault("moa", {}).update(normalized)
            save_config(cfg)
            return {"ok": True, **normalized}
    except HTTPException:
        raise
    except Exception:
        _log.exception("PUT /api/model/moa failed")
        raise HTTPException(status_code=500, detail="Failed to save MoA config")


@app.post("/api/model/set")
async def set_model_assignment(body: ModelAssignment, profile: Optional[str] = None):
    """Assign a model to the main slot or an auxiliary task slot.

    Writes to ``~/.hermes/config.yaml`` — applies to **new** sessions only.
    The currently running chat PTY (if any) is not affected; use the
    ``/model`` slash command inside a chat to hot-swap that specific session.
    """
    scope = (body.scope or "").strip().lower()
    provider = (body.provider or "").strip()
    model = (body.model or "").strip()
    task = (body.task or "").strip().lower()
    base_url = (body.base_url or "").strip()
    api_key = (body.api_key or "").strip()

    if scope not in {"main", "auxiliary"}:
        raise HTTPException(status_code=400, detail="scope must be 'main' or 'auxiliary'")

    try:
        # Expensive-model warning runs BEFORE the profile scope is entered:
        # _profile_scope must never be held across an await (the RLock is
        # reentrant per-thread, so a second coroutine interleaving on the
        # event-loop thread could cross-restore the module globals).
        if model and not body.confirm_expensive_model:
            try:
                from hermes_cli.model_selection_guards import combined_selection_warning

                # Pricing lookup can hit models.dev / a /models endpoint on a
                # cache miss — keep it off the event loop.
                warning = await asyncio.to_thread(
                    combined_selection_warning,
                    model,
                    provider=provider,
                    base_url=base_url,
                )
            except Exception:
                warning = None
            if warning is not None:
                return {
                    "ok": False,
                    "scope": scope,
                    "provider": provider,
                    "model": model,
                    "confirm_required": True,
                    "confirm_message": warning.message,
                }

        def _apply_assignment():
            with _profile_scope(body.profile or profile):
                return _apply_model_assignment_sync(
                    scope, provider, model, task, base_url, api_key
                )

        return await asyncio.to_thread(_apply_assignment)
    except HTTPException:
        raise
    except Exception:
        _log.exception("POST /api/model/set failed")
        raise HTTPException(status_code=500, detail="Failed to save model assignment")


def _apply_model_assignment_sync(
    scope: str, provider: str, model: str, task: str, base_url: str, api_key: str = ""
):
    """Synchronous body of POST /api/model/set.

    Runs inside ``_profile_scope`` (in a worker thread) so every
    load_config/save_config lands in the requested profile.  Raises
    HTTPException for validation errors — the async wrapper re-raises them.
    """
    cfg = load_config()

    if scope == "main":
        if not provider or not model:
            raise HTTPException(status_code=400, detail="provider and model required for main")
        provider, model = _normalize_main_model_assignment(provider, model)
        providers_cfg = cfg.get("providers")
        provider_entry = providers_cfg.get(provider) if isinstance(providers_cfg, dict) else None
        if not base_url and isinstance(provider_entry, dict) and provider_entry.get("base_url"):
            base_url = str(provider_entry.get("base_url") or "").strip()
        model_cfg = _apply_main_model_assignment(
            cfg.get("model", {}), provider, model, base_url, api_key
        )
        if isinstance(provider_entry, dict) and provider_entry.get("api_key"):
            model_cfg["api_key"] = provider_entry["api_key"]
        cfg["model"] = model_cfg

        # When switching the main provider to Nous, mirror the CLI's
        # post-model-selection behaviour (hermes_cli/main.py
        # prompt_enable_tool_gateway / tools_config apply_nous_managed_defaults):
        # auto-route any *unconfigured* tools through the Nous Tool Gateway.
        # This is purely additive — apply_nous_managed_defaults skips every
        # tool where the user already has a direct key (FIRECRAWL_API_KEY,
        # FAL_KEY, etc.) or an explicit backend/provider in config, so it
        # never overwrites a user's own setup. GUI users thus land on the
        # gateway the same way CLI users do, without a separate prompt.
        gateway_tools: list[str] = []
        if provider.strip().lower() == "nous":
            try:
                from hermes_cli.nous_subscription import apply_nous_managed_defaults
                from hermes_cli.tools_config import _get_platform_tools

                enabled = _get_platform_tools(
                    cfg, "cli", include_default_mcp_servers=False
                )
                changed = apply_nous_managed_defaults(
                    cfg,
                    enabled_toolsets=enabled,
                    force_fresh=True,
                )
                gateway_tools = sorted(changed)
            except Exception:
                # Portal lookup hiccups / non-subscriber / non-nous gating
                # must never block saving the model assignment.
                _log.debug("apply_nous_managed_defaults skipped", exc_info=True)

        save_config(cfg)

        # Register a named ``custom_providers`` entry for a custom/local
        # endpoint, mirroring the ``hermes model`` custom flow
        # (_save_custom_provider). Without this the endpoint only lives in
        # ``model.*`` and the picker has no proper ready row for it — the
        # GUI then surfaces a "needs setup" dead-end on the bare ``custom``
        # provider. Dedups by base_url, so re-saving is idempotent.
        if provider.strip().lower() in {"custom", "local"} and base_url:
            try:
                from hermes_cli.main import _auto_provider_name, _save_custom_provider

                _save_custom_provider(
                    base_url,
                    api_key,
                    model,
                    name=_auto_provider_name(base_url),
                )
            except Exception:
                # Never block the assignment on the bookkeeping write —
                # model.* is already persisted and routable.
                _log.debug("custom_providers registration skipped", exc_info=True)

        # Surface auxiliary slots still pinned to a *different* provider than
        # the new main one. Switching the main model does NOT touch aux pins
        # (they're independent, sticky per-task overrides — see
        # auxiliary_client._resolve_auto). A user who switches main away from
        # a now-unpaid provider (e.g. nous with $0 balance) keeps paying 402s
        # on every background aux call until they reset those pins. We never
        # auto-clear them — pinning aux to a cheaper/different model is a
        # legitimate config — but we tell the caller so the UI can offer a
        # "reset to main" nudge instead of silently burning credits.
        new_provider = provider.strip().lower()
        stale_aux: list[dict] = []
        aux_cfg = cfg.get("auxiliary", {})
        if isinstance(aux_cfg, dict):
            for slot in _AUX_TASK_SLOTS:
                slot_cfg = aux_cfg.get(slot)
                if not isinstance(slot_cfg, dict):
                    continue
                slot_provider = str(slot_cfg.get("provider", "") or "").strip()
                if (
                    slot_provider
                    and slot_provider.lower() not in {"auto", ""}
                    and slot_provider.lower() != new_provider
                ):
                    stale_aux.append({
                        "task": slot,
                        "provider": slot_provider,
                        "model": str(slot_cfg.get("model", "") or ""),
                    })

        try:
            effective_config = load_config()
            effective_provider, effective_model = resolve_cron_model_drift_defaults(
                effective_config
            )
            cron_model_impact = build_cron_model_impact(
                current_provider=effective_provider or provider,
                current_model=effective_model or model,
                config=effective_config,
            )
        except Exception:
            _log.debug("cron model impact inspection failed", exc_info=True)
            cron_model_impact = build_cron_model_impact(config=cfg, jobs={})

        return {
            "ok": True,
            "scope": "main",
            "provider": provider,
            "model": model,
            "base_url": model_cfg.get("base_url", ""),
            "gateway_tools": gateway_tools,
            "stale_aux": stale_aux,
            "cron_model_impact": cron_model_impact,
        }

    # scope == "auxiliary"
    aux = cfg.get("auxiliary")
    if not isinstance(aux, dict):
        aux = {}

    if task == "__reset__":
        # Reset every slot to provider="auto", model="" — keeps other fields intact.
        for slot in _AUX_TASK_SLOTS:
            slot_cfg = aux.get(slot)
            if not isinstance(slot_cfg, dict):
                slot_cfg = {}
            slot_cfg["provider"] = "auto"
            slot_cfg["model"] = ""
            slot_cfg.pop("base_url", None)
            clear_model_endpoint_credentials(slot_cfg)
            aux[slot] = slot_cfg
        cfg["auxiliary"] = aux
        save_config(cfg)
        return {"ok": True, "scope": "auxiliary", "reset": True}

    if not provider:
        raise HTTPException(status_code=400, detail="provider required for auxiliary")

    targets = [task] if task else list(_AUX_TASK_SLOTS)
    for slot in targets:
        if slot not in _AUX_TASK_SLOTS:
            raise HTTPException(status_code=400, detail=f"unknown auxiliary task: {slot}")
        slot_cfg = aux.get(slot)
        if not isinstance(slot_cfg, dict):
            slot_cfg = {}
        prev_provider = str(slot_cfg.get("provider") or "").strip().lower()
        new_provider = provider.strip().lower()
        slot_cfg["provider"] = provider
        slot_cfg["model"] = model
        if base_url:
            # Sibling of the main-slot endpoint handling (#65254): an aux
            # assignment for a custom/local endpoint must carry its own
            # base_url, or the slot silently rebinds to whatever
            # model.base_url happens to hold — and breaks entirely once the
            # main slot switches away and clears it. The auxiliary resolver
            # already reads auxiliary.<task>.base_url/api_key
            # (_resolve_task_provider_model), so persisting them here is
            # what actually wires the endpoint in.
            slot_cfg["base_url"] = base_url
            if api_key:
                slot_cfg["api_key"] = api_key
        elif new_provider != prev_provider and new_provider != "custom":
            slot_cfg.pop("base_url", None)
            clear_model_endpoint_credentials(slot_cfg)
        aux[slot] = slot_cfg

    cfg["auxiliary"] = aux
    save_config(cfg)
    return {
        "ok": True,
        "scope": "auxiliary",
        "tasks": targets,
        "provider": provider,
        "model": model,
    }




def _infer_provider_on_model_change(model_val: str, prev_provider: str) -> tuple[str, str]:
    """Infer which provider serves ``model_val`` when the flat Config-page Model
    field changes, given the previously-saved ``prev_provider``.

    Returns ``(provider, model)``; ``provider`` is empty when no switch is
    warranted (leave the existing provider untouched). Two signals, in order:

    1. Curated-catalog detection (``detect_provider_for_model``) — handles the
       ~28 OpenRouter-curated models and direct provider-static catalogs.
    2. Vendor-slug heuristic — a ``vendor/model`` slug cannot belong to a
       single-model / non-aggregator provider (e.g. ``ollama-local``). When the
       current provider is not an aggregator that serves vendor-prefixed slugs,
       route to an aggregator. ``_normalize_main_model_assignment`` (called by
       the caller) keeps the user's current aggregator when they're already on
       one, else falls back to openrouter — the same chokepoint logic as
       ``POST /api/model/set``.
    """
    name = (model_val or "").strip()
    if not name:
        return "", name
    try:
        from hermes_cli.models import (
            _AGGREGATOR_PROVIDERS,
            detect_provider_for_model,
            normalize_provider,
        )
    except Exception:
        return "", name

    try:
        detected = detect_provider_for_model(name, prev_provider)
    except Exception:
        detected = None
    if detected:
        return detected[0], detected[1]

    # Vendor-prefixed slug under a non-aggregator provider → reassign. Use a
    # sentinel "openrouter" here; _normalize_main_model_assignment resolves the
    # real aggregator (keeps a current aggregator, else openrouter).
    if "/" in name:
        try:
            cur_is_aggregator = normalize_provider(prev_provider) in _AGGREGATOR_PROVIDERS
        except Exception:
            cur_is_aggregator = False
        if not cur_is_aggregator:
            return "openrouter", name

    return "", name


def _denormalize_config_from_web(config: Dict[str, Any]) -> Dict[str, Any]:
    """Reverse _normalize_config_for_web before saving.

    Reconstructs ``model`` as a dict by reading the current on-disk config
    to recover model subkeys (provider, base_url, api_mode, etc.) that were
    stripped from the GET response.  The frontend only sees model as a flat
    string; the rest is preserved transparently.

    Also handles ``model_context_length`` — writes it back into the model dict
    as ``context_length``.  A value of 0 or absent means "auto-detect" (omitted
    from the dict so get_model_context_length() uses its normal resolution).
    """
    config = dict(config)
    # Remove any _model_meta that might have leaked in (shouldn't happen
    # with the stripped GET response, but be defensive)
    config.pop("_model_meta", None)

    # Extract and remove model_context_length before processing model
    ctx_override = config.pop("model_context_length", 0)
    if not isinstance(ctx_override, int):
        try:
            ctx_override = int(ctx_override)
        except (TypeError, ValueError):
            ctx_override = 0

    model_val = config.get("model")
    if isinstance(model_val, str) and model_val:
        # Read the current disk config to recover model subkeys
        try:
            disk_config = load_config()
            disk_model = disk_config.get("model")
            if isinstance(disk_model, dict):
                prev_default = str(disk_model.get("default") or "").strip()
                prev_provider = str(disk_model.get("provider") or "").strip()
                # When the model name actually changed, re-detect which
                # provider serves it. The Config-page Model field is a flat
                # string with no provider info, so without this a user who
                # picks an OpenRouter model while their default provider is
                # ollama-local keeps the stale provider and 404s. Only fires
                # on a real model change so saving unrelated config fields
                # never overwrites an explicit provider.
                if model_val != prev_default and prev_provider:
                    new_provider, resolved_model = _infer_provider_on_model_change(
                        model_val, prev_provider
                    )
                    if new_provider and new_provider.strip().lower() != prev_provider.lower():
                        # Route through the canonical assignment chokepoints so
                        # the model is normalized for the new provider and stale
                        # base_url/api_mode/api_key are cleared on the switch
                        # (and preserved on a same-provider re-pick).
                        norm_provider, norm_model = _normalize_main_model_assignment(
                            new_provider, resolved_model
                        )
                        disk_model = _apply_main_model_assignment(
                            disk_model, norm_provider, norm_model
                        )
                        model_val = norm_model
                # Preserve all subkeys, update default with the new value
                disk_model["default"] = model_val
                # Write context_length into the model dict (0 = remove/auto)
                if ctx_override > 0:
                    disk_model["context_length"] = ctx_override
                else:
                    disk_model.pop("context_length", None)
                config["model"] = disk_model
            # Model was previously a bare string — upgrade to dict if
            # user is setting a context_length override
            elif ctx_override > 0:
                config["model"] = {
                    "default": model_val,
                    "context_length": ctx_override,
                }
        except Exception:
            pass  # can't read disk config — just use the string form
    return config


@app.put("/api/config")
async def update_config(body: ConfigUpdate, profile: Optional[str] = None):
    def _run():
        approvals_mode_changed = False
        with _profile_scope(body.profile or profile):
            # The dashboard form is schema-driven (see CONFIG_SCHEMA). Any root
            # key absent from the schema — most visibly ``custom_providers``, but
            # also ``agent.personalities``, ``terminal.lifetime_seconds``, etc. —
            # is not sent in the PUT body. A full-replace save would silently
            # drop those keys. Deep-merge incoming over what's on disk so the
            # frontend can only overwrite what it explicitly sends.
            with _CONFIG_MUTATION_LOCK:
                existing = read_raw_config()
                incoming = _denormalize_config_from_web(body.config)
                merged = _deep_merge(existing, incoming)
                # Compare normalized approvals.mode across the in-memory
                # documents, not config blocks and not cache re-reads: the
                # settings page PUTs the defaulted GET record while disk
                # holds sparse YAML, so a block compare is always-unequal
                # (every autosave would broadcast), and reloading after the
                # save can serve the pre-save cache on an (mtime_ns, size)
                # key collision. Only approvals.mode feeds session.info, so
                # it is the honest trigger.
                approvals_mode_changed = _approval_mode_of(merged) != _approval_mode_of(existing)
                save_config(merged)
        # REST saves bypass the config.set RPC (which re-emits itself), so
        # refresh live sessions' cached approval/YOLO indicators after a mode
        # change. Own-profile saves only: a profile-scoped save targets a
        # different HERMES_HOME than this process's gateway sessions.
        if approvals_mode_changed and not _is_other_profile(body.profile or profile):
            _broadcast_gateway_session_info()
        return {"ok": True}

    try:
        return await asyncio.to_thread(_run)
    except HTTPException:
        raise
    except Exception:
        _log.exception("PUT /api/config failed")
        raise HTTPException(status_code=500, detail="Internal server error")


def _is_other_profile(profile: Optional[str]) -> bool:
    """True when ``profile`` names a profile other than this process's own."""
    requested = (profile or "").strip()
    if not requested or requested.lower() == "current":
        return False
    try:
        target = _resolve_profile_dir(requested)
    except HTTPException:
        return True
    return target.resolve() != get_process_hermes_home().resolve()


def _approval_mode_of(config: Dict[str, Any]) -> str:
    """Normalize approvals.mode from an in-memory config document.

    Both sides of the broadcast comparison use in-memory documents (the raw
    on-disk dict and the about-to-be-saved dict): re-reading through the
    config cache after a save can serve the pre-save document when the
    replacement file collides on the (mtime_ns, size) cache key, which would
    suppress the broadcast exactly when the mode changed. Absent block or
    key normalizes to the same default the approval gate uses.
    """
    from tools.approval import _normalize_approval_mode

    approvals = config.get("approvals")
    default_mode = (DEFAULT_CONFIG.get("approvals") or {}).get("mode", "manual")
    mode = approvals.get("mode", default_mode) if isinstance(approvals, dict) else default_mode
    return _normalize_approval_mode(mode)


def _broadcast_gateway_session_info() -> None:
    """Broadcast session.info on the in-process gateway when it's loaded.

    ``sys.modules`` guard, not an import: gateway never imported means no
    live sessions in this process to notify.
    """
    server = sys.modules.get("tui_gateway.server")
    if server is None:
        return
    try:
        server.broadcast_session_info()
    except Exception:
        _log.exception("session.info broadcast after config save failed")


def _catalog_provider_env_metadata() -> dict:
    """Map provider env vars → desktop card metadata, derived from the catalog.

    Returns ``{env_var: {provider, provider_label, description, url, is_password,
    advanced}}`` for every API-key provider in the unified ``provider_catalog()``
    (i.e. the ``hermes model`` universe). This is what lets the desktop Keys tab
    render a card for a provider even when its env var was never hand-added to
    ``OPTIONAL_ENV_VARS`` — closing the drift where CLI-configurable providers
    (openai-api, kilocode, novita, tencent-tokenhub, copilot, …) were missing
    from the GUI.

    Hand ``OPTIONAL_ENV_VARS`` prose is layered ON TOP of this in the endpoint;
    this only supplies membership + grouping + sensible fallbacks.
    """
    try:
        from hermes_cli.provider_catalog import provider_catalog
    except Exception:
        return {}

    # Env vars already declared with a NON-provider category (e.g. the shared
    # GITHUB_TOKEN, which is a Skills-Hub "tool" credential) must not be
    # promoted into a provider card. Copilot lists GITHUB_TOKEN among its auth
    # aliases, but its provider card uses the provider-owned COPILOT_GITHUB_TOKEN.
    try:
        from hermes_cli.config import OPTIONAL_ENV_VARS as _OPT
    except Exception:
        _OPT = {}
    _non_provider_keys = {
        k for k, v in _OPT.items()
        if (v or {}).get("category") and (v or {}).get("category") != "provider"
    }

    meta: dict = {}
    for d in provider_catalog():
        if d.tab != "keys":
            continue
        # API-key vars: the first is the primary (password) field; any aliases
        # are kept as additional password fields so users can clear them too.
        for env_var in d.api_key_env_vars:
            if env_var in _non_provider_keys:
                continue  # don't hijack a shared tool/messaging credential
            meta.setdefault(
                env_var,
                {
                    "provider": d.slug,
                    "provider_label": d.label,
                    "description": d.description,
                    "url": d.signup_url or None,
                    "is_password": True,
                    "advanced": False,
                    "category": "provider",
                },
            )
        # Base-URL override is an advanced, non-secret field for the same card.
        if d.base_url_env_var:
            meta.setdefault(
                d.base_url_env_var,
                {
                    "provider": d.slug,
                    "provider_label": d.label,
                    "description": f"{d.label} base URL override",
                    "url": None,
                    "is_password": False,
                    "advanced": True,
                    "category": "provider",
                },
            )

        # AWS-SDK providers (Bedrock) authenticate via the AWS credential chain
        # rather than a pasted API key, so they have no api_key_env_vars. Tag
        # their AWS_* settings to the provider card so they still appear on the
        # Keys tab (otherwise Bedrock — a `hermes model` provider — would be
        # invisible in the desktop app).
        if d.auth_type == "aws_sdk":
            for aws_var in ("AWS_REGION", "AWS_PROFILE"):
                existing = meta.get(aws_var, {})
                meta[aws_var] = {
                    "provider": d.slug,
                    "provider_label": d.label,
                    "description": existing.get("description") or f"{d.label} ({aws_var})",
                    "url": existing.get("url"),
                    "is_password": False,
                    "advanced": existing.get("advanced", True),
                    "category": "provider",
                }

        # Vertex AI authenticates via OAuth2 (service-account JSON or ADC), not a
        # pasted API key, so it also has no api_key_env_vars. Tag its credential
        # env var to the provider card so it appears on the Keys tab (otherwise
        # Vertex — a `hermes model` provider — would be invisible in the desktop
        # app). The value is a filesystem path, not a secret string, so it is
        # not a password field.
        if d.auth_type == "vertex":
            existing = meta.get("VERTEX_CREDENTIALS_PATH", {})
            meta["VERTEX_CREDENTIALS_PATH"] = {
                "provider": d.slug,
                "provider_label": d.label,
                "description": existing.get("description")
                or f"{d.label} — service account JSON path (or use ADC)",
                "url": existing.get("url"),
                "is_password": False,
                "advanced": existing.get("advanced", True),
                "category": "provider",
            }
    return meta


@app.get("/api/env")
async def get_env_vars(profile: Optional[str] = None):
    # _profile_scope takes _SKILLS_PROFILE_LOCK and load_env()/catalog
    # discovery read from disk — keep the whole build off the event loop.
    return await asyncio.to_thread(_get_env_vars_sync, profile)


def _get_env_vars_sync(profile: Optional[str] = None):
    with _profile_scope(profile):
        env_on_disk = load_env()
    channel_keys = _channel_managed_env_keys()
    catalog_meta = _catalog_provider_env_metadata()

    def _row(var_name: str, info: dict, *, custom: bool = False) -> dict:
        value = env_on_disk.get(var_name)
        cat_meta = catalog_meta.get(var_name) or {}
        # Hand OPTIONAL_ENV_VARS prose wins where present; the catalog fills any
        # gaps (description/url) and always supplies provider grouping hints.
        return {
            "is_set": bool(value),
            "redacted_value": redact_key(value) if value else None,
            "description": info.get("description") or cat_meta.get("description", ""),
            "url": info.get("url") if info.get("url") is not None else cat_meta.get("url"),
            "category": info.get("category") or cat_meta.get("category", ""),
            "is_password": info.get("password", cat_meta.get("is_password", False)),
            "tools": info.get("tools", []),
            "advanced": info.get("advanced", cat_meta.get("advanced", False)),
            # True when this var is a messaging-platform credential owned by a
            # Channels page card. The Keys/Env page uses this to hide it and
            # avoid duplicating the (richer) Channels configuration UI.
            "channel_managed": var_name in channel_keys,
            # Provider grouping hints derived from the unified provider catalog
            # so the desktop Keys tab groups by the SAME provider identity the
            # CLI `hermes model` picker uses (not desktop-only prefix guesses).
            "provider": cat_meta.get("provider", ""),
            "provider_label": cat_meta.get("provider_label", ""),
            # True when this key exists in the user's .env but is NOT in any
            # catalog (OPTIONAL_ENV_VARS or the provider catalog) — an
            # arbitrary/custom env var the user added directly. Surfaced so the
            # Keys page can list (and let the user manage) them instead of
            # hiding everything it doesn't recognise.
            "custom": custom,
        }

    result = {}
    for var_name, info in OPTIONAL_ENV_VARS.items():
        result[var_name] = _row(var_name, info)
    # Synthesize rows for catalog provider env vars that have no hand entry in
    # OPTIONAL_ENV_VARS — these are the providers that were CLI-configurable but
    # invisible in the desktop app until now.
    for var_name in catalog_meta:
        if var_name not in result:
            result[var_name] = _row(var_name, {})
    # Surface arbitrary/custom keys the user set in .env that aren't in any
    # catalog. These are always "set" (they're on disk). Treated as secrets by
    # default (is_password=True → redacted, reveal-gated) since an unrecognised
    # key could hold anything. Channel-managed credentials are excluded — those
    # belong to the Channels page. This makes the "add a custom key" surface
    # round-trip: a key added there reappears here under its own section.
    for var_name in env_on_disk:
        if var_name in result or var_name in channel_keys:
            continue
        row = _row(var_name, {}, custom=True)
        row["category"] = "custom"
        row["is_password"] = True
        result[var_name] = row
    return result


@app.put("/api/env")
async def set_env_var(body: EnvVarUpdate, profile: Optional[str] = None):
    def _run():
        with _profile_scope(body.profile or profile):
            # Unified credential lifecycle: writes .env AND reconciles any
            # config.yaml mirror still holding the previous value of this var
            # (model.api_key / auxiliary.*.api_key / custom_providers[*]),
            # so a rotation can't leave a stale higher-precedence copy that
            # keeps authenticating with the old key (#62269).
            from hermes_cli.credential_lifecycle import save_provider_env_credential

            return save_provider_env_credential(body.key, body.value)

    try:
        return await asyncio.to_thread(_run)
    except ValueError as exc:
        # save_env_value raises ValueError for invalid names and for keys
        # on the denylist (LD_PRELOAD, PATH, PYTHONPATH, …). Surface the
        # message to the SPA so the user understands why the write was
        # refused instead of seeing an opaque 500.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        _log.exception("PUT /api/env failed")
        raise HTTPException(status_code=500, detail="Internal server error")


# Live credential probes keyed by env var. Each entry is (method, url, auth)
# where auth is "bearer" (Authorization header) or "query" (?key=). A cheap
# read-only models/key call that 401s on a bad token — enough to catch a
# mistyped key before it's persisted. Providers absent from this map (or local
# endpoints) are not network-validated; the client treats those as "unknown".
_CREDENTIAL_PROBES: dict[str, tuple[str, str]] = {
    "OPENROUTER_API_KEY": ("https://openrouter.ai/api/v1/key", "bearer"),
    "OPENAI_API_KEY": ("https://api.openai.com/v1/models", "bearer"),
    "XAI_API_KEY": ("https://api.x.ai/v1/models", "bearer"),
    "GEMINI_API_KEY": ("https://generativelanguage.googleapis.com/v1beta/models", "query"),
}


def _parse_model_ids(resp: "Any") -> List[str]:
    """Extract model ids from an OpenAI-compatible ``/v1/models`` response.

    Tolerant of the common shapes: ``{"data": [{"id": ...}]}`` (OpenAI / vLLM /
    llama.cpp) and a bare ``{"data": ["id", ...]}``. Returns ``[]`` on any
    parse/HTTP error so a slightly non-standard endpoint never hard-blocks.
    """
    try:
        if not resp.is_success:
            return []
        payload = resp.json()
    except Exception:
        return []
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        return []
    ids: List[str] = []
    for item in data:
        if isinstance(item, dict):
            mid = str(item.get("id") or "").strip()
        else:
            mid = str(item or "").strip()
        if mid:
            ids.append(mid)
    return ids


def _custom_endpoint_id(raw: str, fallback: str = "custom") -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", (raw or "").strip()).strip("-_").lower()
    return slug or fallback


def _models_from_custom_endpoint_entry(entry: Dict[str, Any]) -> List[str]:
    models: List[str] = []
    raw_models = entry.get("models")
    if isinstance(raw_models, dict):
        models.extend(str(model).strip() for model in raw_models.keys())
    elif isinstance(raw_models, list):
        models.extend(str(model).strip() for model in raw_models)

    default_model = str(entry.get("model") or entry.get("default_model") or "").strip()
    if default_model:
        models.insert(0, default_model)

    seen: set[str] = set()
    return [model for model in models if model and not (model in seen or seen.add(model))]


def _api_key_display(entry: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """Return ``(has_api_key, preview)`` for a provider or model config block.

    Keys live in ``.env`` behind ``key_env``; only entries written before
    #69449 still carry a plaintext ``api_key``. Checking both keeps the panel
    honest either way — reading only ``api_key`` reported "no API key" for
    every endpoint whose key had been moved to ``.env``.
    """
    plaintext = str(entry.get("api_key") or "").strip()
    if plaintext:
        return True, redact_key(plaintext)
    key_env = str(entry.get("key_env") or "").strip()
    if key_env:
        return True, f"${{{key_env}}}"
    return False, None


def _config_api_key_is_env_ref(endpoint_id: str) -> bool:
    """True when this endpoint's on-disk ``api_key`` is a ``${VAR}`` template.

    ``load_config()`` expands env refs, so a hand-written
    ``api_key: ${MY_KEY}`` is indistinguishable from a literal secret by the
    time it reaches us. Such an entry is already keeping its secret out of
    config.yaml, so migrating it would only copy that secret into a second
    env var the user didn't ask for.
    """
    providers = read_raw_config().get("providers")
    entry = providers.get(endpoint_id) if isinstance(providers, dict) else None
    raw_key = entry.get("api_key") if isinstance(entry, dict) else None
    return bool(isinstance(raw_key, str) and re.search(r"\$\{[^}]+\}", raw_key))


def _custom_endpoint_response(cfg: Dict[str, Any]) -> Dict[str, Any]:
    model_cfg = cfg.get("model", {}) if isinstance(cfg.get("model"), dict) else {}
    current_provider = str(model_cfg.get("provider", "") or "")
    current_model = str(model_cfg.get("default", model_cfg.get("name", "")) or "")
    current_base_url = str(model_cfg.get("base_url", "") or "")

    endpoints: List[Dict[str, Any]] = []
    providers = cfg.get("providers")
    if isinstance(providers, dict):
        for provider_id, raw_entry in providers.items():
            if not isinstance(raw_entry, dict):
                continue
            base_url = str(raw_entry.get("base_url") or raw_entry.get("url") or raw_entry.get("api") or "").strip()
            if not base_url:
                continue
            endpoint_id = str(provider_id)
            models = _models_from_custom_endpoint_entry(raw_entry)
            endpoint_model = str(raw_entry.get("model") or raw_entry.get("default_model") or (models[0] if models else ""))
            has_api_key, api_key_preview = _api_key_display(raw_entry)
            endpoints.append({
                "id": endpoint_id,
                "name": str(raw_entry.get("name") or endpoint_id),
                "base_url": base_url,
                "model": endpoint_model,
                "models": models,
                "context_length": raw_entry.get("context_length"),
                "discover_models": bool(raw_entry.get("discover_models", True)),
                "has_api_key": has_api_key,
                "api_key_preview": api_key_preview,
                "is_current": endpoint_id == current_provider,
                "source": "providers",
            })

    if current_provider.lower() == "custom" and current_base_url and not any(e["id"] == "custom" for e in endpoints):
        has_api_key, api_key_preview = _api_key_display(model_cfg)
        endpoints.insert(0, {
            "id": "custom",
            "name": "Custom",
            "base_url": current_base_url,
            "model": current_model,
            "models": [current_model] if current_model else [],
            "context_length": model_cfg.get("context_length"),
            "discover_models": True,
            "has_api_key": has_api_key,
            "api_key_preview": api_key_preview,
            "is_current": True,
            "source": "direct-config",
        })

    return {
        "endpoints": endpoints,
        "current": {
            "provider": current_provider,
            "model": current_model,
            "base_url": current_base_url,
        },
    }


def _detach_main_model_from_provider(cfg: Dict[str, Any], provider_key: str) -> None:
    """Drop the main-slot mirror of a provider that no longer exists.

    ``activate_custom_endpoint`` copies the endpoint's ``base_url`` and
    ``api_key`` onto ``model``. That mirror outranks the environment at client
    construction (#62269), so deleting the endpoint without clearing it leaves
    the agent still authenticating to the deleted host with the deleted key —
    and leaves that key sitting in config.yaml after the operator believes the
    dashboard removed it.

    Only touches ``model`` when it actually names the deleted provider, so an
    endpoint deleted while a *different* provider is active is left alone.
    """
    model_cfg = cfg.get("model")
    if not isinstance(model_cfg, dict):
        return
    if str(model_cfg.get("provider") or "").strip().lower() != provider_key:
        return
    for field in ("provider", "base_url", "api_key", "key_env"):
        model_cfg.pop(field, None)
    cfg["model"] = model_cfg


def _write_custom_endpoint(cfg: Dict[str, Any], body: CustomEndpointUpdate) -> Tuple[str, Dict[str, Any]]:
    endpoint_id = _custom_endpoint_id(body.id or body.name)
    name = (body.name or "").strip()
    base_url = (body.base_url or "").strip().rstrip("/")
    model = (body.model or "").strip()

    if not name:
        raise HTTPException(status_code=400, detail="name required")
    if not base_url:
        raise HTTPException(status_code=400, detail="base_url required")
    parsed = urllib.parse.urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        raise HTTPException(status_code=400, detail="base_url must include scheme and host")
    if not model:
        raise HTTPException(status_code=400, detail="model required")

    providers = cfg.get("providers")
    if not isinstance(providers, dict):
        providers = {}
    existing = providers.get(endpoint_id)
    if not isinstance(existing, dict):
        existing = {}

    # Merge onto the existing entry rather than replacing it. A providers.<name>
    # block is not owned by this panel: it can carry hand-written keys the
    # dashboard has no field for — ``api_mode``, ``key_env``/``api_key_env``,
    # ``extra_headers`` (which may themselves carry credentials),
    # ``request_overrides`` — and rebuilding from scratch silently dropped every
    # one of them on an unrelated edit, leaving a provider that no longer
    # authenticates or speaks the right protocol.
    entry: Dict[str, Any] = dict(existing)
    entry.update({
        "name": name,
        "base_url": base_url,
        "model": model,
        "discover_models": bool(body.discover_models),
    })
    # Same for the model map: merge rather than replace, so existing models
    # keep their context lengths. ``body.models`` is the catalogue the panel's
    # Test button already discovered — without it only the one hand-typed
    # model survived Save, and every picker showed a single-entry list for a
    # provider serving dozens (#69988). A payload with no ``models`` (older
    # UI) still just ensures the named default is present.
    existing_models = entry.get("models")
    models_map: Dict[str, Any] = dict(existing_models) if isinstance(existing_models, dict) else {}
    for candidate in (*(body.models or ()), model):
        model_id = str(candidate).strip()
        if not model_id:
            continue
        current = models_map.get(model_id)
        models_map[model_id] = dict(current) if isinstance(current, dict) else {}
    entry["models"] = models_map
    if body.context_length and body.context_length > 0:
        entry["context_length"] = int(body.context_length)
        entry["models"][model]["context_length"] = int(body.context_length)

    # API keys never belong in config.yaml (#69449). Write to .env and
    # reference it via ``key_env`` — the same indirection built-in providers
    # use and that runtime_provider.py already resolves at load time.
    env_var = custom_endpoint_key_env(endpoint_id)
    submitted_key = body.api_key.strip() if body.api_key is not None else None
    if submitted_key:
        save_env_value(env_var, submitted_key)
        entry["key_env"] = env_var
        entry.pop("api_key", None)
    elif submitted_key is not None:
        # Blank field means "clear the key", not "leave it alone".
        remove_env_value(env_var)
        entry.pop("key_env", None)
        entry.pop("api_key", None)
    elif str(entry.get("api_key") or "").strip() and not _config_api_key_is_env_ref(endpoint_id):
        # No new key submitted, but this entry still carries one an earlier
        # release wrote in plaintext. Migrate it on the next save so endpoints
        # configured before the fix get cleaned up too, without the user
        # having to re-enter the key.
        save_env_value(env_var, entry["api_key"].strip())
        entry["key_env"] = env_var
        entry.pop("api_key", None)

    providers[endpoint_id] = entry
    cfg["providers"] = providers

    if body.make_default:
        cfg["model"] = _apply_main_model_assignment(
            cfg.get("model", {}), endpoint_id, model, base_url
        )
        if entry.get("key_env") and isinstance(cfg["model"], dict):
            cfg["model"]["key_env"] = entry["key_env"]
            cfg["model"].pop("api_key", None)

    return endpoint_id, entry


@app.get("/api/providers/custom-endpoints")
def list_custom_endpoints(profile: Optional[str] = None):
    """Return configured OpenAI-compatible custom endpoints for Desktop.

    Scoped to the requested profile's config.yaml (issue: custom providers
    only landing in the default profile): the desktop settings UI targets the
    active profile, so read/write must resolve that profile's home rather than
    the process-level HERMES_HOME. Mirrors ``/api/config``'s profile scoping.
    """
    try:
        with _config_profile_scope(profile):
            return _custom_endpoint_response(load_config())
    except HTTPException:
        raise
    except Exception:
        _log.exception("GET /api/providers/custom-endpoints failed")
        raise HTTPException(status_code=500, detail="Failed to list custom endpoints")


@app.post("/api/providers/custom-endpoints")
def upsert_custom_endpoint(body: CustomEndpointUpdate, profile: Optional[str] = None):
    """Create or update a v12+ ``providers`` custom endpoint entry."""
    try:
        with _config_profile_scope(profile):
            cfg = load_config()
            endpoint_id, _entry = _write_custom_endpoint(cfg, body)
            save_config(cfg)
            response = _custom_endpoint_response(cfg)
        response["ok"] = True
        response["id"] = endpoint_id
        return response
    except HTTPException:
        raise
    except Exception:
        _log.exception("POST /api/providers/custom-endpoints failed")
        raise HTTPException(status_code=500, detail="Failed to save custom endpoint")


@app.post("/api/providers/custom-endpoints/{endpoint_id}/activate")
def activate_custom_endpoint(endpoint_id: str, profile: Optional[str] = None):
    """Set a configured custom endpoint as the default model provider."""
    try:
        with _config_profile_scope(profile):
            cfg = load_config()
            provider_key = _custom_endpoint_id(endpoint_id)
            providers = cfg.get("providers")
            entry = providers.get(provider_key) if isinstance(providers, dict) else None
            if not isinstance(entry, dict):
                raise HTTPException(status_code=404, detail="custom endpoint not found")

            models = _models_from_custom_endpoint_entry(entry)
            model = str(entry.get("model") or (models[0] if models else "")).strip()
            base_url = str(entry.get("base_url") or "").strip()
            if not model or not base_url:
                raise HTTPException(status_code=400, detail="custom endpoint is incomplete")

            model_cfg = _apply_main_model_assignment(cfg.get("model", {}), provider_key, model, base_url)
            if entry.get("key_env"):
                model_cfg["key_env"] = entry["key_env"]
                model_cfg.pop("api_key", None)
            elif entry.get("api_key"):
                model_cfg["api_key"] = entry["api_key"]
            cfg["model"] = model_cfg
            save_config(cfg)
        return {"ok": True, "provider": provider_key, "model": model}
    except HTTPException:
        raise
    except Exception:
        _log.exception("POST /api/providers/custom-endpoints/%s/activate failed", endpoint_id)
        raise HTTPException(status_code=500, detail="Failed to activate custom endpoint")


@app.delete("/api/providers/custom-endpoints/{endpoint_id}")
def delete_custom_endpoint(endpoint_id: str, profile: Optional[str] = None):
    """Remove a configured custom endpoint from ``providers``."""
    try:
        with _config_profile_scope(profile):
            cfg = load_config()
            provider_key = _custom_endpoint_id(endpoint_id)
            providers = cfg.get("providers")
            if not isinstance(providers, dict) or provider_key not in providers:
                raise HTTPException(status_code=404, detail="custom endpoint not found")
            providers.pop(provider_key, None)
            cfg["providers"] = providers
            _detach_main_model_from_provider(cfg, provider_key)
            remove_env_value(custom_endpoint_key_env(provider_key))
            save_config(cfg)
            response = _custom_endpoint_response(cfg)
        response["ok"] = True
        return response
    except HTTPException:
        raise
    except Exception:
        _log.exception("DELETE /api/providers/custom-endpoints/%s failed", endpoint_id)
        raise HTTPException(status_code=500, detail="Failed to delete custom endpoint")


@app.post("/api/providers/custom-endpoints/validate")
async def validate_custom_endpoint(body: CustomEndpointUpdate):
    """Probe a custom endpoint by calling its OpenAI-compatible /models URL."""
    import httpx

    base_url = (body.base_url or "").strip().rstrip("/")
    if not base_url:
        return {"ok": False, "reachable": True, "message": "Enter an endpoint URL first.", "models": []}

    url = base_url + "/models"
    headers = {"Accept": "application/json"}
    if body.api_key and body.api_key.strip():
        headers["Authorization"] = f"Bearer {body.api_key.strip()}"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0)) as client:
            resp = await client.get(url, headers=headers)
    except Exception:
        return {"ok": False, "reachable": False, "message": f"Could not reach {url}.", "models": []}

    if resp.status_code in (401, 403):
        return {"ok": False, "reachable": True, "message": "The endpoint rejected the API key.", "models": []}
    if not resp.is_success:
        return {"ok": False, "reachable": True, "message": f"Endpoint returned HTTP {resp.status_code}.", "models": []}

    return {"ok": True, "reachable": True, "message": "", "models": _parse_model_ids(resp)}


@app.post("/api/providers/validate")
async def validate_provider_credential(body: EnvVarUpdate, request: Request):
    """Live-probe a provider credential before it's saved.

    Returns {ok, reachable, message}. ok=True means the provider accepted the
    key; ok=False + reachable=True means the key is bad (caller should block);
    reachable=False means the network probe couldn't run (caller may save with
    a warning rather than hard-blocking offline users).
    """
    _require_token(request)
    import httpx

    key = (body.key or "").strip()
    value = (body.value or "").strip()
    if not value:
        return {"ok": False, "reachable": True, "message": "Enter a value first."}

    # Local / custom endpoint: validate connectivity, not auth — any HTTP
    # response (even 401) proves the endpoint is up. Also surface the model
    # ids the endpoint advertises (OpenAI ``/v1/models`` shape) so the GUI can
    # auto-pick a default without asking the user to type a model name.
    if key == "OPENAI_BASE_URL":
        url = value.rstrip("/") + "/models"
        # Send the optional API key so endpoints that require auth on
        # ``/v1/models`` (many hosted OpenAI-compatible servers) still enumerate
        # their models instead of returning an empty list behind a 401.
        api_key = (body.api_key or "").strip()
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(8.0)) as client:
                resp = await client.get(url, headers=headers)
            return {"ok": True, "reachable": True, "message": "", "models": _parse_model_ids(resp)}
        except Exception:
            return {"ok": False, "reachable": False, "message": f"Could not reach {url}."}

    probe = _CREDENTIAL_PROBES.get(key)
    if not probe:
        # No probe for this provider — can't validate, don't block.
        return {"ok": True, "reachable": False, "message": ""}

    url, auth = probe
    headers = {"Accept": "application/json"}
    params = {}
    if auth == "bearer":
        headers["Authorization"] = f"Bearer {value}"
    else:
        params["key"] = value

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.get(url, headers=headers, params=params)
    except Exception:
        return {"ok": False, "reachable": False, "message": "Could not reach the provider to verify the key."}

    if resp.status_code in (401, 403):
        return {"ok": False, "reachable": True, "message": "That API key was rejected. Double-check it and try again."}
    if resp.status_code == 429 or resp.is_success:
        # 429 = key is valid but rate-limited; success = valid.
        return {"ok": True, "reachable": True, "message": ""}
    return {"ok": False, "reachable": True, "message": f"Provider returned HTTP {resp.status_code} for this key."}


@app.delete("/api/env")
async def remove_env_var(body: EnvVarDelete, profile: Optional[str] = None):
    def _run():
        with _profile_scope(body.profile or profile):
            # Unified credential lifecycle: clears the .env entry AND every
            # mirror of the credential — env-seeded credential_pool entries in
            # auth.json (stale ones kept providers alive in the model picker,
            # #51071/#59761), the affected providers' model-cache rows, and
            # value-matched config.yaml api_key mirrors. OAuth/device-code/
            # manual pool entries for the same provider are preserved.
            from hermes_cli.credential_lifecycle import remove_provider_env_credential

            return remove_provider_env_credential(body.key)

    try:
        result = await asyncio.to_thread(_run)
        if not result.get("found"):
            raise HTTPException(status_code=404, detail=f"{body.key} not found in .env")
        return result
    except HTTPException:
        raise
    except ValueError as exc:
        # remove_env_value raises ValueError for invalid key names. Surface
        # the message to the SPA so the user understands why the delete was
        # refused instead of seeing an opaque 500. Mirrors PUT /api/env.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        _log.exception("DELETE /api/env failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/env/reveal")
async def reveal_env_var(
    body: EnvVarReveal, request: Request, profile: Optional[str] = None
):
    """Return the real (unredacted) value of a single env var.

    Protected by:
    - Ephemeral session token (generated per server start, injected into SPA)
    - Rate limiting (max 5 reveals per 30s window)
    - Audit logging
    """
    # --- Token check ---
    _require_token(request)

    # --- Rate limit ---
    now = time.time()
    cutoff = now - _REVEAL_WINDOW_SECONDS
    _reveal_timestamps[:] = [t for t in _reveal_timestamps if t > cutoff]
    if len(_reveal_timestamps) >= _REVEAL_MAX_PER_WINDOW:
        raise HTTPException(status_code=429, detail="Too many reveal requests. Try again shortly.")
    _reveal_timestamps.append(now)

    # --- Reveal ---
    def _run():
        with _profile_scope(body.profile or profile):
            return load_env()

    env_on_disk = await asyncio.to_thread(_run)
    value = env_on_disk.get(body.key)
    if value is None:
        raise HTTPException(status_code=404, detail=f"{body.key} not found in .env")

    _log.info("env/reveal: %s", body.key)
    return {"key": body.key, "value": value}


# Entries omit fields they don't need to override; the catalog builder fills
# in env_vars from OPTIONAL_ENV_VARS via prefix matching when not specified,
# and pulls required_env from a plugin's PlatformEntry when available.
_PLATFORM_OVERRIDES: dict[str, dict[str, Any]] = {
    "telegram": {
        "name": "Telegram",
        "description": "Run Hermes from Telegram DMs, groups, and topics.",
        "docs_url": "https://core.telegram.org/bots/features#botfather",
        "env_vars": ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS", "TELEGRAM_PROXY"),
        "required_env": ("TELEGRAM_BOT_TOKEN",),
    },
    "discord": {
        "name": "Discord",
        "description": "Connect Hermes to Discord DMs, channels, and threads.",
        "docs_url": "https://discord.com/developers/applications",
        "env_vars": (
            "DISCORD_BOT_TOKEN",
            "DISCORD_ALLOWED_USERS",
        ),
        "required_env": ("DISCORD_BOT_TOKEN",),
    },
    "slack": {
        "name": "Slack",
        "description": "Use Hermes from Slack via Socket Mode. Add allowed Slack member IDs so connected bots can respond.",
        "docs_url": "https://api.slack.com/apps",
        "env_vars": ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_ALLOWED_USERS"),
        "required_env": ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"),
    },
    "mattermost": {
        "name": "Mattermost",
        "description": "Connect Hermes to Mattermost channels and direct messages.",
        "docs_url": "https://mattermost.com/deploy/",
        "env_vars": ("MATTERMOST_URL", "MATTERMOST_TOKEN", "MATTERMOST_ALLOWED_USERS"),
        "required_env": ("MATTERMOST_URL", "MATTERMOST_TOKEN"),
    },
    "matrix": {
        "name": "Matrix",
        "description": "Use Hermes in Matrix rooms and direct messages.",
        "docs_url": "https://matrix.org/ecosystem/servers/",
        "env_vars": (
            "MATRIX_HOMESERVER",
            "MATRIX_ACCESS_TOKEN",
            "MATRIX_USER_ID",
            "MATRIX_ALLOWED_USERS",
        ),
        "required_env": ("MATRIX_HOMESERVER", "MATRIX_ACCESS_TOKEN", "MATRIX_USER_ID"),
    },
    "signal": {
        "name": "Signal",
        "description": "Connect through a signal-cli REST bridge.",
        "docs_url": "https://github.com/bbernhard/signal-cli-rest-api",
        "env_vars": ("SIGNAL_HTTP_URL", "SIGNAL_ACCOUNT", "SIGNAL_ALLOWED_USERS"),
        "required_env": ("SIGNAL_HTTP_URL", "SIGNAL_ACCOUNT"),
    },
    "whatsapp": {
        "name": "WhatsApp",
        "description": (
            "Use Hermes through the bundled WhatsApp bridge with QR-based auth. "
            "LMI uses the separate WhatsApp Unipile transport; do not enable both."
        ),
        "docs_url": "https://github.com/tulir/whatsmeow",
        "env_vars": (
            "WHATSAPP_ENABLED",
            "WHATSAPP_MODE",
            "WHATSAPP_DM_POLICY",
            "WHATSAPP_ALLOWED_USERS",
        ),
        "required_env": (),
    },
    "whatsapp_unipile": {
        "name": "WhatsApp (Unipile)",
        "description": (
            "LMI's customer WhatsApp channel through Unipile. This is separate "
            "from the bundled QR-based WhatsApp bridge."
        ),
        "env_vars": (
            "WHATSAPP_UNIPILE_DSN",
            "WHATSAPP_UNIPILE_API_KEY",
            "WHATSAPP_ACCOUNT_ID",
            "WHATSAPP_ALLOWED_USERS",
        ),
        "required_env": (
            "WHATSAPP_UNIPILE_DSN",
            "WHATSAPP_UNIPILE_API_KEY",
            "WHATSAPP_ACCOUNT_ID",
        ),
    },
    "homeassistant": {
        "name": "Home Assistant",
        "description": "Control your smart home from Hermes via Home Assistant.",
        "docs_url": "https://www.home-assistant.io/docs/authentication/",
        "env_vars": ("HASS_URL", "HASS_TOKEN"),
        "required_env": ("HASS_URL", "HASS_TOKEN"),
    },
    "email": {
        "name": "Email",
        "description": "Talk to Hermes through an IMAP/SMTP mailbox.",
        "docs_url": "https://hermes-agent.nousresearch.com/docs/user-guide/messaging/",
        "env_vars": (
            "EMAIL_ADDRESS",
            "EMAIL_PASSWORD",
            "EMAIL_IMAP_HOST",
            "EMAIL_SMTP_HOST",
        ),
        "required_env": (
            "EMAIL_ADDRESS",
            "EMAIL_PASSWORD",
            "EMAIL_IMAP_HOST",
            "EMAIL_SMTP_HOST",
        ),
    },
    "sms": {
        "name": "SMS (Twilio)",
        "description": "Send and receive text messages via Twilio.",
        "docs_url": "https://www.twilio.com/console",
        "env_vars": ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"),
        "required_env": ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"),
    },
    "dingtalk": {
        "name": "DingTalk",
        "description": "Connect Hermes to DingTalk groups (钉钉).",
        "docs_url": "https://open.dingtalk.com/document/orgapp/the-robot-development-process",
        "env_vars": ("DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET"),
        "required_env": ("DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET"),
    },
    "feishu": {
        "name": "Feishu / Lark",
        "description": "Use Hermes inside Feishu / Lark.",
        "docs_url": "https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/intro",
        "env_vars": (
            "FEISHU_APP_ID",
            "FEISHU_APP_SECRET",
            "FEISHU_ENCRYPT_KEY",
            "FEISHU_VERIFICATION_TOKEN",
        ),
        "required_env": ("FEISHU_APP_ID", "FEISHU_APP_SECRET"),
    },
    "google_chat": {
        "name": "Google Chat",
        "description": "Connect Hermes to Google Chat via Cloud Pub/Sub.",
        "docs_url": "https://hermes-agent.nousresearch.com/docs/user-guide/messaging/google_chat",
    },
    "wecom": {
        "name": "WeCom (group bot)",
        "description": "Send-only WeCom group bot via webhook.",
        "docs_url": "https://developer.work.weixin.qq.com/document/path/91770",
        "env_vars": ("WECOM_BOT_ID", "WECOM_SECRET"),
        "required_env": ("WECOM_BOT_ID",),
    },
    "wecom_callback": {
        "name": "WeCom (app)",
        "description": "Two-way WeCom integration via callback app.",
        "docs_url": "https://developer.work.weixin.qq.com/document/path/90930",
        "env_vars": (
            "WECOM_CALLBACK_CORP_ID",
            "WECOM_CALLBACK_CORP_SECRET",
            "WECOM_CALLBACK_AGENT_ID",
            "WECOM_CALLBACK_TOKEN",
            "WECOM_CALLBACK_ENCODING_AES_KEY",
        ),
        "required_env": (
            "WECOM_CALLBACK_CORP_ID",
            "WECOM_CALLBACK_CORP_SECRET",
            "WECOM_CALLBACK_AGENT_ID",
        ),
    },
    "weixin": {
        "name": "Weixin / WeChat (Personal)",
        "description": "Connect a personal WeChat account through Tencent's iLink Bot API.",
        "docs_url": "https://hermes-agent.nousresearch.com/docs/user-guide/messaging/weixin/",
        "env_vars": ("WEIXIN_ACCOUNT_ID", "WEIXIN_TOKEN", "WEIXIN_BASE_URL"),
        "required_env": ("WEIXIN_ACCOUNT_ID", "WEIXIN_TOKEN"),
    },
    "bluebubbles": {
        "name": "BlueBubbles (iMessage)",
        "description": "Use Hermes through iMessage via a BlueBubbles server.",
        "docs_url": "https://bluebubbles.app/",
        "env_vars": (
            "BLUEBUBBLES_SERVER_URL",
            "BLUEBUBBLES_PASSWORD",
            "BLUEBUBBLES_ALLOWED_USERS",
        ),
        "required_env": ("BLUEBUBBLES_SERVER_URL", "BLUEBUBBLES_PASSWORD"),
    },
    "qqbot": {
        "name": "QQ Bot",
        "description": "Connect Hermes to a QQ Bot from the QQ Open Platform.",
        "docs_url": "https://q.qq.com",
        "env_vars": ("QQ_APP_ID", "QQ_CLIENT_SECRET", "QQ_ALLOWED_USERS"),
        "required_env": ("QQ_APP_ID", "QQ_CLIENT_SECRET"),
    },
    # Teams ships as a platform plugin, so its name/env vars come from the
    # plugin registry. Only the docs link needs an override here so the
    # Channels page can point at the Microsoft Teams setup guide.
    "teams": {
        "description": "Connect Hermes to Microsoft Teams chats via the Bot Framework.",
        "docs_url": "https://hermes-agent.nousresearch.com/docs/user-guide/messaging/teams",
    },
    # Bundled platform plugins: name comes from the plugin registry label;
    # give each a human description (the registry's install_hint is a
    # dependency note, not a description) and a docs link.
    "irc": {
        "description": "Relay messages between an IRC channel (or DMs) and Hermes.",
        "docs_url": "https://hermes-agent.nousresearch.com/docs/user-guide/messaging/irc",
    },
    "line": {
        "description": "Use Hermes from LINE via the LINE Messaging API webhook.",
        "docs_url": "https://hermes-agent.nousresearch.com/docs/user-guide/messaging/line",
    },
    "ntfy": {
        "description": "Chat with Hermes over ntfy push topics (ntfy.sh or self-hosted).",
        "docs_url": "https://hermes-agent.nousresearch.com/docs/user-guide/messaging/ntfy",
    },
    "photon": {
        "description": "Use Hermes through iMessage via Photon's managed Spectrum platform.",
        "docs_url": "https://hermes-agent.nousresearch.com/docs/user-guide/messaging/photon",
    },
    "raft": {
        "description": "Join a Raft workspace as an external agent.",
        "docs_url": "https://hermes-agent.nousresearch.com/docs/user-guide/messaging/raft",
    },
    "simplex": {
        "description": "Talk to Hermes over SimpleX Chat via a local simplex-chat daemon.",
        "docs_url": "https://hermes-agent.nousresearch.com/docs/user-guide/messaging/simplex",
    },
    "yuanbao": {
        "name": "Yuanbao (元宝)",
        "description": "Connect Hermes to Tencent Yuanbao.",
        "docs_url": "",
        "required_env": (),
    },
    "api_server": {
        "name": "API server",
        "description": "Expose Hermes as an OpenAI-compatible HTTP API for tools like Open WebUI.",
        "docs_url": "https://hermes-agent.nousresearch.com/docs/user-guide/messaging/",
        "env_vars": (
            "API_SERVER_ENABLED",
            "API_SERVER_KEY",
            "API_SERVER_PORT",
            "API_SERVER_HOST",
            "API_SERVER_MODEL_NAME",
        ),
        "required_env": (),
    },
    "webhook": {
        "name": "Webhooks",
        "description": "Receive events from GitHub, GitLab, and other webhook sources.",
        "docs_url": "https://hermes-agent.nousresearch.com/docs/user-guide/messaging/webhooks/",
        "env_vars": ("WEBHOOK_ENABLED", "WEBHOOK_PORT", "WEBHOOK_SECRET"),
        "required_env": (),
    },
    "msgraph_webhook": {
        "name": "Microsoft Graph Webhook",
        "description": "Receive Microsoft Graph change notifications (Teams meetings, Outlook, …).",
        "docs_url": "https://hermes-agent.nousresearch.com/docs/user-guide/messaging/msgraph-webhook",
        "required_env": (),
    },
    "whatsapp_cloud": {
        "name": "WhatsApp Cloud API",
        "description": "Use Hermes via Meta's hosted WhatsApp Cloud API (no local bridge).",
        "docs_url": "https://hermes-agent.nousresearch.com/docs/user-guide/messaging/whatsapp-cloud",
    },
    "relay": {
        "name": "Relay (experimental)",
        "description": "Generic relay adapter fronted by the Hermes Relay connector.",
        "docs_url": "",
        "required_env": (),
    },
}

# Display order: well-known platforms surface first; unknown plugins fall to
# the end alphabetically.
_PLATFORM_ORDER: tuple[str, ...] = (
    "telegram",
    "discord",
    "slack",
    "mattermost",
    "matrix",
    "whatsapp",
    "signal",
    "bluebubbles",
    "homeassistant",
    "email",
    "sms",
    "dingtalk",
    "feishu",
    "google_chat",
    "wecom",
    "wecom_callback",
    "weixin",
    "qqbot",
    "yuanbao",
    "api_server",
    "webhook",
)

# Display labels for env vars not in OPTIONAL_ENV_VARS (HOME_CHANNEL_*, bridge
# toggles, Twilio, HASS, Email, etc.). Anything missing from OPTIONAL_ENV_VARS
# falls back here so the UI can still render a friendly label.
_MESSAGING_ENV_FALLBACKS: dict[str, dict[str, Any]] = {
    "SIGNAL_HTTP_URL": {
        "description": "signal-cli REST API base URL, e.g. http://127.0.0.1:8080",
        "prompt": "Signal bridge URL",
        "url": "https://github.com/bbernhard/signal-cli-rest-api",
    },
    "SIGNAL_ACCOUNT": {
        "description": "Signal account phone number registered with the bridge",
        "prompt": "Signal account",
    },
    "SIGNAL_ALLOWED_USERS": {
        "description": "Comma-separated Signal users allowed to use the bot",
        "prompt": "Allowed Signal users",
    },
    "WHATSAPP_ENABLED": {
        "description": "Enable the WhatsApp gateway adapter",
        "prompt": "Enable WhatsApp",
        "advanced": True,
    },
    "WHATSAPP_MODE": {
        "description": "WhatsApp bridge mode",
        "prompt": "WhatsApp mode",
        "advanced": True,
    },
    "WHATSAPP_DM_POLICY": {
        "description": "How WhatsApp direct messages are authorized",
        "prompt": "WhatsApp DM policy",
        "advanced": True,
    },
    "WHATSAPP_ALLOWED_USERS": {
        "description": "Comma-separated WhatsApp users allowed to use the bot",
        "prompt": "Allowed WhatsApp users",
    },
    "HASS_URL": {
        "description": "Home Assistant base URL, e.g. https://homeassistant.local:8123",
        "prompt": "Home Assistant URL",
    },
    "HASS_TOKEN": {
        "description": "Long-lived access token from Home Assistant (Profile → Security)",
        "prompt": "Home Assistant access token",
        "password": True,
    },
    "EMAIL_ADDRESS": {
        "description": "Email address to send and receive from",
        "prompt": "Email address",
    },
    "EMAIL_PASSWORD": {
        "description": "Email account password or app password",
        "prompt": "Email password",
        "password": True,
    },
    "EMAIL_IMAP_HOST": {
        "description": "IMAP server host (e.g. imap.gmail.com)",
        "prompt": "IMAP host",
    },
    "EMAIL_SMTP_HOST": {
        "description": "SMTP server host (e.g. smtp.gmail.com)",
        "prompt": "SMTP host",
    },
    "TWILIO_ACCOUNT_SID": {
        "description": "Twilio Account SID",
        "prompt": "Twilio Account SID",
        "url": "https://www.twilio.com/console",
    },
    "TWILIO_AUTH_TOKEN": {
        "description": "Twilio Auth Token",
        "prompt": "Twilio Auth Token",
        "password": True,
    },
    "WECOM_BOT_ID": {"description": "WeCom group bot ID", "prompt": "WeCom Bot ID"},
    "WECOM_SECRET": {
        "description": "WeCom group bot secret",
        "prompt": "WeCom Secret",
        "password": True,
    },
    "WECOM_CALLBACK_CORP_ID": {
        "description": "WeCom corp ID",
        "prompt": "WeCom Corp ID",
    },
    "WECOM_CALLBACK_CORP_SECRET": {
        "description": "WeCom app corp secret",
        "prompt": "WeCom Corp Secret",
        "password": True,
    },
    "WECOM_CALLBACK_AGENT_ID": {
        "description": "WeCom app agent ID",
        "prompt": "WeCom Agent ID",
    },
    "WECOM_CALLBACK_TOKEN": {
        "description": "WeCom callback verification token",
        "prompt": "WeCom Token",
    },
    "WECOM_CALLBACK_ENCODING_AES_KEY": {
        "description": "WeCom callback AES encoding key",
        "prompt": "WeCom AES Key",
        "password": True,
    },
    "WEIXIN_ACCOUNT_ID": {
        "description": "iLink Bot account ID obtained through QR login in hermes gateway setup",
        "prompt": "iLink Bot account ID",
    },
    "WEIXIN_TOKEN": {
        "description": "iLink Bot token obtained through QR login in hermes gateway setup",
        "prompt": "iLink Bot token",
        "password": True,
    },
    "WEIXIN_BASE_URL": {
        "description": "iLink API base URL saved by QR login (default: https://ilinkai.weixin.qq.com)",
        "prompt": "iLink API base URL",
    },
    "FEISHU_APP_ID": {"description": "Feishu / Lark app ID", "prompt": "App ID"},
    "FEISHU_APP_SECRET": {
        "description": "Feishu / Lark app secret",
        "prompt": "App secret",
        "password": True,
    },
    "FEISHU_ENCRYPT_KEY": {
        "description": "Feishu / Lark encrypt key",
        "prompt": "Encrypt key",
        "password": True,
    },
    "FEISHU_VERIFICATION_TOKEN": {
        "description": "Feishu / Lark verification token",
        "prompt": "Verification token",
        "password": True,
    },
    "DINGTALK_CLIENT_ID": {
        "description": "DingTalk client ID (App key)",
        "prompt": "Client ID",
    },
    "DINGTALK_CLIENT_SECRET": {
        "description": "DingTalk client secret (App secret)",
        "prompt": "Client secret",
        "password": True,
    },
}


def _messaging_platform_catalog() -> tuple[dict[str, Any], ...]:
    """Build the messaging catalog from the gateway's Platform enum + plugin registry.

    Built-in platforms come from ``gateway.config.Platform`` (LOCAL is excluded).
    Plugin platforms come from ``gateway.platform_registry.plugin_entries()``,
    which lets newly installed adapters (e.g. IRC) appear without a code change
    here. Per-platform UI metadata (description, docs URL, env-var picks) lives
    in :data:`_PLATFORM_OVERRIDES`; anything not overridden gets reasonable
    defaults derived from the platform id and required_env.
    """
    from gateway.config import Platform

    # Resolve plugin entries FIRST. Plugin platforms (irc, ntfy, photon, …)
    # leak into ``Platform.__members__`` as pseudo-members the moment any
    # earlier code path calls ``Platform("<plugin id>")`` — and iterating the
    # enum first would then claim them with no plugin metadata, rendering
    # nameless "Irc"/"Ntfy" cards with empty descriptions on the Channels
    # page while the real label/install-hint sat unused in the registry.
    plugin_map: dict[str, Any] = {}
    try:
        # Plugin discovery only runs as a side effect of importing
        # model_tools; this server process doesn't do that, so trigger it
        # explicitly (idempotent) or plugin_entries() is empty here and
        # every plugin platform renders nameless.
        from hermes_cli.plugins import discover_plugins

        discover_plugins()
        from gateway.platform_registry import platform_registry

        for plugin_entry in platform_registry.plugin_entries():
            plugin_map[plugin_entry.name] = plugin_entry
    except Exception:
        _log.debug("plugin platform registry unavailable", exc_info=True)

    seen: set[str] = set()
    entries: list[dict[str, Any]] = []

    for member in Platform.__members__.values():
        if member.value == "local":
            continue
        if member.value in seen:
            continue
        seen.add(member.value)
        entries.append(
            _build_catalog_entry(member.value, plugin_map.get(member.value))
        )

    for name, plugin_entry in plugin_map.items():
        if name in seen:
            continue
        seen.add(name)
        entries.append(_build_catalog_entry(name, plugin_entry))

    order = {pid: idx for idx, pid in enumerate(_PLATFORM_ORDER)}
    entries.sort(
        key=lambda e: (order.get(e["id"], len(_PLATFORM_ORDER)), e["name"].lower())
    )
    return tuple(entries)


def _channel_managed_env_keys() -> frozenset[str]:
    """Env-var keys owned by a Channels page platform card.

    The Channels page is the canonical surface for configuring messaging
    platform credentials (with connection status, test, enable toggle and
    gateway restart). The Keys/Env page consults this set to hide those vars
    so the same fields aren't duplicated in a plainer UI. Best-effort: if the
    gateway catalog can't be built, nothing is flagged and Keys shows it all.
    """
    try:
        keys: set[str] = set()
        for entry in _messaging_platform_catalog():
            keys.update(entry.get("env_vars", ()))
        return frozenset(keys)
    except Exception:
        _log.debug("could not build channel-managed env key set", exc_info=True)
        return frozenset()


# Cross-cutting gateway / relay knobs stay on the Keys → Settings tab even though
# they use the ``messaging`` category in OPTIONAL_ENV_VARS. Platform-scoped vars
# (``DISCORD_*``, ``MATRIX_*``, …) are owned by the Messaging UI instead.
_MESSAGING_KEYS_PAGE_KEYS = frozenset({
    "GATEWAY_ALLOW_ALL_USERS",
    "GATEWAY_PROXY_KEY",
    "GATEWAY_PROXY_URL",
})


def _platform_env_prefixes(platform_id: str) -> tuple[str, ...]:
    """Env-var prefixes owned by a messaging platform card."""
    aliases: dict[str, tuple[str, ...]] = {
        "email": ("EMAIL_",),
        "homeassistant": ("HASS_",),
        "qqbot": ("QQ_", "QQBOT_"),
        "sms": ("TWILIO_",),
        "wecom": ("WECOM_BOT_", "WECOM_SECRET"),
        "wecom_callback": ("WECOM_CALLBACK_",),
    }
    if platform_id in aliases:
        return aliases[platform_id]
    return (platform_id.upper().replace("-", "_") + "_",)


# Which per-platform knobs the setup UI hides, and why: see
# hermes_cli/setup_hidden_env.py. Shared with the `hermes setup gateway`
# wizard so the surfaces ask for the same things.
from hermes_cli.setup_hidden_env import (  # noqa: E402
    is_setup_hidden_env as _is_setup_hidden_env,
)


def _discover_platform_env_vars(platform_id: str) -> tuple[str, ...]:
    """All messaging-category env vars for a platform (override + plugin + prefix)."""
    prefixes = _platform_env_prefixes(platform_id)
    keys: list[str] = []
    for name, info in OPTIONAL_ENV_VARS.items():
        if info.get("category") != "messaging":
            continue
        if name in _MESSAGING_KEYS_PAGE_KEYS:
            continue
        if _is_setup_hidden_env(name):
            continue
        if not any(name.startswith(prefix) for prefix in prefixes):
            continue
        keys.append(name)
    return tuple(sorted(set(keys)))


def _merge_platform_env_vars(
    platform_id: str,
    override: dict[str, Any],
    plugin_entry: Any | None,
) -> tuple[str, ...]:
    """Canonical env-var list for a messaging platform card.

    Required credentials always survive: a platform that genuinely needs one of
    the hidden-suffix vars to connect keeps it, since hiding a required field
    would make the platform unconfigurable.
    """
    discovered = _discover_platform_env_vars(platform_id)
    if "env_vars" in override:
        explicit = tuple(
            key for key in override["env_vars"] if not _is_setup_hidden_env(key)
        )
        return tuple(dict.fromkeys((*explicit, *discovered)))
    if plugin_entry is not None and plugin_entry.required_env:
        return tuple(dict.fromkeys((*tuple(plugin_entry.required_env), *discovered)))
    return discovered


def _build_catalog_entry(
    platform_id: str, plugin_entry: Any | None = None
) -> dict[str, Any]:
    override = _PLATFORM_OVERRIDES.get(platform_id, {})

    env_vars = _merge_platform_env_vars(platform_id, override, plugin_entry)

    if "required_env" in override:
        required_env = tuple(override["required_env"])
    elif plugin_entry is not None:
        required_env = tuple(plugin_entry.required_env or ())
    else:
        required_env = ()

    if override.get("name"):
        name = override["name"]
    elif plugin_entry is not None and plugin_entry.label:
        name = plugin_entry.label
    else:
        name = platform_id.replace("_", " ").title()

    description = override.get("description")
    if not description and plugin_entry is not None:
        description = plugin_entry.install_hint or ""

    return {
        "id": platform_id,
        "name": name,
        "description": description or "",
        "docs_url": override.get("docs_url", ""),
        "env_vars": env_vars,
        "required_env": required_env,
    }


def _catalog_lookup(platform_id: str) -> dict[str, Any] | None:
    for entry in _messaging_platform_catalog():
        if entry["id"] == platform_id:
            return entry
    return None


_WHATSAPP_UNIPILE_REQUIRED_ENV = (
    "WHATSAPP_UNIPILE_DSN",
    "WHATSAPP_UNIPILE_API_KEY",
    "WHATSAPP_ACCOUNT_ID",
)


def _whatsapp_transport_conflict(
    platform_id: str,
    enabled: bool | None,
    env: dict[str, str],
    *,
    runtime: dict[str, Any] | None = None,
) -> str | None:
    """Keep the native bridge and Unipile transport mutually exclusive.

    Both adapters are valid Hermes plugins, but they are different transports
    for the same customer-facing WhatsApp channel.  Allowing both to be
    enabled creates duplicate replies and makes account ownership ambiguous.
    Check the prospective profile environment before writing it so a UI save
    cannot leave a gateway in that split-brain state.
    """
    if enabled is not True:
        return None

    unipile_configured = all(
        str(env.get(key, "") or "").strip()
        for key in _WHATSAPP_UNIPILE_REQUIRED_ENV
    )
    native_enabled = str(env.get("WHATSAPP_ENABLED", "") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    runtime_platforms = runtime.get("platforms") if isinstance(runtime, dict) else {}
    runtime_unipile_connected = bool(
        isinstance(runtime_platforms, dict)
        and isinstance(runtime_platforms.get("whatsapp_unipile"), dict)
        and runtime_platforms["whatsapp_unipile"].get("state") == "connected"
    )
    runtime_native_connected = bool(
        isinstance(runtime_platforms, dict)
        and isinstance(runtime_platforms.get("whatsapp"), dict)
        and runtime_platforms["whatsapp"].get("state") == "connected"
    )

    if platform_id == "whatsapp" and (unipile_configured or runtime_unipile_connected):
        return (
            "Native WhatsApp cannot be enabled while WhatsApp Unipile is "
            "configured. Use the 'WhatsApp Unipile' channel; disable or clear "
            "that transport first if you intentionally want the QR bridge."
        )
    if platform_id == "whatsapp_unipile" and (native_enabled or runtime_native_connected):
        return (
            "WhatsApp Unipile cannot be enabled while the native WhatsApp "
            "bridge is enabled. Disable the 'WhatsApp' channel first so only "
            "one WhatsApp transport can receive and send messages."
        )
    return None


def _messaging_env_info(key: str) -> dict[str, Any]:
    info = OPTIONAL_ENV_VARS.get(key) or _MESSAGING_ENV_FALLBACKS.get(key) or {}
    return {
        "description": info.get("description", ""),
        "prompt": info.get("prompt", key),
        "help": info.get("help", ""),
        "url": info.get("url"),
        "is_password": info.get("password", False),
        "advanced": info.get("advanced", False),
    }


def _gateway_platform_config(platform_id: str):
    from gateway.config import Platform, load_gateway_config

    config = load_gateway_config()
    platform = Platform(platform_id)
    platform_config = config.platforms.get(platform)
    return config, platform, platform_config


def _messaging_platform_payload(
    entry: dict[str, Any],
    env_on_disk: dict[str, str],
    runtime: dict | None,
    scoped: bool = False,
    profile_home: Optional[Path] = None,
) -> dict[str, Any]:
    platform_id = entry["id"]
    runtime_platforms = runtime.get("platforms") if runtime else {}
    runtime_platform = (
        runtime_platforms.get(platform_id, {})
        if isinstance(runtime_platforms, dict)
        else {}
    )
    # Same shared ladder /api/status uses. Before this was unified, the two
    # endpoints disagreed on the same page load — the sidebar strip read
    # "running" (it probed GATEWAY_HEALTH_URL and scoped to the requested
    # profile) while the Channels page rendered "The gateway is not running"
    # (it did neither). Cross-container, profile-scoped, and
    # launch-service-managed deployments each hit that split.
    #
    # profile_home is passed when the request was scoped to a named profile:
    # gateway/status readers resolve process-level paths and do NOT follow the
    # HERMES_HOME contextvar override (#56986 / #69143), so the profile's
    # directory has to be handed over explicitly or messaging silently reports
    # another profile's gateway (#71211).
    liveness = resolve_gateway_liveness(
        profile_dir=profile_home,
        runtime=runtime,
        health_probe=(
            _probe_gateway_health if _GATEWAY_HEALTH_URL else None
        ),
        pid_probe=get_running_pid_cached,
        runtime_reader=read_runtime_status,
        runtime_pid_probe=get_runtime_status_running_pid,
    )
    gateway_running = liveness.running
    runtime_connected = bool(
        gateway_running
        and isinstance(runtime_platform, dict)
        and runtime_platform.get("state") == "connected"
    )
    env_vars = []

    for key in entry["env_vars"]:
        # When profile-scoped, judge only the profile's own .env — the
        # dashboard process's os.environ carries the ROOT install's .env
        # (loaded at startup) and would falsely report the root credentials
        # as the profile's.
        value = env_on_disk.get(key) or ("" if scoped else os.getenv(key, ""))
        env_vars.append(
            {
                "key": key,
                "required": key in entry["required_env"],
                # A production deployment may keep connector credentials in a
                # service-owned runtime.env rather than the dashboard's .env.
                # A connected runtime is authoritative for display, but we do
                # not copy or expose those secret values through this API.
                "is_set": bool(value) or (runtime_connected and key in entry["required_env"]),
                "redacted_value": redact_key(value) if value else None,
                **_messaging_env_info(key),
            }
        )

    if scoped:
        # Profile-scoped view: derive enablement/configuration from the
        # profile's config.yaml + .env only. load_gateway_config()'s
        # env-override layer reads os.environ and would leak the root
        # install's tokens into the profile's reported state.
        try:
            cfg = load_config()
            platforms_cfg = cfg.get("platforms") or {}
            plat_cfg = platforms_cfg.get(platform_id)
            if not isinstance(plat_cfg, dict):
                plat_cfg = {}
            enabled = bool(plat_cfg.get("enabled"))
            hc = plat_cfg.get("home_channel")
            home_channel = hc if isinstance(hc, dict) else None
        except Exception:
            enabled = False
            home_channel = None
        configured = all(env_on_disk.get(key) for key in entry["required_env"])
        if runtime_connected:
            configured = True
    else:
        try:
            gateway_config, platform, platform_config = _gateway_platform_config(
                platform_id
            )
            enabled = bool(platform_config and platform_config.enabled)
            configured = bool(
                platform_config
                and gateway_config._is_platform_connected(platform, platform_config)
            )
            if runtime_connected:
                configured = True
            home_channel = (
                platform_config.home_channel.to_dict()
                if platform_config and platform_config.home_channel
                else None
            )
        except Exception:
            enabled = False
            configured = all(
                env_on_disk.get(key) or os.getenv(key, "")
                for key in entry["required_env"]
            )
            if runtime_connected:
                configured = True
            home_channel = None

    # A live adapter cannot be connected while disabled.  This also covers
    # deployments whose service-owned environment/config is intentionally
    # outside the dashboard's .env surface.
    if runtime_connected:
        enabled = True
        configured = True

    state = (
        runtime_platform.get("state") if isinstance(runtime_platform, dict) else None
    )
    runtime_gateway_state = runtime.get("gateway_state") if isinstance(runtime, dict) else None
    runtime_gateway_error = runtime.get("exit_reason") if isinstance(runtime, dict) else None
    if not enabled:
        state = "disabled"
    elif not configured:
        state = "not_configured"
    elif gateway_running and not state:
        state = "pending_restart"
    elif (
        not gateway_running
        and not state
        and runtime_gateway_state == "startup_failed"
    ):
        state = "startup_failed"
    elif not gateway_running and not state:
        state = "gateway_stopped"

    error_code = (
        runtime_platform.get("error_code")
        if isinstance(runtime_platform, dict)
        else None
    )
    error_message = (
        runtime_platform.get("error_message")
        if isinstance(runtime_platform, dict)
        else None
    )
    if state == "startup_failed":
        error_code = error_code or "startup_failed"
        error_message = error_message or runtime_gateway_error

    whatsapp_setup = None
    if platform_id == "whatsapp":
        whatsapp_mode = (
            env_on_disk.get("WHATSAPP_MODE")
            or ("" if scoped else os.getenv("WHATSAPP_MODE", ""))
        ).strip()
        allowed_users_value = (
            env_on_disk.get("WHATSAPP_ALLOWED_USERS")
            or ("" if scoped else os.getenv("WHATSAPP_ALLOWED_USERS", ""))
        ).strip()
        whatsapp_setup = {
            "mode": whatsapp_mode if whatsapp_mode in {"bot", "self-chat"} else "",
            "allowed_users_set": bool(allowed_users_value),
            "home_channel_set": bool(home_channel),
        }

    payload = {
        "id": platform_id,
        "name": entry["name"],
        "description": entry["description"],
        "docs_url": entry["docs_url"],
        "enabled": enabled,
        "configured": configured,
        "gateway_running": gateway_running,
        "state": state,
        "error_code": error_code,
        "error_message": error_message,
        "updated_at": (
            runtime_platform.get("updated_at")
            if isinstance(runtime_platform, dict)
            else None
        ),
        "home_channel": home_channel,
        "env_vars": env_vars,
    }
    if whatsapp_setup is not None:
        payload["whatsapp_setup"] = whatsapp_setup
    return payload


def _write_platform_enabled(platform_id: str, enabled: bool) -> None:
    write_platform_config_field(platform_id, "enabled", enabled)


_WHATSAPP_ONBOARDING_TTL_SECONDS = 600
_WHATSAPP_ONBOARDING_TERMINAL_STATUSES = {"connected", "error", "expired", "cancelled"}


@dataclass
class _WhatsAppOnboardingSession:
    proc: subprocess.Popen | None
    mode: str
    allowed_users: str
    session_path: str
    expires_at: str
    expires_at_ts: float
    profile: str | None = None
    status: str = "starting"
    qr_payload: str | None = None
    account_id: str | None = None
    account_name: str | None = None
    account_phone: str | None = None
    error: str | None = None


_whatsapp_onboarding_sessions: dict[str, _WhatsAppOnboardingSession] = {}
_whatsapp_onboarding_lock = threading.RLock()


def _utc_iso_from_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_whatsapp_onboarding_mode(value: Any) -> str:
    mode = str(value or "bot").strip().lower()
    if mode not in {"bot", "self-chat"}:
        raise HTTPException(status_code=400, detail="WhatsApp mode must be 'bot' or 'self-chat'.")
    return mode


def _normalize_whatsapp_allowed_users(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return ",".join(part.replace(" ", "") for part in raw.split(",") if part.strip())


def _whatsapp_session_path() -> Path:
    from hermes_constants import get_hermes_dir

    return get_hermes_dir("platforms/whatsapp/session", "whatsapp/session")


def _whatsapp_phone_from_identifier(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    candidate = raw.split("@", 1)[0].split(":", 1)[0]
    digits = re.sub(r"\D+", "", candidate)
    return digits or None


def _whatsapp_linked_account_from_session(session_path: Path) -> tuple[str | None, str | None, str | None]:
    creds_path = session_path / "creds.json"
    try:
        payload = json.loads(creds_path.read_text(encoding="utf-8"))
    except Exception:
        return None, None, None

    account_id: str | None = None
    account_name: str | None = None

    def collect(candidate: Any) -> None:
        nonlocal account_id, account_name
        if not isinstance(candidate, dict):
            return
        if account_id is None:
            for key in ("id", "jid", "lid"):
                value = str(candidate.get(key) or "").strip()
                if value:
                    account_id = value
                    break
        if account_name is None:
            for key in ("name", "verifiedName", "notify", "pushName"):
                value = str(candidate.get(key) or "").strip()
                if value:
                    account_name = value
                    break

    collect(payload.get("me"))
    collect(payload.get("account"))
    collect(payload)
    return account_id, account_name, _whatsapp_phone_from_identifier(account_id)


def _ensure_whatsapp_bridge_dependencies(bridge_dir: Path) -> None:
    """Install bridge dependencies when the dashboard is the setup surface."""
    if (bridge_dir / "node_modules").exists():
        return

    from hermes_constants import find_node_executable, with_hermes_node_path
    from utils import env_int

    npm = find_node_executable("npm")
    if not npm:
        raise HTTPException(
            status_code=500,
            detail="npm was not found. WhatsApp setup needs Node.js and npm.",
        )

    timeout = env_int("WHATSAPP_NPM_INSTALL_TIMEOUT", 300)
    try:
        result = subprocess.run(
            [npm, "install", "--silent"],
            cwd=str(bridge_dir),
            capture_output=True,
            text=True,
            # npm output is UTF-8; guard the Windows ANSI-code-page default
            # against undefined bytes crashing the reader thread (#52649).
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=with_hermes_node_path(),
            creationflags=windows_hide_flags(),
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(
            status_code=500,
            detail="Installing WhatsApp bridge dependencies timed out.",
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to install WhatsApp bridge dependencies: {exc}",
        ) from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if detail:
            detail = "\n".join(detail.splitlines()[-10:])
        raise HTTPException(
            status_code=500,
            detail=f"npm install failed for WhatsApp bridge: {detail or 'no output'}",
        )


def _spawn_whatsapp_pairing_process(session_path: Path, mode: str) -> subprocess.Popen:
    from gateway.platforms.whatsapp_common import resolve_whatsapp_bridge_dir
    from hermes_constants import find_node_executable, with_hermes_node_path

    bridge_dir = resolve_whatsapp_bridge_dir()
    bridge_script = bridge_dir / "bridge.js"
    if not bridge_script.exists():
        raise HTTPException(
            status_code=500,
            detail=f"WhatsApp bridge script was not found at {bridge_script}.",
        )
    node = find_node_executable("node")
    if not node:
        raise HTTPException(
            status_code=500,
            detail="Node.js was not found. WhatsApp setup needs Node.js.",
        )

    _ensure_whatsapp_bridge_dependencies(bridge_dir)
    session_path.mkdir(parents=True, exist_ok=True)

    env = with_hermes_node_path()
    env["WHATSAPP_MODE"] = mode
    env["WHATSAPP_DM_POLICY"] = "pairing"
    return subprocess.Popen(
        [
            node,
            str(bridge_script),
            "--pair-only",
            "--pair-json",
            "--session",
            str(session_path),
        ],
        cwd=str(bridge_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
        env=env,
        creationflags=windows_hide_flags(),
    )


def _terminate_whatsapp_pairing(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _watch_whatsapp_pairing(pairing_id: str, proc: subprocess.Popen) -> None:
    try:
        stream = proc.stdout
        if stream is not None:
            for line in stream:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                event = str(payload.get("event") or "").strip()
                with _whatsapp_onboarding_lock:
                    record = _whatsapp_onboarding_sessions.get(pairing_id)
                    if not record or record.proc is not proc:
                        return
                    if event == "qr":
                        qr = str(payload.get("qr") or "").strip()
                        if qr:
                            record.qr_payload = qr
                            record.status = "waiting"
                            record.error = None
                    elif event == "connected":
                        user = payload.get("user")
                        if isinstance(user, dict):
                            account_id = str(user.get("id") or "").strip()
                            account_name = str(user.get("name") or "").strip()
                            record.account_id = account_id or None
                            record.account_name = account_name or None
                            record.account_phone = _whatsapp_phone_from_identifier(account_id)
                        record.status = "connected"
                        record.error = None
                    elif event == "error":
                        record.status = "error"
                        record.error = str(payload.get("error") or "WhatsApp pairing failed.")
                    elif event == "disconnected" and record.status == "starting":
                        record.status = "waiting"
        returncode = proc.wait()
    except Exception as exc:
        with _whatsapp_onboarding_lock:
            record = _whatsapp_onboarding_sessions.get(pairing_id)
            if record and record.proc is proc and record.status not in _WHATSAPP_ONBOARDING_TERMINAL_STATUSES:
                record.status = "error"
                record.error = str(exc)
        return

    with _whatsapp_onboarding_lock:
        record = _whatsapp_onboarding_sessions.get(pairing_id)
        if not record or record.proc is not proc:
            return
        if record.status in {"connected", "cancelled", "expired"}:
            return
        record.status = "error"
        record.error = (
            "WhatsApp pairing process exited before pairing completed."
            if returncode == 0
            else f"WhatsApp pairing process exited with code {returncode}."
        )


def _run_whatsapp_pairing(pairing_id: str, session_path: Path, mode: str) -> None:
    with _whatsapp_onboarding_lock:
        record = _whatsapp_onboarding_sessions.get(pairing_id)
        if not record or record.status in _WHATSAPP_ONBOARDING_TERMINAL_STATUSES:
            return
        record.status = "installing"

    try:
        proc = _spawn_whatsapp_pairing_process(session_path, mode)
    except Exception as exc:
        with _whatsapp_onboarding_lock:
            record = _whatsapp_onboarding_sessions.get(pairing_id)
            if record and record.status not in _WHATSAPP_ONBOARDING_TERMINAL_STATUSES:
                record.status = "error"
                record.error = str(exc)
        return

    with _whatsapp_onboarding_lock:
        record = _whatsapp_onboarding_sessions.get(pairing_id)
        if not record or record.status in _WHATSAPP_ONBOARDING_TERMINAL_STATUSES:
            _terminate_whatsapp_pairing(proc)
            return
        record.proc = proc
        record.status = "starting"

    _watch_whatsapp_pairing(pairing_id, proc)


def _prune_whatsapp_onboarding_sessions() -> None:
    now = time.time()
    remove_ids: list[str] = []
    for pairing_id, record in _whatsapp_onboarding_sessions.items():
        if (
            record.proc is not None
            and record.status not in _WHATSAPP_ONBOARDING_TERMINAL_STATUSES
            and record.proc.poll() is not None
        ):
            record.status = "error"
            record.error = "WhatsApp pairing process exited before pairing completed."
        if record.expires_at_ts <= now and record.status not in _WHATSAPP_ONBOARDING_TERMINAL_STATUSES:
            _terminate_whatsapp_pairing(record.proc)
            record.status = "expired"
            record.error = "WhatsApp QR setup expired. Start a new setup."
        if record.status in _WHATSAPP_ONBOARDING_TERMINAL_STATUSES and record.expires_at_ts + 300 <= now:
            remove_ids.append(pairing_id)
    for pairing_id in remove_ids:
        _whatsapp_onboarding_sessions.pop(pairing_id, None)


def _supersede_whatsapp_onboarding_sessions(session_path: Path) -> None:
    for existing in _whatsapp_onboarding_sessions.values():
        if existing.session_path == str(session_path) and existing.status not in _WHATSAPP_ONBOARDING_TERMINAL_STATUSES:
            existing.status = "cancelled"
            existing.error = "Superseded by a newer WhatsApp setup session."
            _terminate_whatsapp_pairing(existing.proc)


def _whatsapp_onboarding_payload(pairing_id: str, record: _WhatsAppOnboardingSession) -> dict[str, Any]:
    return {
        "pairing_id": pairing_id,
        "status": record.status,
        "qr_payload": record.qr_payload,
        "expires_at": record.expires_at,
        "mode": record.mode,
        "allowed_users": record.allowed_users,
        "account_id": record.account_id,
        "account_name": record.account_name,
        "account_phone": record.account_phone,
        "error": record.error,
    }


def _restart_gateway_after_whatsapp_onboarding(profile: Optional[str] = None) -> dict[str, Any]:
    try:
        proc, reused = _spawn_gateway_restart(profile)
    except Exception as exc:
        _log.exception("Failed to auto-restart gateway after WhatsApp onboarding")
        return {
            "restart_started": False,
            "restart_error": str(exc),
        }
    if reused:
        _log.info(
            "WhatsApp onboarding: reusing in-flight gateway restart (pid %s)",
            proc.pid,
        )
    return {
        "restart_started": True,
        "restart_action": "gateway-restart",
        "restart_pid": proc.pid,
    }


@app.post("/api/messaging/whatsapp/onboarding/start")
async def start_whatsapp_onboarding(body: WhatsAppOnboardingStart):
    mode = _normalize_whatsapp_onboarding_mode(body.mode)
    allowed_users = _normalize_whatsapp_allowed_users(body.allowed_users)
    effective_profile = body.profile

    with _config_profile_scope(effective_profile):
        session_path = _whatsapp_session_path()
        expires_at_ts = time.time() + _WHATSAPP_ONBOARDING_TTL_SECONDS
        expires_at = _utc_iso_from_ts(expires_at_ts)
        if (session_path / "creds.json").exists():
            pairing_id = secrets.token_urlsafe(16)
            account_id, account_name, account_phone = _whatsapp_linked_account_from_session(session_path)
            record = _WhatsAppOnboardingSession(
                proc=None,
                mode=mode,
                allowed_users=allowed_users,
                session_path=str(session_path),
                expires_at=expires_at,
                expires_at_ts=expires_at_ts,
                profile=effective_profile,
                status="connected",
                account_id=account_id,
                account_name=account_name,
                account_phone=account_phone,
            )
            with _whatsapp_onboarding_lock:
                _prune_whatsapp_onboarding_sessions()
                _supersede_whatsapp_onboarding_sessions(session_path)
                _whatsapp_onboarding_sessions[pairing_id] = record
            return _whatsapp_onboarding_payload(pairing_id, record)

    pairing_id = secrets.token_urlsafe(16)
    record = _WhatsAppOnboardingSession(
        proc=None,
        mode=mode,
        allowed_users=allowed_users,
        session_path=str(session_path),
        expires_at=expires_at,
        expires_at_ts=expires_at_ts,
        profile=effective_profile,
    )

    with _whatsapp_onboarding_lock:
        _prune_whatsapp_onboarding_sessions()
        _supersede_whatsapp_onboarding_sessions(session_path)
        _whatsapp_onboarding_sessions[pairing_id] = record

    threading.Thread(
        target=_run_whatsapp_pairing,
        args=(pairing_id, session_path, mode),
        daemon=True,
    ).start()

    return _whatsapp_onboarding_payload(pairing_id, record)


@app.get("/api/messaging/whatsapp/onboarding/{pairing_id}")
async def get_whatsapp_onboarding_status(pairing_id: str):
    with _whatsapp_onboarding_lock:
        _prune_whatsapp_onboarding_sessions()
        record = _whatsapp_onboarding_sessions.get(pairing_id)
        if not record:
            raise HTTPException(
                status_code=404,
                detail="WhatsApp setup session was not found. Start a new setup.",
            )
        if record.status == "expired":
            raise HTTPException(status_code=410, detail=record.error or "WhatsApp setup expired.")
        return _whatsapp_onboarding_payload(pairing_id, record)


@app.post("/api/messaging/whatsapp/onboarding/{pairing_id}/apply")
async def apply_whatsapp_onboarding(
    pairing_id: str, body: WhatsAppOnboardingApply, profile: Optional[str] = None
):
    with _whatsapp_onboarding_lock:
        _prune_whatsapp_onboarding_sessions()
        record = _whatsapp_onboarding_sessions.get(pairing_id)
        if not record:
            raise HTTPException(
                status_code=404,
                detail="WhatsApp setup session was not found. Start a new setup.",
            )
        if record.status != "connected":
            raise HTTPException(status_code=409, detail="WhatsApp setup is not connected yet.")
        mode = _normalize_whatsapp_onboarding_mode(body.mode or record.mode)
        allowed_users = _normalize_whatsapp_allowed_users(
            record.allowed_users if body.allowed_users is None else body.allowed_users
        )
        if mode == "self-chat" and not allowed_users:
            allowed_users = record.account_phone or record.account_id or ""
        record_profile = record.profile

    effective_profile = body.profile or profile or record_profile
    try:
        with _config_profile_scope(effective_profile):
            save_env_value("WHATSAPP_MODE", mode)
            save_env_value("WHATSAPP_DM_POLICY", "pairing")
            if allowed_users:
                save_env_value("WHATSAPP_ALLOWED_USERS", allowed_users)
            # Blank means "keep the existing allowlist"; explicit clearing
            # still lives in the normal config editor where the field is visible.
            save_env_value("WHATSAPP_ENABLED", "true")
            _write_platform_enabled("whatsapp", True)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        _log.exception("WhatsApp onboarding apply failed")
        raise HTTPException(
            status_code=500,
            detail="Failed to save WhatsApp setup.",
        ) from exc

    with _whatsapp_onboarding_lock:
        _whatsapp_onboarding_sessions.pop(pairing_id, None)

    restart_result = _restart_gateway_after_whatsapp_onboarding(effective_profile)
    return {
        "ok": True,
        "platform": "whatsapp",
        "needs_restart": not restart_result["restart_started"],
        **restart_result,
    }


@app.delete("/api/messaging/whatsapp/onboarding/{pairing_id}")
async def cancel_whatsapp_onboarding(pairing_id: str):
    with _whatsapp_onboarding_lock:
        record = _whatsapp_onboarding_sessions.pop(pairing_id, None)
    if record:
        record.status = "cancelled"
        _terminate_whatsapp_pairing(record.proc)
    return {"ok": True}


_TELEGRAM_ONBOARDING_DEFAULT_URL = "https://setup.hermes-agent.nousresearch.com"
_TELEGRAM_ONBOARDING_USER_AGENT = f"HermesDashboard/{__version__}"
@dataclass
class _TelegramOnboardingPairing:
    poll_token: str
    expires_at: str
    expires_at_ts: float
    bot_token: str | None = None
    bot_username: str | None = None
    owner_user_id: str | None = None


_telegram_onboarding_pairings: dict[str, _TelegramOnboardingPairing] = {}
_telegram_onboarding_lock = threading.RLock()


def _telegram_onboarding_base_url() -> str:
    return (
        os.getenv("TELEGRAM_ONBOARDING_URL", _TELEGRAM_ONBOARDING_DEFAULT_URL)
        .strip()
        .rstrip("/")
    )


def _parse_expiry_ts(value: str) -> float:
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except Exception:
        return time.time() + 600


def _prune_telegram_onboarding_pairings() -> None:
    now = time.time()
    expired = [
        pairing_id
        for pairing_id, record in _telegram_onboarding_pairings.items()
        if record.expires_at_ts <= now
    ]
    for pairing_id in expired:
        _telegram_onboarding_pairings.pop(pairing_id, None)


def _normalize_telegram_user_id(value: Any) -> str | None:
    normalized = str(value or "").strip()
    if _TELEGRAM_USER_ID_RE.fullmatch(normalized):
        return normalized
    return None


def _telegram_onboarding_error_message(error: str, fallback: str) -> str:
    return {
        "not_found": "Telegram pairing was not found. Start a new setup.",
        "expired": "Telegram setup expired. Start a new setup.",
        "claimed": "Telegram setup was already claimed. Start a new setup.",
        "unauthorized": "Telegram setup service rejected this request.",
        "telegram_manager_bot_token_not_configured": "Telegram setup service is not configured.",
        "telegram_token_fetch_failed": "Telegram could not finish bot setup. Try again.",
    }.get(error, fallback)


def _telegram_onboarding_request_sync(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    bearer_token: str | None = None,
) -> dict[str, Any]:
    import httpx

    headers = {
        "Accept": "application/json",
        "User-Agent": _TELEGRAM_ONBOARDING_USER_AGENT,
    }
    request_kwargs: dict[str, Any] = {}
    if body is not None:
        headers["Content-Type"] = "application/json"
        request_kwargs["json"] = body
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"

    url = f"{_telegram_onboarding_base_url()}{path}"
    try:
        with httpx.Client(timeout=httpx.Timeout(10.0)) as client:
            response = client.request(
                method,
                url,
                headers=headers,
                **request_kwargs,
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        try:
            parsed = exc.response.json()
        except Exception:
            parsed = {}
        error = str(parsed.get("error") or parsed.get("status") or "")
        detail = _telegram_onboarding_error_message(
            error,
            "Telegram setup service returned an error.",
        )
        status_code = 404 if exc.response.status_code == 404 else 502
        if error in {"expired", "claimed"}:
            status_code = 410
        raise HTTPException(status_code=status_code, detail=detail) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail="Telegram setup service is unavailable. Try again shortly.",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail="Telegram setup service is unavailable. Try again shortly.",
        ) from exc

    try:
        parsed = response.json()
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail="Telegram setup service returned an invalid response.",
        ) from exc
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=502,
            detail="Telegram setup service returned an invalid response.",
        )
    return parsed


async def _telegram_onboarding_request(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    bearer_token: str | None = None,
) -> dict[str, Any]:
    return await asyncio.to_thread(
        _telegram_onboarding_request_sync,
        method,
        path,
        body=body,
        bearer_token=bearer_token,
    )


@app.post("/api/messaging/telegram/onboarding/start")
async def start_telegram_onboarding(body: TelegramOnboardingStart):
    bot_name = (body.bot_name or "Hermes Agent").strip() or "Hermes Agent"
    payload = await _telegram_onboarding_request(
        "POST",
        "/v1/telegram/pairings",
        body={"bot_name": bot_name},
    )

    pairing_id = str(payload.get("pairing_id") or "").strip()
    poll_token = str(payload.get("poll_token") or "").strip()
    expires_at = str(payload.get("expires_at") or "").strip()
    deep_link = str(payload.get("deep_link") or "").strip()
    qr_payload = str(payload.get("qr_payload") or deep_link).strip()
    suggested_username = str(payload.get("suggested_username") or "").strip()
    if not pairing_id or not poll_token or not expires_at or not deep_link:
        raise HTTPException(
            status_code=502,
            detail="Telegram setup service returned an incomplete response.",
        )

    with _telegram_onboarding_lock:
        _prune_telegram_onboarding_pairings()
        _telegram_onboarding_pairings[pairing_id] = _TelegramOnboardingPairing(
            poll_token=poll_token,
            expires_at=expires_at,
            expires_at_ts=_parse_expiry_ts(expires_at),
        )

    return {
        "pairing_id": pairing_id,
        "suggested_username": suggested_username,
        "deep_link": deep_link,
        "qr_payload": qr_payload,
        "expires_at": expires_at,
    }


@app.get("/api/messaging/telegram/onboarding/{pairing_id}")
async def get_telegram_onboarding_status(pairing_id: str):
    with _telegram_onboarding_lock:
        _prune_telegram_onboarding_pairings()
        record = _telegram_onboarding_pairings.get(pairing_id)
        if not record:
            raise HTTPException(
                status_code=404,
                detail="Telegram setup session was not found. Start a new setup.",
            )
        if record.bot_token:
            return {
                "status": "ready",
                "bot_username": record.bot_username,
                "owner_user_id": record.owner_user_id,
                "expires_at": record.expires_at,
            }
        poll_token = record.poll_token

    payload = await _telegram_onboarding_request(
        "GET",
        f"/v1/telegram/pairings/{urllib.parse.quote(pairing_id, safe='')}",
        bearer_token=poll_token,
    )
    status = str(payload.get("status") or "").strip()
    if status == "waiting":
        with _telegram_onboarding_lock:
            current = _telegram_onboarding_pairings.get(pairing_id)
            expires_at = current.expires_at if current else ""
        return {"status": "waiting", "expires_at": expires_at}

    if status == "ready":
        bot_token = str(payload.get("token") or "").strip()
        bot_username = str(payload.get("bot_username") or "").strip()
        if not bot_token:
            raise HTTPException(
                status_code=502,
                detail="Telegram setup service returned an incomplete response.",
            )
        owner_user_id = _normalize_telegram_user_id(payload.get("owner_user_id"))
        with _telegram_onboarding_lock:
            record = _telegram_onboarding_pairings.get(pairing_id)
            if not record:
                raise HTTPException(
                    status_code=404,
                    detail="Telegram setup session was not found. Start a new setup.",
                )
            record.bot_token = bot_token
            record.bot_username = bot_username or None
            record.owner_user_id = owner_user_id
            return {
                "status": "ready",
                "bot_username": record.bot_username,
                "owner_user_id": record.owner_user_id,
                "expires_at": record.expires_at,
            }

    if status in {"expired", "claimed"}:
        with _telegram_onboarding_lock:
            _telegram_onboarding_pairings.pop(pairing_id, None)
        raise HTTPException(
            status_code=410,
            detail=_telegram_onboarding_error_message(
                status,
                "Telegram setup is no longer available. Start a new setup.",
            ),
        )

    raise HTTPException(
        status_code=502,
        detail="Telegram setup service returned an unknown status.",
    )


def _restart_gateway_after_telegram_onboarding(profile: Optional[str] = None) -> dict[str, Any]:
    """Best-effort gateway restart after saving Telegram QR onboarding.

    The QR flow naturally pulls users into Telegram on another device. If the
    saved token waits on a separate dashboard restart click, Hermes appears
    broken from the chat side. Keep the config save authoritative, but report
    restart failures so the UI can fall back to the existing manual banner.
    """
    try:
        proc, reused = _spawn_gateway_restart(profile)
    except Exception as exc:
        _log.exception("Failed to auto-restart gateway after Telegram onboarding")
        return {
            "restart_started": False,
            "restart_error": str(exc),
        }
    if reused:
        _log.info(
            "Telegram onboarding: reusing in-flight gateway restart (pid %s)",
            proc.pid,
        )
    return {
        "restart_started": True,
        "restart_action": "gateway-restart",
        "restart_pid": proc.pid,
    }


@app.post("/api/messaging/telegram/onboarding/{pairing_id}/apply")
async def apply_telegram_onboarding(
    pairing_id: str, body: TelegramOnboardingApply, profile: Optional[str] = None
):
    allowed_user_ids = []
    seen = set()
    for raw_id in body.allowed_user_ids:
        normalized = _normalize_telegram_user_id(raw_id)
        if not normalized:
            raise HTTPException(
                status_code=400,
                detail="Allowed Telegram user IDs must be numeric.",
            )
        if normalized not in seen:
            seen.add(normalized)
            allowed_user_ids.append(normalized)
    if not allowed_user_ids:
        raise HTTPException(
            status_code=400,
            detail="Add at least one allowed Telegram user ID.",
        )

    with _telegram_onboarding_lock:
        _prune_telegram_onboarding_pairings()
        record = _telegram_onboarding_pairings.get(pairing_id)
        if not record:
            raise HTTPException(
                status_code=404,
                detail="Telegram setup session was not found. Start a new setup.",
            )
        bot_token = record.bot_token
        bot_username = record.bot_username
        if not bot_token:
            raise HTTPException(
                status_code=409,
                detail="Telegram setup is not ready yet.",
            )

    effective_profile = body.profile or profile

    def _apply():
        with _profile_scope(effective_profile):
            save_env_value("TELEGRAM_BOT_TOKEN", bot_token)
            save_env_value("TELEGRAM_ALLOWED_USERS", ",".join(allowed_user_ids))
            _write_platform_enabled("telegram", True)

    try:
        await asyncio.to_thread(_apply)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        _log.exception("Telegram onboarding apply failed")
        raise HTTPException(
            status_code=500,
            detail="Failed to save Telegram setup.",
        ) from exc

    with _telegram_onboarding_lock:
        _telegram_onboarding_pairings.pop(pairing_id, None)

    restart_result = _restart_gateway_after_telegram_onboarding(effective_profile)

    return {
        "ok": True,
        "platform": "telegram",
        "bot_username": bot_username,
        "needs_restart": not restart_result["restart_started"],
        **restart_result,
    }


@app.delete("/api/messaging/telegram/onboarding/{pairing_id}")
async def cancel_telegram_onboarding(pairing_id: str):
    with _telegram_onboarding_lock:
        _telegram_onboarding_pairings.pop(pairing_id, None)
    return {"ok": True}


@app.get("/api/messaging/platforms")
async def get_messaging_platforms(profile: Optional[str] = None):
    # Profile-scoped so the dashboard's global profile switcher shows the
    # TARGET profile's channel credentials/state, not the root install's.
    # load_env() honors the HERMES_HOME contextvar override; the gateway
    # status readers do NOT (they resolve process-level paths), so the
    # profile directory is passed explicitly for those (#71211).
    def _run():
        with _profile_scope(profile) as scoped_dir:
            env_on_disk = load_env()
            runtime = (
                read_runtime_status(path=scoped_dir / "gateway_state.json")
                if scoped_dir is not None
                else read_runtime_status()
            )
            return {
                "env_path": str(get_env_path()),
                "gateway_start_command": _gateway_display_command(profile, "start"),
                "platforms": [
                    _messaging_platform_payload(
                        entry,
                        env_on_disk,
                        runtime,
                        scoped=scoped_dir is not None,
                        profile_home=scoped_dir,
                    )
                    for entry in _messaging_platform_catalog()
                ]
            }

    return await asyncio.to_thread(_run)


def _multiplex_port_binding_conflict(
    platform_id: str, requested_profile: Optional[str]
) -> Optional[str]:
    """Reason enabling ``platform_id`` on the target profile would break a
    multiplexed gateway, or ``None`` when the change is allowed.

    Mirrors the gateway's startup rule (``_start_one_profile_adapters`` in
    gateway/run.py): with ``gateway.multiplex_profiles`` on, the default
    profile owns the single shared HTTP listener and serves every profile via
    the ``/p/<profile>/`` prefix, so a SECONDARY profile must never enable a
    port-binding platform. Without this pre-write check the dashboard happily
    persisted the invalid config and the shared gateway died with
    ``MultiplexConfigError`` on its next start — for ALL profiles. Only
    *enabling* is blocked; disabling/clearing stays allowed so users can
    repair an already-invalid profile.
    """
    from gateway.config import PORT_BINDING_PLATFORM_VALUES, load_gateway_config

    if platform_id not in PORT_BINDING_PLATFORM_VALUES:
        return None

    requested = (requested_profile or "").strip()
    if not requested or requested.lower() == "current":
        from hermes_cli.profiles import get_active_profile_name

        # The dashboard's own profile. "custom" (an unrecognized HERMES_HOME)
        # is outside the profiles tree, so a multiplexed gateway never serves
        # it — nothing to guard.
        target = get_active_profile_name()
    else:
        _resolve_profile_dir(requested)  # same 400/404 as _profile_scope
        target = requested
    if target in ("default", "custom"):
        return None

    # The multiplex flag that matters is the one the shared gateway reads at
    # startup: the DEFAULT profile's gateway config (plus the process-wide
    # GATEWAY_MULTIPLEX_PROFILES override, which load_gateway_config applies).
    with _config_profile_scope("default"):
        if not load_gateway_config().multiplex_profiles:
            return None

    return (
        f"Cannot enable '{platform_id}' on profile '{target}': it binds its "
        "own listener port, and gateway.multiplex_profiles is on, so the "
        "default profile owns the single shared HTTP listener for every "
        "profile. Configure this channel on the default profile instead "
        "(disabling or clearing it here is still allowed)."
    )


@app.put("/api/messaging/platforms/{platform_id}")
async def update_messaging_platform(
    platform_id: str, body: MessagingPlatformUpdate, profile: Optional[str] = None
):
    entry = _catalog_lookup(platform_id)
    if not entry:
        raise HTTPException(
            status_code=404, detail=f"Unknown messaging platform: {platform_id}"
        )

    target_profile = body.profile or profile
    if body.enabled:
        conflict = _multiplex_port_binding_conflict(platform_id, target_profile)
        if conflict:
            # Reject BEFORE any .env/config.yaml write so the profile stays
            # loadable by the multiplexed gateway.
            _log.info(
                "Rejected messaging platform update: platform=%s profile=%s "
                "(multiplex port-binding conflict)",
                platform_id,
                target_profile or "current",
            )
            raise HTTPException(status_code=409, detail=conflict)

    allowed_env = set(entry["env_vars"])

    def _apply():
        with _profile_scope(body.profile or profile) as scoped_dir:
            # Evaluate the complete prospective environment before any write.
            # The Channels UI sends enabled=true even for a credential edit,
            # so this also prevents a save from accidentally activating a
            # second WhatsApp transport.
            prospective_env = dict(load_env())
            for key in body.clear_env:
                prospective_env.pop(key, None)
            for key, value in body.env.items():
                trimmed = value.strip()
                if trimmed:
                    prospective_env[key] = trimmed
            runtime = (
                read_runtime_status(path=scoped_dir / "gateway_state.json")
                if scoped_dir is not None
                else read_runtime_status()
            )
            conflict = _whatsapp_transport_conflict(
                platform_id, body.enabled, prospective_env, runtime=runtime
            )
            if conflict:
                raise HTTPException(status_code=409, detail=conflict)

            for key in body.clear_env:
                if key not in allowed_env:
                    raise HTTPException(
                        status_code=400,
                        detail=f"{key} is not configurable for {entry['name']}",
                    )
                remove_env_value(key)

            for key, value in body.env.items():
                if key not in allowed_env:
                    raise HTTPException(
                        status_code=400,
                        detail=f"{key} is not configurable for {entry['name']}",
                    )
                trimmed = value.strip()
                if trimmed:
                    _validate_messaging_env_value(platform_id, key, trimmed)
                    save_env_value(key, trimmed)

            if body.enabled is not None:
                _write_platform_enabled(platform_id, body.enabled)

    try:
        await asyncio.to_thread(_apply)

        # Audit trail for channel config mutations: names only, never values.
        _log.info(
            "Messaging platform updated: platform=%s profile=%s enabled=%s "
            "env_keys=%s cleared_keys=%s",
            platform_id,
            target_profile or "current",
            body.enabled,
            sorted(body.env),
            sorted(body.clear_env),
        )
        return {"ok": True, "platform": platform_id}
    except HTTPException:
        raise
    except Exception:
        _log.exception("PUT /api/messaging/platforms/%s failed", platform_id)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/messaging/platforms/{platform_id}/test")
async def test_messaging_platform(platform_id: str, profile: Optional[str] = None):
    entry = _catalog_lookup(platform_id)
    if not entry:
        raise HTTPException(
            status_code=404, detail=f"Unknown messaging platform: {platform_id}"
        )

    def _run():
        with _profile_scope(profile) as scoped_dir:
            env_on_disk = load_env()
            runtime = (
                read_runtime_status(path=scoped_dir / "gateway_state.json")
                if scoped_dir is not None
                else read_runtime_status()
            )
            return _messaging_platform_payload(
                entry,
                env_on_disk,
                runtime,
                scoped=scoped_dir is not None,
                profile_home=scoped_dir,
            )

    payload = await asyncio.to_thread(_run)
    if not payload["enabled"]:
        message = f"{entry['name']} is disabled. Enable it, then restart the gateway."
        return {"ok": False, "state": payload["state"], "message": message}
    if not payload["configured"]:
        missing = [
            field["key"]
            for field in payload["env_vars"]
            if field["required"] and not field["is_set"]
        ]
        message = (
            f"Missing required setup: {', '.join(missing)}"
            if missing
            else "Platform setup is incomplete."
        )
        return {"ok": False, "state": payload["state"], "message": message}
    if not payload["gateway_running"]:
        return {
            "ok": False,
            "state": payload["state"],
            "message": "Gateway is not running. Restart the gateway to connect this platform.",
        }
    if payload["state"] == "connected":
        return {
            "ok": True,
            "state": payload["state"],
            "message": f"{entry['name']} is connected.",
        }
    if payload.get("error_message"):
        return {
            "ok": False,
            "state": payload["state"],
            "message": payload["error_message"],
        }
    return {
        "ok": False,
        "state": payload["state"],
        "message": "Setup looks complete, but the gateway has not reported a connection yet. Restart the gateway.",
    }


# ---------------------------------------------------------------------------
# OAuth provider endpoints — status + disconnect (Phase 1)
# ---------------------------------------------------------------------------
#
# Phase 1 surfaces *which OAuth providers exist* and whether each is
# connected, plus a disconnect button. The actual login flow (PKCE for
# Anthropic, device-code for Nous/Codex) still runs in the CLI for now;
# Phase 2 will add in-browser flows. For unconnected providers we return
# the canonical ``hermes auth add <provider>`` command so the dashboard
# can surface a one-click copy.


def _truncate_token(value: Optional[str], visible: int = 6) -> str:
    """Return ``...XXXXXX`` (last N chars) for safe display in the UI.

    We never expose more than the trailing ``visible`` characters of an
    OAuth access token. JWT prefixes (the part before the first dot) are
    stripped first when present so the visible suffix is always part of
    the signing region rather than a meaningless header chunk.

    Returns the Entra-ID placeholder when handed a callable (Azure Foundry
    bearer provider) — the callable is NEVER invoked here.
    """
    if not value:
        return ""
    if callable(value) and not isinstance(value, str):
        # Entra ID bearer provider — never reveal a minted token in the UI.
        return "<entra-id-bearer>"
    s = str(value)
    if "." in s and s.count(".") >= 2:
        # Looks like a JWT — show the trailing piece of the signature only.
        s = s.rsplit(".", 1)[-1]
    if len(s) <= visible:
        return s
    return f"…{s[-visible:]}"


def _anthropic_oauth_status() -> Dict[str, Any]:
    """Status for the "Anthropic API Key" catalog entry.

    Two sources, in priority order:
    1. ``~/.hermes/.anthropic_oauth.json`` — Hermes-managed PKCE flow (what
       this entry's Connect button writes)
    2. ``ANTHROPIC_API_KEY`` → ``ANTHROPIC_TOKEN`` → ``CLAUDE_CODE_OAUTH_TOKEN``
       env vars (registry order) — from ``.env``, the shell, or an external
       secret source like Bitwarden (whose keys are injected into the process
       env during ``load_hermes_dotenv()``, so the same check covers them)

    Claude Code's ``~/.claude/.credentials.json`` is deliberately NOT read
    here — it has its own dedicated catalog entry (``claude-code`` →
    ``_claude_code_only_status``). Reporting it under the API-key entry
    double-counts the token and shadows a real ANTHROPIC_API_KEY.
    """
    try:
        from agent.anthropic_adapter import (
            read_hermes_oauth_credentials,
            _get_hermes_oauth_file,
        )
    except ImportError:
        read_hermes_oauth_credentials = None  # type: ignore
        _get_hermes_oauth_file = None  # type: ignore

    hermes_creds = None
    if read_hermes_oauth_credentials:
        try:
            hermes_creds = read_hermes_oauth_credentials()
        except Exception:
            hermes_creds = None
    if hermes_creds and hermes_creds.get("accessToken"):
        return {
            "logged_in": True,
            "source": "hermes_pkce",
            "source_label": f"Hermes PKCE ({_get_hermes_oauth_file() if _get_hermes_oauth_file else None})",
            "token_preview": _truncate_token(hermes_creds.get("accessToken")),
            "expires_at": hermes_creds.get("expiresAt"),
            "has_refresh_token": bool(hermes_creds.get("refreshToken")),
        }

    # Env-var / secret-source path. ``get_env_value`` checks the process
    # environment first (where Bitwarden-sourced secrets land) then .env.
    env_var_order: tuple = ("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY
        env_var_order = PROVIDER_REGISTRY["anthropic"].api_key_env_vars
    except (ImportError, KeyError):
        pass
    try:
        from hermes_cli.config import get_env_value
    except ImportError:
        get_env_value = None  # type: ignore
    try:
        from hermes_cli.env_loader import format_secret_source_suffix
    except ImportError:
        format_secret_source_suffix = None  # type: ignore

    for var in env_var_order:
        value = (get_env_value(var) if get_env_value else None) or os.getenv(var)
        if not value:
            continue
        suffix = format_secret_source_suffix(var) if format_secret_source_suffix else ""
        return {
            "logged_in": True,
            "source": "env_var",
            "source_label": f"{var}{suffix}",
            "token_preview": _truncate_token(value),
            "expires_at": None,
            "has_refresh_token": False,
        }
    return {"logged_in": False, "source": None}


def _claude_code_only_status() -> Dict[str, Any]:
    """Surface Claude Code CLI credentials as their own provider entry.

    Independent of the Anthropic entry above so users can see whether their
    Claude Code subscription tokens are actively flowing into Hermes even
    when they also have a separate Hermes-managed PKCE login.
    """
    try:
        from agent.anthropic_adapter import read_claude_code_credentials
        creds = read_claude_code_credentials()
    except Exception:
        creds = None
    if creds and creds.get("accessToken"):
        return {
            "logged_in": True,
            "source": "claude_code_cli",
            "source_label": "~/.claude/.credentials.json",
            "token_preview": _truncate_token(creds.get("accessToken")),
            "expires_at": creds.get("expiresAt"),
            "has_refresh_token": bool(creds.get("refreshToken")),
        }
    return {"logged_in": False, "source": None}


def _copilot_acp_status() -> Dict[str, Any]:
    """Status for copilot-acp — credentials are owned by the Copilot CLI.

    There is no cheap programmatic credential probe for the ACP subprocess, so
    this is a read-only "managed by the Copilot CLI" card (like claude-code):
    Hermes never claims a login state it can't verify.
    """
    return {
        "logged_in": False,
        "source": "copilot_cli",
        "source_label": "Managed by the GitHub Copilot CLI",
        "token_preview": None,
        "expires_at": None,
        "has_refresh_token": False,
    }


# Explicit, hand-tuned OAuth/account provider cards. These carry the bits that
# can't be derived from the unified provider catalog: the OAuth ``flow`` shape,
# the per-provider ``status_fn``, the ``cli_command`` fallback, and curated
# display order. They are the OVERRIDE BASE for ``_build_oauth_catalog()``,
# which unions them with every accounts-tab provider in ``provider_catalog()``
# so newly-added OAuth/external providers appear automatically (no hand edit).
# This tuple also still includes two entries that are NOT catalog providers but
# must show on the Accounts tab: the api-key Anthropic PKCE card and the
# synthetic ``claude-code`` subscription row.
# ``flow`` describes the OAuth shape so the modal can pick the right UI:
# ``pkce`` = open URL + paste callback code, ``device_code`` = show code +
# verification URL + poll, ``external`` = read-only (delegated to a third-party
# CLI like Claude Code or Qwen).
_OAUTH_PROVIDER_CATALOG: tuple[Dict[str, Any], ...] = (
    {
        "id": "nous",
        "name": "Nous Portal",
        "flow": "device_code",
        "cli_command": "hermes auth add nous",
        "docs_url": "https://portal.nousresearch.com",
        "status_fn": None,  # dispatched via auth.get_nous_auth_status
    },
    {
        "id": "openai-codex",
        "name": "ChatGPT or Codex Subscription",
        "flow": "device_code",
        "cli_command": "hermes auth add openai-codex",
        "docs_url": "https://platform.openai.com/docs",
        "status_fn": None,  # dispatched via auth.get_codex_auth_status
    },
    {
        "id": "qwen-oauth",
        "name": "Qwen (via Qwen CLI)",
        "flow": "external",
        "cli_command": "hermes auth add qwen-oauth",
        "docs_url": "https://github.com/QwenLM/qwen-code",
        "status_fn": None,  # dispatched via auth.get_qwen_auth_status
    },
    {
        "id": "minimax-oauth",
        "name": "MiniMax (OAuth)",
        # MiniMax's flow is structurally device-code (verification URI +
        # user code, backend polls the token endpoint) with a PKCE
        # extension for code-binding. The dashboard renders the same UX
        # as Nous's device-code flow; the PKCE bit is a security
        # extension that doesn't change the operator experience.
        "flow": "device_code",
        "cli_command": "hermes auth add minimax-oauth",
        "docs_url": "https://www.minimax.io",
        "status_fn": None,  # dispatched via auth.get_minimax_oauth_auth_status
    },
    {
        "id": "xai-oauth",
        "name": "xAI Grok OAuth (SuperGrok / Premium+)",
        # Device code is the default because it works in remote shells,
        # containers, and desktop installs without requiring a reachable
        # 127.0.0.1 callback.
        "flow": "device_code",
        "cli_command": "hermes auth add xai-oauth",
        "docs_url": "https://hermes-agent.nousresearch.com/docs/guides/xai-grok-oauth",
        "status_fn": None,  # dispatched via auth.get_xai_oauth_auth_status
    },
    {
        "id": "copilot-acp",
        "name": "GitHub Copilot (ACP)",
        "flow": "external",
        "cli_command": "copilot /login",
        "docs_url": "https://docs.github.com/en/copilot",
        "status_fn": _copilot_acp_status,
    },
    # ── Anthropic / Claude entries sit at the bottom: the API-key path
    # first, then the subscription OAuth path (which only works with extra
    # usage credits on top of a Claude Max plan — see disclaimer in name).
    {
        "id": "anthropic",
        "name": "Anthropic API Key",
        "flow": "pkce",
        "cli_command": "hermes auth add anthropic",
        "docs_url": "https://docs.claude.com/en/api/getting-started",
        "status_fn": _anthropic_oauth_status,
    },
    {
        "id": "claude-code",
        "name": "Anthropic OAuth: Required Extra Usage Credits to Use Subscription",
        "flow": "external",
        "cli_command": "claude setup-token",
        "docs_url": "https://docs.claude.com/en/docs/claude-code",
        "status_fn": _claude_code_only_status,
    },
)


def _resolve_provider_status(provider_id: str, status_fn) -> Dict[str, Any]:
    """Dispatch to the right status helper for an OAuth provider entry."""
    if status_fn is not None:
        try:
            return status_fn()
        except Exception as e:
            return {"logged_in": False, "error": str(e)}
    try:
        from hermes_cli import auth as hauth
        if provider_id == "nous":
            # Read-only accounts-tab card: refresh-free snapshot so listing
            # providers never performs an OAuth refresh.
            raw = hauth.get_nous_auth_status_local()
            return {
                "logged_in": bool(raw.get("logged_in")),
                "source": "nous_portal",
                "source_label": raw.get("portal_base_url") or "Nous Portal",
                "token_preview": _truncate_token(raw.get("access_token")),
                "expires_at": raw.get("access_expires_at"),
                "has_refresh_token": bool(raw.get("has_refresh_token")),
            }
        if provider_id == "openai-codex":
            raw = hauth.get_codex_auth_status()
            return {
                "logged_in": bool(raw.get("logged_in")),
                "source": raw.get("source") or "openai_codex",
                "source_label": raw.get("auth_mode") or "OpenAI Codex",
                "token_preview": _truncate_token(raw.get("api_key")),
                "expires_at": None,
                "has_refresh_token": False,
                "last_refresh": raw.get("last_refresh"),
            }
        if provider_id == "qwen-oauth":
            raw = hauth.get_qwen_auth_status()
            return {
                "logged_in": bool(raw.get("logged_in")),
                "source": "qwen_cli",
                "source_label": raw.get("auth_store_path") or "Qwen CLI",
                "token_preview": _truncate_token(raw.get("access_token")),
                "expires_at": raw.get("expires_at"),
                "has_refresh_token": bool(raw.get("has_refresh_token")),
            }
        if provider_id == "minimax-oauth":
            raw = hauth.get_minimax_oauth_auth_status()
            return {
                "logged_in": bool(raw.get("logged_in")),
                "source": "minimax_oauth",
                "source_label": f"MiniMax ({raw.get('region', 'global')})",
                "token_preview": None,
                "expires_at": raw.get("expires_at"),
                "has_refresh_token": True,
            }
        if provider_id == "xai-oauth":
            raw = hauth.get_xai_oauth_auth_status()
            # source_label is meant to be a human-readable origin (auth-store
            # path / credential source), not the internal auth_mode string
            # ("oauth_pkce"). Prefer the store path, then the source slug.
            return {
                "logged_in": bool(raw.get("logged_in")),
                "source": raw.get("source") or "xai_oauth",
                "source_label": raw.get("auth_store") or raw.get("source") or "xAI Grok OAuth",
                "token_preview": _truncate_token(raw.get("api_key")),
                "expires_at": None,
                "has_refresh_token": True,
                "last_refresh": raw.get("last_refresh"),
            }
        # No hand-written branch for this provider id: fall through to the
        # canonical slug-driven dispatcher so accounts-tab providers derived
        # from the unified catalog (which carry status_fn=None) still reflect
        # real login state instead of rendering permanently logged-out. This
        # closes the membership-auto-extends-but-status-doesn't gap: add an
        # OAuth/account provider plugin and its card shows the right state.
        raw = hauth.get_auth_status(provider_id)
        if isinstance(raw, dict) and "logged_in" in raw:
            return {
                "logged_in": bool(raw.get("logged_in")),
                "source": raw.get("source") or raw.get("provider") or provider_id,
                "source_label": (
                    raw.get("source_label")
                    or raw.get("auth_store")
                    or raw.get("auth_store_path")
                    or raw.get("base_url")
                    or raw.get("name")
                    or ""
                ),
                "token_preview": _truncate_token(
                    raw.get("access_token") or raw.get("api_key")
                ),
                "expires_at": raw.get("expires_at") or raw.get("access_expires_at"),
                "has_refresh_token": bool(raw.get("has_refresh_token")),
            }
    except Exception as e:
        return {"logged_in": False, "error": str(e)}
    return {"logged_in": False}


def _oauth_provider_disconnect_command(provider: Dict[str, Any]) -> Optional[str]:
    """Shell command that clears an external provider's credentials.

    External providers store their credentials outside Hermes, so the disconnect
    API deliberately refuses them (we never delete files another CLI owns on the
    user's behalf via a silent API call). For the ones we know how to clear we
    instead hand the GUI a command it can *run in the embedded terminal* — the
    user sees exactly what executes, and Hermes then stops resolving the token.

    Claude Code has no scriptable logout (only the interactive ``/logout``), so
    we remove the credential the same way logout does: the macOS Keychain entry
    (``Claude Code-credentials``) and/or the ``~/.claude/.credentials.json``
    file — the two sources ``read_claude_code_credentials()`` consults. Returns
    None for providers we can't safely clear (the GUI shows a manual hint).
    """
    if provider.get("flow") != "external":
        return None
    if provider.get("id") == "claude-code":
        rm_file = "rm -f ~/.claude/.credentials.json"
        if sys.platform == "darwin":
            return f'security delete-generic-password -s "Claude Code-credentials" 2>/dev/null; {rm_file}'
        return rm_file
    return None


def _oauth_provider_disconnect_hint(provider: Dict[str, Any], status: Dict[str, Any]) -> Optional[str]:
    """Return the manual disconnect path when the API cannot clear this provider."""
    if provider.get("flow") == "external":
        if _oauth_provider_disconnect_command(provider):
            # The GUI offers a one-click "run in terminal" path; this hint is the
            # fallback wording for surfaces that only show text.
            return "Managed outside Hermes — run the disconnect command to remove it."
        return "Managed by that provider's CLI; remove it there."
    if status.get("source") == "env_var":
        return "Remove the API key from Settings → Keys instead."
    return None


def _build_oauth_catalog() -> list[Dict[str, Any]]:
    """Build the Accounts-tab provider list.

    MEMBERSHIP is the union of:
      1. ``_OAUTH_PROVIDER_CATALOG`` — the explicit, hand-tuned cards that carry
         bespoke flow / status_fn / cli_command (including the api-key Anthropic
         PKCE card and the synthetic claude-code subscription row, which are not
         catalog providers), and
      2. every accounts-tab provider in the unified ``provider_catalog()`` (the
         ``hermes model`` universe) — so any OAuth/external provider added as a
         plugin appears automatically, with sensible defaults, even if no
         explicit card was written for it.

    The explicit catalog wins on metadata; the unified catalog guarantees we
    never silently drop a provider the CLI picker offers. Order: explicit cards
    first (their curated order), then any catalog-only providers appended in
    ``hermes model`` order.
    """
    rows: list[Dict[str, Any]] = []
    seen: set[str] = set()

    # 1. Explicit hand-tuned cards (authoritative metadata + curated order).
    for entry in _OAUTH_PROVIDER_CATALOG:
        if entry["id"] in seen:
            continue
        seen.add(entry["id"])
        rows.append(dict(entry))

    # 2. Catalog accounts-providers not already covered — keeps the Accounts tab
    #    in lockstep with the `hermes model` universe (zero-edit for new plugins).
    try:
        from hermes_cli.provider_catalog import provider_catalog
        for d in provider_catalog():
            if d.tab != "accounts" or d.slug in seen:
                continue
            seen.add(d.slug)
            rows.append({
                "id": d.slug,
                "name": d.label,
                "flow": "external",
                "cli_command": f"hermes auth add {d.slug}",
                "docs_url": d.signup_url or "",
                "status_fn": None,
            })
    except Exception:
        pass

    return rows


@app.get("/api/providers/oauth")
async def list_oauth_providers(profile: Optional[str] = None):
    """Enumerate every OAuth-capable LLM provider with current status.

    Response shape (per provider):
        id              stable identifier (used in DELETE path)
        name            human label
        flow            "pkce" | "device_code" | "external"
        cli_command     fallback CLI command for users to run manually
        disconnect_command  shell command that clears an external provider's
                            creds (run in the embedded terminal), else null
        docs_url        external docs/portal link for the "Learn more" link
        status:
          logged_in        bool — currently has usable creds
          source           short slug ("hermes_pkce", "claude_code", ...)
          source_label     human-readable origin (file path, env var name)
          token_preview    last N chars of the token, never the full token
          expires_at       ISO timestamp string or null
          has_refresh_token bool

    Membership is derived from the unified provider_catalog() so this stays in
    sync with the `hermes model` picker; _OAUTH_OVERRIDES supplies per-provider
    flow/status/cli metadata.
    """
    def _run():
        with _profile_scope(profile):
            providers = []
            for p in _build_oauth_catalog():
                status = _resolve_provider_status(p["id"], p.get("status_fn"))
                disconnect_hint = _oauth_provider_disconnect_hint(p, status)
                providers.append({
                    "id": p["id"],
                    "name": p["name"],
                    "flow": p["flow"],
                    "cli_command": p["cli_command"],
                    "docs_url": p["docs_url"],
                    "disconnect_hint": disconnect_hint,
                    "disconnect_command": _oauth_provider_disconnect_command(p),
                    "disconnectable": disconnect_hint is None,
                    "status": status,
                })
            return {"providers": providers}

    return await asyncio.to_thread(_run)


@app.delete("/api/providers/oauth/{provider_id}")
async def disconnect_oauth_provider(
    provider_id: str,
    request: Request,
    profile: Optional[str] = None,
):
    """Disconnect an OAuth provider. Token-protected (matches /env/reveal)."""
    _require_token(request)

    def _run():
        with _profile_scope(profile):
            catalog_by_id = {p["id"]: p for p in _build_oauth_catalog()}
            provider = catalog_by_id.get(provider_id)
            if provider is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown provider: {provider_id}. "
                           f"Available: {', '.join(sorted(catalog_by_id))}",
                )

            disconnect_hint = _oauth_provider_disconnect_hint(provider, {})
            if disconnect_hint:
                raise HTTPException(
                    status_code=400,
                    detail=f"{provider['name']} cannot be disconnected automatically. {disconnect_hint}",
                )

            status = _resolve_provider_status(provider_id, provider.get("status_fn"))
            disconnect_hint = _oauth_provider_disconnect_hint(provider, status)
            if disconnect_hint:
                raise HTTPException(
                    status_code=400,
                    detail=f"{provider['name']} cannot be disconnected automatically. {disconnect_hint}",
                )

            # Anthropic clears only the Hermes-managed PKCE file and auth-store entry.
            # The separate claude-code catalog row is external/read-only and rejected
            # above so we never pretend to remove ~/.claude/* credentials owned by the CLI.
            if provider_id == "anthropic":
                cleared = False
                try:
                    from agent.anthropic_adapter import _get_hermes_oauth_file
                    oauth_file = _get_hermes_oauth_file()
                    if oauth_file.exists():
                        oauth_file.unlink()
                        cleared = True
                except Exception:
                    pass
                # Also clear the credential pool entry if present.
                try:
                    from hermes_cli.auth import clear_provider_auth
                    cleared = clear_provider_auth("anthropic") or cleared
                except Exception:
                    pass
                _log.info("oauth/disconnect: %s", provider_id)
                return {"ok": bool(cleared), "provider": provider_id}

            try:
                from hermes_cli.auth import clear_provider_auth, invalidate_nous_auth_status_cache
                cleared = clear_provider_auth(provider_id)
                if provider_id == "nous":
                    invalidate_nous_auth_status_cache()
                _log.info("oauth/disconnect: %s (cleared=%s)", provider_id, cleared)
                return {"ok": bool(cleared), "provider": provider_id}
            except Exception as e:
                _log.exception("disconnect %s failed", provider_id)
                raise HTTPException(status_code=500, detail=str(e))

    return await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# OAuth Phase 2 — in-browser PKCE & device-code flows
# ---------------------------------------------------------------------------
#
# Two flow shapes are supported:
#
#   PKCE (Anthropic):
#     1. POST /api/providers/oauth/anthropic/start
#          → server generates code_verifier + challenge, builds claude.ai
#            authorize URL, stashes verifier in _oauth_sessions[session_id]
#          → returns { session_id, flow: "pkce", auth_url }
#     2. UI opens auth_url in a new tab. User authorizes, copies code.
#     3. POST /api/providers/oauth/anthropic/submit { session_id, code }
#          → server exchanges (code + verifier) → tokens at console.anthropic.com
#          → persists to ~/.hermes/.anthropic_oauth.json AND credential pool
#          → returns { ok: true, status: "approved" }
#
#   Device code (Nous, OpenAI Codex):
#     1. POST /api/providers/oauth/{nous|openai-codex}/start
#          → server hits provider's device-auth endpoint
#          → gets { user_code, verification_url, device_code, interval, expires_in }
#          → spawns background poller thread that polls the token endpoint
#            every `interval` seconds until approved/expired
#          → stores poll status in _oauth_sessions[session_id]
#          → returns { session_id, flow: "device_code", user_code,
#                      verification_url, expires_in, poll_interval }
#     2. UI opens verification_url in a new tab and shows user_code.
#     3. UI polls GET /api/providers/oauth/{provider}/poll/{session_id}
#          every 2s until status != "pending".
#     4. On "approved" the background thread has already saved creds; UI
#        refreshes the providers list.
#
# Sessions are kept in-memory only (single-process FastAPI) and time out
# after 15 minutes. A periodic cleanup runs on each /start call to GC
# expired sessions so the dict doesn't grow without bound.

_OAUTH_SESSION_TTL_SECONDS = 15 * 60
_oauth_sessions: Dict[str, Dict[str, Any]] = {}
_oauth_sessions_lock = threading.Lock()

# Import OAuth constants from canonical source instead of duplicating.
# Guarded so hermes web still starts if anthropic_adapter is unavailable;
# Phase 2 endpoints will return 501 in that case.
try:
    from agent.anthropic_adapter import (
        _OAUTH_CLIENT_ID as _ANTHROPIC_OAUTH_CLIENT_ID,
        _OAUTH_TOKEN_URL as _ANTHROPIC_OAUTH_TOKEN_URL,
        _OAUTH_TOKEN_URLS as _ANTHROPIC_OAUTH_TOKEN_URLS,
        _OAUTH_REDIRECT_URI as _ANTHROPIC_OAUTH_REDIRECT_URI,
        _OAUTH_SCOPES as _ANTHROPIC_OAUTH_SCOPES,
        _generate_pkce as _generate_pkce_pair,
    )
    _ANTHROPIC_OAUTH_AVAILABLE = True
except ImportError:
    _ANTHROPIC_OAUTH_AVAILABLE = False
_ANTHROPIC_OAUTH_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"


def _gc_oauth_sessions() -> None:
    """Drop expired sessions. Called opportunistically on /start."""
    cutoff = time.time() - _OAUTH_SESSION_TTL_SECONDS
    with _oauth_sessions_lock:
        stale = [sid for sid, sess in _oauth_sessions.items() if sess["created_at"] < cutoff]
        for sid in stale:
            _oauth_sessions.pop(sid, None)


def _oauth_profile_name(profile: Optional[str]) -> Optional[str]:
    requested = (profile or "").strip()
    if not requested or requested.lower() == "current":
        return None
    return requested


def _validate_oauth_profile(profile: Optional[str]) -> None:
    profile_name = _oauth_profile_name(profile)
    if profile_name:
        _resolve_profile_dir(profile_name)


def _new_oauth_session(
    provider_id: str,
    flow: str,
    profile: Optional[str] = None,
) -> tuple[str, Dict[str, Any]]:
    """Create + register a new OAuth session, return (session_id, session_dict)."""
    sid = secrets.token_urlsafe(16)
    profile_name = _oauth_profile_name(profile)
    sess = {
        "session_id": sid,
        "provider": provider_id,
        "flow": flow,
        "profile": profile_name,
        "created_at": time.time(),
        "status": "pending",  # pending | approved | denied | expired | error
        "error_message": None,
    }
    with _oauth_sessions_lock:
        _oauth_sessions[sid] = sess
    return sid, sess


def _oauth_session_profile(
    session_id: str,
    fallback: Optional[str] = None,
) -> Optional[str]:
    """Return the profile that owns an OAuth session, if one was provided."""
    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
        profile = sess.get("profile") if sess else None
    return profile or _oauth_profile_name(fallback)


def _save_anthropic_oauth_creds(access_token: str, refresh_token: str, expires_at_ms: int) -> None:
    """Persist Anthropic PKCE creds to both Hermes file AND credential pool.

    Mirrors what auth_commands.add_command does so the dashboard flow leaves
    the system in the same state as ``hermes auth add anthropic``.
    """
    from agent.anthropic_adapter import _get_hermes_oauth_file
    oauth_file = _get_hermes_oauth_file()
    payload = {
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "expiresAt": expires_at_ms,
    }
    # atomic_json_write creates the temp with mode 0o600 (via mkstemp) *before*
    # any content is written, then fsyncs and atomically replaces the target.
    # The previous os.replace + post-hoc chmod left a TOCTOU window in which the
    # OAuth token file was world-readable at the default umask (0o644 on most
    # hosts) between the rename and the chmod. atomic_json_write also preserves
    # the existing file's owner and cleans up its temp on failure.
    from utils import atomic_json_write

    atomic_json_write(oauth_file, payload, indent=2, mode=0o600)
    # Best-effort credential-pool insert. Failure here doesn't invalidate
    # the file write — pool registration only matters for the rotation
    # strategy, not for runtime credential resolution.
    try:
        from agent.credential_pool import (
            PooledCredential,
            load_pool,
            AUTH_TYPE_OAUTH,
            SOURCE_MANUAL,
        )
        import uuid
        pool = load_pool("anthropic")
        # Avoid duplicate entries: delete any prior dashboard-issued OAuth entry
        existing = [e for e in pool.entries() if getattr(e, "source", "").startswith(f"{SOURCE_MANUAL}:dashboard_pkce")]
        for e in existing:
            try:
                pool.remove_entry(getattr(e, "id", ""))
            except Exception:
                pass
        entry = PooledCredential(
            provider="anthropic",
            id=uuid.uuid4().hex[:6],
            label="dashboard PKCE",
            auth_type=AUTH_TYPE_OAUTH,
            priority=0,
            source=f"{SOURCE_MANUAL}:dashboard_pkce",
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at_ms=expires_at_ms,
        )
        pool.add_entry(entry)
    except Exception as e:
        _log.warning("anthropic pool add (dashboard) failed: %s", e)


def _start_anthropic_pkce(profile: Optional[str] = None) -> Dict[str, Any]:
    """Begin PKCE flow. Returns the auth URL the UI should open."""
    if not _ANTHROPIC_OAUTH_AVAILABLE:
        raise HTTPException(status_code=501, detail="Anthropic OAuth not available (missing adapter)")
    verifier, challenge = _generate_pkce_pair()
    sid, sess = _new_oauth_session("anthropic", "pkce", profile=profile)
    sess["verifier"] = verifier
    sess["state"] = verifier  # Anthropic round-trips verifier as state
    params = {
        "code": "true",
        "client_id": _ANTHROPIC_OAUTH_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": _ANTHROPIC_OAUTH_REDIRECT_URI,
        "scope": _ANTHROPIC_OAUTH_SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": verifier,
    }
    auth_url = f"{_ANTHROPIC_OAUTH_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"
    return {
        "session_id": sid,
        "flow": "pkce",
        "auth_url": auth_url,
        "expires_in": _OAUTH_SESSION_TTL_SECONDS,
    }


def _submit_anthropic_pkce(
    session_id: str,
    code_input: str,
    profile: Optional[str] = None,
) -> Dict[str, Any]:
    """Exchange authorization code for tokens. Persists on success."""
    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
    if not sess or sess["provider"] != "anthropic" or sess["flow"] != "pkce":
        raise HTTPException(status_code=404, detail="Unknown or expired session")
    if sess["status"] != "pending":
        return {"ok": False, "status": sess["status"], "message": sess.get("error_message")}

    # Anthropic's redirect callback page formats the code as `<code>#<state>`.
    # Strip the state suffix if present (we already have the verifier server-side).
    parts = code_input.strip().split("#", 1)
    code = parts[0].strip()
    if not code:
        return {"ok": False, "status": "error", "message": "No code provided"}
    state_from_callback = parts[1] if len(parts) > 1 else ""

    exchange_data = json.dumps({
        "grant_type": "authorization_code",
        "client_id": _ANTHROPIC_OAUTH_CLIENT_ID,
        "code": code,
        "state": state_from_callback or sess["state"],
        "redirect_uri": _ANTHROPIC_OAUTH_REDIRECT_URI,
        "code_verifier": sess["verifier"],
    }).encode()
    # Anthropic migrated the OAuth token endpoint to platform.claude.com;
    # console.anthropic.com now 404s. Try the new host first, then fall back.
    result = None
    last_exc = None
    for _endpoint in _ANTHROPIC_OAUTH_TOKEN_URLS:
        req = urllib.request.Request(
            _endpoint,
            data=exchange_data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "hermes-dashboard/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = json.loads(resp.read().decode())
            break
        except Exception as e:
            last_exc = e
            continue
    if result is None:
        with _oauth_sessions_lock:
            sess["status"] = "error"
            sess["error_message"] = f"Token exchange failed: {last_exc}"
        return {"ok": False, "status": "error", "message": sess["error_message"]}

    access_token = result.get("access_token", "")
    refresh_token = result.get("refresh_token", "")
    expires_in = int(result.get("expires_in") or 3600)
    if not access_token:
        with _oauth_sessions_lock:
            sess["status"] = "error"
            sess["error_message"] = "No access token returned"
        return {"ok": False, "status": "error", "message": sess["error_message"]}

    expires_at_ms = int(time.time() * 1000) + (expires_in * 1000)
    try:
        with _profile_scope(_oauth_session_profile(session_id, profile)):
            _save_anthropic_oauth_creds(access_token, refresh_token, expires_at_ms)
    except Exception as e:
        with _oauth_sessions_lock:
            sess["status"] = "error"
            sess["error_message"] = f"Save failed: {e}"
        return {"ok": False, "status": "error", "message": sess["error_message"]}
    with _oauth_sessions_lock:
        sess["status"] = "approved"
    _log.info("oauth/pkce: anthropic login completed (session=%s)", session_id)
    return {"ok": True, "status": "approved"}


async def _start_device_code_flow(
    provider_id: str,
    profile: Optional[str] = None,
) -> Dict[str, Any]:
    """Initiate a device-code flow (Nous, OpenAI Codex, MiniMax, or xAI).

    Calls the provider's device-auth endpoint via the existing CLI helpers,
    then spawns a background poller. Returns the user-facing display fields
    so the UI can render the verification page link + user code.
    """
    if provider_id == "nous":
        from hermes_cli.auth import (
            _request_device_code,
            PROVIDER_REGISTRY,
        )
        import httpx
        pconfig = PROVIDER_REGISTRY["nous"]
        portal_base_url = (
            os.getenv("HERMES_PORTAL_BASE_URL")
            or os.getenv("NOUS_PORTAL_BASE_URL")
            or pconfig.portal_base_url
        ).rstrip("/")
        client_id = pconfig.client_id
        scope = pconfig.scope

        def _do_nous_device_request():
            with httpx.Client(
                timeout=httpx.Timeout(15.0),
                headers={"Accept": "application/json"},
            ) as client:
                return (
                    _request_device_code(
                        client=client,
                        portal_base_url=portal_base_url,
                        client_id=client_id,
                        scope=scope,
                    ),
                    scope,
                )

        device_data, effective_scope = await asyncio.get_running_loop().run_in_executor(
            None, _do_nous_device_request
        )
        sid, sess = _new_oauth_session("nous", "device_code", profile=profile)
        sess["device_code"] = str(device_data["device_code"])
        sess["interval"] = int(device_data["interval"])
        sess["expires_at"] = time.time() + int(device_data["expires_in"])
        sess["portal_base_url"] = portal_base_url
        sess["client_id"] = client_id
        sess["scope"] = effective_scope
        threading.Thread(
            target=_nous_poller, args=(sid,), daemon=True, name=f"oauth-poll-{sid[:6]}"
        ).start()
        return {
            "session_id": sid,
            "flow": "device_code",
            "user_code": str(device_data["user_code"]),
            "verification_url": str(device_data["verification_uri_complete"]),
            "expires_in": int(device_data["expires_in"]),
            "poll_interval": int(device_data["interval"]),
        }

    if provider_id == "openai-codex":
        # Codex uses fixed OpenAI device-auth endpoints; reuse the helper.
        sid, _ = _new_oauth_session("openai-codex", "device_code", profile=profile)
        # Use the helper but in a thread because it polls inline.
        # We can't extract just the start step without refactoring auth.py,
        # so we run the full helper in a worker and proxy the user_code +
        # verification_url back via the session dict. The helper prints
        # to stdout — we capture nothing here, just status.
        threading.Thread(
            target=_codex_full_login_worker, args=(sid,), daemon=True,
            name=f"oauth-codex-{sid[:6]}",
        ).start()
        # Block briefly until the worker has populated the user_code, OR error.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with _oauth_sessions_lock:
                s = _oauth_sessions.get(sid)
            if s and (s.get("user_code") or s["status"] != "pending"):
                break
            await asyncio.sleep(0.1)
        with _oauth_sessions_lock:
            s = _oauth_sessions.get(sid, {})
        if s.get("status") == "error":
            raise HTTPException(status_code=500, detail=s.get("error_message") or "device-auth failed")
        if not s.get("user_code"):
            raise HTTPException(status_code=504, detail="device-auth timed out before returning a user code")
        return {
            "session_id": sid,
            "flow": "device_code",
            "user_code": s["user_code"],
            "verification_url": s["verification_url"],
            "expires_in": int(s.get("expires_in") or 900),
            "poll_interval": int(s.get("interval") or 5),
        }

    if provider_id == "minimax-oauth":
        # MiniMax uses a device-code-style flow (verification URI + user
        # code + background poll) with a PKCE extension on top. From the
        # operator's perspective it's identical to Nous's device-code
        # flow; the PKCE bit (verifier + challenge from
        # _minimax_pkce_pair) is a security extension that binds the
        # token exchange to the original session.
        from hermes_cli.auth import (
            _minimax_pkce_pair,
            _minimax_request_user_code,
            MINIMAX_OAUTH_CLIENT_ID,
            MINIMAX_OAUTH_GLOBAL_BASE,
        )
        import httpx
        verifier, challenge, state = _minimax_pkce_pair()
        portal_base_url = (
            os.getenv("MINIMAX_PORTAL_BASE_URL") or MINIMAX_OAUTH_GLOBAL_BASE
        ).rstrip("/")
        def _do_minimax_request():
            with httpx.Client(
                timeout=httpx.Timeout(15.0),
                headers={"Accept": "application/json"},
                follow_redirects=True,
            ) as client:
                return _minimax_request_user_code(
                    client=client,
                    portal_base_url=portal_base_url,
                    client_id=MINIMAX_OAUTH_CLIENT_ID,
                    code_challenge=challenge,
                    state=state,
                )
        device_data = await asyncio.get_event_loop().run_in_executor(
            None, _do_minimax_request
        )
        sid, sess = _new_oauth_session("minimax-oauth", "device_code", profile=profile)
        # The CLI flow names this `interval_ms` because MiniMax's
        # `interval` field is in milliseconds (defensive default 2000ms
        # in _minimax_poll_token).
        interval_raw = device_data.get("interval")
        sess["interval_ms"] = (
            int(interval_raw) if interval_raw is not None else None
        )
        sess["user_code"] = str(device_data["user_code"])
        sess["code_verifier"] = verifier
        sess["state"] = state
        sess["portal_base_url"] = portal_base_url
        sess["client_id"] = MINIMAX_OAUTH_CLIENT_ID
        sess["region"] = "global"
        # `expired_in` from MiniMax is overloaded — could be a unix-ms
        # timestamp OR a seconds-from-now duration. Mirror the heuristic
        # in _minimax_poll_token. Stash the raw value for the poller;
        # compute a derived expires_at + UI-friendly expires_in seconds.
        expired_in_raw = int(device_data["expired_in"])
        sess["expired_in_raw"] = expired_in_raw
        if expired_in_raw > 1_000_000_000_000:  # likely unix-ms
            expires_at_ts = expired_in_raw / 1000.0
            expires_in_seconds = max(0, int(expires_at_ts - time.time()))
        else:
            expires_at_ts = time.time() + expired_in_raw
            expires_in_seconds = expired_in_raw
        sess["expires_at"] = expires_at_ts
        threading.Thread(
            target=_minimax_poller,
            args=(sid,),
            daemon=True,
            name=f"oauth-poll-{sid[:6]}",
        ).start()
        return {
            "session_id": sid,
            "flow": "device_code",
            "user_code": str(device_data["user_code"]),
            "verification_url": str(device_data["verification_uri"]),
            "expires_in": expires_in_seconds,
            "poll_interval": max(2, (sess["interval_ms"] or 2000) // 1000),
        }

    if provider_id == "xai-oauth":
        from hermes_cli.auth import _xai_oauth_request_device_code
        import httpx

        def _do_xai_device_request():
            with httpx.Client(
                timeout=httpx.Timeout(20.0),
                headers={"Accept": "application/json"},
            ) as client:
                return _xai_oauth_request_device_code(client)

        device_data = await asyncio.get_running_loop().run_in_executor(
            None, _do_xai_device_request
        )
        sid, sess = _new_oauth_session("xai-oauth", "device_code", profile=profile)
        sess["device_code"] = str(device_data["device_code"])
        sess["interval"] = int(device_data["interval"])
        sess["expires_at"] = time.time() + int(device_data["expires_in"])
        threading.Thread(
            target=_xai_device_poller,
            args=(sid,),
            daemon=True,
            name=f"oauth-poll-{sid[:6]}",
        ).start()
        return {
            "session_id": sid,
            "flow": "device_code",
            "user_code": str(device_data["user_code"]),
            "verification_url": str(
                device_data.get("verification_uri_complete")
                or device_data["verification_uri"]
            ),
            "expires_in": int(device_data["expires_in"]),
            "poll_interval": int(device_data["interval"]),
        }

    raise HTTPException(status_code=400, detail=f"Provider {provider_id} does not support device-code flow")


def _nous_poller(session_id: str) -> None:
    """Background poller that drives a Nous device-code flow to completion."""
    from hermes_cli.auth import (
        _poll_for_token,
        refresh_nous_oauth_from_state,
    )
    from datetime import datetime, timezone
    import httpx
    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
    if not sess:
        return
    portal_base_url = sess["portal_base_url"]
    client_id = sess["client_id"]
    device_code = sess["device_code"]
    interval = sess["interval"]
    scope = sess.get("scope")
    expires_in = max(60, int(sess["expires_at"] - time.time()))
    try:
        with httpx.Client(timeout=httpx.Timeout(15.0), headers={"Accept": "application/json"}) as client:
            token_data = _poll_for_token(
                client=client,
                portal_base_url=portal_base_url,
                client_id=client_id,
                device_code=device_code,
                expires_in=expires_in,
                poll_interval=interval,
            )
        # Same post-processing as _nous_device_code_login (validate/refresh JWT)
        now = datetime.now(timezone.utc)
        token_ttl = int(token_data.get("expires_in") or 0)
        auth_state = {
            "portal_base_url": portal_base_url,
            "inference_base_url": token_data.get("inference_base_url"),
            "client_id": client_id,
            "scope": token_data.get("scope") or scope,
            "token_type": token_data.get("token_type", "Bearer"),
            "access_token": token_data["access_token"],
            "refresh_token": token_data.get("refresh_token"),
            "obtained_at": now.isoformat(),
            "expires_at": (
                datetime.fromtimestamp(now.timestamp() + token_ttl, tz=timezone.utc).isoformat()
                if token_ttl else None
            ),
            "expires_in": token_ttl,
        }
        with _profile_scope(_oauth_session_profile(session_id)):
            full_state = refresh_nous_oauth_from_state(
                auth_state,
                timeout_seconds=15.0,
                force_refresh=False,
            )
            from hermes_cli.auth import persist_nous_credentials
            persist_nous_credentials(full_state)
        with _oauth_sessions_lock:
            sess["status"] = "approved"
        _log.info("oauth/device: nous login completed (session=%s)", session_id)
    except Exception as e:
        _log.warning("nous device-code poll failed (session=%s): %s", session_id, e)
        with _oauth_sessions_lock:
            sess["status"] = "error"
            sess["error_message"] = str(e)


def _minimax_poller(session_id: str) -> None:
    """Background poller that drives a MiniMax OAuth flow to completion.

    Mirrors `_nous_poller` but calls the MiniMax-specific token endpoint,
    which uses a PKCE-style ``code_verifier`` + ``user_code`` rather than
    the ``device_code`` field used by Nous. On success, builds the same
    auth_state dict that ``_minimax_oauth_login`` (the CLI flow) builds
    and persists via ``_minimax_save_auth_state`` — so the dashboard
    path leaves the system in the same state as
    ``hermes auth add minimax-oauth``.
    """
    from hermes_cli.auth import (
        _minimax_poll_token,
        _minimax_resolve_token_expiry_unix,
        _minimax_save_auth_state,
        MINIMAX_OAUTH_GLOBAL_INFERENCE,
        MINIMAX_OAUTH_SCOPE,
    )
    from datetime import datetime, timezone
    import httpx
    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
    if not sess:
        return
    portal_base_url = sess["portal_base_url"]
    client_id = sess["client_id"]
    user_code = sess["user_code"]
    code_verifier = sess["code_verifier"]
    interval_ms = sess.get("interval_ms")
    expired_in_raw = sess["expired_in_raw"]
    try:
        with httpx.Client(
            timeout=httpx.Timeout(15.0),
            headers={"Accept": "application/json"},
            follow_redirects=True,
        ) as client:
            token_data = _minimax_poll_token(
                client=client,
                portal_base_url=portal_base_url,
                client_id=client_id,
                user_code=user_code,
                code_verifier=code_verifier,
                expired_in=expired_in_raw,
                interval_ms=interval_ms,
            )
        # Build the auth_state dict in the same shape as the CLI flow's
        # `_minimax_oauth_login` so `_minimax_save_auth_state` writes
        # the canonical record. Region is fixed to "global" for the
        # dashboard path; cn-region operators can still use the CLI
        # flow which supports `--region cn`.
        now = datetime.now(timezone.utc)
        expires_at_ts = _minimax_resolve_token_expiry_unix(
            int(token_data["expired_in"]), now=now,
        )
        expires_in_s = max(0, int(expires_at_ts - now.timestamp()))
        auth_state = {
            "provider": "minimax-oauth",
            "region": sess.get("region", "global"),
            "portal_base_url": portal_base_url,
            "inference_base_url": MINIMAX_OAUTH_GLOBAL_INFERENCE,
            "client_id": client_id,
            "scope": MINIMAX_OAUTH_SCOPE,
            "token_type": token_data.get("token_type", "Bearer"),
            "access_token": token_data["access_token"],
            "refresh_token": token_data["refresh_token"],
            "resource_url": token_data.get("resource_url"),
            "obtained_at": now.isoformat(),
            "expires_at": datetime.fromtimestamp(
                expires_at_ts, tz=timezone.utc
            ).isoformat(),
            "expires_in": expires_in_s,
        }
        with _profile_scope(_oauth_session_profile(session_id)):
            _minimax_save_auth_state(auth_state)
        with _oauth_sessions_lock:
            sess["status"] = "approved"
        _log.info("oauth/device: minimax login completed (session=%s)", session_id)
    except Exception as e:
        _log.warning("minimax device-code poll failed (session=%s): %s", session_id, e)
        with _oauth_sessions_lock:
            sess["status"] = "error"
            sess["error_message"] = str(e)


def _xai_device_poller(session_id: str) -> None:
    """Background poller for xAI's OAuth device-code flow."""
    import httpx
    from hermes_cli.auth import (
        _save_xai_oauth_tokens,
        _xai_oauth_discovery,
        _xai_oauth_poll_device_token,
        mark_provider_active_if_unset,
        unsuppress_credential_source,
    )

    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
    if not sess:
        return
    device_code = sess["device_code"]
    interval = int(sess["interval"])
    expires_in = max(60, int(sess["expires_at"] - time.time()))
    try:
        discovery = _xai_oauth_discovery(20.0)
        with httpx.Client(
            timeout=httpx.Timeout(20.0),
            headers={"Accept": "application/json"},
        ) as client:
            token_data = _xai_oauth_poll_device_token(
                client,
                token_endpoint=discovery["token_endpoint"],
                device_code=device_code,
                expires_in=expires_in,
                poll_interval=interval,
            )
        tokens = {
            "access_token": str(token_data.get("access_token", "") or "").strip(),
            "refresh_token": str(token_data.get("refresh_token", "") or "").strip(),
            "id_token": str(token_data.get("id_token", "") or "").strip(),
            "expires_in": token_data.get("expires_in"),
            "token_type": str(token_data.get("token_type") or "Bearer").strip() or "Bearer",
        }
        with _profile_scope(_oauth_session_profile(session_id)):
            _save_xai_oauth_tokens(
                tokens,
                discovery=discovery,
                last_refresh=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                auth_mode="oauth_device_code",
                # Persist credentials without hijacking an existing active
                # chat provider.
                set_active=False,
            )
            # Mirror `hermes auth add xai-oauth`: first credential may become
            # active when none is set yet; never overwrite an existing choice.
            mark_provider_active_if_unset("xai-oauth")
            # The singleton write above is the single source of truth: the
            # credential-pool load seeds it as the canonical ``device_code``
            # entry. Do NOT also insert a parallel ``manual:dashboard_*`` pool
            # entry — that duplicates the single-use refresh token across two
            # entries and triggers rotation churn / ``refresh_token_reused``.
            # An interactive dashboard login is also an explicit re-enable
            # signal, so clear any ``device_code`` suppression left by a
            # prior ``hermes auth remove xai-oauth`` (mirrors auth_add_command
            # and the ``hermes model`` re-login path in _login_xai_oauth).
            unsuppress_credential_source("xai-oauth", "device_code")
        with _oauth_sessions_lock:
            sess["status"] = "approved"
        _log.info("oauth/device: xai login completed (session=%s)", session_id)
    except Exception as e:
        _log.warning("xai device-code poll failed (session=%s): %s", session_id, e)
        with _oauth_sessions_lock:
            sess["status"] = "error"
            sess["error_message"] = str(e)


def _http_response_error_detail(resp: Any) -> str:
    """Best-effort extraction of a short provider error detail."""
    try:
        payload = resp.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            parts = [
                str(error.get(key, "")).strip()
                for key in ("message", "error_description", "code", "type")
                if str(error.get(key, "")).strip()
            ]
            if parts:
                return ": ".join(parts)
        if isinstance(error, str) and error.strip():
            return error.strip()
        for key in ("detail", "message", "error_description"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    text = str(getattr(resp, "text", "") or "").strip()
    return text[:500]


def _codex_device_code_start_error(resp: Any) -> str:
    """Dashboard-facing OpenAI Codex device-code start failure."""
    status = getattr(resp, "status_code", "unknown")
    detail = _http_response_error_detail(resp)
    lower = detail.lower()
    if "device" in lower and ("authori" in lower or "enable" in lower):
        message = (
            "OpenAI rejected the device-code login request. Your OpenAI "
            "account may need device-code authorization enabled before Hermes "
            "can start this dashboard login. Enable device-code authorization "
            "in OpenAI, then return here and click Login again."
        )
    else:
        message = (
            "OpenAI rejected the device-code login request. Please try Login "
            "again from the dashboard after checking your OpenAI account settings."
        )
    if detail:
        return f"{message} (HTTP {status}: {detail})"
    return f"{message} (HTTP {status})"


def _codex_full_login_worker(session_id: str) -> None:
    """Run the complete OpenAI Codex device-code flow.

    Codex doesn't use the standard OAuth device-code endpoints; it has its
    own ``/api/accounts/deviceauth/usercode`` (JSON body, returns
    ``device_auth_id``) and ``/api/accounts/deviceauth/token`` (JSON body
    polled until 200). On success the response carries an
    ``authorization_code`` + ``code_verifier`` that get exchanged at
    CODEX_OAUTH_TOKEN_URL with grant_type=authorization_code.

    The flow is replicated inline (rather than calling
    _codex_device_code_login) because that helper prints/blocks/polls in a
    single function — we need to surface the user_code to the dashboard the
    moment we receive it, well before polling completes.
    """
    try:
        import httpx
        from hermes_cli.auth import (
            CODEX_OAUTH_CLIENT_ID,
            CODEX_OAUTH_TOKEN_URL,
        )
        issuer = "https://auth.openai.com"

        # Step 1: request device code
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            resp = client.post(
                f"{issuer}/api/accounts/deviceauth/usercode",
                json={"client_id": CODEX_OAUTH_CLIENT_ID},
                headers={"Content-Type": "application/json"},
            )
        if resp.status_code != 200:
            raise RuntimeError(_codex_device_code_start_error(resp))
        device_data = resp.json()
        user_code = device_data.get("user_code", "")
        device_auth_id = device_data.get("device_auth_id", "")
        poll_interval = max(3, int(device_data.get("interval", "5")))
        if not user_code or not device_auth_id:
            raise RuntimeError("device-code response missing user_code or device_auth_id")
        verification_url = f"{issuer}/codex/device"
        with _oauth_sessions_lock:
            sess = _oauth_sessions.get(session_id)
            if not sess:
                return
            sess["user_code"] = user_code
            sess["verification_url"] = verification_url
            sess["device_auth_id"] = device_auth_id
            sess["interval"] = poll_interval
            sess["expires_in"] = 15 * 60  # OpenAI's effective limit
            sess["expires_at"] = time.time() + sess["expires_in"]
            # Captured now (not re-derived after cancel pops the session) so a
            # cancelled session can never fall back to the caller's current
            # profile scope at save time.
            session_profile = sess.get("profile")

        # Step 2: poll until authorized
        deadline = time.monotonic() + sess["expires_in"]
        code_resp = None
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            while time.monotonic() < deadline:
                if sess.get("cancelled"):
                    _log.info("oauth/device: openai-codex login cancelled (session=%s)", session_id)
                    return
                time.sleep(poll_interval)
                if sess.get("cancelled"):
                    _log.info("oauth/device: openai-codex login cancelled (session=%s)", session_id)
                    return
                poll = client.post(
                    f"{issuer}/api/accounts/deviceauth/token",
                    json={"device_auth_id": device_auth_id, "user_code": user_code},
                    headers={"Content-Type": "application/json"},
                )
                if poll.status_code == 200:
                    code_resp = poll.json()
                    break
                if poll.status_code in {403, 404}:
                    continue  # user hasn't authorized yet
                raise RuntimeError(f"deviceauth/token poll returned {poll.status_code}")

        if code_resp is None:
            with _oauth_sessions_lock:
                sess["status"] = "expired"
                sess["error_message"] = "Device code expired before approval"
            return

        if sess.get("cancelled"):
            _log.info("oauth/device: openai-codex login cancelled before token exchange (session=%s)", session_id)
            return

        # Step 3: exchange authorization_code for tokens
        authorization_code = code_resp.get("authorization_code", "")
        code_verifier = code_resp.get("code_verifier", "")
        if not authorization_code or not code_verifier:
            raise RuntimeError("device-auth response missing authorization_code/code_verifier")
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            token_resp = client.post(
                CODEX_OAUTH_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": authorization_code,
                    "redirect_uri": f"{issuer}/deviceauth/callback",
                    "client_id": CODEX_OAUTH_CLIENT_ID,
                    "code_verifier": code_verifier,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if token_resp.status_code != 200:
            raise RuntimeError(f"token exchange returned {token_resp.status_code}")
        tokens = token_resp.json()
        access_token = tokens.get("access_token", "")
        refresh_token = tokens.get("refresh_token", "")
        if not access_token:
            raise RuntimeError("token exchange did not return access_token")

        from hermes_cli.auth import _save_codex_tokens

        # The cancellation check and the save must be one atomic critical
        # section under the same lock cancel_oauth_session() uses. Checking
        # "cancelled" and then saving as two separate steps left a window
        # where DELETE could flip the flag between them and the worker would
        # still persist tokens after the user believed the login was
        # aborted. Holding the lock across both closes that window: DELETE
        # either lands before this section (worker observes cancelled and
        # returns) or blocks until this section (and the save) is done.
        with _oauth_sessions_lock:
            if sess.get("cancelled"):
                _log.info("oauth/device: openai-codex login cancelled before token save (session=%s)", session_id)
                return
            with _profile_scope(session_profile):
                _save_codex_tokens({
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                })
            sess["status"] = "approved"
        _log.info("oauth/device: openai-codex login completed (session=%s)", session_id)
    except Exception as e:
        _log.warning("codex device-code worker failed (session=%s): %s", session_id, e)
        with _oauth_sessions_lock:
            s = _oauth_sessions.get(session_id)
            if s:
                s["status"] = "error"
                s["error_message"] = str(e)


@app.post("/api/providers/oauth/{provider_id}/start")
async def start_oauth_login(
    provider_id: str,
    request: Request,
    profile: Optional[str] = None,
):
    """Initiate an OAuth login flow. Token-protected."""
    _require_token(request)
    _gc_oauth_sessions()
    _validate_oauth_profile(profile)
    valid = {p["id"] for p in _OAUTH_PROVIDER_CATALOG}
    if provider_id not in valid:
        raise HTTPException(status_code=400, detail=f"Unknown provider {provider_id}")
    catalog_entry = next(p for p in _OAUTH_PROVIDER_CATALOG if p["id"] == provider_id)
    if catalog_entry["flow"] == "external":
        raise HTTPException(
            status_code=400,
            detail=f"{provider_id} uses an external CLI; run `{catalog_entry['cli_command']}` manually",
        )
    try:
        # The pkce branch is gated on provider_id == "anthropic" because
        # `_start_anthropic_pkce()` is hardcoded to the Anthropic flow.
        # Routing any other future pkce-flagged provider through it would
        # silently launch the Anthropic OAuth flow (the bug fixed in this
        # change for MiniMax). New PKCE providers must add their own
        # start function and an explicit branch here.
        if catalog_entry["flow"] == "pkce" and provider_id == "anthropic":
            return _start_anthropic_pkce(profile=profile)
        if catalog_entry["flow"] == "device_code":
            return await _start_device_code_flow(provider_id, profile=profile)
    except HTTPException:
        raise
    except Exception as e:
        _log.exception("oauth/start %s failed", provider_id)
        raise HTTPException(status_code=500, detail=str(e))
    raise HTTPException(status_code=400, detail="Unsupported flow")


@app.post("/api/providers/oauth/{provider_id}/submit")
async def submit_oauth_code(
    provider_id: str,
    body: OAuthSubmitBody,
    request: Request,
    profile: Optional[str] = None,
):
    """Submit the auth code for PKCE flows. Token-protected."""
    _require_token(request)
    if provider_id == "anthropic":
        return await asyncio.get_running_loop().run_in_executor(
            None, _submit_anthropic_pkce, body.session_id, body.code, profile,
        )
    raise HTTPException(status_code=400, detail=f"submit not supported for {provider_id}")


@app.get("/api/providers/oauth/{provider_id}/poll/{session_id}")
async def poll_oauth_session(
    provider_id: str,
    session_id: str,
    profile: Optional[str] = None,
):
    """Poll a session's status (no auth — read-only state).

    Shared by the device-code flows (Nous, OpenAI Codex, MiniMax, xAI).
    Each surfaces progress through the same background-worker-updated
    ``status`` field, so a single poll endpoint serves them all.
    """
    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    if sess["provider"] != provider_id:
        raise HTTPException(status_code=400, detail="Provider mismatch for session")
    return {
        "session_id": session_id,
        "status": sess["status"],
        "error_message": sess.get("error_message"),
        "expires_at": sess.get("expires_at"),
    }


@app.delete("/api/providers/oauth/sessions/{session_id}")
async def cancel_oauth_session(
    session_id: str,
    request: Request,
    profile: Optional[str] = None,
):
    """Cancel a pending OAuth session. Token-protected.

    Marks the session dict ``cancelled`` before popping it so any
    background worker still holding a reference to that same dict (e.g.
    the Codex device-code poller) observes the cancellation and stops
    polling/exchanging/saving instead of completing the login after the
    user believed it was aborted.
    """
    _require_token(request)
    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
        if sess is not None:
            sess["cancelled"] = True
        _oauth_sessions.pop(session_id, None)
    if sess is None:
        return {"ok": False, "message": "session not found"}
    return {"ok": True, "session_id": session_id}


# ---------------------------------------------------------------------------
# Session detail endpoints
# ---------------------------------------------------------------------------



def _session_latest_descendant(session_id: str, db):
    """Resolve a session id to the newest child leaf session.

    /model may create child sessions. Dashboard refresh should continue the
    newest child instead of reopening the old parent.
    """
    def row_get(row, key, index):
        if isinstance(row, dict):
            return row.get(key)
        try:
            return row[key]
        except Exception:
            try:
                return row[index]
            except Exception:
                return None

    sid = db.resolve_session_id(session_id)
    if not sid or not db.get_session(sid):
        return None, []

    conn = (
        getattr(db, "conn", None)
        or getattr(db, "_conn", None)
        or getattr(db, "connection", None)
        or getattr(db, "_connection", None)
    )

    rows = []
    if conn is not None:
        raw_rows = conn.execute(
            """
            WITH RECURSIVE descendants(id, parent_session_id, started_at) AS (
                SELECT id, parent_session_id, started_at FROM sessions WHERE id = ?
                UNION
                SELECT s.id, s.parent_session_id, s.started_at
                FROM sessions s
                JOIN descendants d ON s.parent_session_id = d.id
            )
            SELECT id, parent_session_id, started_at FROM descendants
            """,
            (sid,),
        ).fetchall()
        for row in raw_rows:
            rows.append({
                "id": row_get(row, "id", 0),
                "parent_session_id": row_get(row, "parent_session_id", 1),
                "started_at": row_get(row, "started_at", 2),
            })
    else:
        rows = db.list_sessions_rich(limit=10000, offset=0, compact_rows=True)

    children = {}
    for row in rows:
        rid = row.get("id")
        parent = row.get("parent_session_id")
        if rid and parent:
            children.setdefault(parent, []).append(row)

    def started(row):
        try:
            return float(row.get("started_at") or 0)
        except Exception:
            return 0.0

    current = sid
    path = [sid]
    seen = {sid}

    while children.get(current):
        candidates = [r for r in children[current] if r.get("id") not in seen]
        if not candidates:
            break
        candidates.sort(key=started, reverse=True)
        current = candidates[0]["id"]
        path.append(current)
        seen.add(current)

    return current, path


# CRITICAL — every literal-path route below MUST be declared BEFORE the
# templated ``/api/sessions/{session_id}`` family that follows. FastAPI/
# Starlette match routes in registration order, and the ``{session_id}``
# pattern is unconstrained — it would otherwise swallow e.g.
# ``DELETE /api/sessions/empty``, ``POST /api/sessions/bulk-delete``, or
# ``GET /api/sessions/stats`` as "operate on the session with id
# 'empty'" / "'bulk-delete'" / "'stats'", which would 404 (or worse,
# succeed and delete the wrong row). Same story as the older
# ``/api/sessions/search`` endpoint up at line ~1191. If you split or
# reorder this block, move every route in it together.
# Keep the dashboard import endpoint stream-safe: FastAPI otherwise parses and
# buffers an arbitrarily large JSON body before SessionDB can enforce its own
# per-session and transaction-work limits.
_SESSION_IMPORT_MAX_BYTES = 25 * 1024 * 1024


async def _read_session_import_body(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _SESSION_IMPORT_MAX_BYTES:
            raise HTTPException(status_code=413, detail="Session import payload is too large")
        body.extend(chunk)
    return bytes(body)


def _import_sessions_for_profile(profile: Optional[str], sessions: List[Dict[str, Any]]) -> Dict[str, Any]:
    db = _open_session_db_for_profile(profile, read_only=False)
    try:
        return db.import_sessions(sessions)
    finally:
        db.close()


app.include_router(_sessions_routes.manage_router)
from hermes_cli.web_routers.sessions import (  # noqa: E402,F401 — legacy re-exports; tests call these via web_server.<name>
    bulk_delete_sessions_endpoint,
    import_sessions_endpoint,
    count_empty_sessions_endpoint,
    delete_empty_sessions_endpoint,
    get_session_stats,
    get_session_detail,
    get_session_latest_descendant,
    get_session_messages,
    delete_session_endpoint,
    rename_session_endpoint,
    export_session_endpoint,
    prune_sessions_endpoint,
)










# Serialises the one-time writable schema bootstrap for read-only opens.
# Concurrent first-load polls otherwise race sqlite file creation: the losers
# open mode=ro against a store whose schema is still being written and every
# query raises "no such table: sessions".
_session_db_bootstrap_lock = threading.Lock()


def _session_db_read_probe_statements() -> tuple:
    """Stale-schema probes for read-only opens, derived from SCHEMA_SQL.

    Read-only opens skip _reconcile_columns(), so an older store would
    otherwise 500 on every poll until something opened it writable. Derived
    from the same schema the writable reconciler applies, so any column
    added there is probed here automatically — the previous hand-written
    probe listed four columns and went stale the first time a new column
    (sessions.last_activity_at) shipped, leaving the desktop sidebar empty
    after `hermes update` until the first message forced a writable open.
    """
    from hermes_state_schema import schema_read_probe_statements

    return schema_read_probe_statements()


# Stores where a heal WRITABLE OPEN SUCCEEDED and the read probe still
# failed afterwards: the schema problem is one reconciliation cannot fix
# (e.g. a NOT-NULL-without-default column SQLite refuses to ADD). Retrying
# the full writable init on every poll would hammer a live DB for nothing,
# so such stores fall back to the raw read-only open until restart. A
# FAILED writable open (transient lock) is deliberately NOT recorded —
# the next poll retries the heal.
_session_db_heal_exhausted: set = set()

# Deduplicates the heal-failure warning per store per process, so a
# persistent problem is loud once instead of once per sidebar poll.
_session_db_heal_warned: set = set()


def _open_session_db_at_path(db_path: Path, *, read_only: bool):
    """Open a SessionDB at an explicit path with an explicit access mode.

    Writable opens keep the full init and repair path. Read-only opens
    bootstrap a missing or zero-byte store once, and heal an older or
    malformed schema through one writable open before reopening read-only.
    The healthy read path never takes a write lock or requests a checkpoint.

    Scope of the heal: the probe checks every table/column declared in
    SCHEMA_SQL (see ``schema_read_probe_statements``), so ANY schema
    addition escalates a stale store to a one-time writable open — the same
    reconcile the store's own backend runs at startup. Tables created
    outside SCHEMA_SQL (telemetry ``tel_*``, FTS shadow tables) are
    deliberately outside both the probe and the heal.
    """
    import sqlite3

    from hermes_state import SessionDB, is_malformed_schema_error

    if not read_only:
        return SessionDB(db_path=db_path, read_only=False)

    def _needs_bootstrap() -> bool:
        try:
            return db_path.stat().st_size == 0
        except FileNotFoundError:
            return True
        except OSError:
            return False

    if _needs_bootstrap():
        with _session_db_bootstrap_lock:
            if _needs_bootstrap():
                SessionDB(db_path=db_path, read_only=False).close()

    def _open_probed():
        db = SessionDB(db_path=db_path, read_only=True)
        # Unit-test fakes may replace SessionDB without exposing a raw
        # connection. Probe only real connections.
        conn = getattr(db, "_conn", None)
        if conn is not None and str(db_path) not in _session_db_heal_exhausted:
            try:
                for statement in _session_db_read_probe_statements():
                    conn.execute(statement).fetchone()
            except BaseException:
                db.close()
                raise
        return db

    try:
        return _open_probed()
    except sqlite3.DatabaseError as exc:
        message = str(exc).lower()
        stale_schema = "no such table" in message or "no such column" in message
        if not stale_schema and not is_malformed_schema_error(exc):
            raise
        SessionDB(db_path=db_path, read_only=False).close()
        try:
            return _open_probed()
        except sqlite3.DatabaseError as still_stale:
            message = str(still_stale).lower()
            if "no such table" not in message and "no such column" not in message:
                raise
            # The writable open succeeded but the store is STILL behind the
            # probe: reconciliation cannot fix this one. Serve reads without
            # the probe (queries touching the broken part will still fail,
            # everything else works) and stop paying the writable init per
            # poll.
            _session_db_heal_exhausted.add(str(db_path))
            if str(db_path) not in _session_db_heal_warned:
                _session_db_heal_warned.add(str(db_path))
                _log.warning(
                    "state.db at %s is missing schema that a writable "
                    "reconcile could not add (%s); read paths may partially "
                    "fail until the store is repaired",
                    db_path,
                    still_stale,
                )
            return _open_probed()


def _open_session_db_for_profile(profile: Optional[str], *, read_only: bool):
    """Open a SessionDB with an explicit access mode for a profile.

    ``profile`` None/empty selects this process's own ``state.db``. A named
    profile opens that profile's on-disk store directly. Access-mode
    semantics are documented on :func:`_open_session_db_at_path`.
    """
    from hermes_state import _default_db_path

    if profile:
        _name, home = _cron_profile_home(profile)
        db_path = Path(home) / "state.db"
    else:
        db_path = Path(_default_db_path())
    return _open_session_db_at_path(db_path, read_only=read_only)


# In-process throttle for the opportunistic auto-archive trigger, keyed by
# profile. Bounds the config.yaml read to at most once per this window per
# profile; the actual sweep is throttled far more coarsely by state_meta
# (sessions.min_interval_hours) inside maybe_auto_archive.
_AUTO_ARCHIVE_CHECK_INTERVAL_S = 300.0
_last_auto_archive_check: Dict[str, float] = {}


def _maybe_auto_archive_for_profile(profile: Optional[str]) -> None:
    """Run the config-gated stale-session auto-archive for ``profile``.

    The Desktop backend is spawned as ``hermes serve`` — it runs neither the
    interactive CLI nor the messaging gateway, so neither of those startup
    hooks fire for Desktop users. Triggering the (double-throttled, config-off
    by default) sweep from the session-list path is what makes
    ``sessions.auto_archive`` take effect there. Never raises.
    """
    try:
        key = profile or ""
        now = time.monotonic()
        last = _last_auto_archive_check.get(key)
        if last is not None and now - last < _AUTO_ARCHIVE_CHECK_INTERVAL_S:
            return
        _last_auto_archive_check[key] = now

        from hermes_cli.config import load_config as _load_full_config
        cfg = (_load_full_config().get("sessions") or {})
        if not cfg.get("auto_archive", False):
            return
        db = _open_session_db_for_profile(profile, read_only=False)
        try:
            db.maybe_auto_archive(
                idle_days=float(cfg.get("auto_archive_days", 3)),
                min_interval_hours=int(cfg.get("min_interval_hours", 24)),
            )
        finally:
            db.close()
    except Exception as exc:
        _log.debug("opportunistic auto-archive skipped: %s", exc)


async def _auto_archive_ticker_loop(
    interval_s: float = 3600.0, initial_delay_s: float = 90.0
) -> None:
    """Live timer for the stale-session auto-archive (primary profile).

    A long-running Desktop/serve backend must keep sweeping on schedule even
    when no ``/api/sessions`` request arrives to fire the opportunistic
    trigger — e.g. the app sits open for days on an idle chat. The real
    cadence is still owned by state_meta (``sessions.min_interval_hours``)
    inside ``maybe_auto_archive``; this loop is only the poll rate.
    """

    def _sweep() -> None:
        _maybe_auto_archive_for_profile(None)

    await asyncio.sleep(initial_delay_s)
    while True:
        try:
            await asyncio.to_thread(_sweep)
        except Exception as exc:
            _log.debug("auto-archive tick skipped: %s", exc)
        await asyncio.sleep(interval_s)














def _prune_sessions(body: SessionPrune):
    """Delete ended sessions matching filters (mirrors `hermes sessions prune`)."""
    has_window = (
        body.started_before is not None or body.started_after is not None
    )
    if body.older_than_days is not None and body.older_than_days < 1 and not has_window:
        raise HTTPException(status_code=400, detail="older_than_days must be >= 1")
    # Mirror the CLI: the implicit 90-day cutoff only applies to a truly bare
    # prune. Any attribute filter (source, title, model, ...) suppresses it
    # unless the caller explicitly sent older_than_days.
    _attr_filters_set = any(
        getattr(body, f) is not None
        for f in (
            "source", "title_like", "end_reason", "cwd_prefix",
            "min_messages", "max_messages", "model_like", "provider",
            "user_id", "chat_id", "chat_type", "branch_like",
            "min_tokens", "max_tokens", "min_cost", "max_cost",
            "min_tool_calls", "max_tool_calls",
        )
    )
    _older_than_explicit = "older_than_days" in body.model_fields_set
    _effective_older_than = body.older_than_days
    if has_window or (_attr_filters_set and not _older_than_explicit):
        _effective_older_than = None
    profile_home = _cron_profile_home(body.profile)[1] if body.profile else get_hermes_home()
    db = _open_session_db_for_profile(body.profile, read_only=False)
    try:
        filters = dict(
            older_than_days=_effective_older_than,
            source=(body.source or None),
            started_before=body.started_before,
            started_after=body.started_after,
            title_like=(body.title_like or None),
            end_reason=(body.end_reason or None),
            cwd_prefix=(body.cwd_prefix or None),
            min_messages=body.min_messages,
            max_messages=body.max_messages,
            model_like=(body.model_like or None),
            provider=(body.provider or None),
            user_id=(body.user_id or None),
            chat_id=(body.chat_id or None),
            chat_type=(body.chat_type or None),
            branch_like=(body.branch_like or None),
            min_tokens=body.min_tokens,
            max_tokens=body.max_tokens,
            min_cost=body.min_cost,
            max_cost=body.max_cost,
            min_tool_calls=body.min_tool_calls,
            max_tool_calls=body.max_tool_calls,
            archived=None if body.include_archived else False,
        )
        skipped_open = db.count_open_prune_matches(**filters)
        if body.dry_run:
            rows = db.list_prune_candidates(**filters)
            return {
                "ok": True,
                "removed": 0,
                "matched": len(rows),
                "skipped_open": skipped_open,
                # Rows are ordered by last activity, not creation time.
                "oldest_last_active": rows[0]["last_active"] if rows else None,
                "newest_last_active": rows[-1]["last_active"] if rows else None,
                "oldest_started_at": (
                    min(r["started_at"] for r in rows) if rows else None
                ),
                "newest_started_at": (
                    max(r["started_at"] for r in rows) if rows else None
                ),
                "sessions": [
                    {
                        "id": r["id"],
                        "source": r["source"],
                        "title": r.get("title"),
                        "model": r.get("model"),
                        "started_at": r["started_at"],
                        "last_active": r["last_active"],
                        "message_count": r["message_count"],
                    }
                    for r in rows
                ],
            }
        sessions_dir = profile_home / "sessions"
        removed = db.prune_sessions(
            sessions_dir=sessions_dir if sessions_dir.exists() else None,
            **filters,
        )
        return {"ok": True, "removed": removed, "skipped_open": skipped_open}
    finally:
        db.close()




# ---------------------------------------------------------------------------
# Log viewer endpoint
# ---------------------------------------------------------------------------


@app.get("/api/logs")
async def get_logs(
    file: str = "agent",
    lines: int = 100,
    level: Optional[str] = None,
    component: Optional[str] = None,
    search: Optional[str] = None,
):
    from hermes_cli.logs import _read_tail, LOG_FILES

    log_name = LOG_FILES.get(file)
    if not log_name:
        raise HTTPException(status_code=400, detail=f"Unknown log file: {file}")
    log_path = get_hermes_home() / "logs" / log_name
    if not log_path.exists():
        return {"file": file, "lines": []}

    try:
        from hermes_logging import COMPONENT_PREFIXES
    except ImportError:
        COMPONENT_PREFIXES = {}

    # Normalize "ALL" / "all" / empty → no filter. _matches_filters treats an
    # empty tuple as "must match a prefix" (startswith(()) is always False),
    # so passing () instead of None silently drops every line.
    min_level = level if level and level.upper() != "ALL" else None
    if component and component.lower() != "all":
        comp_prefixes = COMPONENT_PREFIXES.get(component)
        if comp_prefixes is None:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown component: {component}. "
                       f"Available: {', '.join(sorted(COMPONENT_PREFIXES))}",
            )
    else:
        comp_prefixes = None

    has_filters = bool(min_level or comp_prefixes or search)
    result = _read_tail(
        log_path, min(lines, 500) if not search else 2000,
        has_filters=has_filters,
        min_level=min_level,
        component_prefixes=comp_prefixes,
    )
    # Post-filter by search term (case-insensitive substring match).
    # _read_tail doesn't support free-text search, so we filter here and
    # trim to the requested line count afterward.
    if search:
        needle = search.lower()
        result = [l for l in result if needle in l.lower()][-min(lines, 500):]
    return {"file": file, "lines": result}


# ---------------------------------------------------------------------------
# Cron job management endpoints
# ---------------------------------------------------------------------------


def _cron_optional_text(value: Any, *, strip_trailing_slash: bool = False) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if strip_trailing_slash:
        text = text.rstrip("/")
    return text or None


def _cron_string_list(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if isinstance(value, str):
        raw_items = re.split(r"[\n,]", value)
    elif isinstance(value, (list, tuple)):
        raw_items = value
    else:
        return None
    items = [str(item).strip() for item in raw_items if str(item).strip()]
    return items or None


def _normalize_dashboard_cron_script(value: Any, profile_home: Path) -> Optional[str]:
    """Validate a dashboard-selected cron script against the profile sandbox."""
    text = _cron_optional_text(value)
    if not text:
        return None

    scripts_root = (profile_home / "scripts").resolve()
    raw_path = Path(text).expanduser()
    candidate = raw_path.resolve() if raw_path.is_absolute() else (scripts_root / raw_path).resolve()
    try:
        relative = candidate.relative_to(scripts_root)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"script must be inside {scripts_root}",
        ) from exc
    if not candidate.exists():
        raise HTTPException(status_code=400, detail=f"script does not exist: {candidate}")
    if not candidate.is_file():
        raise HTTPException(status_code=400, detail=f"script is not a file: {candidate}")
    return str(relative)


def _validate_dashboard_cron_effective_job(job: Dict[str, Any]) -> None:
    prompt = _cron_optional_text(job.get("prompt"))
    script = _cron_optional_text(job.get("script"))
    skills = _cron_string_list(job.get("skills")) or _cron_string_list(job.get("skill"))
    no_agent = bool(job.get("no_agent"))

    if no_agent:
        if not script:
            raise HTTPException(
                status_code=400,
                detail="no_agent=True requires a script",
            )
        return

    if not (prompt or skills or script):
        raise HTTPException(
            status_code=400,
            detail="agent cron jobs require a prompt, skill, or script",
        )


def _normalize_dashboard_cron_updates(
    updates: Dict[str, Any],
    profile_home: Path,
) -> Dict[str, Any]:
    """Normalize dashboard JSON into cron.jobs.update_job's storage shape.

    This intentionally stays in the dashboard adapter layer: cron/jobs.py is the
    source of truth for scheduling behaviour; the dashboard only translates form
    payloads into the shapes that existing core functions already accept.
    """
    normalized = dict(updates or {})

    for key in ("model", "provider", "workdir"):
        if key in normalized:
            normalized[key] = _cron_optional_text(normalized[key])
    if "script" in normalized:
        normalized["script"] = _normalize_dashboard_cron_script(
            normalized["script"],
            profile_home,
        )
    if "base_url" in normalized:
        normalized["base_url"] = _cron_optional_text(
            normalized["base_url"], strip_trailing_slash=True
        )
    if "deliver" in normalized:
        normalized["deliver"] = _cron_optional_text(normalized["deliver"]) or "local"
    if "context_from" in normalized:
        normalized["context_from"] = _cron_string_list(normalized["context_from"])
    if "enabled_toolsets" in normalized:
        normalized["enabled_toolsets"] = _cron_string_list(normalized["enabled_toolsets"])
    return normalized


def _validate_dashboard_cron_context_from(
    refs: Optional[List[str]],
    profile_name: str,
) -> None:
    if not refs:
        return
    for ref in refs:
        # "self" (the continuity toggle) resolves to the job's own id at run
        # time — it can't be validated against the store (create precedes the
        # job's existence).
        if isinstance(ref, str) and ref.strip().lower() == "self":
            continue
        if not _call_cron_for_profile(profile_name, "get_job", ref):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"context_from job '{ref}' not found in profile "
                    f"'{profile_name}'"
                ),
            )


def _cron_profile_dicts() -> List[Dict[str, Any]]:
    """Return the minimal profile records needed by cron aggregation.

    The two callers only consume ``name``.  ``list_profiles()`` also parses
    config/distribution metadata, probes gateway processes, and counts skills
    for every profile; polling cron jobs through that path creates avoidable
    GIL pressure on large profile pools.
    """
    from hermes_cli import profiles as profiles_mod
    try:
        return [
            {
                "name": name,
                "path": str(home),
                "is_default": name == "default",
            }
            for name, home in profiles_mod.profiles_to_serve(multiplex=True)
        ]
    except Exception:
        _log.exception("Failed to list profiles for cron dashboard; falling back to directory scan")
        return _fallback_profile_dicts(profiles_mod)


def _cron_default_profile() -> str:
    """Profile to target when a cron request carries no explicit ``profile``.

    A desktop pool backend runs one process per profile (HERMES_HOME already
    scoped), but these cron endpoints deliberately route storage through the
    profiles tree via ``_cron_profile_home`` — so a hardcoded ``"default"``
    fallback would write a non-default profile's job into ``~/.hermes``.
    Resolve the process's own profile instead. ``custom`` (an unrecognized
    HERMES_HOME outside the profiles tree) has no profile-dir equivalent, so
    it keeps the legacy ``default`` fallback.
    """
    try:
        from hermes_cli.profiles import get_active_profile_name

        name = get_active_profile_name()
    except Exception:
        return "default"
    return "default" if name in ("default", "custom") else name


def _cron_profile_home(profile: Optional[str]) -> Tuple[str, Path]:
    """Resolve a profile query value to (profile_name, HERMES_HOME)."""
    from hermes_cli import profiles as profiles_mod

    raw = (profile or _cron_default_profile()).strip() or "default"
    try:
        canon = profiles_mod.normalize_profile_name(raw)
        profiles_mod.validate_profile_name(canon)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not profiles_mod.profile_exists(canon):
        raise HTTPException(status_code=404, detail=f"Profile '{canon}' does not exist.")
    return canon, profiles_mod.get_profile_dir(canon)


def _annotate_cron_job(job: Dict[str, Any], profile: str, home: Path) -> Dict[str, Any]:
    annotated = dict(job)
    annotated["profile"] = profile
    annotated["profile_name"] = profile
    annotated["hermes_home"] = str(home)
    annotated["is_default_profile"] = profile == "default"
    return annotated


def _call_cron_for_profile(target_profile: Optional[str], func_name: str, *args, **kwargs):
    """Run cron.jobs helpers against the selected profile's cron directory.

    The dashboard is a single process that can inspect many profiles. Route
    storage through cron.jobs' execution-context override so dashboard calls
    cannot retarget a concurrent desktop ticker's load/save transaction.
    """
    profile_name, home = _cron_profile_home(target_profile)
    from cron import jobs as cron_jobs
    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    token = set_hermes_home_override(str(home))
    try:
        with cron_jobs.use_cron_store(home):
            if func_name == "create_job":
                from cron.scheduler import create_job_with_scheduler_registration

                result = create_job_with_scheduler_registration(*args, **kwargs)
            else:
                result = getattr(cron_jobs, func_name)(*args, **kwargs)
    finally:
        reset_hermes_home_override(token)

    if isinstance(result, list):
        return [_annotate_cron_job(j, profile_name, home) for j in result]
    if isinstance(result, dict):
        return _annotate_cron_job(result, profile_name, home)
    return result


def _notify_cron_provider_for_profile(target_profile: Optional[str]) -> None:
    """Best-effort provider reconcile against one profile's job store.

    Fail-closed for external providers on a multi-profile dashboard: an
    external provider's ``reconcile`` converges its REMOTE registry toward
    one profile's jobs.json, and its orphan cleanup cancels every remote
    entry absent from that store. The NAS registry is not profile-scoped,
    so reconciling profile B would silently disarm profile A's one-shots.
    Until the provider contract carries a profile identity through
    arm/cancel/list, a multi-profile dashboard must not drive unscoped
    external reconciles at all — the affected profile simply re-arms on
    its next fire/start (idempotent via dedup_key). The built-in provider
    re-reads jobs.json each tick and stays a no-op here.
    """
    try:
        _profile_name, home = _cron_profile_home(target_profile)
        from cron import jobs as cron_jobs
        from cron.scheduler_provider import (
            InProcessCronScheduler,
            resolve_cron_scheduler,
        )
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )

        token = set_hermes_home_override(str(home))
        try:
            with cron_jobs.use_cron_store(home):
                provider = resolve_cron_scheduler()
                if not isinstance(provider, InProcessCronScheduler):
                    profile_names = [
                        str(p.get("name") or "")
                        for p in _cron_profile_dicts()
                    ]
                    if len([n for n in profile_names if n]) > 1:
                        _log.warning(
                            "Skipping cron provider reconcile for profile %s: "
                            "external provider '%s' reconcile is not "
                            "profile-scoped and would disarm other profiles' "
                            "armed one-shots. The mutated profile re-arms "
                            "idempotently on its next fire/start.",
                            target_profile,
                            provider.name,
                        )
                        return
                provider.on_jobs_changed()
        finally:
            reset_hermes_home_override(token)
    except Exception:
        _log.debug(
            "Cron provider reconciliation failed for profile %s",
            target_profile,
            exc_info=True,
        )


def _mutate_cron_for_profile(
    target_profile: Optional[str], func_name: str, *args, **kwargs
):
    """Apply a cron store mutation and reconcile its scheduler provider."""
    result = _call_cron_for_profile(target_profile, func_name, *args, **kwargs)
    if result:
        _notify_cron_provider_for_profile(target_profile)
    return result


def _find_cron_job_profile(job_id: str) -> Optional[str]:
    for profile in _cron_profile_dicts():
        name = str(profile.get("name") or "")
        if not name:
            continue
        jobs = _call_cron_for_profile(name, "list_jobs", True)
        if any(j.get("id") == job_id or j.get("name") == job_id for j in jobs):
            return name
    return None


def _list_cron_jobs_sync(profile: str = "all"):
    requested = (profile or "all").strip()
    if requested.lower() != "all":
        return _call_cron_for_profile(requested, "list_jobs", True)

    jobs: List[Dict[str, Any]] = []
    for item in _cron_profile_dicts():
        name = str(item.get("name") or "")
        if not name:
            continue
        try:
            jobs.extend(_call_cron_for_profile(name, "list_jobs", True))
        except Exception:
            _log.exception("Failed to list cron jobs for profile %s", name)
    return jobs


async def _run_cron_dashboard_io(func, *args, **kwargs):
    """Run cron dashboard profile/job I/O outside the FastAPI event loop."""
    if inspect.iscoroutinefunction(func):
        raise TypeError("_run_cron_dashboard_io only accepts sync callables")
    result = await run_in_threadpool(func, *args, **kwargs)
    if inspect.isawaitable(result):
        raise TypeError("_run_cron_dashboard_io sync callable returned an awaitable")
    return result


def _raise_if_cron_registration_error(e: Exception) -> None:
    """Re-raise a cron partial-failure (job saved, external scheduler
    registration failed) as HTTP 424 with the structured envelope.

    Shared by every dashboard cron-create surface so the contract can't
    drift between copies. The lazy import keeps cron out of module import.
    """
    from cron.scheduler import CronSchedulerRegistrationError

    if isinstance(e, CronSchedulerRegistrationError):
        raise HTTPException(status_code=424, detail=e.to_dict()) from e


from hermes_cli.web_routers import cron as _cron_routes  # noqa: E402

app.include_router(_cron_routes.router)
from hermes_cli.web_routers.cron import (  # noqa: E402,F401 — legacy re-exports; tests call these via web_server.<name>
    list_cron_jobs,
    get_cron_job,
    list_cron_job_runs,
    create_cron_job,
    get_cron_delivery_targets,
    update_cron_job,
    pause_cron_job,
    resume_cron_job,
    trigger_cron_job,
    delete_cron_job,
    cron_fire_webhook,
    list_cron_blueprints,
    instantiate_blueprint,
)


def _get_cron_job_sync(job_id: str, profile: Optional[str] = None):
    selected = profile or _find_cron_job_profile(job_id)
    if not selected:
        raise HTTPException(status_code=404, detail="Job not found")
    job = _call_cron_for_profile(selected, "get_job", job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job




def _list_cron_job_runs_sync(job_id: str, profile: Optional[str] = None, limit: int = 20):
    """Run sessions produced by a cron job, newest first.

    Cron runs are stored as ordinary sessions whose id is
    ``cron_{job_id}_{timestamp}`` (see cron/scheduler.run_job). A job's history
    is therefore every session whose id carries that prefix; ``source='cron'``
    narrows it and the id prefix binds it to this job. Powers the run-history
    list under each job in the desktop cron detail. Same row shape as
    ``/api/sessions`` so the frontend can reuse SessionInfo.

    Backed by ``SessionDB.list_cron_job_runs`` — a bounded ``[prefix, hi)``
    id-range scan, not the compression-chain CTE used for the recents list,
    so the cost scales with the requested window and not the (unbounded) total
    cron history.
    """
    selected = profile or _find_cron_job_profile(job_id)
    # job_id may be a human name; resolve to the canonical id used in run-session ids.
    canonical = job_id
    if selected:
        job = _call_cron_for_profile(selected, "get_job", job_id)
        if job and job.get("id"):
            canonical = str(job["id"])

    try:
        limit_n = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit_n = 20

    db = _open_session_db_for_profile(selected, read_only=True)
    try:
        runs = db.list_cron_job_runs(canonical, limit=limit_n, offset=0)
        now = time.time()
        for s in runs:
            s["is_active"] = (
                s.get("ended_at") is None
                and (now - s.get("last_active", s.get("started_at", 0))) < 300
            )
            s["archived"] = bool(s.get("archived"))
            if selected:
                s["profile"] = selected
        return {"runs": runs, "limit": limit_n}
    finally:
        db.close()




def _create_cron_job_sync(body: CronJobCreate, profile: Optional[str] = None):
    try:
        profile_name, profile_home = _cron_profile_home(profile)
        script = _normalize_dashboard_cron_script(body.script, profile_home)
        skills = _cron_string_list(body.skills)
        context_from = _cron_string_list(body.context_from)
        _validate_dashboard_cron_context_from(context_from, profile_name)
        no_agent = bool(body.no_agent)
        _validate_dashboard_cron_effective_job({
            "prompt": body.prompt,
            "skills": skills,
            "script": script,
            "no_agent": no_agent,
        })
        return _mutate_cron_for_profile(
            profile_name,
            "create_job",
            prompt=body.prompt or "",
            schedule=body.schedule,
            name=body.name,
            deliver=_cron_optional_text(body.deliver) or "local",
            skills=skills,
            model=_cron_optional_text(body.model),
            provider=_cron_optional_text(body.provider),
            base_url=_cron_optional_text(body.base_url, strip_trailing_slash=True),
            script=script,
            context_from=context_from,
            enabled_toolsets=_cron_string_list(body.enabled_toolsets),
            workdir=_cron_optional_text(body.workdir),
            no_agent=no_agent,
        )
    except HTTPException:
        raise
    except Exception as e:
        _raise_if_cron_registration_error(e)
        _log.exception("POST /api/cron/jobs failed")
        raise HTTPException(status_code=400, detail=str(e))






def _update_cron_job_sync(job_id: str, body: CronJobUpdate, profile: Optional[str] = None):
    selected = profile or _find_cron_job_profile(job_id)
    if not selected:
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        profile_name, profile_home = _cron_profile_home(selected)
        existing = _call_cron_for_profile(profile_name, "get_job", job_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Job not found")
        updates = _normalize_dashboard_cron_updates(
            body.updates,
            profile_home,
        )
        if "context_from" in updates:
            _validate_dashboard_cron_context_from(
                updates.get("context_from"),
                profile_name,
            )
        execution_fields = {"prompt", "skill", "skills", "script", "no_agent"}
        if execution_fields.intersection(updates):
            effective = {**existing, **updates}
            if "skills" in updates and "skill" not in updates:
                effective["skill"] = None
            _validate_dashboard_cron_effective_job(effective)
        job = _mutate_cron_for_profile(profile_name, "update_job", job_id, updates)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job




def _pause_cron_job_sync(job_id: str, profile: Optional[str] = None):
    selected = profile or _find_cron_job_profile(job_id)
    if not selected:
        raise HTTPException(status_code=404, detail="Job not found")
    job = _mutate_cron_for_profile(selected, "pause_job", job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job




def _resume_cron_job_sync(job_id: str, profile: Optional[str] = None):
    selected = profile or _find_cron_job_profile(job_id)
    if not selected:
        raise HTTPException(status_code=404, detail="Job not found")
    job = _mutate_cron_for_profile(selected, "resume_job", job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job




def _trigger_cron_job_sync(job_id: str, profile: Optional[str] = None):
    selected = profile or _find_cron_job_profile(job_id)
    if not selected:
        raise HTTPException(status_code=404, detail="Job not found")
    job = _call_cron_for_profile(selected, "resolve_job_ref", job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    # Do not expose the job as due before claiming it: the built-in ticker and
    # external/manual fire paths share the same durable claim, so only one can
    # execute this selected run even if they race across processes. Active jobs
    # keep the legacy provider call shape; paused jobs need the explicit force
    # flag to resume and claim atomically.
    force = not job.get("enabled", True) or job.get("state") == "paused"
    ran = _fire_cron_job_for_profile(selected, job["id"], force=force)
    refreshed = _call_cron_for_profile(selected, "get_job", job["id"])
    if refreshed and refreshed.get("last_run_at") != job.get("last_run_at"):
        return refreshed
    if not ran:
        raise HTTPException(
            status_code=409,
            detail="Job is already running or was claimed by another scheduler",
        )
    if refreshed:
        return refreshed
    # A one-shot may remove itself after exhausting repeat=1. Keep the response
    # shape compatible without inventing an outcome that is no longer present
    # in the job store; authoritative list refresh removes the completed row.
    return {
        **job,
        "enabled": False,
        "state": "completed",
    }




def _delete_cron_job_sync(job_id: str, profile: Optional[str] = None):
    selected = profile or _find_cron_job_profile(job_id)
    if not selected:
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        removed = _mutate_cron_for_profile(selected, "remove_job", job_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"ok": True}




def _fire_cron_job_for_profile(
    profile: str,
    job_id: str,
    *,
    force: bool = False,
) -> bool:
    """DEPRECATED for NAS webhook fires (superseded by gateway forwarding);
    retained for the dashboard trigger path — do not add new uses.

    Run ONE due cron job end-to-end for ``profile`` via the resolved
    scheduler provider's ``fire_due`` (store CAS claim + ``run_one_job``).

    Superseded by :func:`_forward_cron_fire_to_gateway`: cron fires must
    execute in the GATEWAY process (which owns the live platform adapters),
    not the dashboard. Executing here delivered through the standalone path
    only, which cannot serve relay-fronted logical platforms (their only
    sender is the live relay adapter — no native credential exists on the
    box) or E2EE rooms. Kept temporarily because external callers may still
    resolve it via the web_deps late-binding seam.
    """
    _profile_name, home = _cron_profile_home(profile)
    from cron import jobs as cron_jobs
    from cron.scheduler_provider import (
        provider_supports_force_fire,
        resolve_cron_scheduler,
    )
    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    token = set_hermes_home_override(str(home))
    try:
        with cron_jobs.use_cron_store(home):
            provider = resolve_cron_scheduler()
            if force:
                if not provider_supports_force_fire(provider):
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"Cron provider '{getattr(provider, 'name', 'custom')}' "
                            "does not support atomic forced firing of paused jobs"
                        ),
                    )
                return bool(
                    provider.fire_due(job_id, adapters=None, loop=None, force=True)
                )
            return bool(provider.fire_due(job_id, adapters=None, loop=None))
    finally:
        reset_hermes_home_override(token)


def _profile_env_value(home: Path, key: str) -> str:
    """Best-effort read of one KEY=VALUE line from a profile's .env file."""
    try:
        env_path = home / ".env"
        if not env_path.is_file():
            return ""
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _gateway_fire_endpoint(profile: str, home: Path) -> str:
    """Resolve the loopback URL of the gateway api_server's cron-fire route.

    Port resolution mirrors gateway/config.py's api_server load order for the
    TARGET profile: ``platforms.api_server.extra.port`` in the profile's
    config.yaml, then ``API_SERVER_PORT`` (process env for the active profile,
    the profile's own .env otherwise), then the adapter default 8642. The bind
    host is the adapter's loopback default — the dashboard and gateway share a
    network namespace in every supported deployment (same host process tree,
    or the same container under s6).

    Multiplex mode (one gateway serving several profiles) exposes per-profile
    mirrors under ``/p/<profile>/…``, so a non-default profile routes through
    the default gateway's port with that prefix; per-profile-gateway mode
    (each profile its own process/port) uses the bare path on the profile's
    own port.
    """
    import os as _os

    port = 0
    try:
        # Profile-scoped read through the CANONICAL loader (managed-scope
        # overlay, ${ENV_VAR} expansion, profile pathing) — never a raw
        # yaml.safe_load of config.yaml (tests/hermes_cli/
        # test_config_read_guard.py). The HERMES_HOME override scopes
        # get_config_path() to the TARGET profile, same pattern the
        # deprecated _fire_cron_job_for_profile used for its store scope.
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )

        token = set_hermes_home_override(str(home))
        try:
            profile_cfg = load_config()
        finally:
            reset_hermes_home_override(token)
        raw = cfg_get(
            profile_cfg, "platforms", "api_server", "extra", "port", default=None
        )
        if raw:
            port = int(raw)
    except Exception:
        port = 0
    if not port:
        raw = (
            _os.getenv("API_SERVER_PORT", "")
            if profile == _cron_default_profile()
            else _profile_env_value(home, "API_SERVER_PORT")
        )
        try:
            port = int(raw) if raw else 0
        except ValueError:
            port = 0
    if not port:
        port = 8642

    multiplex = False
    try:
        cfg = load_config()
        multiplex = bool(cfg_get(cfg, "gateway", "multiplex_profiles", default=False))
        env_flag = _os.getenv("GATEWAY_MULTIPLEX_PROFILES", "").strip().lower()
        if env_flag in {"1", "true", "yes", "on"}:
            multiplex = True
        elif env_flag in {"0", "false", "no", "off"}:
            multiplex = False
    except Exception:
        pass

    if multiplex and profile != "default":
        return f"http://127.0.0.1:{port}/p/{profile}/api/cron/fire"
    return f"http://127.0.0.1:{port}/api/cron/fire"


async def _forward_cron_fire_to_gateway(
    profile: str, job_id: str, authorization: str
) -> Optional[Tuple[int, Dict[str, Any]]]:
    """Forward a Chronos fire callback to the gateway api_server on loopback.

    The dashboard is the hosted deployment's only public HTTP door (Fly proxy
    → internal_port 9119), but cron execution belongs to the GATEWAY process:
    it owns the live platform adapters, so delivery works for relay-fronted
    logical platforms and E2EE rooms — the standalone path the dashboard used
    to run cannot serve either. This forwards the fire byte-preserved (same
    job_id, same NAS bearer — the gateway re-verifies the JWT itself) and
    passes the gateway's response through.

    Returns ``(status_code, body)`` from the gateway, or ``None`` when the
    gateway is unreachable (not started yet after a scale-to-zero wake,
    restarting, or api_server disabled) — the caller maps that to 503 so NAS
    retries per the Chronos contract (non-2xx = retryable; the store CAS
    de-dupes the eventual double fire), UNLESS the profile's gateway was
    deliberately stopped (see :func:`_gateway_intentionally_stopped`), in
    which case the caller drops the fire with 200 — retrying into an
    operator-stopped gateway can never succeed and only burns scheduler
    retries (OOF-266).
    """
    _profile_name, home = _cron_profile_home(profile)
    url = _gateway_fire_endpoint(_profile_name, home)
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                json={"job_id": job_id},
                headers={"Authorization": authorization},
            )
    except Exception as exc:
        _log.warning(
            "cron fire forward to %s failed (%s: %s); returning 503 for NAS retry",
            url, type(exc).__name__, exc,
        )
        return None
    try:
        body = resp.json()
    except Exception:
        body = {"raw": (resp.text or "")[:500]}
    if not isinstance(body, dict):
        body = {"raw": body}
    return resp.status_code, body


def _gateway_intentionally_stopped(profile: Optional[str]) -> bool:
    """True when the profile's gateway is stopped BY OPERATOR INTENT.

    Reads the durable ``desired_state`` field of the profile's
    ``gateway_state.json`` — written exclusively by the s6 lifecycle
    commands (``hermes gateway stop`` persists ``"stopped"``; start and
    restart persist ``"running"``, see service_manager's
    ``_write_gateway_desired_state``). This is the same operator-intent
    signal container-boot reconciliation trusts, and it is precisely NOT
    set to "stopped" during transient windows (crash loops, drains,
    scale-to-zero wakes, restarts) — so it cleanly splits "retry will
    eventually succeed" from "retry can never succeed".

    Deliberately does NOT fall back to the volatile ``gateway_state``
    runtime field: a legacy file without ``desired_state`` (or a gateway
    that crashed before persisting) must stay on the retryable-503 path.
    Failing open to "not intentionally stopped" is the safe direction —
    the worst case is retries against a dead gateway, which is exactly
    today's behavior.

    Exception-safe: any resolution or parse failure returns False.
    """
    import json as _json

    try:
        _name, home = _cron_profile_home(profile)
        state_file = home / "gateway_state.json"
        if not state_file.exists():
            return False
        data = _json.loads(state_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False
        return data.get("desired_state") == "stopped"
    except Exception:
        return False




# ---------------------------------------------------------------------------
# Automation Blueprints — parameterized automation blueprints. The dashboard renders the
# slot schema as a form; submitting instantiates a real cron job via the same
# create_job path. See cron/blueprint_catalog.py for the single source of truth.
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# MCP server endpoints — list / add / remove / test.
#
# Wraps the same config data layer the CLI uses (hermes_cli.mcp_config), so
# servers managed here show up under `hermes mcp list` and vice versa.  Secrets
# in stdio `env` blocks are redacted on read; the agent picks them up from
# config.yaml at session start exactly as with CLI-added servers.
# ---------------------------------------------------------------------------


def _normalize_mcp_server_create(
    body: MCPServerCreate,
) -> tuple[str, Dict[str, Any], Optional[str]]:
    """Validate a Dashboard MCP create request and build its safe config.

    The returned config never contains the submitted Bearer token. Callers
    persist the token with the shared Bearer helper only after they enter the
    intended profile scope. Keeping this conversion shared makes the
    standalone MCP page and the Profile Builder enforce the same
    transport/auth contract.
    """
    from hermes_cli.mcp_config import (
        _bearer_auth_headers,
        _strip_bearer_prefix,
    )
    from hermes_cli.mcp_security import validate_mcp_server_entry

    name = (body.name or "").strip()
    if not name:
        raise ValueError("Server name is required")

    url = (body.url or "").strip()
    command = (body.command or "").strip()
    auth = (body.auth or "none").strip().lower()
    bearer_token = (
        body.bearer_token.get_secret_value()
        if body.bearer_token is not None
        else None
    )

    if bool(url) == bool(command):
        raise ValueError("Provide exactly one of URL (HTTP/SSE) or command (stdio)")
    if auth not in {"none", "header", "oauth"}:
        raise ValueError(f"Unsupported auth mode: {auth}")

    server_config: Dict[str, Any] = {}
    if url:
        if body.args:
            raise ValueError("Arguments are only supported for stdio MCP servers")
        if body.env:
            raise ValueError(
                "Environment variables are only supported for stdio MCP servers"
            )
        if auth == "header":
            normalized = _strip_bearer_prefix(bearer_token) if bearer_token else ""
            if not normalized or normalized.lower() == "bearer":
                raise ValueError("Bearer token is required")
            server_config["headers"] = _bearer_auth_headers(name)
        elif body.bearer_token is not None:
            raise ValueError("Bearer token requires header authentication")

        server_config["url"] = url
        if auth == "oauth":
            server_config["auth"] = "oauth"
    else:
        if auth != "none" or body.bearer_token is not None:
            raise ValueError(
                "HTTP authentication is not supported for stdio MCP servers"
            )
        server_config["command"] = command
        if body.args:
            server_config["args"] = list(body.args)
        if body.env:
            server_config["env"] = dict(body.env)

    issues = validate_mcp_server_entry(name, server_config)
    if issues:
        raise ValueError(f"Server '{name}' rejected: {'; '.join(issues)}")
    return name, server_config, bearer_token


def _redact_mcp_env(env: Dict[str, Any]) -> Dict[str, str]:
    """Mask secret-shaped MCP env values for read responses."""
    out: Dict[str, str] = {}
    for k, v in (env or {}).items():
        try:
            out[str(k)] = redact_key(str(v)) if v else ""
        except Exception:
            out[str(k)] = "***"
    return out


def _mcp_server_summary(name: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    transport = "http" if cfg.get("url") else ("stdio" if cfg.get("command") else "unknown")
    auth = cfg.get("auth")
    headers = cfg.get("headers") or {}
    if not auth and isinstance(headers, dict) and any(
        str(key).lower() == "authorization" for key in headers
    ):
        auth = "header"
    return {
        "name": name,
        "transport": transport,
        "url": cfg.get("url"),
        "command": cfg.get("command"),
        "args": list(cfg.get("args") or []),
        "env": _redact_mcp_env(cfg.get("env") or {}),
        "auth": auth,
        "enabled": cfg.get("enabled", True) is not False,
        # Tool selection: list of enabled tool names, or None = all.
        "tools": cfg.get("tools"),
    }


from hermes_cli.web_routers import mcp as _mcp_routes  # noqa: E402

app.include_router(_mcp_routes.router)
from hermes_cli.web_routers.mcp import (  # noqa: E402,F401 — legacy re-exports; tests call these via web_server.<name>
    list_mcp_servers,
    add_mcp_server,
    replace_mcp_servers,
    remove_mcp_server,
    test_mcp_server,
    auth_mcp_server,
    mcp_oauth_flow_status,
    mcp_oauth_callback,
    set_mcp_server_enabled,
    list_mcp_catalog,
    install_mcp_catalog_entry,
)










_MCP_DASHBOARD_OAUTH_TTL = 15 * 60
_MAX_PENDING_MCP_OAUTH_FLOWS = 8
_mcp_oauth_flows: dict[str, "DashboardOAuthFlow"] = {}
_mcp_oauth_flows_lock = threading.Lock()
_mcp_oauth_transactions: dict[tuple[str, str], threading.Lock] = {}
_mcp_oauth_transactions_lock = threading.Lock()


def _gc_mcp_oauth_flows() -> None:
    cutoff = time.time() - _MCP_DASHBOARD_OAUTH_TTL
    with _mcp_oauth_flows_lock:
        stale = [
            flow_id
            for flow_id, flow in _mcp_oauth_flows.items()
            if getattr(flow, "created_at", 0) < cutoff
        ]
        for flow_id in stale:
            _mcp_oauth_flows.pop(flow_id, None)


def _mcp_oauth_callback_url_from_base(base_url: str, server_name: str) -> str:
    from urllib.parse import quote

    return f"{base_url.rstrip('/')}/api/mcp/oauth/callback/{quote(server_name, safe='')}"


def _mcp_oauth_callback_url(request: Request, server_name: str) -> str:
    """Build the externally reachable callback URL for a dashboard flow."""
    from urllib.parse import urlparse, urlunparse

    from hermes_cli.dashboard_auth.prefix import prefix_from_request, resolve_public_url

    from urllib.parse import quote

    suffix = f"/api/mcp/oauth/callback/{quote(server_name, safe='')}"
    public_url = resolve_public_url()
    if public_url:
        return f"{public_url}{suffix}"
    base = urlparse(str(request.base_url))
    prefix = prefix_from_request(request)
    return urlunparse(base._replace(path=f"{prefix}{suffix}", params="", query="", fragment=""))


def _mcp_oauth_transaction(flow) -> threading.Lock:
    key = (flow.hermes_home, flow.server_name)
    with _mcp_oauth_transactions_lock:
        return _mcp_oauth_transactions.setdefault(key, threading.Lock())


def _run_dashboard_mcp_oauth(flow, cfg: dict) -> None:
    """Run the normal MCP probe with dashboard redirect/callback handlers."""
    from hermes_cli.mcp_config import (
        _oauth_tokens_present,
        _probe_single_server,
        _save_mcp_server,
    )
    try:
        from agent.secret_scope import (
            build_profile_secret_scope,
            reset_secret_scope,
            set_secret_scope,
        )
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from tools.mcp_dashboard_oauth import dashboard_oauth_flow
        from tools.mcp_oauth import HermesTokenStorage, force_interactive_oauth
        from tools.mcp_oauth_manager import get_manager

        home_token = set_hermes_home_override(flow.hermes_home)
        secret_token = set_secret_scope(build_profile_secret_scope(Path(flow.hermes_home)))
        try:
            transaction = _mcp_oauth_transaction(flow)
            with transaction, force_interactive_oauth(), dashboard_oauth_flow(flow):
                manager = get_manager()
                storage = HermesTokenStorage(flow.server_name)
                backup = storage.snapshot()
                previous_entry = None
                try:
                    previous_entry = manager.remove(
                        flow.server_name,
                        hermes_home=flow.hermes_home,
                    )
                    tools = _probe_single_server(
                        flow.server_name,
                        cfg,
                        connect_timeout=max(float(cfg.get("connect_timeout", 0) or 0), 315),
                    )
                    if not _oauth_tokens_present(flow.server_name):
                        raise RuntimeError(
                            "The server responded, but no OAuth token was obtained — "
                            "this provider may require a manually-registered OAuth client."
                        )
                    _save_mcp_server(flow.server_name, cfg)
                    flow.tools = [{"name": t, "description": d} for t, d in tools]
                    flow.mark_approved()
                    if flow.reconnect_live:
                        from tools.mcp_tool import reconnect_mcp_server

                        reconnect_mcp_server(flow.server_name)
                except Exception:
                    storage.restore(backup, only_if_absent=True)
                    manager.restore_entry(
                        flow.server_name,
                        previous_entry,
                        hermes_home=flow.hermes_home,
                    )
                    raise
        finally:
            reset_secret_scope(secret_token)
            reset_hermes_home_override(home_token)
    except Exception as exc:
        msg = str(exc)
        # Providers that gate RFC 7591 registration to pre-approved clients
        # (Figma's MCP catalog, etc.) 403 the register call before any
        # authorization URL exists — surface what's actually happening
        # instead of a bare "403 Forbidden".
        try:
            from tools.mcp_oauth import humanize_oauth_registration_error

            humanized = humanize_oauth_registration_error(
                flow.server_name,
                exc,
                server_url=cfg.get("url") if isinstance(cfg, dict) else None,
            )
            if humanized:
                msg = humanized
        except Exception:
            pass
        flow.mark_error(msg)
    finally:
        flow.mark_worker_done()














def _mcp_install_action_name(name: str) -> str:
    """Unique per-entry mcp-install action name (+ registered log file), so a
    re-click or a second catalog install doesn't overwrite the first's tracked
    process/log while its git clone is still running."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48] or "server"
    digest = hashlib.sha1(name.encode()).hexdigest()[:8]
    action = f"mcp-install-{slug}-{digest}"
    _ACTION_LOG_FILES.setdefault(action, f"action-{action}.log")
    return action


=======
>>>>>>> upstream/main
_ACTION_LOG_FILES.setdefault("computer-use-grant", "action-computer-use-grant.log")

# Cache discovered plugins per-process (refresh on explicit re-scan).
_dashboard_plugins_cache: Optional[list] = None


def _get_dashboard_plugins(force_rescan: bool = False) -> list:
    global _dashboard_plugins_cache
    stale = _dashboard_plugins_cache is None or force_rescan or any(
        not Path(p["_dir"]).is_dir() for p in _dashboard_plugins_cache
    )
    if stale:
        _dashboard_plugins_cache = _discover_dashboard_plugins()
    return _dashboard_plugins_cache


# Router mounting. ORDER IS ROUTE-MATCHING ORDER: literal paths must land before
# templated siblings (e.g. /api/sessions/bulk-delete before /api/sessions/{id}).
from hermes_cli.web_routers import (  # noqa: E402
    files as _files_routes,
    git as _git_routes,
    local_models as _local_models_routes,
    status as _status_routes,
    actions as _actions_routes,
    audio as _audio_routes,
    sessions as _sessions_routes,
    profiles as _profiles_routes,
    memory_providers as _memory_providers_routes,
    config_env as _config_env_routes,
    models as _models_routes,
    messaging as _messaging_routes,
    oauth as _oauth_routes,
    cron as _cron_routes,
    mcp as _mcp_routes,
    ops as _ops_routes,
    skills as _skills_routes,
    tools as _tools_routes,
    analytics as _analytics_routes,
    chat_ws as _chat_ws_routes,
    dashboard_ui as _dashboard_ui_routes,
)

app.include_router(_files_routes.router)
app.include_router(_git_routes.router)
app.include_router(_local_models_routes.router)
app.include_router(_status_routes.router)
app.include_router(_actions_routes.router)
app.include_router(_audio_routes.router)
app.include_router(_actions_routes.status_router)
app.include_router(_sessions_routes.list_router)
app.include_router(_profiles_routes.sessions_router)
app.include_router(_sessions_routes.search_router)
app.include_router(_memory_providers_routes.router)
app.include_router(_config_env_routes.config_router)
app.include_router(_models_routes.router)
app.include_router(_config_env_routes.router)
app.include_router(_messaging_routes.router)
app.include_router(_oauth_routes.router)
app.include_router(_sessions_routes.manage_router)
app.include_router(_status_routes.logs_router)
app.include_router(_cron_routes.router)
app.include_router(_mcp_routes.router)
app.include_router(_ops_routes.router)
app.include_router(_skills_routes.hub_router)
app.include_router(_profiles_routes.router)
app.include_router(_skills_routes.router)
app.include_router(_tools_routes.router)
app.include_router(_analytics_routes.router)
app.include_router(_chat_ws_routes.router)
app.include_router(_dashboard_ui_routes.router)

# Plugin API routes and the dashboard auth routes (/login, /auth/*, /api/auth/*)
# mount before the SPA catch-all so /{full_path:path} doesn't swallow them. Auth
# routes are always mounted — the gate middleware decides enforcement.
_mount_plugin_api_routes()
from hermes_cli.dashboard_auth.routes import router as _dashboard_auth_router  # noqa: E402

app.include_router(_dashboard_auth_router)
mount_spa(app)


def _no_auth_provider_message(host: str) -> str:
    """Actionable SystemExit text for a gated bind with no registered auth provider.

    Names the exact trigger: on a loopback bind the ONLY trigger is
    dashboard.public_url, so print the offending URL and the remove-it exit.
    Bundled providers expose ``LAST_SKIP_REASON`` so an installed-but-
    unconfigured provider is not reported as merely "no providers".
    """
    skip_reasons: list[str] = []
    try:
        from plugins.dashboard_auth import nous as _nous_plugin

        if _nous_plugin.LAST_SKIP_REASON:
            skip_reasons.append(f"  • nous: {_nous_plugin.LAST_SKIP_REASON}")
    except Exception:
        pass

    if host in _LOOPBACK_HOST_VALUES:
        public_url = ""
        try:
            from hermes_cli.dashboard_auth.prefix import resolve_public_url

            public_url = resolve_public_url()
        except Exception:
            pass
        gate_reason = (
            f"dashboard.public_url is set to "
            f"{public_url or '<a non-loopback URL>'} — an "
            f"operator-declared external URL engages the auth gate "
            f"even on a loopback bind"
        )
        fix_hint = (
            "If this dashboard should be LOCAL-ONLY (no reverse "
            "proxy), remove dashboard.public_url from config.yaml "
            "(and unset HERMES_DASHBOARD_PUBLIC_URL) to restore the "
            "unauthenticated loopback mode.\n"
        )
    else:
        gate_reason = f"the auth gate engages on non-loopback binds ({host})"
        fix_hint = ""

    fix_hint += (
        "Configure an auth provider before exposing the dashboard:\n"
        "  • Password: set dashboard.basic_auth.username + "
        "password_hash in config.yaml\n"
        "    (hash with: python -c \"from "
        "plugins.dashboard_auth.basic import hash_password; "
        "print(hash_password('your-password'))\")\n"
        "  • OAuth: run `hermes dashboard register` (Nous Portal) or "
        "install a DashboardAuthProvider plugin.\n"
        "There is no unauthenticated public-dashboard option. For "
        "local-only use, bind 127.0.0.1 and leave dashboard.public_url "
        "unset; a configured external public URL requires auth even "
        "when a local reverse proxy reaches a loopback backend."
    )
    # Credentials exist but the bundled provider is disabled (#54489). Basic
    # auth needs a username AND a credential; a half-configured block is silent.
    try:
        from hermes_cli.config import load_config as _load_cfg
        from hermes_cli.plugins_cmd import _BASIC_AUTH_PLUGIN_KEYS

        cfg = _load_cfg()
        ba = (cfg.get("dashboard") or {}).get("basic_auth") or {}
        disabled = (cfg.get("plugins") or {}).get("disabled") or []
        has_creds = bool(ba.get("username")) and bool(ba.get("password_hash") or ba.get("password"))
        if has_creds and (set(disabled) & _BASIC_AUTH_PLUGIN_KEYS):
            fix_hint = (
                "The 'basic' dashboard-auth plugin is in "
                "plugins.disabled but dashboard.basic_auth is "
                "configured.\n"
                "Remove 'basic' from plugins.disabled (or run "
                "`hermes plugins enable basic`), then restart the "
                "dashboard.\n\n"
            ) + fix_hint
    except Exception:
        pass
    msg = (
        f"Refusing to bind dashboard to {host} — {gate_reason}, "
        f"but no auth providers are registered.\n\n"
    )
    if skip_reasons:
        msg += "Bundled providers reported these issues:\n" + "\n".join(skip_reasons) + "\n\n"
    return msg + fix_hint


def _configure_auth_gate(
    host: str,
    allow_public: bool,
    ssh_session_token: Optional[str],
    ssh_owner_nonce: Optional[str],
) -> None:
    """Resolve the trusted public hosts + auth-gate flag onto ``app.state``.

    Fails closed (``SystemExit`` with an actionable message) when the gate
    engages but no dashboard auth provider is registered.
    """
    # dashboard.public_url is also the exact Host/Origin trust declaration for
    # reverse-proxy deployments; resolved once so middleware never reloads
    # config. A non-loopback public hostname engages the gate even on a loopback
    # backend, else the SPA's local session token becomes remotely reachable.
    app.state.trusted_public_hosts = _dashboard_public_hosts()
    # auth_required drives middleware, SPA-token injection, WS auth, the
    # startup refusal, the gate-on banner and uvicorn proxy_headers.
    if _desktop_loopback_auth_exempt(host, ssh_session_token, ssh_owner_nonce):
        # public_url describes the operator's PUBLIC deployment, not this
        # Desktop-owned loopback backend (#96490), which authenticates with the
        # per-spawn session token the ticket-only gate would refuse.
        app.state.auth_required = should_require_auth(host)
        _log.info(
            "Desktop-owned loopback backend: dashboard.public_url does not "
            "engage the ticket gate for this process; the public deployment "
            "keeps its own gate.",
        )
    else:
        app.state.auth_required = should_require_dashboard_auth(host, app.state.trusted_public_hosts)

    # ``--insecure`` no longer disables the gate (June 2026 hermes-0day
    # hardening); warn that it is a no-op rather than silently ignore it.
    if allow_public and host not in _LOOPBACK_HOST_VALUES:
        _log.warning(
            "--insecure no longer bypasses dashboard authentication. A "
            "non-loopback bind (%s) now ALWAYS requires an auth provider "
            "(OAuth or the bundled password provider). Configure one — see "
            "below — or bind to 127.0.0.1 and reach it over an SSH tunnel / "
            "Tailscale.", host,
        )

    if app.state.auth_required:
        # No escape hatch serves a gated dashboard without a provider.
        from hermes_cli.dashboard_auth import list_providers
        if not list_providers():
            raise SystemExit(_no_auth_provider_message(host))
        _log.info(
            "Dashboard binding to %s with auth gate enabled. Providers: %s",
            host,
            ", ".join(p.name for p in list_providers()),
        )


def _build_uvicorn_server(host: str, port: int):
    """Build the uvicorn ``Config`` + ``Server`` for this bind (reads ``app.state.auth_required``).

    uvicorn.Server is driven directly (not uvicorn.run) so startup is split from
    the main loop: after startup() the socket is bound and held by uvicorn, so the
    OS-assigned port can be read with no pre-bind-then-close TOCTOU. Explicit
    taken ports are caught by the #93608 preflight probe; uvicorn's own bind
    error stays the fallback for races.
    """
    import uvicorn

    # WS keepalive ping runs ON the agent event loop; a GIL-holding worker call
    # can starve it for minutes, so uvicorn misses the pong and drops a healthy
    # local socket (#53773/#48445/#50005). The ping only detects half-open
    # connections (proxy 524, dropped tunnels), impossible on loopback where a
    # dead client sends a real FIN/RST -> WebSocketDisconnect. So: no ping on
    # loopback; non-loopback sits behind a Cloudflare Tunnel (~100s idle) and
    # keeps a config-driven cadence (dashboard.ws_ping_interval/_timeout,
    # #79635) defaulting to 20/20.
    _is_loopback = host in _LOOPBACK_HOST_VALUES
    try:
        _dash_cfg = load_config().get("dashboard") or {}
    except Exception:
        _dash_cfg = {}

    def _ws_ping_setting(key: str, default: float = 20.0) -> float:
        try:
            return float(_dash_cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    config = uvicorn.Config(
        app, host=host, port=port, log_level="warning",
        # Off by default so _ws_client_is_allowed sees the real peer, not
        # X-Forwarded-For. Gated mode runs behind a TLS terminator and needs
        # X-Forwarded-Proto for cookie Secure flags.
        proxy_headers=bool(app.state.auth_required),
        # Loopback-only unless the operator trusts a bounded upstream proxy, so
        # spoofed X-Forwarded-* from arbitrary callers is never honoured.
        forwarded_allow_ips=_dashboard_forwarded_allow_ips(_dash_cfg),
        ws_ping_interval=None if _is_loopback else _ws_ping_setting("ws_ping_interval"),
        ws_ping_timeout=None if _is_loopback else _ws_ping_setting("ws_ping_timeout"),
        ws_max_size=_DESKTOP_ATTACHMENT_WS_MAX_BYTES,
    )
    return config, uvicorn.Server(config)


def _best_effort(what: str, fn) -> None:
    """Run a best-effort startup step; any failure (import included) is a debug line."""
    try:
        fn()
    except Exception as exc:
        _log.debug("%s skipped: %s", what, exc)


def _on_server_started(
    server,
    *,
    host: str,
    port: int,
    headless: bool,
    open_browser: bool,
    initial_profile: str,
    start_mcp_discovery_after_bind: bool,
) -> None:
    """Post-bind arming on the serving loop right after ``server.startup()``.

    Reap prior corpses, parent-death watchdog, process identity, READY
    announcement, browser open, deferred MCP discovery, loop-noise filter,
    loop heartbeat.
    """
    # Clear corpses from a previous unclean Desktop exit (crash/SIGKILL/update
    # handoff leaves an orphaned backend + its MCP subtree) before stacking a
    # new tree (EMFILE / missing tabs). The watchdog only protects *this*
    # process going forward.
    def _reap_desktop_serves() -> None:
        from hermes_cli.dashboard_procs import _reap_orphaned_desktop_local_serves

        _reap_orphaned_desktop_local_serves()

    def _reap_mcp_helpers() -> None:
        from hermes_cli.process_identity import reap_orphaned_mcp_helpers

        reap_orphaned_mcp_helpers()

    if os.getenv("HERMES_DESKTOP") == "1":
        _best_effort("orphan desktop-local serve reap", _reap_desktop_serves)
    # Same sweep for stdio MCP helpers (#61514): positive identity only (spawn
    # ledger + spawner provably dead); anything alive or unprovable is untouched.
    _best_effort("orphan MCP helper reap", _reap_mcp_helpers)

    # No-op for standalone `hermes serve` (no HERMES_PARENT_PID).
    _start_parent_death_watchdog()

    actual_port = _read_bound_port(server, fallback=port)
    app.state.bound_port = actual_port

    # Positive process identity in the machine spawn ledger (+ Windows
    # kill-on-close job). Registered AFTER the bind so the entry carries the
    # ACTUAL port — what lets `hermes update` relaunch a manually-started serve
    # on its real endpoint (#63206).
    def _register_identity() -> None:
        from hermes_cli.process_identity import attach_self_to_kill_on_close_job, register_self

        register_self(
            "serve" if headless else "dashboard",
            detail={"host": host, "port": actual_port, "profile": initial_profile or ""},
        )
        attach_self_to_kill_on_close_job()

    _best_effort("process-identity registration", _register_identity)

    _write_dashboard_ready_file(actual_port)
    # Port-discovery sentinel parsed by the Desktop spawn (matches either
    # token). Written to fd 1: tui_gateway.server redirects sys.stdout to
    # stderr at import, and the Desktop watches child.stdout (#96282).
    ready_token = "HERMES_BACKEND_READY" if headless else "HERMES_DASHBOARD_READY"
    _write_machine_sentinel_line(f"{ready_token} port={actual_port}")
    if headless:
        # Auth-gated JSON-RPC/WS only — announce the bind, not a URL. flush:
        # a piped stdout otherwise surfaces this minutes after the sentinel.
        print(f"  Hermes backend listening on {host}:{actual_port}", flush=True)
    else:
        print(f"  Hermes Web UI → http://{host}:{actual_port}")
    _maybe_open_browser(host, actual_port, open_browser, initial_profile)

    if start_mcp_discovery_after_bind:
        # Desktop `serve`: the ~350ms `mcp` SDK import holds the GIL while the
        # renderer does its WS handshake + first hydration reads, so arm it one
        # second later when the shell is painted and idle. An agent build inside
        # that second fires the deferred start itself (wait_for_mcp_discovery).
        try:
            from hermes_cli.mcp_startup import defer_background_mcp_discovery

            defer_background_mcp_discovery(
                logger=_log,
                thread_name="dashboard-mcp-discovery",
                delay=_DESKTOP_MCP_DISCOVERY_DELAY_S,
            )
        except Exception:
            _log.debug("Deferred MCP discovery arm failed", exc_info=True)

    # Collapse the peer-hangup teardown flood (#50005): 50+ identical WinError
    # 10054 tracebacks per Desktop disconnect become one debug line.
    def _install_noise_filter() -> None:
        from tui_gateway.loop_noise import install_loop_noise_filter

        install_loop_noise_filter(asyncio.get_running_loop())

    _best_effort("loop noise filter install", _install_noise_filter)

    # Loop heartbeat watchdog (CF-1): a 2s call_later tick whose drift equals
    # any GIL stall, so a stalled-loop WS drop is diagnosable from the log.
    # call_later (not a task) dies with the loop — nothing to cancel.
    _hb_interval = 2.0
    _hb_stall_threshold = 5.0
    _hb_loop = asyncio.get_running_loop()

    def _loop_heartbeat(expected: float) -> None:
        now = _hb_loop.time()
        drift = now - expected
        if drift > _hb_stall_threshold:
            _log.warning("event loop stalled %.1fs (GIL pressure suspected)", drift)
        _hb_loop.call_later(_hb_interval, _loop_heartbeat, now + _hb_interval)

    _hb_loop.call_later(_hb_interval, _loop_heartbeat, _hb_loop.time() + _hb_interval)


def _run_serve(serve, config, host: str, port: int) -> None:
    """Drive ``serve()`` on the loop uvicorn expects.

    POSIX keeps ``asyncio.run`` (already a SelectorEventLoop / uvloop). On
    Windows ``asyncio.run`` defaults to a ProactorEventLoop, on which uvicorn
    binds a socket that never accepts (#50641), so mirror uvicorn's own runner +
    loop factory there (hand-installed selector policy for uvicorn < 0.36).
    Ctrl+C -> clean return; probe-to-bind port race -> sentinel + exit code.
    """
    runner = asyncio.run
    runner_kwargs: dict = {}
    if sys.platform == "win32":
        # Resolved FIRST; the serve call is outside this try so genuine
        # serve-time errors (port in use) propagate instead of double-running.
        try:
            from uvicorn._compat import asyncio_run as runner

            runner_kwargs = {"loop_factory": config.get_loop_factory()}
        except Exception:
            runner = asyncio.run
            runner_kwargs = {}
            try:
                asyncio.set_event_loop_policy(
                    asyncio.WindowsSelectorEventLoopPolicy()  # type: ignore[attr-defined]
                )
            except Exception:
                pass

    # ``capture_signals()`` re-raises the captured signal after graceful
    # shutdown; console Ctrl+C lands as KeyboardInterrupt = clean exit.
    # (Re-raised SIGTERM/SIGBREAK keep their terminate disposition.)
    try:
        runner(serve(), **runner_kwargs)
    except KeyboardInterrupt:
        return
    except SystemExit as exc:
        # Probe-to-bind race (#93608): uvicorn's bind_socket() exits 1 — re-check
        # and translate a confirmed conflict into the sentinel + distinct code.
        if exc.code == 1 and _port_bind_conflict(host, port):
            _report_port_in_use(host, port)
            raise SystemExit(PORT_IN_USE_EXIT_CODE) from None
        raise


def start_server(
    host: str = "127.0.0.1",
    port: int = 9119,
    open_browser: bool = True,
    allow_public: bool = False,
    initial_profile: str = "",
    headless: bool = False,
    ssh_session_token: Optional[str] = None,
    ssh_owner_nonce: Optional[str] = None,
    start_mcp_discovery_after_bind: bool = False,
):
    """Start the web UI server.

    ``initial_profile`` is appended to the auto-opened URL as ``?profile=<name>``
    (profile alias ``<profile> dashboard``). ``headless`` is the ``serve`` path:
    JSON-RPC/WS backend, no UI build, no SPA mount (``HERMES_SERVE_HEADLESS``).
    ``ssh_session_token``/``ssh_owner_nonce`` are process-local Desktop SSH
    bootstrap state, never persisted or exported to children.
    ``start_mcp_discovery_after_bind`` (Desktop ``serve``) defers MCP discovery
    until the ready sentinel is written so its SDK import can't hold the GIL
    against the pre-bind path.
    """
    _apply_ssh_session_token(ssh_session_token or "")
    _apply_ssh_owner_nonce(ssh_owner_nonce)

    # Dashboard-mode starts don't route through main.py's `serve` path, which
    # applies the same RLIMIT_NOFILE floor (policy in resource_limits, #81547).
    from hermes_cli.resource_limits import apply_nofile_soft_limit

    apply_nofile_soft_limit()

    import uvicorn  # noqa: F401 — fail fast (before any side effects) when the dashboard extra is missing

    try:
        from hermes_cli.nous_auth_keepalive import start_nous_auth_keepalive

        start_nous_auth_keepalive()
    except Exception as exc:
        _log.debug("Nous auth keepalive did not start: %s", exc)

    _configure_auth_gate(host, allow_public, ssh_session_token, ssh_owner_nonce)

    # host_header_middleware validates Host against this (DNS rebinding,
    # GHSA-ppp5-vxwm-4cf7).
    app.state.bound_host = host

    config, server = _build_uvicorn_server(host, port)

    # Flush-on-kill guard (#94724): chaining SIGTERM/SIGINT handlers persist
    # in-memory transcripts to state.db before shutdown. Installed BEFORE
    # uvicorn's capture_signals() so uvicorn re-raises into them as the
    # "original" handlers — kills outside the serve window are covered too.
    try:
        from tui_gateway.server import install_exit_flush_signal_handlers

        install_exit_flush_signal_handlers()
    except Exception as exc:
        _log.debug("exit-flush signal handlers not installed: %s", exc)

    # #93608: uvicorn's bind_socket() would exit 1 with a bare ERROR line,
    # indistinguishable from "backend broken". Probe first so a conflict
    # surfaces as the BACKEND_PORT_IN_USE sentinel + distinct exit code.
    # ``--port 0`` is skipped by the probe.
    if _port_bind_conflict(host, port):
        _report_port_in_use(host, port)
        raise SystemExit(PORT_IN_USE_EXIT_CODE)

    async def _serve():
        # startup split from main_loop so the bound (ephemeral) port is readable.
        if not config.loaded:
            config.load()
        server.lifespan = config.lifespan_class(config)
        with server.capture_signals():
            await server.startup()
            if server.should_exit:
                return

            _on_server_started(
                server,
                host=host,
                port=port,
                headless=headless,
                open_browser=open_browser,
                initial_profile=initial_profile,
                start_mcp_discovery_after_bind=start_mcp_discovery_after_bind,
            )

            await server.main_loop()
            if server.started:
                await server.shutdown()

    _run_serve(_serve, config, host, port)


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from typing import List  # noqa: F401,E402
from typing import Literal  # noqa: F401,E402
import atexit  # noqa: F401,E402
import base64  # noqa: F401,E402
import binascii  # noqa: F401,E402
import concurrent.futures  # noqa: F401,E402
import contextlib  # noqa: F401,E402
from contextlib import contextmanager  # noqa: F401,E402
from dataclasses import dataclass  # noqa: F401,E402
from datetime import datetime  # noqa: F401,E402
import functools  # noqa: F401,E402
import hashlib  # noqa: F401,E402
import importlib.util  # noqa: F401,E402
import inspect  # noqa: F401,E402
import ipaddress  # noqa: F401,E402
import json  # noqa: F401,E402
import math  # noqa: F401,E402
import mimetypes  # noqa: F401,E402
import queue  # noqa: F401,E402
import shlex  # noqa: F401,E402
import shutil  # noqa: F401,E402
import stat  # noqa: F401,E402
import tempfile  # noqa: F401,E402
from datetime import timezone  # noqa: F401,E402
import yaml  # noqa: F401,E402
import zipfile  # noqa: F401,E402


_PLUGIN_COMPAT_LAZY = {
    'AudioTranscriptionRequest': ('hermes_cli.web_models', 'AudioTranscriptionRequest'),
    'AutomationBlueprintInstantiate': ('hermes_cli.web_models', 'AutomationBlueprintInstantiate'),
    'BackupRequest': ('hermes_cli.web_models', 'BackupRequest'),
    'BulkDeleteSessions': ('hermes_cli.web_models', 'BulkDeleteSessions'),
    'CONFIG_SCHEMA': ('hermes_cli.web_server_config', 'CONFIG_SCHEMA'),
    'ChatImageUpload': ('hermes_cli.web_models', 'ChatImageUpload'),
    'ConfigUpdate': ('hermes_cli.web_models', 'ConfigUpdate'),
    'CredentialPoolAdd': ('hermes_cli.web_models', 'CredentialPoolAdd'),
    'CronJobCreate': ('hermes_cli.web_models', 'CronJobCreate'),
    'CronJobUpdate': ('hermes_cli.web_models', 'CronJobUpdate'),
    'CuratorPause': ('hermes_cli.web_models', 'CuratorPause'),
    'CustomEndpointUpdate': ('hermes_cli.web_models', 'CustomEndpointUpdate'),
    'DEFAULT_CONFIG': ('hermes_cli.config', 'DEFAULT_CONFIG'),
    'DebugShareRequest': ('hermes_cli.web_models', 'DebugShareRequest'),
    'EnvVarDelete': ('hermes_cli.web_models', 'EnvVarDelete'),
    'EnvVarReveal': ('hermes_cli.web_models', 'EnvVarReveal'),
    'EnvVarUpdate': ('hermes_cli.web_models', 'EnvVarUpdate'),
    'FontSetBody': ('hermes_cli.web_models', 'FontSetBody'),
    'FsWriteText': ('hermes_cli.web_models', 'FsWriteText'),
    'GitBranchSwitchBody': ('hermes_cli.web_models', 'GitBranchSwitchBody'),
    'GitCommitBody': ('hermes_cli.web_models', 'GitCommitBody'),
    'GitFileBody': ('hermes_cli.web_models', 'GitFileBody'),
    'GitPathBody': ('hermes_cli.web_models', 'GitPathBody'),
    'GitWorktreeAddBody': ('hermes_cli.web_models', 'GitWorktreeAddBody'),
    'GitWorktreeRemoveBody': ('hermes_cli.web_models', 'GitWorktreeRemoveBody'),
    'HookCreate': ('hermes_cli.web_models', 'HookCreate'),
    'HookDelete': ('hermes_cli.web_models', 'HookDelete'),
    'ImportRequest': ('hermes_cli.web_models', 'ImportRequest'),
    'LearningNodeEdit': ('hermes_cli.web_models', 'LearningNodeEdit'),
    'LearningNodeRef': ('hermes_cli.web_models', 'LearningNodeRef'),
    'MCPCatalogInstall': ('hermes_cli.web_models', 'MCPCatalogInstall'),
    'MCPEnabledToggle': ('hermes_cli.web_models', 'MCPEnabledToggle'),
    'MCPServerCreate': ('hermes_cli.web_models', 'MCPServerCreate'),
    'MCPServersReplace': ('hermes_cli.web_models', 'MCPServersReplace'),
    'ManagedDirectoryCreate': ('hermes_cli.web_models', 'ManagedDirectoryCreate'),
    'ManagedFileDelete': ('hermes_cli.web_models', 'ManagedFileDelete'),
    'ManagedFileUpload': ('hermes_cli.web_models', 'ManagedFileUpload'),
    'ManagedFilesPolicy': ('hermes_cli.web_server_files', 'ManagedFilesPolicy'),
    'MemoryProviderConfigUpdate': ('hermes_cli.web_models', 'MemoryProviderConfigUpdate'),
    'MemoryProviderSelect': ('hermes_cli.web_models', 'MemoryProviderSelect'),
    'MemoryProviderSetupRequest': ('hermes_cli.web_models', 'MemoryProviderSetupRequest'),
    'MemoryReset': ('hermes_cli.web_models', 'MemoryReset'),
    'MessagingPlatformUpdate': ('hermes_cli.web_models', 'MessagingPlatformUpdate'),
    'MoaConfigPayload': ('hermes_cli.web_models', 'MoaConfigPayload'),
    'MoaModelSlot': ('hermes_cli.web_models', 'MoaModelSlot'),
    'MoaPresetPayload': ('hermes_cli.web_models', 'MoaPresetPayload'),
    'ModelAssignment': ('hermes_cli.web_models', 'ModelAssignment'),
    'OAuthSubmitBody': ('hermes_cli.web_models', 'OAuthSubmitBody'),
    'OPTIONAL_ENV_VARS': ('hermes_cli.config', 'OPTIONAL_ENV_VARS'),
    'PairingApprove': ('hermes_cli.web_models', 'PairingApprove'),
    'PairingRevoke': ('hermes_cli.web_models', 'PairingRevoke'),
    'ProfileActiveUpdate': ('hermes_cli.web_models', 'ProfileActiveUpdate'),
    'ProfileCreate': ('hermes_cli.web_models', 'ProfileCreate'),
    'ProfileDescribeAuto': ('hermes_cli.web_models', 'ProfileDescribeAuto'),
    'ProfileDescriptionUpdate': ('hermes_cli.web_models', 'ProfileDescriptionUpdate'),
    'ProfileModelUpdate': ('hermes_cli.web_models', 'ProfileModelUpdate'),
    'ProfileRename': ('hermes_cli.web_models', 'ProfileRename'),
    'ProfileSoulUpdate': ('hermes_cli.web_models', 'ProfileSoulUpdate'),
    'ProviderConfigSchema': ('plugins.memory.config_schema', 'ProviderConfigSchema'),
    'ProviderField': ('plugins.memory.config_schema', 'ProviderField'),
    'PtyBridge': ('hermes_cli.pty_bridge', 'PtyBridge'),
    'PtySessionRegistry': ('hermes_cli.pty_session', 'PtySessionRegistry'),
    'PtyUnavailableError': ('hermes_cli.pty_bridge', 'PtyUnavailableError'),
    'RawConfigUpdate': ('hermes_cli.web_models', 'RawConfigUpdate'),
    'RegistryFull': ('hermes_cli.pty_session', 'RegistryFull'),
    'STORAGE_HONCHO_HOST_BLOCK': ('plugins.memory.config_schema', 'STORAGE_HONCHO_HOST_BLOCK'),
    'SessionImport': ('hermes_cli.web_models', 'SessionImport'),
    'SessionPrune': ('hermes_cli.web_models', 'SessionPrune'),
    'SessionRename': ('hermes_cli.web_models', 'SessionRename'),
    'SkillContentUpdate': ('hermes_cli.web_models', 'SkillContentUpdate'),
    'SkillCreate': ('hermes_cli.web_models', 'SkillCreate'),
    'SkillInstallRequest': ('hermes_cli.web_models', 'SkillInstallRequest'),
    'SkillToggle': ('hermes_cli.web_models', 'SkillToggle'),
    'SkillUninstallRequest': ('hermes_cli.web_models', 'SkillUninstallRequest'),
    'SkillsUpdateRequest': ('hermes_cli.web_models', 'SkillsUpdateRequest'),
    'TTSLeaseRequest': ('hermes_cli.web_models', 'TTSLeaseRequest'),
    'TTSSpeakRequest': ('hermes_cli.web_models', 'TTSSpeakRequest'),
    'TelegramOnboardingApply': ('hermes_cli.web_models', 'TelegramOnboardingApply'),
    'TelegramOnboardingStart': ('hermes_cli.web_models', 'TelegramOnboardingStart'),
    'TerminalBackendSelect': ('hermes_cli.web_models', 'TerminalBackendSelect'),
    'ThemeSetBody': ('hermes_cli.web_models', 'ThemeSetBody'),
    'ToolsetEnvUpdate': ('hermes_cli.web_models', 'ToolsetEnvUpdate'),
    'ToolsetModelSelect': ('hermes_cli.web_models', 'ToolsetModelSelect'),
    'ToolsetPostSetup': ('hermes_cli.web_models', 'ToolsetPostSetup'),
    'ToolsetProviderSelect': ('hermes_cli.web_models', 'ToolsetProviderSelect'),
    'ToolsetToggle': ('hermes_cli.web_models', 'ToolsetToggle'),
    'WebhookCreate': ('hermes_cli.web_models', 'WebhookCreate'),
    'WebhookEnabledToggle': ('hermes_cli.web_models', 'WebhookEnabledToggle'),
    'WhatsAppOnboardingApply': ('hermes_cli.web_models', 'WhatsAppOnboardingApply'),
    'WhatsAppOnboardingStart': ('hermes_cli.web_models', 'WhatsAppOnboardingStart'),
    'activate_custom_endpoint': ('hermes_cli.web_routers.config_env', 'activate_custom_endpoint'),
    'add_credential_pool_entry': ('hermes_cli.web_routers.ops', 'add_credential_pool_entry'),
    'add_mcp_server': ('hermes_cli.web_routers.mcp', 'add_mcp_server'),
    'apply_telegram_onboarding': ('hermes_cli.web_routers.messaging', 'apply_telegram_onboarding'),
    'apply_whatsapp_onboarding': ('hermes_cli.web_routers.messaging', 'apply_whatsapp_onboarding'),
    'approve_pairing': ('hermes_cli.web_routers.ops', 'approve_pairing'),
    'auth_mcp_server': ('hermes_cli.web_routers.mcp', 'auth_mcp_server'),
    'build_cron_model_impact': ('hermes_cli.config', 'build_cron_model_impact'),
    'bulk_delete_sessions_endpoint': ('hermes_cli.web_routers.sessions', 'bulk_delete_sessions_endpoint'),
    'cancel_oauth_session': ('hermes_cli.web_routers.oauth', 'cancel_oauth_session'),
    'cancel_telegram_onboarding': ('hermes_cli.web_routers.messaging', 'cancel_telegram_onboarding'),
    'cancel_whatsapp_onboarding': ('hermes_cli.web_routers.messaging', 'cancel_whatsapp_onboarding'),
    'cfg_get': ('hermes_cli.config', 'cfg_get'),
    'check_config_version': ('hermes_cli.config', 'check_config_version'),
    'check_hermes_update': ('hermes_cli.web_routers.actions', 'check_hermes_update'),
    'clear_model_endpoint_credentials': ('hermes_cli.config', 'clear_model_endpoint_credentials'),
    'clear_pending_pairing': ('hermes_cli.web_routers.ops', 'clear_pending_pairing'),
    'coerce_provider_id': ('hermes_cli.config', 'coerce_provider_id'),
    'console_ws': ('hermes_cli.web_routers.chat_ws', 'console_ws'),
    'count_empty_sessions_endpoint': ('hermes_cli.web_routers.sessions', 'count_empty_sessions_endpoint'),
    'create_cron_job': ('hermes_cli.web_routers.cron', 'create_cron_job'),
    'create_hook': ('hermes_cli.web_routers.ops', 'create_hook'),
    'create_managed_directory': ('hermes_cli.web_routers.files', 'create_managed_directory'),
    'create_profile_endpoint': ('hermes_cli.web_routers.profiles', 'create_profile_endpoint'),
    'create_skill': ('hermes_cli.web_routers.skills', 'create_skill'),
    'create_webhook': ('hermes_cli.web_routers.ops', 'create_webhook'),
    'cron_fire_webhook': ('hermes_cli.web_routers.cron', 'cron_fire_webhook'),
    'custom_endpoint_key_env': ('hermes_cli.config', 'custom_endpoint_key_env'),
    'delete_agent_plugin': ('hermes_cli.web_routers.dashboard_ui', 'delete_agent_plugin'),
    'delete_cron_job': ('hermes_cli.web_routers.cron', 'delete_cron_job'),
    'delete_custom_endpoint': ('hermes_cli.web_routers.config_env', 'delete_custom_endpoint'),
    'delete_empty_sessions_endpoint': ('hermes_cli.web_routers.sessions', 'delete_empty_sessions_endpoint'),
    'delete_hook': ('hermes_cli.web_routers.ops', 'delete_hook'),
    'delete_learning_node': ('hermes_cli.web_routers.status', 'delete_learning_node'),
    'delete_managed_file': ('hermes_cli.web_routers.files', 'delete_managed_file'),
    'delete_profile_endpoint': ('hermes_cli.web_routers.profiles', 'delete_profile_endpoint'),
    'delete_session_endpoint': ('hermes_cli.web_routers.sessions', 'delete_session_endpoint'),
    'delete_webhook': ('hermes_cli.web_routers.ops', 'delete_webhook'),
    'derive_gateway_busy': ('gateway.status', 'derive_gateway_busy'),
    'derive_gateway_drainable': ('gateway.status', 'derive_gateway_drainable'),
    'describe_profile_auto_endpoint': ('hermes_cli.web_routers.profiles', 'describe_profile_auto_endpoint'),
    'detect_install_method': ('hermes_cli.config', 'detect_install_method'),
    'disconnect_oauth_provider': ('hermes_cli.web_routers.oauth', 'disconnect_oauth_provider'),
    'download_dashboard_backup': ('hermes_cli.web_routers.ops', 'download_dashboard_backup'),
    'download_managed_file': ('hermes_cli.web_routers.files', 'download_managed_file'),
    'enable_webhooks': ('hermes_cli.web_routers.ops', 'enable_webhooks'),
    'env_var_enabled': ('utils', 'env_var_enabled'),
    'events_ws': ('hermes_cli.web_routers.chat_ws', 'events_ws'),
    'export_session_endpoint': ('hermes_cli.web_routers.sessions', 'export_session_endpoint'),
    'find_provider_entry': ('hermes_cli.config', 'find_provider_entry'),
    'format_docker_update_message': ('hermes_cli.config', 'format_docker_update_message'),
    'fs_default_cwd': ('hermes_cli.web_routers.files', 'fs_default_cwd'),
    'fs_download': ('hermes_cli.web_routers.files', 'fs_download'),
    'fs_git_root': ('hermes_cli.web_routers.files', 'fs_git_root'),
    'fs_list': ('hermes_cli.web_routers.files', 'fs_list'),
    'fs_read_data_url': ('hermes_cli.web_routers.files', 'fs_read_data_url'),
    'fs_read_text': ('hermes_cli.web_routers.files', 'fs_read_text'),
    'fs_write_text': ('hermes_cli.web_routers.files', 'fs_write_text'),
    'gateway_drain': ('hermes_cli.web_routers.actions', 'gateway_drain'),
    'gateway_ws': ('hermes_cli.web_routers.chat_ws', 'gateway_ws'),
    'get_action_status': ('hermes_cli.web_routers.actions', 'get_action_status'),
    'get_active_profile_endpoint': ('hermes_cli.web_routers.profiles', 'get_active_profile_endpoint'),
    'get_auxiliary_models': ('hermes_cli.web_routers.models', 'get_auxiliary_models'),
    'get_client_voice_config': ('hermes_cli.web_routers.audio', 'get_client_voice_config'),
    'get_computer_use_status': ('hermes_cli.web_routers.tools', 'get_computer_use_status'),
    'get_config': ('hermes_cli.web_routers.config_env', 'get_config'),
    'get_config_path': ('hermes_cli.config', 'get_config_path'),
    'get_config_raw': ('hermes_cli.web_routers.analytics', 'get_config_raw'),
    'get_cron_delivery_targets': ('hermes_cli.web_routers.cron', 'get_cron_delivery_targets'),
    'get_cron_job': ('hermes_cli.web_routers.cron', 'get_cron_job'),
    'get_curator_status': ('hermes_cli.web_routers.status', 'get_curator_status'),
    'get_dashboard_font': ('hermes_cli.web_routers.dashboard_ui', 'get_dashboard_font'),
    'get_dashboard_plugins': ('hermes_cli.web_routers.dashboard_ui', 'get_dashboard_plugins'),
    'get_dashboard_themes': ('hermes_cli.web_routers.dashboard_ui', 'get_dashboard_themes'),
    'get_defaults': ('hermes_cli.web_routers.config_env', 'get_defaults'),
    'get_egress_status': ('hermes_cli.web_routers.config_env', 'get_egress_status'),
    'get_elevenlabs_voices': ('hermes_cli.web_routers.audio', 'get_elevenlabs_voices'),
    'get_env_path': ('hermes_cli.config', 'get_env_path'),
    'get_env_vars': ('hermes_cli.web_routers.config_env', 'get_env_vars'),
    'get_health': ('hermes_cli.web_routers.status', 'get_health'),
    'get_hermes_home': ('hermes_cli.config', 'get_hermes_home'),
    'get_learning_graph': ('hermes_cli.web_routers.status', 'get_learning_graph'),
    'get_learning_node': ('hermes_cli.web_routers.status', 'get_learning_node'),
    'get_logs': ('hermes_cli.web_routers.status', 'get_logs'),
    'get_media': ('hermes_cli.web_routers.files', 'get_media'),
    'get_memory_provider_config': ('hermes_cli.web_routers.memory_providers', 'get_memory_provider_config'),
    'get_memory_status': ('hermes_cli.web_routers.ops', 'get_memory_status'),
    'get_messaging_platforms': ('hermes_cli.web_routers.messaging', 'get_messaging_platforms'),
    'get_moa_models': ('hermes_cli.web_routers.models', 'get_moa_models'),
    'get_model_info': ('hermes_cli.web_routers.models', 'get_model_info'),
    'get_model_options': ('hermes_cli.web_routers.models', 'get_model_options'),
    'get_models_analytics': ('hermes_cli.web_routers.analytics', 'get_models_analytics'),
    'get_plugins_hub': ('hermes_cli.web_routers.dashboard_ui', 'get_plugins_hub'),
    'get_portal_status': ('hermes_cli.web_routers.status', 'get_portal_status'),
    'get_process_hermes_home': ('hermes_cli.config', 'get_process_hermes_home'),
    'get_profile_setup_command': ('hermes_cli.web_routers.profiles', 'get_profile_setup_command'),
    'get_profile_soul': ('hermes_cli.web_routers.profiles', 'get_profile_soul'),
    'get_profiles_sessions': ('hermes_cli.web_routers.profiles', 'get_profiles_sessions'),
    'get_profiles_sessions_sidebar': ('hermes_cli.web_routers.profiles', 'get_profiles_sessions_sidebar'),
    'get_provider_config_schema': ('plugins.memory.config_schema', 'get_provider_config_schema'),
    'get_recommended_default_model': ('hermes_cli.web_routers.models', 'get_recommended_default_model'),
    'get_running_pid': ('gateway.status', 'get_running_pid'),
    'get_running_pid_cached': ('gateway.status', 'get_running_pid_cached'),
    'get_runtime_status_running_pid': ('gateway.status', 'get_runtime_status_running_pid'),
    'get_schema': ('hermes_cli.web_routers.config_env', 'get_schema'),
    'get_session_detail': ('hermes_cli.web_routers.sessions', 'get_session_detail'),
    'get_session_latest_descendant': ('hermes_cli.web_routers.sessions', 'get_session_latest_descendant'),
    'get_session_messages': ('hermes_cli.web_routers.sessions', 'get_session_messages'),
    'get_session_stats': ('hermes_cli.web_routers.sessions', 'get_session_stats'),
    'get_sessions': ('hermes_cli.web_routers.sessions', 'get_sessions'),
    'get_skill_content': ('hermes_cli.web_routers.skills', 'get_skill_content'),
    'get_skills': ('hermes_cli.web_routers.skills', 'get_skills'),
    'get_ssh_ownership': ('hermes_cli.web_routers.status', 'get_ssh_ownership'),
    'get_status': ('hermes_cli.web_routers.status', 'get_status'),
    'get_system_stats': ('hermes_cli.web_routers.status', 'get_system_stats'),
    'get_telegram_onboarding_status': ('hermes_cli.web_routers.messaging', 'get_telegram_onboarding_status'),
    'get_terminal_backends': ('hermes_cli.web_routers.tools', 'get_terminal_backends'),
    'get_toolset_config': ('hermes_cli.web_routers.tools', 'get_toolset_config'),
    'get_toolset_models': ('hermes_cli.web_routers.tools', 'get_toolset_models'),
    'get_toolsets': ('hermes_cli.web_routers.tools', 'get_toolsets'),
    'get_update_receipt': ('hermes_cli.web_routers.actions', 'get_update_receipt'),
    'get_usage_analytics': ('hermes_cli.web_routers.analytics', 'get_usage_analytics'),
    'get_whatsapp_onboarding_status': ('hermes_cli.web_routers.messaging', 'get_whatsapp_onboarding_status'),
    'git_base_branches_route': ('hermes_cli.web_routers.git', 'git_base_branches_route'),
    'git_branch_switch_route': ('hermes_cli.web_routers.git', 'git_branch_switch_route'),
    'git_branches_route': ('hermes_cli.web_routers.git', 'git_branches_route'),
    'git_commit_context_route': ('hermes_cli.web_routers.git', 'git_commit_context_route'),
    'git_commit_route': ('hermes_cli.web_routers.git', 'git_commit_route'),
    'git_create_pr_route': ('hermes_cli.web_routers.git', 'git_create_pr_route'),
    'git_file_diff_route': ('hermes_cli.web_routers.git', 'git_file_diff_route'),
    'git_push_route': ('hermes_cli.web_routers.git', 'git_push_route'),
    'git_rev_parse_route': ('hermes_cli.web_routers.git', 'git_rev_parse_route'),
    'git_revert_route': ('hermes_cli.web_routers.git', 'git_revert_route'),
    'git_review_diff_route': ('hermes_cli.web_routers.git', 'git_review_diff_route'),
    'git_review_list_route': ('hermes_cli.web_routers.git', 'git_review_list_route'),
    'git_ship_info_route': ('hermes_cli.web_routers.git', 'git_ship_info_route'),
    'git_stage_route': ('hermes_cli.web_routers.git', 'git_stage_route'),
    'git_status_route': ('hermes_cli.web_routers.git', 'git_status_route'),
    'git_unstage_route': ('hermes_cli.web_routers.git', 'git_unstage_route'),
    'git_worktree_add_route': ('hermes_cli.web_routers.git', 'git_worktree_add_route'),
    'git_worktree_remove_route': ('hermes_cli.web_routers.git', 'git_worktree_remove_route'),
    'git_worktrees_route': ('hermes_cli.web_routers.git', 'git_worktrees_route'),
    'grant_computer_use_permissions': ('hermes_cli.web_routers.tools', 'grant_computer_use_permissions'),
    'import_sessions_endpoint': ('hermes_cli.web_routers.sessions', 'import_sessions_endpoint'),
    'install_mcp_catalog_entry': ('hermes_cli.web_routers.mcp', 'install_mcp_catalog_entry'),
    'install_skill_hub': ('hermes_cli.web_routers.skills', 'install_skill_hub'),
    'instantiate_blueprint': ('hermes_cli.web_routers.cron', 'instantiate_blueprint'),
    'is_nix_install_method': ('hermes_cli.config', 'is_nix_install_method'),
    'list_checkpoints': ('hermes_cli.web_routers.ops', 'list_checkpoints'),
    'list_credential_pool': ('hermes_cli.web_routers.ops', 'list_credential_pool'),
    'list_cron_blueprints': ('hermes_cli.web_routers.cron', 'list_cron_blueprints'),
    'list_cron_job_runs': ('hermes_cli.web_routers.cron', 'list_cron_job_runs'),
    'list_cron_jobs': ('hermes_cli.web_routers.cron', 'list_cron_jobs'),
    'list_custom_endpoints': ('hermes_cli.web_routers.config_env', 'list_custom_endpoints'),
    'list_hooks': ('hermes_cli.web_routers.ops', 'list_hooks'),
    'list_managed_files': ('hermes_cli.web_routers.files', 'list_managed_files'),
    'list_mcp_catalog': ('hermes_cli.web_routers.mcp', 'list_mcp_catalog'),
    'list_mcp_servers': ('hermes_cli.web_routers.mcp', 'list_mcp_servers'),
    'list_oauth_providers': ('hermes_cli.web_routers.oauth', 'list_oauth_providers'),
    'list_pairing': ('hermes_cli.web_routers.ops', 'list_pairing'),
    'list_profiles_endpoint': ('hermes_cli.web_routers.profiles', 'list_profiles_endpoint'),
    'list_skills_hub_sources': ('hermes_cli.web_routers.skills', 'list_skills_hub_sources'),
    'list_webhooks': ('hermes_cli.web_routers.ops', 'list_webhooks'),
    'load_env': ('hermes_cli.config', 'load_env'),
    'mcp_oauth_callback': ('hermes_cli.web_routers.mcp', 'mcp_oauth_callback'),
    'mcp_oauth_flow_status': ('hermes_cli.web_routers.mcp', 'mcp_oauth_flow_status'),
    'normalize_updated_at': ('gateway.status', 'normalize_updated_at'),
    'open_profile_terminal_endpoint': ('hermes_cli.web_routers.profiles', 'open_profile_terminal_endpoint'),
    'parse_active_agents': ('gateway.status', 'parse_active_agents'),
    'pause_cron_job': ('hermes_cli.web_routers.cron', 'pause_cron_job'),
    'poll_oauth_session': ('hermes_cli.web_routers.oauth', 'poll_oauth_session'),
    'post_agent_plugin_disable': ('hermes_cli.web_routers.dashboard_ui', 'post_agent_plugin_disable'),
    'post_agent_plugin_enable': ('hermes_cli.web_routers.dashboard_ui', 'post_agent_plugin_enable'),
    'post_agent_plugin_install': ('hermes_cli.web_routers.dashboard_ui', 'post_agent_plugin_install'),
    'post_agent_plugin_update': ('hermes_cli.web_routers.dashboard_ui', 'post_agent_plugin_update'),
    'post_plugin_visibility': ('hermes_cli.web_routers.dashboard_ui', 'post_plugin_visibility'),
    'preview_skill_hub': ('hermes_cli.web_routers.skills', 'preview_skill_hub'),
    'prune_checkpoints': ('hermes_cli.web_routers.ops', 'prune_checkpoints'),
    'prune_sessions_endpoint': ('hermes_cli.web_routers.sessions', 'prune_sessions_endpoint'),
    'pty_ws': ('hermes_cli.web_routers.chat_ws', 'pty_ws'),
    'pub_ws': ('hermes_cli.web_routers.chat_ws', 'pub_ws'),
    'put_plugin_providers': ('hermes_cli.web_routers.dashboard_ui', 'put_plugin_providers'),
    'read_managed_file': ('hermes_cli.web_routers.files', 'read_managed_file'),
    'read_raw_config': ('hermes_cli.config', 'read_raw_config'),
    'read_runtime_status': ('gateway.status', 'read_runtime_status'),
    'recommended_update_command_for_method': ('hermes_cli.config', 'recommended_update_command_for_method'),
    'redact_key': ('hermes_cli.config', 'redact_key'),
    'remove_credential_pool_entry': ('hermes_cli.web_routers.ops', 'remove_credential_pool_entry'),
    'remove_env_value': ('hermes_cli.config', 'remove_env_value'),
    'remove_env_var': ('hermes_cli.web_routers.config_env', 'remove_env_var'),
    'remove_mcp_server': ('hermes_cli.web_routers.mcp', 'remove_mcp_server'),
    'rename_profile_endpoint': ('hermes_cli.web_routers.profiles', 'rename_profile_endpoint'),
    'rename_session_endpoint': ('hermes_cli.web_routers.sessions', 'rename_session_endpoint'),
    'replace_mcp_servers': ('hermes_cli.web_routers.mcp', 'replace_mcp_servers'),
    'rescan_dashboard_plugins': ('hermes_cli.web_routers.dashboard_ui', 'rescan_dashboard_plugins'),
    'reset_memory': ('hermes_cli.web_routers.ops', 'reset_memory'),
    'resolve_cron_model_drift_defaults': ('hermes_cli.config', 'resolve_cron_model_drift_defaults'),
    'resolve_gateway_liveness': ('gateway.status', 'resolve_gateway_liveness'),
    'restart_gateway': ('hermes_cli.web_routers.actions', 'restart_gateway'),
    'resume_cron_job': ('hermes_cli.web_routers.cron', 'resume_cron_job'),
    'reveal_env_var': ('hermes_cli.web_routers.config_env', 'reveal_env_var'),
    'revoke_pairing': ('hermes_cli.web_routers.ops', 'revoke_pairing'),
    'run_backup': ('hermes_cli.web_routers.ops', 'run_backup'),
    'run_config_migrate': ('hermes_cli.web_routers.status', 'run_config_migrate'),
    'run_curator': ('hermes_cli.web_routers.status', 'run_curator'),
    'run_debug_share_endpoint': ('hermes_cli.web_routers.status', 'run_debug_share_endpoint'),
    'run_doctor': ('hermes_cli.doctor', 'run_doctor'),
    'run_dump': ('hermes_cli.dump', 'run_dump'),
    'run_import': ('hermes_cli.web_routers.ops', 'run_import'),
    'run_import_upload': ('hermes_cli.web_routers.ops', 'run_import_upload'),
    'run_prompt_size': ('hermes_cli.web_routers.status', 'run_prompt_size'),
    'run_security_audit': ('hermes_cli.web_routers.ops', 'run_security_audit'),
    'run_toolset_post_setup': ('hermes_cli.web_routers.tools', 'run_toolset_post_setup'),
    'save_config': ('hermes_cli.config', 'save_config'),
    'save_env_value': ('hermes_cli.config', 'save_env_value'),
    'save_toolset_env': ('hermes_cli.web_routers.tools', 'save_toolset_env'),
    'scan_skill_hub': ('hermes_cli.web_routers.skills', 'scan_skill_hub'),
    'search_sessions': ('hermes_cli.web_routers.sessions', 'search_sessions'),
    'search_skills_hub': ('hermes_cli.web_routers.skills', 'search_skills_hub'),
    'select_terminal_backend': ('hermes_cli.web_routers.tools', 'select_terminal_backend'),
    'select_toolset_model': ('hermes_cli.web_routers.tools', 'select_toolset_model'),
    'select_toolset_provider': ('hermes_cli.web_routers.tools', 'select_toolset_provider'),
    'serve_plugin_asset': ('hermes_cli.web_routers.dashboard_ui', 'serve_plugin_asset'),
    'set_active_profile_endpoint': ('hermes_cli.web_routers.profiles', 'set_active_profile_endpoint'),
    'set_curator_paused': ('hermes_cli.web_routers.status', 'set_curator_paused'),
    'set_dashboard_font': ('hermes_cli.web_routers.dashboard_ui', 'set_dashboard_font'),
    'set_dashboard_theme': ('hermes_cli.web_routers.dashboard_ui', 'set_dashboard_theme'),
    'set_env_var': ('hermes_cli.web_routers.config_env', 'set_env_var'),
    'set_mcp_server_enabled': ('hermes_cli.web_routers.mcp', 'set_mcp_server_enabled'),
    'set_memory_provider': ('hermes_cli.web_routers.ops', 'set_memory_provider'),
    'set_moa_models': ('hermes_cli.web_routers.models', 'set_moa_models'),
    'set_model_assignment': ('hermes_cli.web_routers.models', 'set_model_assignment'),
    'set_webhook_enabled': ('hermes_cli.web_routers.ops', 'set_webhook_enabled'),
    'setup_memory_provider': ('hermes_cli.web_routers.memory_providers', 'setup_memory_provider'),
    'speak_stream_ws': ('hermes_cli.web_routers.audio', 'speak_stream_ws'),
    'speak_text': ('hermes_cli.web_routers.audio', 'speak_text'),
    'start_gateway': ('hermes_cli.web_routers.ops', 'start_gateway'),
    'start_oauth_login': ('hermes_cli.web_routers.oauth', 'start_oauth_login'),
    'start_telegram_onboarding': ('hermes_cli.web_routers.messaging', 'start_telegram_onboarding'),
    'start_whatsapp_onboarding': ('hermes_cli.web_routers.messaging', 'start_whatsapp_onboarding'),
    'stop_gateway': ('hermes_cli.web_routers.ops', 'stop_gateway'),
    'stream_managed_file': ('hermes_cli.web_routers.files', 'stream_managed_file'),
    'submit_oauth_code': ('hermes_cli.web_routers.oauth', 'submit_oauth_code'),
    'test_mcp_server': ('hermes_cli.web_routers.mcp', 'test_mcp_server'),
    'test_messaging_platform': ('hermes_cli.web_routers.messaging', 'test_messaging_platform'),
    'toggle_skill': ('hermes_cli.web_routers.skills', 'toggle_skill'),
    'toggle_toolset': ('hermes_cli.web_routers.tools', 'toggle_toolset'),
    'transcribe_audio_upload': ('hermes_cli.web_routers.audio', 'transcribe_audio_upload'),
    'trigger_cron_job': ('hermes_cli.web_routers.cron', 'trigger_cron_job'),
    'tts_lease': ('hermes_cli.web_routers.audio', 'tts_lease'),
    'uninstall_skill_hub': ('hermes_cli.web_routers.skills', 'uninstall_skill_hub'),
    'update_config': ('hermes_cli.web_routers.config_env', 'update_config'),
    'update_config_raw': ('hermes_cli.web_routers.analytics', 'update_config_raw'),
    'update_cron_job': ('hermes_cli.web_routers.cron', 'update_cron_job'),
    'update_hermes': ('hermes_cli.web_routers.actions', 'update_hermes'),
    'update_learning_node': ('hermes_cli.web_routers.status', 'update_learning_node'),
    'update_memory_provider_config': ('hermes_cli.web_routers.memory_providers', 'update_memory_provider_config'),
    'update_messaging_platform': ('hermes_cli.web_routers.messaging', 'update_messaging_platform'),
    'update_profile_description_endpoint': ('hermes_cli.web_routers.profiles', 'update_profile_description_endpoint'),
    'update_profile_model_endpoint': ('hermes_cli.web_routers.profiles', 'update_profile_model_endpoint'),
    'update_profile_soul': ('hermes_cli.web_routers.profiles', 'update_profile_soul'),
    'update_skill_content': ('hermes_cli.web_routers.skills', 'update_skill_content'),
    'update_skills_hub': ('hermes_cli.web_routers.skills', 'update_skills_hub'),
    'upload_chat_image': ('hermes_cli.web_routers.files', 'upload_chat_image'),
    'upload_managed_file': ('hermes_cli.web_routers.files', 'upload_managed_file'),
    'upload_managed_file_stream': ('hermes_cli.web_routers.files', 'upload_managed_file_stream'),
    'upsert_custom_endpoint': ('hermes_cli.web_routers.config_env', 'upsert_custom_endpoint'),
    'validate_custom_endpoint': ('hermes_cli.web_routers.config_env', 'validate_custom_endpoint'),
    'validate_provider_credential': ('hermes_cli.web_routers.config_env', 'validate_provider_credential'),
    'windows_detach_flags': ('hermes_cli._subprocess_compat', 'windows_detach_flags'),
    'windows_hide_flags': ('hermes_cli._subprocess_compat', 'windows_hide_flags'),
    'write_platform_config_field': ('hermes_cli.config', 'write_platform_config_field'),
}


def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
