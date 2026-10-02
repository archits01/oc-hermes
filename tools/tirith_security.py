"""Tirith pre-exec scanning. PM owns its optional pinned binary; exit codes
remain the verdict authority and operational failures obey fail_open."""

import json
import logging
import os
import shutil
<<<<<<< HEAD
import stat
import struct
=======
>>>>>>> upstream/main
import subprocess
import threading
import time
from contextvars import copy_context
from pathlib import Path

from hermes_constants import hermes_home_key

logger = logging.getLogger(__name__)
_REPO = "sheeki03/tirith"
# Only the release workflow may attest checksums, not arbitrary repo workflows.
_COSIGN_IDENTITY_REGEXP = f"^https://github.com/{_REPO}/\\.github/workflows/release\\.yml@refs/tags/v"
_COSIGN_ISSUER = "https://token.actions.githubusercontent.com"

# --- Config helpers ---
def _env_bool(key: str, default: bool) -> bool:
    val = os.getenv(key)
    return default if val is None else val.lower() in {"1", "true", "yes"}


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ[key])
    except (KeyError, ValueError):
        return default


def _load_security_config() -> dict:
    """Security settings from config.yaml, with env var overrides."""
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly().get("security", {}) or {}
    except Exception:
        cfg = {}
    return {
        "tirith_enabled": _env_bool("TIRITH_ENABLED", cfg.get("tirith_enabled", True)),
        "tirith_path": os.getenv("TIRITH_BIN", cfg.get("tirith_path", "tirith")),
        "tirith_timeout": _env_int("TIRITH_TIMEOUT", cfg.get("tirith_timeout", 5)),
        "tirith_fail_open": _env_bool("TIRITH_FAIL_OPEN", cfg.get("tirith_fail_open", True))}


# Circuit breaker: after _CRASH_LIMIT consecutive spawn/execution failures tirith is disabled so a broken
# binary can't turn every tool call into a fail-open retry loop (#41400). The breaker HALF-OPENS after
# _CIRCUIT_RETRY_S: one caller re-probes tirith for real, and any completed scan (exit 0/1/2 — allow/block/warn
# all prove the binary is healthy) closes it, while a failed probe re-arms the timer. Without the TTL this was
# a one-way latch: once open, the reset branch below was unreachable for the rest of the process.
# Thread safety: crash counting stays lock-free — a racing double-increment only opens the breaker one call
# early, which is harmless, and matches the mcp_tool.py error counters rather than the locked _warn_once
# pattern. _breaker_lock guards ONLY the half-open claim (TTL check + timestamp re-arm, nanoseconds); it is
# never held across the subprocess probe, so it cannot reintroduce the #41400 hang. Claiming re-arms
# _circuit_open_at first, so concurrent callers see a fresh TTL and stay fail-open: one probe per TTL window.
_CRASH_LIMIT = 3
_CIRCUIT_RETRY_S = 300  # half-open probe interval (seconds)
_crash_count: int = 0
_circuit_open: bool = False
_circuit_open_at: float = 0.0
_breaker_lock = threading.Lock()

# Warn-once: spawn/path warnings sit in the hot path and would otherwise repeat once per
# terminal command while tirith is unavailable.
_warned_messages: set[str] = set()
_warned_lock = threading.Lock()

def _record_tirith_crash() -> None:
    global _crash_count, _circuit_open, _circuit_open_at
    _crash_count += 1
    if _crash_count >= _CRASH_LIMIT:
        _circuit_open, _circuit_open_at = True, time.monotonic()
        logger.warning("tirith circuit breaker opened after %d consecutive failures; "
                       "disabling for %ds", _crash_count, _CIRCUIT_RETRY_S)


def _warn_once(key: str, message: str, *args) -> None:
    """``logger.warning`` at most once per ``key`` for the process lifetime."""
    with _warned_lock:
        if key in _warned_messages:
            return
        _warned_messages.add(key)
    logger.warning(message, *args)


<<<<<<< HEAD
def _reset_spawn_warning_state() -> None:
    """Clear the warn-once dedupe set. Called when tirith is freshly
    (re)installed so a subsequent failure surfaces again — e.g. user
    deletes the binary mid-session.
    """
    with _warned_lock:
        _warned_messages.clear()

# Disk-persistent failure marker — avoids retry across process restarts
_MARKER_TTL = 86400  # 24 hours


def _get_hermes_home() -> str:
    """Return the Hermes home directory, respecting HERMES_HOME env var."""
    return str(get_hermes_home())


def _failure_marker_path() -> str:
    """Return the path to the install-failure marker file."""
    return os.path.join(_get_hermes_home(), ".tirith-install-failed")


def _read_failure_reason() -> str | None:
    """Read the failure reason from the disk marker.

    Returns the reason string, or None if the marker doesn't exist or is
    older than _MARKER_TTL.
    """
    try:
        p = _failure_marker_path()
        mtime = os.path.getmtime(p)
        if (time.time() - mtime) >= _MARKER_TTL:
            return None
        with open(p, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def _is_install_failed_on_disk() -> bool:
    """Check if a recent install failure was persisted to disk.

    Returns False (allowing retry) when:
    - No marker exists
    - Marker is older than _MARKER_TTL (24h)
    - Marker reason is 'cosign_missing' and cosign is now on PATH
    """
    reason = _read_failure_reason()
    if reason is None:
        return False
    if reason == "cosign_missing" and shutil.which("cosign"):
        _clear_install_failed()
        return False
    return True


def _mark_install_failed(reason: str = ""):
    """Persist install failure to disk to avoid retry on next process.

    Args:
        reason: Short tag identifying the failure cause. Use "cosign_missing"
                when cosign is not on PATH so the marker can be auto-cleared
                once cosign becomes available.
    """
    try:
        p = _failure_marker_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(reason)
    except OSError:
        pass


def _clear_install_failed():
    """Remove the failure marker after successful install."""
    # Reset the warn-once dedupe set so a subsequent failure (e.g. user
    # deletes the binary) surfaces in the log again instead of being
    # silently suppressed by a stale dedupe key from before the fix.
    _reset_spawn_warning_state()
    try:
        os.unlink(_failure_marker_path())
    except OSError:
        pass


def _hermes_bin_dir() -> str:
    """Return $HERMES_HOME/bin, creating it if needed."""
    d = os.path.join(_get_hermes_home(), "bin")
    os.makedirs(d, exist_ok=True)
    return d


def _detect_target() -> str | None:
    """Return the Rust target triple for the current platform, or None.

    Windows is intentionally unsupported — tirith does not ship a Windows
    build. Callers should treat `None` as "this platform will never have
    tirith" and silently fall back to pattern-matching guards.
    """
    system = platform.system()
    machine = platform.machine().lower()

    # Android (Termux) is ABI-compatible with Linux — reuse Linux binaries.
    if system == "Darwin":
        plat = "apple-darwin"
    elif system in {"Linux", "Android"}:
        plat = "unknown-linux-gnu"
    else:
        return None

    if machine in {"x86_64", "amd64"}:
        arch = "x86_64"
    elif machine in {"aarch64", "arm64"}:
        arch = "aarch64"
    else:
        return None

    return f"{arch}-{plat}"


def is_platform_supported() -> bool:
    """True when tirith ships a prebuilt binary for this OS+arch.

    Used by callers (CLI banner, etc.) to distinguish "tirith failed to
    install" from "tirith was never going to install here" — the latter
    is silent because there is nothing the user can do about it.
    """
    return _detect_target() is not None


def _expected_elf_machine() -> int | None:
    """Return the ELF ``e_machine`` value for this host, when known.

    Tirith is distributed as native binaries.  ``os.access(path, X_OK)`` is
    not enough to establish that a cached executable can run: on Linux an
    x86-64 ELF copied onto an ARM host is still executable and is only
    rejected later by ``execve`` with ``ENOEXEC``.  Keep the mapping small and
    explicit; non-ELF formats (for example Mach-O or a script) are left to
    their normal interpreter/loader checks.
    """
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return 62  # EM_X86_64
    if machine in {"aarch64", "arm64"}:
        return 183  # EM_AARCH64
    return None


def _binary_is_compatible(path: str) -> bool:
    """Return whether a cached Tirith binary is compatible with this host.

    A non-ELF file is accepted so a user-provided script or a platform-native
    format remains supported.  If the file cannot be read, retain the old
    existence/access decision and let the subprocess report the operational
    failure; this avoids turning a transient read race into an install loop.
    """
    expected = _expected_elf_machine()
    if expected is None:
        return True
    try:
        with open(path, "rb") as handle:
            header = handle.read(20)
    except OSError:
        return True
    if not header.startswith(b"\x7fELF"):
        return True
    if len(header) < 20 or header[4] != 2:  # only 64-bit releases are shipped
        return False
    if header[5] == 1:
        endian = "<"
    elif header[5] == 2:
        endian = ">"
    else:
        return False
    try:
        machine = struct.unpack_from(endian + "H", header, 18)[0]
    except struct.error:
        return False
    return machine == expected


def _usable_binary(path: str) -> bool:
    """Check existence, execute permission, and host compatibility together."""
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        return False
    if _binary_is_compatible(path):
        return True
    logger.warning(
        "ignoring incompatible tirith binary %s for host architecture %s",
        path,
        platform.machine(),
    )
    return False


def _download_file(url: str, dest: str, timeout: int = 10):
    """Download a URL to a local file."""
    req = urllib.request.Request(url)
    from agent.secret_scope import get_secret
    token = get_secret("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"token {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


=======
>>>>>>> upstream/main
def _verify_cosign(checksums_path: str, sig_path: str, cert_path: str) -> bool | None:
    """Cosign provenance of checksums.txt: True verified, False rejected, None if cosign absent/failed."""
    if not (cosign := shutil.which("cosign")):
        logger.info("cosign not found on PATH")
        return None
    try:
        result = subprocess.run(
            [cosign, "verify-blob", "--certificate", cert_path, "--signature", sig_path,
             "--certificate-identity-regexp", _COSIGN_IDENTITY_REGEXP,
             "--certificate-oidc-issuer", _COSIGN_ISSUER, checksums_path],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("cosign execution failed: %s", exc)
        return None
    if result.returncode:
        logger.warning("cosign verification failed (exit %d): %s", result.returncode, result.stderr.strip())
        return False
    logger.info("cosign provenance verification passed")
    return True


def verify_release_provenance(directory: Path, log) -> tuple[bool, str]:
    """Verify PM-acquired checksum provenance; this function never downloads.

    Missing/broken cosign is optional; an explicit rejection is fatal.
    """
    if not shutil.which("cosign"):
        logger.info("cosign not on PATH — installing tirith with SHA-256 verification only")
        return False, ""
    checksums = directory / "checksums.txt"
    signature, certificate = directory / "checksums.txt.sig", directory / "checksums.txt.pem"
    if not signature.is_file() or not certificate.is_file():
        logger.info("cosign artifacts unavailable, proceeding with SHA-256 only")
        return False, ""
    verified = _verify_cosign(str(checksums), str(signature), str(certificate))
    if verified is False:
        log("tirith install aborted: cosign provenance verification failed")
        return False, "cosign_verification_failed"
    return verified is True, ""


# One non-blocking startup attempt per routed home. Durable selection, failure
# recovery, download locks and publication belong to PM, not disk markers here.
_install_lock = threading.Lock()
_install_threads: dict[str, threading.Thread] = {}
_install_attempted: set[str] = set()


def _claim_install_attempt() -> bool:
    """Share the one-attempt budget between cold scans and startup threads."""
    with _install_lock:
        home = hermes_home_key()
        if home in _install_attempted:
            return False
        _install_attempted.add(home)
        return True


def _install_in_flight() -> threading.Thread | None:
    thread = _install_threads.get(hermes_home_key())
    return thread if thread is not None and thread.is_alive() else None


def is_platform_supported() -> bool:
    """Whether PM has a managed Tirith build for this host."""
    import pm

    try:
        return pm.get_package("tirith").missing_reason(pm.current_target()) is None
    except RuntimeError:
        return False


def _local_tirith(configured_path: str) -> str | None:
    expanded = os.path.expanduser(configured_path)
    if configured_path != "tirith":
        return (expanded if os.path.isfile(expanded) and os.access(expanded, os.X_OK)
                else shutil.which(expanded))
    if external := shutil.which("tirith"):
        return external
    import pm

    selected = pm.installed_package("tirith")
    return str(selected.binary) if selected and selected.binary else None


def _resolve_tirith_path(configured_path: str) -> str:
<<<<<<< HEAD
    """Resolve the tirith binary path, auto-installing if necessary.

    If the user explicitly set a path (anything other than the bare "tirith"
    default), that path is authoritative — we never fall through to
    auto-download a different binary.

    For the default "tirith":
    1. PATH lookup via shutil.which
    2. $HERMES_HOME/bin/tirith (previously auto-installed)
    3. Auto-install from GitHub releases → $HERMES_HOME/bin/tirith

    Failed installs are cached for the process lifetime (and persisted to
    disk for 24h) to avoid repeated network attempts.
    """
    global _resolved_path, _install_failure_reason

    # Fast path: successfully resolved on a previous call.
    if _resolved_path is not None and _resolved_path is not _INSTALL_FAILED:
        if _usable_binary(str(_resolved_path)):
            return _resolved_path
        _resolved_path = None

    expanded = os.path.expanduser(configured_path)
    explicit = _is_explicit_path(configured_path)
    install_failed = _resolved_path is _INSTALL_FAILED

    # Platform has no tirith build (Windows etc.). Cache the verdict and
    # return the unexpanded configured path — the spawn loop will fail-open
    # via the dedupe'd OSError handler, but only after the first call; on
    # subsequent calls the fast-path above short-circuits before spawning.
    if not explicit and not is_platform_supported():
        _resolved_path = _INSTALL_FAILED
        _install_failure_reason = "unsupported_platform"
        return expanded

    # Explicit path: check it and stop. Never auto-download a replacement.
    if explicit:
        if _usable_binary(expanded):
            _resolved_path = expanded
            return expanded
        # Also try shutil.which in case it's a bare name on PATH
        found = shutil.which(expanded)
        if found and _usable_binary(found):
            _resolved_path = found
            return found
        logger.warning("Configured tirith path %r not found; scanning disabled", configured_path)
        _resolved_path = _INSTALL_FAILED
        _install_failure_reason = "explicit_path_missing"
        return expanded

    # Default "tirith" — always re-run cheap local checks so a manual
    # install is picked up even after a previous network failure (P2 fix:
    # long-lived gateway/CLI recovers without restart).
    found = shutil.which("tirith")
    if found and _usable_binary(found):
        _resolved_path = found
        _install_failure_reason = ""
        _clear_install_failed()
=======
    """Resolve for a scan; do not wait on a startup download already in flight."""
    if found := _local_tirith(configured_path):
>>>>>>> upstream/main
        return found
    if configured_path == "tirith":
        import pm

<<<<<<< HEAD
    hermes_bin = os.path.join(_hermes_bin_dir(), "tirith")
    if _usable_binary(hermes_bin):
        _resolved_path = hermes_bin
        _install_failure_reason = ""
        _clear_install_failed()
        return hermes_bin

    # Local checks failed.  If a previous install attempt already failed,
    # skip the network retry — UNLESS the failure was "cosign_missing" and
    # cosign is now available (retryable cause resolved in-process).
    if install_failed:
        if _install_failure_reason == "cosign_missing" and shutil.which("cosign"):
            # Retryable cause resolved — clear sentinel and fall through to retry
            _resolved_path = None
            _install_failure_reason = ""
            _clear_install_failed()
            install_failed = False
        else:
            return expanded

    # If a background install thread is running, don't start a parallel one —
    # return the configured path; the OSError handler in check_command_security
    # will apply fail_open until the thread finishes.
    if _install_thread is not None and _install_thread.is_alive():
        return expanded

    # Check disk failure marker before attempting network download.
    # Preserve the marker's real reason so in-memory retry logic can
    # detect retryable causes (e.g. cosign_missing) without restart.
    disk_reason = _read_failure_reason()
    if disk_reason is not None and _is_install_failed_on_disk():
        _resolved_path = _INSTALL_FAILED
        _install_failure_reason = disk_reason
        return expanded

    installed, reason = _install_tirith()
    if installed:
        _resolved_path = installed
        _install_failure_reason = ""
        _clear_install_failed()
        return installed

    # Install failed — cache the miss and persist reason to disk
    _resolved_path = _INSTALL_FAILED
    _install_failure_reason = reason
    _mark_install_failed(reason)
    return expanded


def _background_install(*, log_failures: bool = True):
    """Background thread target: download and install tirith."""
    global _resolved_path, _install_failure_reason
    with _install_lock:
        # Double-check after acquiring lock (another thread may have resolved)
        if _resolved_path is not None:
            if _resolved_path is _INSTALL_FAILED or _usable_binary(str(_resolved_path)):
                return
            _resolved_path = None

        # Re-check local paths (may have been installed by another process)
        found = shutil.which("tirith")
        if found and _usable_binary(found):
            _resolved_path = found
            _install_failure_reason = ""
            return

        hermes_bin = os.path.join(_hermes_bin_dir(), "tirith")
        if _usable_binary(hermes_bin):
            _resolved_path = hermes_bin
            _install_failure_reason = ""
            return

        installed, reason = _install_tirith(log_failures=log_failures)
        if installed:
            _resolved_path = installed
            _install_failure_reason = ""
            _clear_install_failed()
        else:
            _resolved_path = _INSTALL_FAILED
            _install_failure_reason = reason
            _mark_install_failed(reason)
=======
        if pm.lazy_installs_allowed():
            _start_background_install(log_failures=True)
    return os.path.expanduser(configured_path)


def _background_install(*, log_failures: bool) -> None:
    import pm

    try:
        pm.ensure("tirith")
    except Exception as exc:
        log = logger.warning if log_failures else logger.debug
        log("tirith install failed: %s", exc)
>>>>>>> upstream/main


def _start_background_install(*, log_failures: bool) -> None:
    if _claim_install_attempt():
        context = copy_context()
        thread = threading.Thread(
            target=context.run, args=(_background_install,),
            kwargs={"log_failures": log_failures}, daemon=True,
        )
        _install_threads[hermes_home_key()] = thread
        thread.start()


def ensure_installed(*, log_failures: bool = True, explicit: bool = False):
    """Opt-in startup is non-blocking. Explicit setup waits and reports errors.

    Explicit executable configuration remains authoritative, including a miss.
    Lazy refusal never starts a thread; already installed tools remain usable.
    """
    import pm

    cfg = _load_security_config()
    if not cfg["tirith_enabled"]:
        return None
<<<<<<< HEAD

    # Already resolved from a previous call
    if _resolved_path is not None and _resolved_path is not _INSTALL_FAILED:
        path = _resolved_path
        if _usable_binary(str(path)):
            return path
        _resolved_path = None
        return None

    # Platform has no tirith build (e.g. Windows) — don't probe PATH,
    # don't start a download thread, don't write a disk failure marker.
    # Pattern-matching guards still run; this path stays silent.
    if not is_platform_supported():
        _resolved_path = _INSTALL_FAILED
        _install_failure_reason = "unsupported_platform"
        return None

    configured_path = cfg["tirith_path"]
    explicit = _is_explicit_path(configured_path)
    expanded = os.path.expanduser(configured_path)

    # Explicit path: synchronous check only, no download
    if explicit:
        if _usable_binary(expanded):
            _resolved_path = expanded
            return expanded
        found = shutil.which(expanded)
        if found and _usable_binary(found):
            _resolved_path = found
            return found
        _resolved_path = _INSTALL_FAILED
        _install_failure_reason = "explicit_path_missing"
        return None

    # Default "tirith" — quick local checks first (no network)
    found = shutil.which("tirith")
    if found and _usable_binary(found):
        _resolved_path = found
        _install_failure_reason = ""
        _clear_install_failed()
        return found

    hermes_bin = os.path.join(_hermes_bin_dir(), "tirith")
    if _usable_binary(hermes_bin):
        _resolved_path = hermes_bin
        _install_failure_reason = ""
        _clear_install_failed()
        return hermes_bin

    # If previously failed in-memory, check if the cause is now resolved
    if _resolved_path is _INSTALL_FAILED:
        if _install_failure_reason == "cosign_missing" and shutil.which("cosign"):
            _resolved_path = None
            _install_failure_reason = ""
            _clear_install_failed()
        else:
            return None

    # Check disk failure marker (skip network attempt for 24h, unless
    # the cosign_missing reason was resolved — handled by _is_install_failed_on_disk).
    # Preserve the marker's real reason for in-memory retry logic.
    disk_reason = _read_failure_reason()
    if disk_reason is not None and _is_install_failed_on_disk():
        _resolved_path = _INSTALL_FAILED
        _install_failure_reason = disk_reason
=======
    configured = cfg["tirith_path"]
    if configured != "tirith":
        return _local_tirith(configured)
    if explicit:
        pm.ensure("tirith", explicit=True)
        selected = pm.installed_package("tirith")
        return str(selected.binary) if selected and selected.binary else None
    if found := _local_tirith(configured):
        return found
    if not is_platform_supported() or not pm.lazy_installs_allowed():
>>>>>>> upstream/main
        return None
    _start_background_install(log_failures=log_failures)
    return None


def missing_is_expected() -> bool:
    """Whether an unresolved default tirith is by design rather than a fault.

    The first launch after a PM install starts the download in the background,
    and a lazy-install policy refusal is the operator's choice; neither is
    actionable. A missing explicit ``tirith_path`` always is.
    """
    import pm

    configured = _load_security_config()["tirith_path"]
    if configured != "tirith":
        return False
    if _install_in_flight():
        return True
    return _local_tirith(configured) is not None or not pm.lazy_installs_allowed()


# --- Main API ---
_MAX_FINDINGS = 50
_MAX_SUMMARY_LEN = 500
_EXIT_ACTIONS = {0: "allow", 1: "block", 2: "warn"}
# Summary when tirith's JSON is unparseable and only the exit code is known.
_NO_DETAILS_SUMMARY = {
    "block": "security issue detected (details unavailable)",
    "warn": "security warning detected (details unavailable)"}
_VARIATION_SELECTOR_16 = "\ufe0f"
# Code points that carry the Unicode ``Emoji`` property and take VS16 for emoji presentation: the
# Miscellaneous Symbols / Dingbats blocks, the SMP emoji planes, and the BMP singletons outside them
# (©️ ®️ ‼️ ⁉️ ™️ ℹ️ arrows, ⌚ ⌨️ ⏏️ media keys, Ⓜ️ ▪️ ▶️ ◀️ ◻️ ⤴️ ⬅️ ⬛ ⭐ ⭕ 〰️ 〽️ ㊗️ ㊙️).
# Digits, ``#`` and ``*`` also carry the property (keycap bases) but are deliberately absent: VS16
# after a letter or digit is exactly the steganography signal the rule exists for.
_EMOJI_PRESENTATION_BASE_RANGES = (
    (0x00A9, 0x00A9), (0x00AE, 0x00AE), (0x203C, 0x203C), (0x2049, 0x2049), (0x2122, 0x2122),
    (0x2139, 0x2139), (0x2194, 0x2199), (0x21A9, 0x21AA), (0x231A, 0x231B), (0x2328, 0x2328),
    (0x23CF, 0x23CF), (0x23E9, 0x23F3), (0x23F8, 0x23FA), (0x24C2, 0x24C2), (0x25AA, 0x25AB),
    (0x25B6, 0x25B6), (0x25C0, 0x25C0), (0x25FB, 0x25FE), (0x2600, 0x27BF), (0x2934, 0x2935),
    (0x2B05, 0x2B07), (0x2B1B, 0x2B1C), (0x2B50, 0x2B50), (0x2B55, 0x2B55), (0x3030, 0x3030),
    (0x303D, 0x303D), (0x3297, 0x3297), (0x3299, 0x3299), (0x1F000, 0x1FAFF))


def _verdict(action: str, summary: str = "", findings: list | None = None) -> dict:
    return {"action": action, "findings": [] if findings is None else findings, "summary": summary}


def _fail(fail_open: bool, open_summary: str, closed_summary: str) -> dict:
    return _verdict("allow", open_summary) if fail_open else _verdict("block", closed_summary)


def _crash(fail_open: bool, open_summary: str, closed_summary: str) -> dict:
    """An operational failure: count it toward the circuit breaker, then fail open/closed."""
    _record_tirith_crash()
    return _fail(fail_open, open_summary, closed_summary)


def check_command_security(command: str) -> dict:
    """Run the tirith scan on a command -> ``{"action": allow|warn|block, "findings", "summary"}``.
    Exit code determines the action; JSON enriches. Spawn failures/timeouts respect fail_open."""
    global _crash_count, _circuit_open, _circuit_open_at
    cfg = _load_security_config()
    if not cfg["tirith_enabled"]:
        return _verdict("allow")
    # Circuit breaker: if tirith has crashed _CRASH_LIMIT times in a row, stop trying and fail open (issue
    # #41400). After _CIRCUIT_RETRY_S the breaker half-opens: exactly one caller claims the probe slot —
    # claiming re-arms _circuit_open_at under _breaker_lock, so concurrent callers see a fresh TTL and stay
    # fail-open — and falls through to a real scan below.
    if _circuit_open:
        with _breaker_lock:
            if _circuit_open and time.monotonic() - _circuit_open_at < _CIRCUIT_RETRY_S:
                return _verdict("allow", "tirith disabled (circuit breaker)")
            if _circuit_open:  # TTL expired: claim the single-flight probe slot for this window
                _circuit_open_at = time.monotonic()
                logger.info("tirith circuit breaker half-open: probing after %ds", _CIRCUIT_RETRY_S)
    # No binary for this platform, ever: skip the resolver so we never spawn.
    if cfg["tirith_path"] == "tirith" and not is_platform_supported():
        return _verdict("allow")
    tirith_path = _resolve_tirith_path(cfg["tirith_path"])
    timeout, fail_open = cfg["tirith_timeout"], cfg["tirith_fail_open"]
    if tirith_path is None:
        _warn_once("tirith_path_none", "tirith path resolved to None; scanning disabled")
        return _fail(fail_open, "tirith path unavailable", "tirith path unavailable (fail-closed)")
    if tirith_path == "tirith" and (install := _install_in_flight()):
        if fail_open:
            return _verdict("allow", "tirith installing")
        install.join()
        tirith_path = _resolve_tirith_path(cfg["tirith_path"])
    try:
        result = subprocess.run(
            [tirith_path, "check", "--json", "--non-interactive", "--shell", "posix", "--", command],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout,
            stdin=subprocess.DEVNULL)
    except OSError as exc:
        # FileNotFoundError / PermissionError / exec format error: dedupe by (class, errno)
        # so each failure mode surfaces once, not per command.
        _warn_once(f"tirith_spawn_failed:{type(exc).__name__}:{getattr(exc, 'errno', '')}",
                   "tirith spawn failed: %s", exc)
        return _crash(fail_open, f"tirith unavailable: {exc}", f"tirith spawn failed (fail-closed): {exc}")
    except subprocess.TimeoutExpired:
        _warn_once(f"tirith_timeout:{timeout}", "tirith timed out after %ds", timeout)
        return _crash(fail_open, f"tirith timed out ({timeout}s)", "tirith timed out (fail-closed)")
    exit_code = result.returncode
    if (action := _EXIT_ACTIONS.get(exit_code)) is None:
        # Unknown exit code (includes signal-killed, e.g. -11): respect fail_open.
        logger.warning("tirith returned unexpected exit code %d", exit_code)
        return _crash(fail_open, f"tirith exit code {exit_code} (fail-open)",
                      f"tirith exit code {exit_code} (fail-closed)")
    # Any completed scan (allow/block/warn) proves the binary is healthy: clear the streak and close the
    # breaker. This is the half-open probe's recovery path, and it also fixes the streak never resetting on
    # block/warn verdicts.
    _crash_count = 0
    if _circuit_open:
        _circuit_open, _circuit_open_at = False, 0.0
        logger.info("tirith circuit breaker closed after successful probe")
    # JSON enriches findings/summary; a parse failure never changes the verdict.
    findings, summary = [], ""
    try:
        data = json.loads(result.stdout) if result.stdout.strip() else {}
        findings = data.get("findings", [])[:_MAX_FINDINGS]
        summary = (data.get("summary", "") or "")[:_MAX_SUMMARY_LEN]
    except (json.JSONDecodeError, AttributeError):
        logger.debug("tirith JSON parse failed, using exit code only")
        summary = _NO_DETAILS_SUMMARY.get(action, "")
    # .app is a legitimate gTLD: a warn consisting solely of lookalike_tld findings for .app is a
    # known false positive and is downgraded to allow. Any other finding keeps the warn.
    if action == "warn" and findings and all(_is_app_tld_finding(f) for f in findings):
        return _verdict("allow")
    # VS16 follows ordinary emoji-capable code points in standard emoji-presentation sequences.
    # Preserve warnings for every other selector, including VS16 after text, because those can
    # carry the steganographic payload that Tirith is intended to detect.
    if action == "warn" and findings and all(_is_emoji_variation_selector_finding(f) for f in findings) \
            and _has_only_emoji_presentation_selectors(command):
        return _verdict("allow")
    return _verdict(action, summary, findings)


def _is_app_tld_finding(finding: dict) -> bool:
    """True if this finding is a lookalike_tld warning for the .app TLD only."""
    if not isinstance(finding, dict) or finding.get("rule_id") != "lookalike_tld":
        return False
    return any(
        val is not None and ".app" in str(val).lower()
        for val in (finding.get(k) for k in ("value", "tld", "detail", "description", "message")))


def _is_emoji_variation_selector_finding(finding: dict) -> bool:
    """True only for the Tirith rule that reports variation selectors."""
    return isinstance(finding, dict) and finding.get("rule_id") == "variation_selector"


def _has_only_emoji_presentation_selectors(command: str) -> bool:
    """Whether every variation selector is VS16 immediately after an emoji-capable base."""
    selectors = ("\ufe00", "\U000e0100")
    saw_selector = False
    for idx, char in enumerate(command):
        if not selectors[0] <= char <= "\ufe0f" and not selectors[1] <= char <= "\U000e01ef":
            continue
        saw_selector = True
        if char != _VARIATION_SELECTOR_16 or idx == 0:
            return False
        base = ord(command[idx - 1])
        if not any(start <= base <= end for start, end in _EMOJI_PRESENTATION_BASE_RANGES):
            return False
    return saw_selector
