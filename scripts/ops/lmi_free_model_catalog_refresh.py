#!/usr/bin/env python3
"""Refresh the verified, keyless OpenCode Free model catalog.

This job deliberately does *not* trust a model's pricing metadata.  A model
only reaches the picker after the OpenCode ``/models`` endpoint lists it and a
one-token anonymous request succeeds using Hermes' own headers.  It is safe to
run daily from a profile-scoped cron because the cache follows ``HERMES_HOME``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_cli.models import (  # noqa: E402
    _OPENCODE_FREE_STATIC_MODELS,
    OPENCODE_FREE_CATALOG_MAX_MODELS,
    _read_opencode_free_catalog,
    _valid_opencode_free_model_id,
    clear_verified_opencode_free_catalog,
    get_fresh_opencode_free_catalog_snapshot,
    get_stored_opencode_free_model_ids,
    get_verified_opencode_free_model_ids,
    opencode_model_api_mode,
    opencode_zen_free_headers,
    write_verified_opencode_free_catalog,
)


DEFAULT_BASE_URL = "https://opencode.ai/zen/v1"
_TRANSIENT_STATUS_CODES = frozenset({408, 425, 429})
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_DISCOVERED_MODELS = 1000
MAX_PROBE_CANDIDATES = min(32, OPENCODE_FREE_CATALOG_MAX_MODELS)


def _headers() -> dict[str, str]:
    """Use exactly the keyless runtime identity plus request metadata."""
    headers = opencode_zen_free_headers()
    headers.update({
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    return headers


def _is_transient_status(status: int) -> bool:
    return status in _TRANSIENT_STATUS_CODES or 500 <= status <= 599


def _request_json(
    request: urllib.request.Request,
    *,
    timeout: float,
) -> tuple[str, int, Any]:
    """Return ``(outcome, status, payload)`` without leaking response bodies.

    ``outcome`` is ``success``, ``transient``, or ``definitive``.  A provider
    auth/validation rejection is definitive; timeouts, 429s and 5xx responses
    are deliberately non-destructive because they do not prove a promotion
    ended.
    """
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", response.getcode()))
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            return "definitive", status, None
        if not 200 <= status < 300:
            return ("transient" if _is_transient_status(status) else "definitive", status, None)
        try:
            return "success", status, json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return "definitive", status, None
    except urllib.error.HTTPError as exc:
        return ("transient" if _is_transient_status(exc.code) else "definitive", exc.code, None)
    except (urllib.error.URLError, TimeoutError, OSError):
        return "transient", 0, None


def fetch_open_code_models(base_url: str, *, timeout: float) -> tuple[str, list[str]]:
    """Fetch bare IDs from the documented OpenAI-compatible ``/models`` route."""
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/models", headers=_headers(), method="GET"
    )
    outcome, _status, payload = _request_json(request, timeout=timeout)
    if outcome != "success" or not isinstance(payload, dict):
        return outcome, []
    data = payload.get("data")
    if not isinstance(data, list) or len(data) > MAX_DISCOVERED_MODELS:
        return "definitive", []
    result: list[str] = []
    seen: set[str] = set()
    for item in data:
        model_id = item.get("id") if isinstance(item, dict) else None
        if not _valid_opencode_free_model_id(model_id):
            continue
        key = model_id.lower()
        if key not in seen:
            result.append(model_id)
            seen.add(key)
    return "success", result


def conservative_candidates(discovered: list[str]) -> list[str]:
    """Keep only known-good IDs or explicitly ``-free`` promotions.

    Being shown by ``/models`` is not price proof.  The pre-existing static
    verified list and a still-valid previous verification are the only
    non-suffix sources that can become probe candidates.
    """
    known = {
        model.lower()
        for model in (*_OPENCODE_FREE_STATIC_MODELS, *get_stored_opencode_free_model_ids())
    }
    candidates = [
        model for model in discovered
        if model.lower() in known or model.lower().endswith("-free")
    ]
    return candidates[:MAX_PROBE_CANDIDATES]


def probe_anonymous_model(base_url: str, model_id: str, *, timeout: float) -> str:
    """Verify one candidate via the same per-model wire Hermes uses."""
    mode = opencode_model_api_mode("opencode-free", model_id)
    if mode == "codex_responses":
        endpoint = f"{base_url.rstrip('/')}/responses"
        payload = {
            "model": model_id,
            "input": "ping",
            "max_output_tokens": 1,
            "stream": False,
        }
    elif mode == "anthropic_messages":
        # The current keyless runtime uses the OpenCode placeholder as an
        # Anthropic x-api-key. Until runtime and provider document an anonymous
        # Anthropic wire, fail closed instead of verifying a route the actual
        # agent would call differently.
        return "definitive"
    else:
        endpoint = f"{base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": model_id,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "stream": False,
        }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers=_headers(),
        method="POST",
    )
    outcome, _status, payload = _request_json(request, timeout=timeout)
    if outcome != "success" or not isinstance(payload, dict) or payload.get("error"):
        return "definitive" if outcome == "success" else outcome
    if mode == "codex_responses":
        response_id = payload.get("id")
        output = payload.get("output")
        valid_output = (
            isinstance(output, list)
            and bool(output)
            and any(
                isinstance(item, dict)
                and item.get("type") == "message"
                and isinstance(item.get("content"), list)
                and any(
                    isinstance(content, dict)
                    and content.get("type") == "output_text"
                    and isinstance(content.get("text"), str)
                    and content["text"].strip()
                    for content in item["content"]
                )
                for item in output
            )
        )
        if isinstance(response_id, str) and response_id.strip() and valid_output:
            return "success"
        return "definitive"
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return "definitive"
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return "definitive"
    role = str(message.get("role") or "").strip().lower()
    content = message.get("content")
    if role != "assistant" or not (isinstance(content, str) and content.strip()):
        return "definitive"
    return "success"


def _refresh_picker_cache(models: list[str]) -> None:
    """Warm Hermes' normal picker cache after a verified replacement only."""
    try:
        from hermes_cli.models import update_provider_cache_entry

        update_provider_cache_entry("opencode-free", models)
    except Exception:
        # The verified cache has already been atomically written.  A warm
        # picker cache is an optimisation, never a reason to fail the cron.
        return


def refresh_catalog(base_url: str = DEFAULT_BASE_URL, *, timeout: float = 10.0) -> dict[str, Any]:
    """Probe and atomically publish a safe OpenCode Free catalog.

    On any transient discovery/probe failure, retain the previous catalog only
    when its original verification is still inside the runtime max-age window.
    We intentionally do not rewrite it, so a 429 can never extend its trust.
    """
    normalized_base = base_url.strip().rstrip("/")
    if not normalized_base.startswith("https://"):
        raise ValueError("OpenCode Free refresh requires an https endpoint")

    previous_snapshot = get_fresh_opencode_free_catalog_snapshot()
    previous = previous_snapshot[0] if previous_snapshot else []
    previous_verified_at = previous_snapshot[1] if previous_snapshot else None
    discovery_outcome, discovered = fetch_open_code_models(normalized_base, timeout=timeout)
    if discovery_outcome != "success":
        if discovery_outcome == "transient" and previous:
            _refresh_picker_cache(previous)
            return {"status": "retained_transient", "models": len(previous)}
        if discovery_outcome == "definitive":
            # A malformed or rejected authoritative discovery response proves
            # the old cache cannot remain a managed availability signal.
            clear_verified_opencode_free_catalog()
            return {"status": "definitive_discovery_failed", "models": 0}
        return {"status": "unavailable", "models": 0}

    candidates = conservative_candidates(discovered)
    accepted: list[str] = []
    retained_transient: list[str] = []
    previous_by_key = {model.lower(): model for model in previous}
    for model_id in candidates:
        outcome = probe_anonymous_model(normalized_base, model_id, timeout=timeout)
        if outcome == "success":
            accepted.append(model_id)
        elif outcome == "transient" and model_id.lower() in previous_by_key:
            # Retain only rediscovered, previously verified models.  Omitted
            # rows and definitive rejections never survive this refresh.
            retained_transient.append(previous_by_key[model_id.lower()])

    if accepted:
        # One catalog timestamp cannot safely represent new proof alongside a
        # transient old proof.  Publish only newly verified rows; dropping the
        # transient subset is conservative and cannot extend its TTL.
        verified_at = time.time()
        write_verified_opencode_free_catalog(
            accepted, verified_at=verified_at, source=normalized_base
        )
        _refresh_picker_cache(accepted)
        return {"status": "updated", "models": len(accepted)}
    if retained_transient and previous_verified_at is not None:
        write_verified_opencode_free_catalog(
            retained_transient,
            verified_at=previous_verified_at,
            source=normalized_base,
        )
        _refresh_picker_cache(retained_transient)
        return {"status": "retained_transient", "models": len(retained_transient)}
    if not accepted:
        # A complete, definitive empty result must not replace a useful
        # fallback with an empty row.  Once any old verification expires the
        # runtime automatically falls back to the in-repo static list.
        # A successful catalog read plus definitive model rejections proves
        # the old cache is no longer acceptable.  Remove it rather than
        # accidentally extending managed-picker availability until its TTL.
        clear_verified_opencode_free_catalog()
        return {"status": "no_verified_models", "models": 0}



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    result = refresh_catalog(args.base_url, timeout=args.timeout)
    print(json.dumps(result, sort_keys=True))
    # A transient outage is not a broken runtime: the static or bounded prior
    # catalog remains active.  Cron observability can key off the status.
    return 0 if result["status"] != "unavailable" else 1


if __name__ == "__main__":
    raise SystemExit(main())
