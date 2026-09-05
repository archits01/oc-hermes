---
sidebar_position: 11
title: Model Catalog
description: Remotely-hosted manifest driving curated model picker lists for OpenRouter and Nous Portal.
---

# Model Catalog

Hermes fetches curated model lists for **OpenRouter** and **Nous Portal** from a JSON manifest hosted alongside the docs site. This lets maintainers update picker lists without shipping a new `hermes-agent` release.

When the manifest is unreachable (offline, network blocked, hosting failure), Hermes silently falls back to the in-repo snapshot that ships with the CLI. The manifest never breaks the picker — worst case you see whatever list was bundled with your installed version.

## Live manifest URL

```
https://hermes-agent.nousresearch.com/docs/api/model-catalog.json
```

Published on every merge to `main` via the existing `deploy-site.yml` GitHub Pages pipeline. The source of truth lives in the repo at `website/static/api/model-catalog.json`.

## Schema

```json
{
  "version": 1,
  "updated_at": "2026-04-25T22:00:00Z",
  "metadata": {},
  "providers": {
    "openrouter": {
      "metadata": {},
      "models": [
        {"id": "z-ai/glm-5.2",         "description": "default", "default": true},
        {"id": "moonshotai/kimi-k3",   "description": "recommended", "metadata": {}},
        {"id": "openai/gpt-5.4",       "description": ""}
      ]
    },
    "nous": {
      "metadata": {},
      "models": [
        {"id": "z-ai/glm-5.2", "default": true},
        {"id": "anthropic/claude-opus-4.7"},
        {"id": "moonshotai/kimi-k3"}
      ]
    }
  }
}
```

Field notes:

- **`version`** — integer schema version. Future schemas bump this; Hermes refuses manifests with versions it doesn't understand and falls back to the hardcoded snapshot.
- **`metadata`** — free-form dict at the manifest, provider, and model level. Any keys. Hermes ignores unknown fields, so you can annotate entries (`"tier": "paid"`, `"tags": [...]`, etc.) without coordinating a schema change.
- **`description`** — OpenRouter-only. Drives picker badge text (`"recommended"`, `"free"`, `"default"`, or empty). Nous Portal doesn't use this — free-tier gating is determined live from the Portal's pricing endpoint.
- **`default`** — exactly one entry per provider may carry `"default": true`. That model is the **silent default**: what Hermes lands on when the user never selected a model (GUI onboarding confirm card, `provider` configured with no `model`, empty `model.default`). Read cache-only at runtime (`get_default_model_from_cache`) so hot resolution paths never hit the network; when no cached manifest exists, Hermes falls back to the in-repo `PREFERRED_SILENT_DEFAULT_MODEL` constant, which must match the labeled entry. This lets maintainers rotate the silent default without shipping a release. It is deliberately a capable low-cost model, never the priciest flagship.
- **Pricing and context length** are NOT in the manifest. Those come from live provider APIs (`/v1/models` endpoints, models.dev) at fetch time.

## Fetch behavior

| When | What happens |
|---|---|
| `/model` or `hermes model` | Fetches if disk cache is stale, else uses cache |
| Gateway running | Background refresh every `ttl_minutes` (default 20), so the picker never lags the published manifest by more than one window |
| Disk cache fresh (< TTL) | No network hit |
| Network failure with cache | Silent fallback to cache, one log line |
| Network failure, no cache | Silent fallback to in-repo snapshot |
| Manifest fails schema validation | Treated as unreachable |

Cache location: `~/.hermes/cache/model_catalog.json`.

## Config

```yaml
model_catalog:
  enabled: true
  url: https://hermes-agent.nousresearch.com/docs/api/model-catalog.json
  ttl_minutes: 20
  providers: {}
```

Set `enabled: false` to disable remote fetch entirely and always use the in-repo snapshot (this also disables the gateway's background refresh). `ttl_minutes` sets both the cache lifetime and the gateway refresh cadence; the legacy `ttl_hours` key is still honoured if you set it explicitly.

### Per-provider override URLs

Third parties can self-host their own curation list using the same schema. Point a provider at a custom URL:

```yaml
model_catalog:
  providers:
    openrouter:
      url: https://example.com/my-openrouter-curation.json
```

The overriding manifest only needs to populate the provider block(s) it cares about. Other providers continue to resolve against the master URL.

### Hiding providers from the picker

`excluded_providers` lets you hide specific providers from the `/model` picker even when valid credentials exist. Useful when credentials are present for legacy or testing providers that shouldn't appear in normal use (e.g. an old Copilot or OpenRouter token still cached in `auth.json` or discovered via the `gh` CLI).

```yaml
model_catalog:
  excluded_providers:
    - copilot
    - openrouter
    - openai
```

The exclusion is matched case-insensitively against every key a provider can surface under — the Hermes id and models.dev id (built-in mapped providers), the overlay pid and resolved Hermes slug (overlay providers), and the canonical slug (canonical providers) — so a single entry like `copilot` hides the provider regardless of which section emits it. It is honored by every `/model` picker surface: the gateway interactive/text pickers, the TUI picker, and the interactive `hermes model` CLI picker. An empty list (or omitting the key) has no effect.

`included_providers` is the complementary allowlist. When non-empty, picker
payloads contain only matching provider slugs, names, or aliases. Use it for a
managed installation that intentionally offers a small catalog; an empty list
preserves normal explicit-provider discovery.

```yaml
model_catalog:
  included_providers:
    - opencomputer
    - xai-oauth
    - opencode-free
    - openrouter
```

### Showing only free models from selected providers

`free_only_providers` constrains named provider rows to models whose live
pricing reports zero input and output cost. If pricing is unavailable, Hermes
hides that constrained row rather than guessing. Other providers are unchanged;
dedicated keyless catalogs such as `opencode-free` already contain only models
verified for anonymous use.

```yaml
model_catalog:
  free_only_providers:
    - openrouter
    - novita
```

This can be combined with `excluded_providers`: primary and custom providers
stay fully selectable while aggregators with changing promotions show only
their currently free models.

For managed OpenComputer installations, zero-priced OpenRouter and Novita rows
also require explicit tool capability in the same live catalog record; a label
or `:free` suffix is not admission proof. Successful live pricing and the
OpenRouter live catalog are refreshed within a bounded five-minute process TTL.

### LMI daily OpenCode Free refresh

On LMI-PI-01, install the exact, idempotent daily refresh job with:

```bash
bash /opt/opencomputer-v2/scripts/ops/install_lmi_free_model_catalog_refresh_cron.sh --install
```

Use `--check` for the canonical installed entry, `--dry-run` to inspect the
non-mutating update, and `--render` to print it. The job runs under
`HERMES_HOME=/opt/opencomputer-v2-data`, uses the deployed checkout's venv,
locks its single refresh, logs below the data path, and preserves unrelated
crontab entries.

## Updating the manifest

Maintainers:

```bash
# Re-generate from the in-repo hardcoded lists (keeps manifest in sync after
# editing OPENROUTER_MODELS or _PROVIDER_MODELS["nous"] in hermes_cli/models.py).
python scripts/build_model_catalog.py
```

Then PR the resulting change to `website/static/api/model-catalog.json` to `main`. The docs site auto-deploys on merge and the new manifest is live within a few minutes.

You can also hand-edit the JSON directly for fine-grained metadata changes that don't belong in the in-repo snapshot — the generator script is a convenience, not the single source of truth.
