# LMI Unipile live-overlay integration

This package is the source-controlled adapter boundary for the currently
untracked WhatsApp and Instagram Unipile overlays. The deployment-owned live
adapter files and the reviewed CRM/media bridge are recorded as pinned,
read-only inputs in [`manifest.yaml`](manifest.yaml); they are not copied into
Hermes and this package never calls Unipile by itself.

`deployment.py` now provides the only supported wiring entry point:
`install_deployment_media_tools`. It accepts an explicit
`MediaBridgeDeploymentConfig`, the manifest-pinned reviewed media module, and
the active deployment's `SessionDB`. It constructs the bridge only from those
deployment-owned values — it never reads environment variables or imports an
arbitrary provider module. Each channel gets distinct global names so both
adapters can run together:

* `whatsapp_offer_media` / `instagram_offer_media` send only the fixed
  `portfolio-media-v1` offer.
* `whatsapp_send_approved_media` / `instagram_send_approved_media` send only
  the fixed `portfolio-delivery-v1` caption,
  requires the exact inbound provider message id, and accepts media ids only.

The reviewed bridge remains authoritative for exact-chat consent, opt-outs,
approved-media file/hash checks, idempotency, locks, and provider outcomes.
The scope resolver requires the dispatcher-supplied canonical `session_id` to
point to a durable gateway session row with a matching adapter platform,
`chat_id`, and `origin_json`. It also requires a `VerifiedInboundMediaScopeRegistry`
binding created from a non-empty raw webhook `account_id` that exactly equals
the adapter and deployment account. Model tool arguments can provide neither
channel, account, nor chat.

The current deployment-owned adapter copies do **not** yet perform that strict
check: they accept a missing webhook `account_id`, and Hermes `SessionSource`
does not persist an account id. [`ADAPTER_HARDENING.md`](ADAPTER_HARDENING.md)
contains the minimal required patch points. Until those changes are reviewed
and applied together, installation is intentionally blocked/review-only.

This overlay is still review-only until an explicit deployment review verifies
the pinned module and input hashes, passes the serving state database and
adapter account ids, applies the adapter hardening patch, changes the manifest
status, and runs a real controlled end-to-end test. No provider calls occur
during import or installation.
