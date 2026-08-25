"""Tests for inventory._apply_pricing — the pricing/tier enrichment that

feeds the desktop GUI model picker (and onboarding) so it can show $/Mtok
columns + Free/Pro badges and gate paid models on free Nous accounts, the
same way the `hermes model` CLI picker does.
"""

import hermes_cli.inventory as inv
import hermes_cli.models as models_mod
import hermes_cli.model_switch as model_switch


def test_novita_missing_price_dimension_is_unknown_not_free(monkeypatch):
    """A missing output/input source field must not be coerced to zero."""
    monkeypatch.setenv("NOVITA_API_KEY", "test-key")
    monkeypatch.setattr(models_mod, "_cached_catalog", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(models_mod, "_cache_catalog", lambda _key, value: value)

    class _Response:
        def read(self):
            return b'''{"data":[
                {"id":"missing-output","input_token_price_per_m":"10"},
                {"id":"missing-input","output_token_price_per_m":"10"},
                {"id":"complete","input_token_price_per_m":"0","output_token_price_per_m":"0"}
            ]}'''

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(models_mod, "_urlopen_model_catalog_request", lambda *_args, **_kwargs: _Response())

    pricing = models_mod._fetch_novita_pricing(force_refresh=True)
    assert pricing == {"complete": {"prompt": "0.0", "completion": "0.0"}}


def test_novita_pricing_retains_only_explicit_tool_capability(monkeypatch):
    monkeypatch.setenv("NOVITA_API_KEY", "test-key")
    monkeypatch.setattr(models_mod, "_cached_catalog", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(models_mod, "_cache_catalog", lambda _key, value: value)

    class _Response:
        def read(self):
            return b'''{"data":[
                {"id":"image-only-free","input_token_price_per_m":"0","output_token_price_per_m":"0","supported_parameters":["images"]},
                {"id":"unknown-free","input_token_price_per_m":"0","output_token_price_per_m":"0"},
                {"id":"tool-free","input_token_price_per_m":"0","output_token_price_per_m":"0","supported_parameters":["tools","tool_choice"]},
                {"id":"tool-paid","input_token_price_per_m":"1","output_token_price_per_m":"1","capabilities":{"tool_calling":true}}
            ]}'''

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(models_mod, "_urlopen_model_catalog_request", lambda *_args, **_kwargs: _Response())

    pricing = models_mod._fetch_novita_pricing(force_refresh=True)
    assert "tool_capable" not in pricing["image-only-free"]
    assert "tool_capable" not in pricing["unknown-free"]
    assert pricing["tool-free"]["tool_capable"] is True
    assert pricing["tool-paid"]["tool_capable"] is True


def test_free_only_novita_requires_zero_prices_and_explicit_tool_capability(monkeypatch):
    rows = [{
        "slug": "novita",
        "models": ["image-only-free", "unknown-free", "tool-free", "tool-paid"],
        "total_models": 4,
    }]
    monkeypatch.setattr(models_mod, "get_pricing_for_provider", lambda *_args, **_kwargs: {
        "image-only-free": {"prompt": "0", "completion": "0"},
        "unknown-free": {"prompt": "0", "completion": "0"},
        "tool-free": {"prompt": "0", "completion": "0", "tool_capable": True},
        "tool-paid": {"prompt": "0", "completion": "0.000001", "tool_capable": True},
    })

    filtered = model_switch._apply_catalog_provider_policy(
        rows,
        included_providers=["novita"],
        free_only_providers=["novita"],
    )

    assert filtered[0]["models"] == ["tool-free"]


def test_free_only_openrouter_requires_explicit_live_tool_capability(monkeypatch):
    rows = [{
        "slug": "openrouter",
        "models": ["curated-missing-free", "curated-malformed-free", "curated-tools-free", "curated-tools-paid"],
        "total_models": 4,
    }]
    monkeypatch.setattr(models_mod, "get_pricing_for_provider", lambda *_args, **_kwargs: {
        "curated-missing-free": {"prompt": "0", "completion": "0"},
        "curated-malformed-free": {"prompt": "0", "completion": "0", "tool_capable": "unknown"},
        "curated-tools-free": {"prompt": "0", "completion": "0", "tool_capable": True},
        "curated-tools-paid": {"prompt": "0", "completion": "0.000001", "tool_capable": True},
    })

    filtered = model_switch._apply_catalog_provider_policy(
        rows,
        included_providers=["openrouter"],
        free_only_providers=["openrouter"],
    )

    assert filtered[0]["models"] == ["curated-tools-free"]


def test_inventory_badge_does_not_mark_missing_completion_as_free(monkeypatch):
    monkeypatch.setattr(models_mod, "get_pricing_for_provider", lambda *_args, **_kwargs: {
        "incomplete": {"prompt": "0"},
    })
    rows = [{"slug": "openrouter", "models": ["incomplete"]}]

    inv._apply_pricing(rows)

    assert rows[0]["pricing"]["incomplete"]["free"] is False


def test_free_only_provider_filter_uses_live_pricing_and_fails_closed(monkeypatch):
    rows = [
        {
            "slug": "openrouter",
            "models": ["lab/free", "lab/paid", "lab/unknown"],
            "total_models": 3,
            "pricing": {
                "lab/free": {"input": "free", "output": "free", "free": True},
                "lab/paid": {"input": "$1.00", "output": "$2.00", "free": False},
            },
        },
        {"slug": "xai-oauth", "models": ["grok"], "total_models": 1},
        {"slug": "novita", "models": ["uncatalogued"], "total_models": 1},
    ]
    monkeypatch.setattr(
        models_mod,
        "get_pricing_for_provider",
        lambda slug, **_kwargs: {
            "lab/free": {"prompt": "0", "completion": "0", "tool_capable": True},
            "lab/paid": {"prompt": "0.000001", "completion": "0", "tool_capable": True},
        } if slug == "openrouter" else {},
    )

    filtered = inv._filter_free_only_provider_rows(
        rows, ["OPENROUTER", "novita"]
    )

    assert [row["slug"] for row in filtered] == ["openrouter", "xai-oauth"]
    assert filtered[0]["models"] == ["lab/free"]
    assert filtered[0]["total_models"] == 1
    assert set(filtered[0]["pricing"]) == {"lab/free"}
    assert filtered[1] is rows[1]


def test_included_provider_filter_accepts_slug_name_and_alias():
    rows = [
        {"slug": "opencomputer", "name": "OpenComputer", "models": ["a"]},
        {"slug": "custom-x", "name": "Private", "aliases": ["custom:private"], "models": ["b"]},
        {"slug": "xai", "name": "xAI", "models": ["c"]},
    ]
    filtered = inv._filter_included_provider_rows(
        rows, ["OPENCOMPUTER", "custom:private"]
    )
    assert filtered == rows[:2]
    assert inv._filter_included_provider_rows(rows, []) is rows


def test_shared_provider_policy_blocks_paid_and_unknown_price_models(monkeypatch):
    rows = [
        {"slug": "openrouter", "models": ["lab/free", "lab/paid", "lab/unknown"], "total_models": 3},
        {"slug": "xai", "models": ["grok"], "total_models": 1},
    ]
    monkeypatch.setattr(models_mod, "get_pricing_for_provider", lambda *_args, **_kwargs: {
        "lab/free": {"prompt": "0", "completion": "0", "tool_capable": True},
        "lab/paid": {"prompt": "0", "completion": "1"},
        "lab/unknown": {"prompt": "0"},
    })
    filtered = model_switch._apply_catalog_provider_policy(
        rows,
        included_providers=["openrouter"],
        free_only_providers=["openrouter"],
    )
    assert [row["slug"] for row in filtered] == ["openrouter"]
    assert filtered[0]["models"] == ["lab/free"]


def _patch_pricing(monkeypatch, *, free_tier, pricing, unavailable=None):
    monkeypatch.setattr(models_mod, "get_pricing_for_provider", lambda slug, **kw: pricing.get(slug, {}))
    monkeypatch.setattr(models_mod, "check_nous_free_tier", lambda *, force_fresh=False: free_tier)
    monkeypatch.setattr(
        models_mod, "partition_nous_models_by_tier",
        lambda ids, pr, free_tier: (
            [m for m in ids if m not in (unavailable or [])],
            list(unavailable or []),
        ),
    )


def test_apply_pricing_formats_per_model_prices(monkeypatch):
    """Each model gets formatted input/output/cache + a free flag."""
    _patch_pricing(
        monkeypatch,
        free_tier=False,
        pricing={
            "openrouter": {
                "a/paid": {"prompt": "0.000003", "completion": "0.000015", "input_cache_read": "0.0000003"},
                "b/free": {"prompt": "0", "completion": "0"},
            }
        },
    )
    rows = [{"slug": "openrouter", "models": ["a/paid", "b/free"]}]
    inv._apply_pricing(rows)

    pricing = rows[0]["pricing"]
    assert pricing["a/paid"] == {"input": "$3.00", "output": "$15.00", "cache": "$0.30", "free": False}
    assert pricing["b/free"]["free"] is True
    assert pricing["b/free"]["input"] == "free"


def test_apply_pricing_free_models_get_flat_100_percent_sale(monkeypatch):
    """Free models show -100% chrome; was_* only when original was served."""
    _patch_pricing(
        monkeypatch,
        free_tier=False,
        pricing={
            "nous": {
                "a/free": {
                    "prompt": "0",
                    "completion": "0",
                    "original": {
                        "prompt": "0.000002",
                        "completion": "0.00001",
                    },
                },
                "b/natively-free": {
                    "prompt": "0",
                    "completion": "0",
                },
            }
        },
    )
    rows = [{"slug": "nous", "models": ["a/free", "b/natively-free"]}]
    inv._apply_pricing(rows)
    free = rows[0]["pricing"]["a/free"]
    assert free["free"] is True
    assert free["discount_percent"] == 100
    assert free["was_input"] == "$2.00"
    assert free["was_output"] == "$10.00"
    native = rows[0]["pricing"]["b/natively-free"]
    assert native["free"] is True
    assert native["discount_percent"] == 100
    # No gateway original → no fabricated was prices.
    assert "was_input" not in native
    assert "was_output" not in native


def test_apply_pricing_omits_sale_when_original_not_cheaper(monkeypatch):
    _patch_pricing(
        monkeypatch,
        free_tier=False,
        pricing={
            "nous": {
                "a/eq": {
                    "prompt": "0.000002",
                    "completion": "0.00001",
                    "original": {
                        "prompt": "0.000002",
                        "completion": "0.00001",
                    },
                },
            }
        },
    )
    rows = [{"slug": "nous", "models": ["a/eq"]}]
    inv._apply_pricing(rows)
    assert "discount_percent" not in rows[0]["pricing"]["a/eq"]
