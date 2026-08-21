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

__all__ = [
    "FIXED_MEDIA_CAPTION_TEMPLATE_ID",
    "FIXED_MEDIA_OFFER_TEMPLATE_ID",
    "InstagramMediaOverlay",
    "MediaChatScope",
    "MediaOverlay",
    "MediaOverlayError",
    "WhatsAppMediaOverlay",
    "register_adapter_media_tools",
]
