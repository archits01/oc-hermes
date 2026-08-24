# Upstream reconciliation — 2026-08-24

## Before

- `oc-branding` was based on `0318a1a47ea53a105a6ce81d7321a4e2d25b9305` and was 739 commits behind upstream `a0ca7c19204e514f9590ce3b812e029b315ab9e9`.
- The desktop and gateway had accumulated upstream API and type changes that could not be safely adopted as isolated files.
- VM deployment helpers had intentionally moved out of the fork and into `/opt/lmi-ops`, but the scheduled fork-sync workflow still required a deleted repository script.

## After (candidate)

- The candidate carries the upstream merge and keeps OpenComputer product identity, the thin-client connection bootstrap, and signed updater/release plumbing.
- The GitHub/VM boundary is explicit: Git carries desktop/bootstrap/web/TUI client and release code only; agent, gateway, tools, plugins, optional MCPs, VM cron/health helpers, and LMI adapter sources are restored to upstream or remain VM-owned. The only non-client exceptions are the one-line default product identity and a pre-existing upstream test-whitespace cleanup.
- The VM now owns the reviewed Unipile MCP source at `/opt/lmi-ops/mcp-src`; its pin checker validates that source and the live config points to it. Sarvam already runs from the VM data-root install.
- A candidate-sync boundary checker rejects any future non-client source path relative to `upstream/main`, and the invariant checker rejects returned VM-owned LMI source paths.
- Electron integration seams were reconciled as coherent upstream pairs: data-URL limits, authenticated remote downloads, and platform translucency.
- The concurrent fork correction `f2845b3192` was incorporated and completed with its missing main-tab ownership helpers and fallback-pane gate; the full group-chat regression suite verifies that behavior.
- User-facing desktop status, upgrade, gateway, remote/cloud, and bootstrap wording remains branded as OpenComputer; Hermes protocol identifiers and CLI commands remain unchanged for runtime compatibility.
- Desktop gateway error wording is tested through real connect-error, disconnected-request, and pending-request-close paths; the plugin-bridge error is separately exercised, and the invariant checker verifies constructor wiring so these client surfaces cannot silently revert to Hermes wording.
- The invariant checker parses Electron Builder's effective `build.publish` GitHub configuration and requires `archits01/oc-hermes`, rather than trusting an unused updater constant.
- It resolves the effective macOS updater precedence (`dmg.publish`, then `mac.publish`, then global publish), rejects overrides/multiple publishers, and keeps distributable DMG/Windows/Linux labels branded as OpenComputer.
- The protected macOS release workflow runs the invariant checker before packaging and verifies the generated packaged `app-update.yml` has exactly `github / archits01 / oc-hermes` before any release asset upload.
- It now checks out only `refs/tags/<tag>`, proves the peeled tag equals the checked-out commit, uses `gh release create --verify-tag`, and rechecks remote tag identity before upload or publish.
- The complete desktop plugin suite is part of acceptance; after reconciling stale test fixtures it passes 530 tests, including canonical Bot Chat activity and OpenComputer update-message coverage.
- The scheduled fork-sync invariants now run an executable structural checker (including negative mutation self-tests), preserve unresolved `*-CONFLICTS` evidence indefinitely, use a unique Actions run/attempt branch name, and deliberately do not require VM-owned deployment files.
- Clean and conflicted candidates preserve the fork workflow plus its invariant checker before a `GITHUB_TOKEN` push, while reporting upstream control-plane changes for deliberate porting.
- The old local direct-merge script is retired so it cannot bypass the candidate branch and invariant gate; the GitHub workflow is the sole scheduled sync path.

## Why

The goal is to consume upstream Hermes improvements without silently turning an OpenComputer thin client back into a generic Hermes desktop, while keeping LMI agent behavior and operations on the VM rather than in the fork.

## Verification required before shipping

- Desktop TypeScript typecheck and production build.
- Focused Electron tests for authenticated downloads, translucency, packaged updates, default connection seeding, and source-update routing.
- Static OpenComputer/LMI invariant checks, workflow syntax/lint checks, a full diff review, and an independent final review.
- Reconcile any newer `origin/oc-branding` commit before pushing; then verify the LMI VM checkout, service health, and effective runtime configuration separately.
