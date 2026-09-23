# Required deployment adapter hardening (not applied)

Status: **review artifact only.** The files pinned in `manifest.yaml` are
deployment-owned and remain read-only in this repository. Do not copy this
patch into a live VM without an explicit deployment review and controlled test.

## Why this patch is required

The current WhatsApp and Instagram copies both accept a webhook whose
`account_id` is empty. Their Hermes `SessionSource` persists platform and chat
but not account id. A tool that receives only `session_id` cannot reconstruct
which Unipile account an empty-account webhook came from. It must fail closed.

Hermes has no platform-plugin hook that receives all three of `(raw payload,
current gateway session id, plugin context)` after `SessionStore` creates the
row and before `AIAgent` tool execution. The least invasive safe route is a
pre-queue binding keyed by Hermes's deterministic session key. The binding is
process-local and is intentionally lost on restart; tool calls then fail closed
until a new authenticated inbound message rebuilds it.

## Exact patch points in each deployment-owned adapter

1. In `WhatsAppAdapter._is_own_whatsapp_event` (the account check immediately
   after `account_id = ...`), replace the conditional mismatch check with:

   ```python
   if not self._account_id or not account_id or account_id != self._account_id:
       logger.info("[whatsapp] rejecting missing or foreign account_id")
       return False
   ```

   In `InstagramAdapter._is_own_instagram_event`, make the same replacement
   with the Instagram log label. Retain the existing account-type validation.

2. In each `_handle_message_event`, immediately after `source = self.build_source(...)`
   and before `event = MessageEvent(...)`, add the following. `media_runtime`
   is a deployment-owned shared object created in the bootstrap below; it must
   be the same object for both adapters.

   ```python
   media_runtime.bind_inbound(
       adapter=self,
       channel="whatsapp",  # "instagram" in InstagramAdapter
       source=source,
       inbound_payload=payload,
   )
   ```

   If this raises `MediaOverlayError`, log the error and `return`; do not queue
   the event. The helper repeats the raw account check and derives the exact
   session key using the same `build_session_key` options as
   `BasePlatformAdapter.handle_message`.

3. Add the following tiny runtime wrapper in the deployment bootstrap (not in
   a model tool, adapter constructor, or webhook handler):

   ```python
   from plugins.platforms.lmi_unipile_overlay.deployment import (
       MediaBridgeDeploymentConfig,
       VerifiedInboundMediaScopeRegistry,
       bind_verified_adapter_inbound_event,
       install_deployment_media_tools,
   )
   from hermes_state import SessionDB

   # The bootstrap obtains these values from the deployment's secret/config
   # source. They are not read by the overlay itself.
   config = MediaBridgeDeploymentConfig(...)
   inbound_scopes = VerifiedInboundMediaScopeRegistry(config)
   session_db = SessionDB(db_path=config.session_db_path)
   install_deployment_media_tools(
       ctx,
       config=config,
       media_module=verified_manifest_pinned_media_module,
       session_db=session_db,
       inbound_scopes=inbound_scopes,
   )

   class MediaRuntime:
       def bind_inbound(self, *, adapter, channel, source, inbound_payload):
           return bind_verified_adapter_inbound_event(
               adapter=adapter, channel=channel, source=source,
               inbound_payload=inbound_payload, config=config,
               inbound_scopes=inbound_scopes,
           )
   media_runtime = MediaRuntime()
   ```

   The installer must be invoked once from the shared deployment bootstrap
   while the platform plugin context (`ctx`) is available; calling it
   independently from both adapter modules would create duplicate global tool
   names.

## Required tests before enabling

- Missing raw `account_id` is rejected before `MessageEvent` or queue admission.
- A foreign raw account id is rejected before queue admission.
- A valid WhatsApp event can invoke only `whatsapp_*` tools for its own chat.
- A valid Instagram event cannot invoke `whatsapp_*` tools and vice versa.
- A service restart with no new inbound binding returns review/no-send.
- One real sandbox or controlled lead chat proves provider output and confirms
  no tool can target another account or chat.
