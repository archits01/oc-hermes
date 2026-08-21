# LMI Unipile live-overlay integration

This package is the source-controlled adapter boundary for the currently
untracked WhatsApp and Instagram Unipile overlays. The deployment-owned live
adapter files and the reviewed CRM/media bridge are recorded as pinned,
read-only inputs in [`manifest.yaml`](manifest.yaml); they are not copied into
Hermes and this package never calls Unipile by itself.

Adapters inject the reviewed `MediaFollowupBridge` through
`register_adapter_media_tools`. The adapter must also supply a resolver that
maps the active Hermes session to its exact inbound channel/account/chat; the
model never supplies those values. Each channel gets distinct global names so
both adapters can run together:

* `whatsapp_offer_media` / `instagram_offer_media` send only the fixed
  `portfolio-media-v1` offer.
* `whatsapp_send_approved_media` / `instagram_send_approved_media` send only
  the fixed `portfolio-delivery-v1` caption,
  requires the exact inbound provider message id, and accepts media ids only.

The reviewed bridge remains authoritative for exact-chat consent, opt-outs,
approved-media file/hash checks, idempotency, locks, and provider outcomes.
This overlay is review-only until an explicit deployment review updates the
manifest, constructs the deployment-owned bridge/provider configuration, and
binds exact session scope before each live adapter dispatches an inbound turn.
