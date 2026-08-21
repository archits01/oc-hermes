"""Source-controlled LMI Unipile live-overlay integration.

The WhatsApp and Instagram adapters remain deployment-owned overlays.  This
package is the reviewed, provider-independent boundary they can use for the
consented media offer/follow-up tools.
"""

from .bridge import (
    FIXED_MEDIA_CAPTION_TEMPLATE_ID,
    FIXED_MEDIA_OFFER_TEMPLATE_ID,
    InstagramMediaOverlay,
    MediaChatScope,
    MediaOverlay,
    MediaOverlayError,
    WhatsAppMediaOverlay,
    register_adapter_media_tools,
)
from .deployment import (
    MediaBridgeDeploymentConfig,
    SessionDatabaseMediaScopeResolver,
    VerifiedInboundMediaScopeRegistry,
    bind_verified_adapter_inbound_event,
    construct_reviewed_media_bridge,
    install_deployment_media_tools,
    open_session_database_scope_resolver,
)

__all__ = [
    "FIXED_MEDIA_CAPTION_TEMPLATE_ID",
    "FIXED_MEDIA_OFFER_TEMPLATE_ID",
    "InstagramMediaOverlay",
    "MediaChatScope",
    "MediaOverlay",
    "MediaOverlayError",
    "WhatsAppMediaOverlay",
    "register_adapter_media_tools",
    "MediaBridgeDeploymentConfig",
    "SessionDatabaseMediaScopeResolver",
    "VerifiedInboundMediaScopeRegistry",
    "bind_verified_adapter_inbound_event",
    "construct_reviewed_media_bridge",
    "install_deployment_media_tools",
    "open_session_database_scope_resolver",
]
