#!/usr/bin/env python3
"""Refresh the verified, keyless OpenCode Free model catalog.

This job deliberately does *not* trust a model's pricing metadata.  A model
only reaches the picker after the OpenCode ``/models`` endpoint lists it and a
a bounded anonymous request returns assistant output using Hermes' own headers.  It is safe to
run daily from a profile-scoped cron because the cache follows ``HERMES_HOME``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_cli.models import (  # noqa: E402
    _OPENCODE_FREE_STATIC_MODELS,
    _OPENCODE_KEYLESS_EXTRA_SLUGS,
    OPENCODE_FREE_CATALOG_MAX_MODELS,
    _read_opencode_free_catalog,
    _valid_opencode_free_model_id,
    clear_opencode_free_discovery_catalog,
    clear_verified_opencode_free_catalog,
    get_fresh_opencode_free_catalog_snapshot,
    opencode_free_catalog_path,
    get_stored_opencode_free_model_ids,
    get_verified_opencode_free_model_ids,
    opencode_model_api_mode,
    opencode_zen_free_headers,
    write_opencode_free_discovery_catalog,
    write_nous_free_catalog,
    write_openrouter_free_catalog,
    write_verified_opencode_free_catalog,
)


DEFAULT_BASE_URL = "https://opencode.ai/zen/v1"
_TRANSIENT_STATUS_CODES = frozenset({408, 425, 429})
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_DISCOVERED_MODELS = 1000
MAX_PROBE_CANDIDATES = min(32, OPENCODE_FREE_CATALOG_MAX_MODELS)
MAX_PROBE_ATTEMPTS = 2
PROBE_RETRY_DELAY_SECONDS = 1.0
RESPONSES_PROBE_MAX_OUTPUT_TOKENS = 512


def _headers() -> dict[str, str]:
    """Use OpenCode auth/session headers when available, else anonymous."""
    headers = opencode_zen_free_headers(
        session_id=f"ses_{uuid.uuid4().hex}",
        request_id=f"req_{uuid.uuid4().hex}",
    )
    headers.update({
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    return headers


def _is_transient_status(status: int) -> bool:
    return status in _TRANSIENT_STATUS_CODES or 500 <= status <= 599


def _provider_error_outcome(payload: Any, *, status: int = 0) -> str:
    """Classify an error envelope without treating infrastructure as delisting.

    OpenCode sometimes returns HTTP 2xx with an error object, and the
    Responses stream can terminate as incomplete when the reasoning budget is
    exhausted. Only explicit model/auth/contract failures are definitive;
    infrastructure, quota, and ambiguous envelopes remain transient.
    """
    if not isinstance(payload, dict):
        return "definitive"
    error = payload.get("error")
    if not isinstance(error, dict):
        return "definitive"
    error_type = str(error.get("type") or "").strip().lower()
    message = str(error.get("message") or "").strip().lower()
    definitive_markers = (
        "model is unavailable",
        "model not found",
        "model_not_found",
        "model is not supported",
        "not supported",
        "invalid api key",
        "authentication failed",
        "unauthorized",
        "forbidden",
    )
    if any(marker in message for marker in definitive_markers):
        return "definitive"
    transient_markers = (
        "internal server error",
        "endpoint is unavailable",
        "rate limit",
        "try again",
        "temporarily",
        "overload",
        "timeout",
        "upstream request failed",
    )
    if status >= 500 or error_type in {
        "server_error",
        "internal_server_error",
        "ratelimiterror",
        "rate_limit",
        "freeusagelimiterror",
        "timeout",
    } or any(marker in message for marker in transient_markers):
        return "transient"
    return "definitive"


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
    extra_free = {str(model).lower() for model in _OPENCODE_KEYLESS_EXTRA_SLUGS}
    candidates = [
        model for model in discovered
        if model.lower() in known
        or model.lower().endswith("-free")
        or model.lower() in extra_free
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
            "max_output_tokens": RESPONSES_PROBE_MAX_OUTPUT_TOKENS,
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
            # Reasoning-capable chat models may spend the first tokens on
            # hidden reasoning; leave enough room to prove visible assistant text.
            "max_tokens": 256,
            "stream": False,
        }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers=_headers(),
        method="POST",
    )
    outcome, status, payload = _request_json(request, timeout=timeout)
    if outcome != "success" or not isinstance(payload, dict):
        # Responses-only models may reject the non-streaming shape with a
        # definitive 4xx even though their streaming endpoint is healthy.
        if mode == "codex_responses" and status in (400, 404, 405, 422):
            return probe_responses_stream(base_url, model_id, timeout=timeout)
        return "definitive" if outcome == "success" else outcome
    if payload.get("error"):
        # Some relays return HTTP 200 with an infrastructure/quota error
        # envelope. Probe the runtime stream for Responses models, but retain
        # an explicit transient classification instead of delisting a model.
        if mode == "codex_responses":
            streamed = probe_responses_stream(base_url, model_id, timeout=timeout)
            if streamed == "success":
                return streamed
            return (
                "transient"
                if _provider_error_outcome(payload, status=status) == "transient"
                else streamed
            )
        return _provider_error_outcome(payload, status=status)
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
        return probe_responses_stream(base_url, model_id, timeout=timeout)
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return "definitive"
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return "definitive"
    role = str(message.get("role") or "").strip().lower()
    content = message.get("content")
    if role != "assistant":
        return "definitive"
    if isinstance(content, str) and content.strip():
        return "success"
    # A reasoning-capable chat model can exhaust the bounded visible-output
    # budget while still proving that the endpoint is alive. Retry/retain it;
    # do not call that an ended promotion.
    if isinstance(message.get("reasoning"), str) and message["reasoning"].strip():
        return "transient"
    return "definitive"


def probe_responses_stream(base_url: str, model_id: str, *, timeout: float) -> str:
    """Check the streaming Responses wire used by the runtime.

    Some relay models return an empty non-streaming envelope but emit real
    assistant text on the streaming endpoint. Require text plus a terminal
    response.completed event before accepting the model.
    """
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/responses",
        data=json.dumps({
            "model": model_id,
            "input": "Reply OK",
            "max_output_tokens": RESPONSES_PROBE_MAX_OUTPUT_TOKENS,
            "stream": True,
        }).encode("utf-8"),
        headers=_headers(),
        method="POST",
    )
    size = 0
    has_text = False
    deadline = time.monotonic() + timeout
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            while time.monotonic() < deadline:
                raw = response.readline(MAX_RESPONSE_BYTES - size + 1)
                if not raw:
                    break
                size += len(raw)
                if size > MAX_RESPONSE_BYTES:
                    return "definitive"
                if not raw.startswith(b"data:"):
                    continue
                try:
                    event = json.loads(raw[5:].strip())
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get("type")
                if kind == "response.output_text.delta":
                    delta = event.get("delta")
                    has_text |= isinstance(delta, str) and bool(delta.strip())
                if kind in ("error", "response.failed", "response.incomplete"):
                    response = event.get("response")
                    response = response if isinstance(response, dict) else {}
                    details = response.get("incomplete_details")
                    reason = details.get("reason") if isinstance(details, dict) else ""
                    if reason == "max_output_tokens":
                        return "transient"
                    error = response.get("error")
                    if not isinstance(error, dict):
                        error = event.get("error")
                    if isinstance(error, dict):
                        classified = _provider_error_outcome({"error": error})
                        if classified == "definitive":
                            return "definitive"
                    # An incomplete/failed stream without an explicit
                    # model/auth rejection is ambiguous infrastructure.
                    return "transient"
                if kind == "response.completed":
                    return "success" if has_text else "definitive"
        return "transient"
    except urllib.error.HTTPError as exc:
        return "transient" if _is_transient_status(exc.code) else "definitive"
    except (urllib.error.URLError, TimeoutError, OSError):
        return "transient"


def probe_candidate_with_retries(
    base_url: str, model_id: str, *, timeout: float, attempts: int = MAX_PROBE_ATTEMPTS
) -> str:
    """Retry only inconclusive provider failures before excluding a candidate.

    A transient timeout/429 is not proof that a free promotion ended. Keep the
    retry bounded so the daily job cannot loop or turn a provider outage into a
    large request burst; definitive rejects still stop immediately.
    """
    bounded_attempts = max(1, min(int(attempts), MAX_PROBE_ATTEMPTS))
    outcome = "transient"
    for attempt in range(bounded_attempts):
        outcome = probe_anonymous_model(base_url, model_id, timeout=timeout)
        if outcome != "transient" or attempt + 1 >= bounded_attempts:
            return outcome
        time.sleep(PROBE_RETRY_DELAY_SECONDS)
    return outcome


def _refresh_picker_cache(models: list[str]) -> None:
    """Warm Hermes' normal picker cache after a verified replacement only."""
    try:
        from hermes_cli.models import update_provider_cache_entry

        update_provider_cache_entry("opencode-free", models)
    except Exception:
        # The verified cache has already been atomically written.  A warm
        # picker cache is an optimisation, never a reason to fail the cron.
        return


def _refresh_nous_free_catalog() -> int | None:
    """Cache the Nous/Portal free recommendations for disabled picker rows."""
    try:
        from hermes_cli.models import fetch_nous_recommended_models

        payload = fetch_nous_recommended_models()
        if not isinstance(payload, dict):
            return None
        items: list[dict] = []
        recommended = payload.get("freeRecommendedModels")
        if isinstance(recommended, list):
            items.extend(item for item in recommended if isinstance(item, dict))
        for key in ("freeRecommendedCompactionModel", "freeRecommendedVisionModel"):
            item = payload.get(key)
            if isinstance(item, dict):
                items.append(item)
        ids: list[str] = []
        seen: set[str] = set()
        for item in items:
            model_id = item.get("modelName")
            price = str(item.get("tokenPrice") or "").strip().lower()
            if not isinstance(model_id, str) or not model_id.strip() or "$0.00" not in price:
                continue
            if model_id.lower() not in seen:
                ids.append(model_id)
                seen.add(model_id.lower())
        if not ids:
            return None
        write_nous_free_catalog(ids, discovered_at=time.time())
        return len(ids)
    except Exception:
        # A Portal refresh outage must not erase the last bounded discovery;
        # its expiry still prevents stale models from remaining visible.
        return None


def _refresh_openrouter_free_catalog() -> int | None:
    """Cache live OpenRouter zero-price IDs for disabled picker rows."""
    try:
        from hermes_cli.models import fetch_openrouter_models, get_pricing_for_provider

        rows = fetch_openrouter_models()
        pricing = get_pricing_for_provider("openrouter") or {}
        ids: list[str] = []
        for model_id, _description in rows:
            price = pricing.get(model_id) or {}
            try:
                is_free = float(price.get("prompt", 1)) == 0 and float(price.get("completion", 1)) == 0
            except (TypeError, ValueError):
                is_free = False
            if is_free and model_id not in ids:
                ids.append(model_id)
        if not ids:
            return None
        write_openrouter_free_catalog(ids, discovered_at=time.time())
        return len(ids)
    except Exception:
        return None


def refresh_catalog(base_url: str = DEFAULT_BASE_URL, *, timeout: float = 10.0) -> dict[str, Any]:
    """Probe and atomically publish a safe OpenCode Free catalog.

    On any transient discovery/probe failure, retain the previous catalog only
    when its original verification is still inside the runtime max-age window.
    We intentionally do not rewrite it, so a 429 can never extend its trust.
    """
    normalized_base = base_url.strip().rstrip("/")
    if not normalized_base.startswith("https://"):
        raise ValueError("OpenCode Free refresh requires an https endpoint")

    nous_models = _refresh_nous_free_catalog()
    openrouter_models = _refresh_openrouter_free_catalog()

    previous_snapshot = get_fresh_opencode_free_catalog_snapshot()
    previous = previous_snapshot[0] if previous_snapshot else []
    previous_verified_at = previous_snapshot[1] if previous_snapshot else None
    previous_item_verified_at: dict[str, float] = {}
    if previous:
        try:
            with opencode_free_catalog_path().open(encoding="utf-8") as handle:
                previous_payload = json.load(handle)
            for item in previous_payload.get("models", []):
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    stamp = item.get("verified_at")
                    if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and stamp > 0:
                        previous_item_verified_at[item["id"].lower()] = float(stamp)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            previous_item_verified_at = {}
    discovery_outcome, discovered = fetch_open_code_models(normalized_base, timeout=timeout)
    if discovery_outcome != "success":
        if discovery_outcome == "transient" and previous:
            _refresh_picker_cache(previous)
            return {"status": "retained_transient", "models": len(previous)}
        if discovery_outcome == "definitive":
            # A malformed or rejected authoritative discovery response proves
            # the old cache cannot remain a managed availability signal.
            clear_verified_opencode_free_catalog()
            clear_opencode_free_discovery_catalog()
            return {"status": "definitive_discovery_failed", "models": 0}
        return {"status": "unavailable", "models": 0}

    candidates = conservative_candidates(discovered)
    # Keep advertised free IDs visible as disabled picker rows even when a
    # provider probe rejects them. This is discovery, not runtime proof.
    write_opencode_free_discovery_catalog(
        candidates, discovered_at=time.time(), source=normalized_base
    )
    accepted: list[str] = []
    retained_transient: list[str] = []
    previous_by_key = {model.lower(): model for model in previous}
    for model_id in candidates:
        outcome = probe_candidate_with_retries(normalized_base, model_id, timeout=timeout)
        if outcome == "success":
            accepted.append(model_id)
        elif outcome == "transient" and model_id.lower() in previous_by_key:
            # Retain only rediscovered, previously verified models.  Omitted
            # rows and definitive rejections never survive this refresh.
            retained_transient.append(previous_by_key[model_id.lower()])

    if accepted:
        # Publish newly verified rows and retain rediscovered transient rows
        # with their original per-model proof timestamps. This keeps a provider
        # 429/503 from making known-good rows disappear, without extending the
        # transient rows trust window.
        verified_at = time.time()
        combined = list(accepted)
        model_verified_at = {model.lower(): verified_at for model in accepted}
        accepted_keys = set(model_verified_at)
        for model_id in retained_transient:
            key = model_id.lower()
            if key in accepted_keys:
                continue
            combined.append(model_id)
            model_verified_at[key] = previous_item_verified_at.get(
                key, previous_verified_at or verified_at
            )
        write_verified_opencode_free_catalog(
            combined,
            verified_at=verified_at,
            source=normalized_base,
            model_verified_at=model_verified_at,
        )
        _refresh_picker_cache(combined)
        return {"status": "updated", "models": len(combined)}
    if retained_transient and previous_verified_at is not None:
        write_verified_opencode_free_catalog(
            retained_transient,
            verified_at=previous_verified_at,
            source=normalized_base,
            model_verified_at={
                model.lower(): previous_item_verified_at.get(model.lower(), previous_verified_at)
                for model in retained_transient
            },
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
    parser.add_argument("--timeout", type=float, default=240.0)
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
