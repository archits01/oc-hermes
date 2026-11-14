"""Behavior contracts for the profile-scoped verified OpenCode Free catalog."""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest


def _load_refresh_script():
    root = Path(__file__).resolve().parents[2]
    path = root / "scripts" / "ops" / "lmi_free_model_catalog_refresh.py"
    spec = importlib.util.spec_from_file_location("lmi_free_model_catalog_refresh_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def opencode_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _write_cache(home: Path, *, verified_at: float, models: list[str]) -> None:
    (home / "opencode_free_model_catalog.json").write_text(
        json.dumps(
            {
                "version": 1,
                "provider": "opencode-free",
                "verified_at": verified_at,
                "source": "https://opencode.ai/zen/v1",
                "models": [{"id": model, "verified_at": verified_at} for model in models],
            }
        ),
        encoding="utf-8",
    )


def test_runtime_uses_fresh_profile_verified_cache_not_static(opencode_home):
    from hermes_cli.models import (
        _model_in_provider_catalog,
        cached_provider_model_ids,
        get_default_model_for_provider,
        get_verified_opencode_free_model_ids,
        provider_model_ids,
    )

    _write_cache(
        opencode_home,
        verified_at=time.time(),
        models=["newly-verified-free", "x-preview-f-free"],
    )

    expected = ["newly-verified-free", "x-preview-f-free"]
    assert get_verified_opencode_free_model_ids() == expected
    assert provider_model_ids("opencode-free") == expected
    assert cached_provider_model_ids("opencode-free") == expected
    assert get_default_model_for_provider("opencode-free") == expected[0]
    assert _model_in_provider_catalog("newly-verified-free", {"opencode-free"})
    assert not _model_in_provider_catalog("hy3-free", {"opencode-free"})


def test_picker_row_uses_profile_verified_cache(opencode_home):
    from hermes_cli.model_switch import list_authenticated_providers

    _write_cache(
        opencode_home,
        verified_at=time.time(),
        models=["newly-verified-free", "x-preview-f-free"],
    )
    rows = list_authenticated_providers(for_picker=True)
    free = next(row for row in rows if row["slug"] == "opencode-free")
    assert free["models"] == ["newly-verified-free", "x-preview-f-free"]


def test_stale_or_malformed_catalog_falls_back_to_static(opencode_home):
    from hermes_cli.models import _OPENCODE_FREE_STATIC_MODELS, get_verified_opencode_free_model_ids

    _write_cache(
        opencode_home,
        verified_at=time.time() - 49 * 60 * 60,
        models=["expired-free"],
    )
    assert get_verified_opencode_free_model_ids() == list(_OPENCODE_FREE_STATIC_MODELS)

    (opencode_home / "opencode_free_model_catalog.json").write_text("not-json", encoding="utf-8")
    assert get_verified_opencode_free_model_ids() == list(_OPENCODE_FREE_STATIC_MODELS)


def test_conservative_candidates_never_admit_plain_paid_ids(opencode_home):
    script = _load_refresh_script()
    known = script._OPENCODE_FREE_STATIC_MODELS[0]

    assert script.conservative_candidates(["paid-model", "new-promo-free", known]) == [
        "new-promo-free",
        known,
    ]


def test_probe_uses_keyless_honest_chat_completions_wire(monkeypatch, opencode_home):
    script = _load_refresh_script()
    captured = {}

    def fake_request(request, *, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["authorization"] = request.get_header("Authorization")
        captured["user_agent"] = request.get_header("User-agent")
        captured["referer"] = request.get_header("Http-referer")
        return "success", 200, {
            "id": "ok",
            "choices": [{"message": {"role": "assistant", "content": "OK"}}],
        }

    monkeypatch.setattr(script, "_request_json", fake_request)

    assert script.probe_anonymous_model("https://example.test/v1", "new-promo-free", timeout=1) == "success"
    assert captured["url"] == "https://example.test/v1/chat/completions"
    assert captured["body"]["max_tokens"] == 256
    assert captured["body"]["model"] == "new-promo-free"
    from hermes_cli.models import opencode_zen_free_runtime

    runtime_headers = opencode_zen_free_runtime(
        "opencode-free", "new-promo-free"
    )["default_headers"]
    assert captured["authorization"] == runtime_headers["Authorization"]
    assert captured["user_agent"] == runtime_headers["User-Agent"]
    assert captured["referer"] == runtime_headers["HTTP-Referer"]


def test_probe_uses_responses_wire_for_verified_responses_models(monkeypatch, opencode_home):
    script = _load_refresh_script()
    captured = {}

    def fake_request(request, *, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return "success", 200, {
            "id": "ok",
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": "OK"}],
            }],
        }

    monkeypatch.setattr(script, "_request_json", fake_request)
    monkeypatch.setattr(script, "opencode_model_api_mode", lambda *_args: "codex_responses")

    assert script.probe_anonymous_model(
        "https://example.test/v1", "responses-promo-free", timeout=1
    ) == "success"
    assert captured["url"] == "https://example.test/v1/responses"
    assert captured["body"]["max_output_tokens"] == script.RESPONSES_PROBE_MAX_OUTPUT_TOKENS
    assert "messages" not in captured["body"]


@pytest.mark.parametrize(
    ("mode", "payload"),
    [
        ("chat_completions", {}),
        ("chat_completions", {"choices": []}),
        ("chat_completions", {"choices": [{"message": {}}]}),
        ("chat_completions", {"choices": [{"message": {"role": "assistant"}}]}),
        ("codex_responses", {}),
        ("codex_responses", {"id": "ok"}),
        ("codex_responses", {"output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}]}),
        ("codex_responses", {"id": "ok", "output": []}),
        ("codex_responses", {"id": "ok", "output": [{}]}),
    ],
)
def test_probe_rejects_malformed_success_responses(monkeypatch, opencode_home, mode, payload):
    script = _load_refresh_script()
    monkeypatch.setattr(script, "opencode_model_api_mode", lambda *_args: mode)
    monkeypatch.setattr(
        script, "_request_json", lambda *_args, **_kwargs: ("success", 200, payload)
    )
    monkeypatch.setattr(script, "probe_responses_stream", lambda *_args, **_kwargs: "definitive")

    assert script.probe_anonymous_model(
        "https://example.test/v1", "candidate-free", timeout=1
    ) == "definitive"


def test_provider_error_envelope_distinguishes_transient_and_definitive(opencode_home):
    script = _load_refresh_script()
    assert script._provider_error_outcome(
        {"error": {"type": "error", "message": "Internal server error"}}
    ) == "transient"
    assert script._provider_error_outcome(
        {"error": {"type": "server_error", "message": "Model is unavailable"}}
    ) == "definitive"


def test_reasoning_only_chat_response_is_retained(monkeypatch, opencode_home):
    script = _load_refresh_script()
    monkeypatch.setattr(script, "opencode_model_api_mode", lambda *_args: "chat_completions")
    monkeypatch.setattr(
        script,
        "_request_json",
        lambda *_args, **_kwargs: (
            "success",
            200,
            {"choices": [{"message": {"role": "assistant", "content": None, "reasoning": "thinking"}}]},
        ),
    )
    assert script.probe_anonymous_model(
        "https://example.test/v1", "reasoning-free", timeout=1
    ) == "transient"


def test_incomplete_responses_stream_is_retained(monkeypatch, opencode_home):
    script = _load_refresh_script()

    class FakeResponse:
        def __enter__(self):
            self.lines = iter([
                b'data: {"type":"response.incomplete","response":{"status":"incomplete","incomplete_details":{"reason":"max_output_tokens"}}}' + bytes([10]),
                bytes([10]),
            ])
            return self

        def __exit__(self, *_args):
            return False

        def readline(self, *_args):
            return next(self.lines, b"")

    monkeypatch.setattr(script.urllib.request, "urlopen", lambda *_args, **_kwargs: FakeResponse())
    assert script.probe_responses_stream(
        "https://example.test/v1", "reasoning-free", timeout=1
    ) == "transient"



def test_transient_probe_retries_once_before_rejecting(monkeypatch, opencode_home):
    script = _load_refresh_script()
    calls = []

    def fake_probe(*_args, **_kwargs):
        calls.append(True)
        return "transient" if len(calls) == 1 else "success"

    monkeypatch.setattr(script, "probe_anonymous_model", fake_probe)
    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)

    assert script.probe_candidate_with_retries(
        "https://example.test/v1", "retry-free", timeout=1
    ) == "success"
    assert len(calls) == 2


def test_responses_empty_envelope_falls_back_to_stream_probe(monkeypatch, opencode_home):
    script = _load_refresh_script()
    monkeypatch.setattr(script, "opencode_model_api_mode", lambda *_args: "codex_responses")
    monkeypatch.setattr(
        script, "_request_json", lambda *_args, **_kwargs: ("success", 200, {"id": "ok", "output": []})
    )
    monkeypatch.setattr(script, "probe_responses_stream", lambda *_args, **_kwargs: "success")
    assert script.probe_anonymous_model("https://example.test/v1", "stream-promo-free", timeout=1) == "success"


def test_anthropic_mode_promotions_fail_closed_until_runtime_is_keyless(monkeypatch, opencode_home):
    script = _load_refresh_script()
    monkeypatch.setattr(script, "opencode_model_api_mode", lambda *_args: "anthropic_messages")
    monkeypatch.setattr(
        script, "_request_json",
        lambda *_args, **_kwargs: pytest.fail("anthropic probe must not run"),
    )
    assert script.probe_anonymous_model(
        "https://example.test/v1", "anthropic-promo-free", timeout=1
    ) == "definitive"


def test_catalog_and_probe_bounds_fail_closed(monkeypatch, opencode_home):
    script = _load_refresh_script()
    discovered = [f"model-{index}-free" for index in range(script.MAX_DISCOVERED_MODELS + 1)]
    monkeypatch.setattr(
        script, "_request_json", lambda *_args, **_kwargs: (
            "success", 200, {"data": [{"id": model} for model in discovered]}
        ),
    )
    outcome, models = script.fetch_open_code_models(
        "https://example.test/v1", timeout=1
    )
    assert outcome == "definitive" and models == []
    assert len(script.conservative_candidates(discovered)) == script.MAX_PROBE_CANDIDATES

    from hermes_cli.models import write_verified_opencode_free_catalog

    with pytest.raises(ValueError, match="too many"):
        write_verified_opencode_free_catalog(
            [f"verified-{index}-free" for index in range(65)]
        )


def test_transient_probe_retains_but_never_extends_prior_verification(monkeypatch, opencode_home):
    script = _load_refresh_script()
    old_time = time.time() - 60
    _write_cache(opencode_home, verified_at=old_time, models=["x-preview-f-free"])

    monkeypatch.setattr(
        script, "fetch_open_code_models", lambda *_args, **_kwargs: ("success", ["x-preview-f-free"])
    )
    monkeypatch.setattr(script, "probe_anonymous_model", lambda *_args, **_kwargs: "transient")

    result = script.refresh_catalog(timeout=1)
    payload = json.loads((opencode_home / "opencode_free_model_catalog.json").read_text())
    assert result == {"status": "retained_transient", "models": 1}
    assert payload["verified_at"] == old_time


def test_mixed_probe_outcomes_keep_only_rediscovered_transient_prior(monkeypatch, opencode_home):
    script = _load_refresh_script()
    old_time = time.time() - 60
    _write_cache(
        opencode_home,
        verified_at=old_time,
        models=["transient-free", "rejected-free", "omitted-free"],
    )
    monkeypatch.setattr(
        script,
        "fetch_open_code_models",
        lambda *_args, **_kwargs: ("success", ["transient-free", "rejected-free"]),
    )
    monkeypatch.setattr(
        script,
        "probe_anonymous_model",
        lambda _base, model, **_kwargs: (
            "transient" if model == "transient-free" else "definitive"
        ),
    )

    assert script.refresh_catalog(timeout=1) == {"status": "retained_transient", "models": 1}
    payload = json.loads((opencode_home / "opencode_free_model_catalog.json").read_text())
    assert payload["verified_at"] == old_time
    assert [item["id"] for item in payload["models"]] == ["transient-free"]



def test_success_plus_transient_retains_prior_item_timestamp(monkeypatch, opencode_home):
    script = _load_refresh_script()
    old_time = time.time() - 60
    _write_cache(opencode_home, verified_at=old_time, models=["transient-free"])
    monkeypatch.setattr(
        script,
        "fetch_open_code_models",
        lambda *_args, **_kwargs: ("success", ["transient-free", "new-free"]),
    )
    monkeypatch.setattr(
        script,
        "probe_anonymous_model",
        lambda _base, model, **_kwargs: "transient" if model == "transient-free" else "success",
    )
    result = script.refresh_catalog(timeout=1)
    payload = json.loads((opencode_home / "opencode_free_model_catalog.json").read_text())
    assert result == {"status": "updated", "models": 2}
    stamps = {item["id"]: item["verified_at"] for item in payload["models"]}
    assert stamps["transient-free"] == old_time
    assert stamps["new-free"] > old_time


def test_successful_refresh_writes_only_individually_verified_candidates(monkeypatch, opencode_home):
    script = _load_refresh_script()
    known = script._OPENCODE_FREE_STATIC_MODELS[0]
    monkeypatch.setattr(
        script,
        "fetch_open_code_models",
        lambda *_args, **_kwargs: ("success", ["paid-model", "new-promo-free", known]),
    )
    monkeypatch.setattr(script, "probe_anonymous_model", lambda _base, model, **_kwargs: "success")

    result = script.refresh_catalog(timeout=1)
    payload = json.loads((opencode_home / "opencode_free_model_catalog.json").read_text())
    assert result == {"status": "updated", "models": 2}
    assert [item["id"] for item in payload["models"]] == ["new-promo-free", known]
    assert script.get_verified_opencode_free_model_ids() == ["new-promo-free", known]


def test_unavailable_discovery_does_not_create_unverified_cache(monkeypatch, opencode_home):
    script = _load_refresh_script()
    monkeypatch.setattr(script, "fetch_open_code_models", lambda *_args, **_kwargs: ("transient", []))

    assert script.refresh_catalog(timeout=1) == {"status": "unavailable", "models": 0}
    assert not (opencode_home / "opencode_free_model_catalog.json").exists()


def test_definitive_discovery_failure_clears_prior_verification(monkeypatch, opencode_home):
    script = _load_refresh_script()
    _write_cache(opencode_home, verified_at=time.time(), models=["x-preview-f-free"])
    monkeypatch.setattr(script, "fetch_open_code_models", lambda *_args, **_kwargs: ("definitive", []))
    monkeypatch.setattr(script, "_refresh_picker_cache", lambda *_args: pytest.fail("must not warm"))

    assert script.refresh_catalog(timeout=1) == {
        "status": "definitive_discovery_failed", "models": 0,
    }
    assert not (opencode_home / "opencode_free_model_catalog.json").exists()


def test_definitive_rejection_removes_managed_picker_availability(monkeypatch, opencode_home):
    """Managed policy is stricter than the upstream static fallback."""
    from hermes_cli import model_switch

    row = {
        "slug": "opencode-free", "name": "OpenCode Free", "models": ["x-preview-f-free"],
        "total_models": 1, "is_current": False, "is_user_defined": False, "source": "hermes",
    }
    monkeypatch.setattr(model_switch, "list_authenticated_providers", lambda **_kwargs: [row])

    # No proof and stale proof are both hidden in a managed allowlist.
    assert model_switch.list_picker_providers(included_providers=["opencode-free"]) == []
    _write_cache(
        opencode_home,
        verified_at=time.time() - 49 * 60 * 60,
        models=["x-preview-f-free"],
    )
    assert model_switch.list_picker_providers(included_providers=["opencode-free"]) == []

    _write_cache(opencode_home, verified_at=time.time(), models=["x-preview-f-free"])
    assert [item["slug"] for item in model_switch.list_picker_providers(
        included_providers=["opencode-free"]
    )] == ["opencode-free"]

    script = _load_refresh_script()
    monkeypatch.setattr(
        script, "fetch_open_code_models", lambda *_args, **_kwargs: ("success", ["x-preview-f-free"])
    )
    monkeypatch.setattr(script, "probe_anonymous_model", lambda *_args, **_kwargs: "definitive")
    assert script.refresh_catalog(timeout=1) == {"status": "no_verified_models", "models": 0}
    assert model_switch.list_picker_providers(included_providers=["opencode-free"]) == []


def test_managed_picker_discards_reinserted_current_model_not_in_verified_cache(monkeypatch, opencode_home):
    """The current-model recovery path cannot bypass verified OpenCode IDs."""
    from hermes_cli import model_switch

    _write_cache(opencode_home, verified_at=time.time(), models=["verified-free"])
    reinserted_row = {
        "slug": "opencode-free", "name": "OpenCode Free",
        "models": ["current-but-unverified", "verified-free"], "total_models": 2,
        "is_current": True, "is_user_defined": False, "source": "hermes",
    }
    monkeypatch.setattr(
        model_switch, "list_authenticated_providers", lambda **_kwargs: [reinserted_row]
    )

    rows = model_switch.list_picker_providers(
        current_provider="opencode-free",
        current_model="current-but-unverified",
        included_providers=["opencode-free"],
    )
    assert rows[0]["models"] == ["verified-free"]
