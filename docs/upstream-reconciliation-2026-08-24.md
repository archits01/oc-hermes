# Upstream reconciliation — 2026-08-24

## Before

- `oc-branding` was based on `0318a1a47ea53a105a6ce81d7321a4e2d25b9305` and was 739 commits behind upstream `a0ca7c19204e514f9590ce3b812e029b315ab9e9`.
- The desktop and gateway had accumulated upstream API and type changes that could not be safely adopted as isolated files.
- VM deployment helpers had intentionally moved out of the fork and into `/opt/lmi-ops`, but the scheduled fork-sync workflow still required a deleted repository script.

## After (candidate)

- The candidate carries the upstream merge and keeps the OpenComputer product identity, LMI messaging source classification, LMI dashboard plugin, default-connection bootstrap, and fork-owned updater targets.
- Electron integration seams were reconciled as coherent upstream pairs: data-URL limits, authenticated remote downloads, and platform translucency.
- The scheduled fork-sync invariants now protect the customer-facing OpenComputer surfaces and deliberately do not require VM-owned deployment files.

## Why

The goal is to consume upstream Hermes improvements without silently turning an OpenComputer/LMI client back into a generic Hermes desktop or coupling source merges to mutable VM operations.

## Verification required before shipping

- Desktop TypeScript typecheck and production build.
- Focused Electron tests for authenticated downloads, translucency, packaged updates, default connection seeding, and source-update routing.
- Static OpenComputer/LMI invariant checks, workflow syntax/lint checks, a full diff review, and an independent final review.
- Reconcile any newer `origin/oc-branding` commit before pushing; then verify the LMI VM checkout, service health, and effective runtime configuration separately.
