import os
import json
import logging
from typing import Any, Optional
from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server import NotificationOptions, Server
import mcp.server.stdio
from pydantic import AnyUrl

from .unipile_client_extended import UnipileClientExtended

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Response slimming: strip unused fields to save tokens ──

def _slim_connection(c):
    """Keep only fields the agent needs from a connection object."""
    if not isinstance(c, dict):
        return c
    return {
        "provider_id": c.get("provider_id") or c.get("id", ""),
        "display_name": c.get("display_name") or c.get("name", ""),
        "headline": c.get("headline", ""),
        "company": c.get("company", ""),
        "location": c.get("location", ""),
        "network_distance": c.get("network_distance", ""),
        "public_profile_url": c.get("public_profile_url", ""),
    }

def _slim_message(m):
    """Keep only fields the agent needs from a message object."""
    if not isinstance(m, dict):
        return m
    result = {
        "id": m.get("id", ""),
        "is_sender": m.get("is_sender", False),
        "text": m.get("text", ""),
        "timestamp": m.get("timestamp", ""),
        "sender_id": m.get("sender_id", ""),
    }
    attachments = m.get("attachments")
    if attachments:
        result["attachments"] = attachments
    return result

def _slim_chat(c):
    """Keep only fields the agent needs from a chat object."""
    if not isinstance(c, dict):
        return c
    slim_attendees = []
    for a in c.get("attendees", []):
        if isinstance(a, dict):
            slim_attendees.append({
                "attendee_provider_id": a.get("attendee_provider_id", ""),
                "display_name": a.get("display_name") or a.get("attendee_name", ""),
            })
    return {
        "id": c.get("id", ""),
        "timestamp": c.get("timestamp", ""),
        "attendees": slim_attendees,
    }

def _slim_search_person(p):
    """Keep only fields the agent needs from a search result person."""
    if not isinstance(p, dict):
        return p
    return {
        "provider_id": p.get("provider_id") or p.get("id", ""),
        "display_name": p.get("display_name") or p.get("name", ""),
        "first_name": p.get("first_name", ""),
        "last_name": p.get("last_name", ""),
        "headline": p.get("headline", ""),
        "company": p.get("company", ""),
        "location": p.get("location", ""),
        "network_distance": p.get("network_distance", ""),
        "public_identifier": p.get("public_identifier", ""),
        "public_profile_url": p.get("public_profile_url", ""),
    }

def _slim_account(a):
    """Keep only fields the agent needs from an account object."""
    if not isinstance(a, dict):
        return a
    return {
        "id": a.get("id", ""),
        "type": a.get("type", ""),
        "name": a.get("name") or a.get("display_name", ""),
        "status": a.get("status", ""),
    }

def _slim_post(p):
    """Keep only fields the agent needs from a post object."""
    if not isinstance(p, dict):
        return p
    result = {
        "social_id": p.get("social_id", ""),
        "id": p.get("id", ""),
        "text": (p.get("text") or "")[:500],  # truncate long posts
        "reaction_counter": p.get("reaction_counter"),
        "comment_counter": p.get("comment_counter"),
        "created_at": p.get("created_at") or p.get("timestamp", ""),
    }
    author = p.get("author")
    if isinstance(author, dict):
        result["author"] = {
            "display_name": author.get("display_name", ""),
            "provider_id": author.get("provider_id", ""),
        }
    attachments = p.get("attachments")
    if attachments:
        result["attachments"] = attachments
    perms = p.get("permissions")
    if isinstance(perms, dict):
        result["permissions"] = perms
    return result


def _slim_comment(c):
    """Keep only fields the agent needs from a comment object."""
    if not isinstance(c, dict):
        return c
    result = {
        "id": c.get("id", ""),
        "text": c.get("text", ""),
        "created_at": c.get("created_at") or c.get("timestamp", ""),
        "reaction_counter": c.get("reaction_counter"),
    }
    author = c.get("author")
    if isinstance(author, dict):
        result["author"] = {
            "display_name": author.get("display_name", ""),
            "provider_id": author.get("provider_id", ""),
        }
    return result


def _slim_reaction(r):
    """Keep only fields the agent needs from a reaction object."""
    if not isinstance(r, dict):
        return r
    result = {
        "reaction_type": r.get("reaction_type") or r.get("type", ""),
    }
    author = r.get("author") or r.get("user")
    if isinstance(author, dict):
        result["author"] = {
            "display_name": author.get("display_name", ""),
            "provider_id": author.get("provider_id", ""),
        }
    return result


def _slim_response(raw_json_str, item_key, slim_fn):
    """Parse a JSON response, slim its items list, return as JSON string."""
    try:
        data = json.loads(raw_json_str)
        if isinstance(data, dict) and "error" in data:
            return raw_json_str  # pass errors through
        if isinstance(data, dict) and item_key in data:
            items = data[item_key]
            if isinstance(items, list):
                data[item_key] = [slim_fn(i) for i in items]
            # Keep cursor for pagination
            slimmed = {item_key: data[item_key]}
            if "cursor" in data:
                slimmed["cursor"] = data["cursor"]
            if "object" in data:
                slimmed["object"] = data["object"]
            return json.dumps(slimmed, default=str)
        elif isinstance(data, list):
            return json.dumps([slim_fn(i) for i in data], default=str)
        return raw_json_str
    except (json.JSONDecodeError, TypeError):
        return raw_json_str


class UnipileWrapperExtended:
    def __init__(self, dsn: Optional[str] = None, api_key: Optional[str] = None):
        dsn = dsn or os.getenv("UNIPILE_DSN")
        api_key = api_key or os.getenv("UNIPILE_API_KEY")

        if not dsn:
            raise ValueError("UNIPILE_DSN environment variable is required")
        if not api_key:
            raise ValueError("UNIPILE_API_KEY environment variable is required")

        self.client = UnipileClientExtended(dsn=dsn, api_key=api_key)

    def get_accounts(self) -> str:
        try:
            accounts = self.client.get_accounts()
            raw = json.dumps(accounts, default=str)
            return _slim_response(raw, "items", _slim_account)
        except Exception as e:
            logger.error(f"Error getting accounts: {str(e)}")
            return json.dumps({"error": str(e)})

    def get_connections(self, account_id: str, limit: int = 50, cursor: Optional[str] = None) -> str:
        try:
            connections = self.client.get_connections(account_id=account_id, limit=limit, cursor=cursor)
            raw = json.dumps(connections, default=str)
            return _slim_response(raw, "items", _slim_connection)
        except Exception as e:
            logger.error(f"Error getting connections: {str(e)}")
            return json.dumps({"error": str(e)})

    def get_chats(self, account_id: str, limit: int = 10) -> str:
        try:
            chats = self.client.get_chats(account_id=account_id, limit=limit)
            raw = json.dumps(chats, default=str)
            return _slim_response(raw, "items", _slim_chat)
        except Exception as e:
            logger.error(f"Error getting chats: {str(e)}")
            return json.dumps({"error": str(e)})

    def get_messages(self, chat_id: str, limit: int = 20) -> str:
        try:
            messages = self.client.get_messages(chat_id=chat_id, limit=limit)
            raw = json.dumps(messages, default=str)
            return _slim_response(raw, "items", _slim_message)
        except Exception as e:
            logger.error(f"Error getting messages: {str(e)}")
            return json.dumps({"error": str(e)})

    def send_message(self, account_id: str, text: str, chat_id: Optional[str] = None,
                     attendee_id: Optional[str] = None) -> str:
        try:
            result = self.client.send_message(
                account_id=account_id,
                chat_id=chat_id,
                text=text,
                attendee_id=attendee_id
            )
            return json.dumps(result, default=str)
        except Exception as e:
            logger.error(f"Error sending message: {str(e)}")
            return json.dumps({"error": str(e)})

    def send_connection_request(self, account_id: str, profile_url: str,
                               message: Optional[str] = None) -> str:
        try:
            result = self.client.send_connection_request(
                account_id=account_id,
                profile_url=profile_url,
                message=message
            )
            return json.dumps(result, default=str)
        except Exception as e:
            logger.error(f"Error sending connection request: {str(e)}")
            return json.dumps({"error": str(e)})

    def create_post(self, account_id: str, text: str,
                    image_path: Optional[str] = None,
                    external_link: Optional[str] = None) -> str:
        try:
            result = self.client.create_post(
                account_id=account_id,
                text=text,
                image_path=image_path,
                external_link=external_link,
            )
            return json.dumps(result, default=str)
        except Exception as e:
            logger.error(f"Error creating post: {str(e)}")
            return json.dumps({"error": str(e)})

    def create_instagram_post(self, account_id: str, text: str, image_path: str) -> str:
        if not image_path:
            return json.dumps({"error": "Instagram posts require image_path"})
        return self.create_post(account_id=account_id, text=text, image_path=image_path)

    def delete_post(self, account_id: str, post_id: str) -> str:
        try:
            result = self.client.delete_post(
                account_id=account_id,
                post_id=post_id,
            )
            return json.dumps(result, default=str)
        except Exception as e:
            logger.error(f"Error deleting post: {str(e)}")
            return json.dumps({"error": str(e)})

    def send_email(self, account_id: str, to: list, subject: str, body: str,
                  cc: Optional[list] = None, bcc: Optional[list] = None) -> str:
        try:
            result = self.client.send_email(
                account_id=account_id,
                to=to,
                subject=subject,
                body=body,
                cc=cc,
                bcc=bcc
            )
            return json.dumps(result, default=str)
        except Exception as e:
            logger.error(f"Error sending email: {str(e)}")
            return json.dumps({"error": str(e)})

    def search_linkedin_people(self, account_id: str, **kwargs) -> str:
        try:
            result = self.client.search_linkedin_people(account_id=account_id, **kwargs)
            raw = json.dumps(result, default=str)
            return _slim_response(raw, "items", _slim_search_person)
        except Exception as e:
            logger.error(f"Error searching LinkedIn people: {str(e)}")
            return json.dumps({"error": str(e)})

    def search_linkedin_companies(self, account_id: str, **kwargs) -> str:
        try:
            result = self.client.search_linkedin_companies(account_id=account_id, **kwargs)
            return json.dumps(result, default=str)
        except Exception as e:
            logger.error(f"Error searching LinkedIn companies: {str(e)}")
            return json.dumps({"error": str(e)})

    def search_linkedin_jobs(self, account_id: str, **kwargs) -> str:
        try:
            result = self.client.search_linkedin_jobs(account_id=account_id, **kwargs)
            return json.dumps(result, default=str)
        except Exception as e:
            logger.error(f"Error searching LinkedIn jobs: {str(e)}")
            return json.dumps({"error": str(e)})

    def get_search_param_ids(self, account_id: str, param_type: str, keywords: str, limit: int = 100) -> str:
        try:
            result = self.client.get_linkedin_search_parameters(
                account_id=account_id,
                param_type=param_type,
                keywords=keywords,
                limit=limit
            )
            return json.dumps(result, default=str)
        except Exception as e:
            logger.error(f"Error getting search parameters: {str(e)}")
            return json.dumps({"error": str(e)})


    def get_post(self, account_id: str, post_id: str) -> str:
        try:
            result = self.client.get_post(account_id=account_id, post_id=post_id)
            return json.dumps(_slim_post(result), default=str)
        except Exception as e:
            logger.error(f"Error getting post: {str(e)}")
            return json.dumps({"error": str(e)})

    def list_user_posts(self, account_id: str, identifier: str,
                        limit: int = 10, cursor: Optional[str] = None) -> str:
        try:
            result = self.client.list_user_posts(
                account_id=account_id, identifier=identifier,
                limit=limit, cursor=cursor,
            )
            raw = json.dumps(result, default=str)
            return _slim_response(raw, "items", _slim_post)
        except Exception as e:
            logger.error(f"Error listing user posts: {str(e)}")
            return json.dumps({"error": str(e)})

    def list_post_reactions(self, account_id: str, post_id: str,
                            limit: int = 50, cursor: Optional[str] = None) -> str:
        try:
            result = self.client.list_post_reactions(
                account_id=account_id, post_id=post_id,
                limit=limit, cursor=cursor,
            )
            raw = json.dumps(result, default=str)
            return _slim_response(raw, "items", _slim_reaction)
        except Exception as e:
            logger.error(f"Error listing post reactions: {str(e)}")
            return json.dumps({"error": str(e)})

    def list_post_comments(self, account_id: str, post_id: str,
                           limit: int = 50, sort_by: str = "MOST_RECENT",
                           cursor: Optional[str] = None) -> str:
        try:
            result = self.client.list_post_comments(
                account_id=account_id, post_id=post_id,
                limit=limit, sort_by=sort_by, cursor=cursor,
            )
            raw = json.dumps(result, default=str)
            return _slim_response(raw, "items", _slim_comment)
        except Exception as e:
            logger.error(f"Error listing post comments: {str(e)}")
            return json.dumps({"error": str(e)})

    def reply_to_comment(self, account_id: str, post_id: str, text: str,
                         comment_id: Optional[str] = None) -> str:
        try:
            result = self.client.reply_to_comment(
                account_id=account_id, post_id=post_id,
                text=text, comment_id=comment_id,
            )
            return json.dumps(result, default=str)
        except Exception as e:
            logger.error(f"Error replying to comment: {str(e)}")
            return json.dumps({"error": str(e)})


    # ── Cross-platform tools (added for multi-platform support) ────────

    def get_user_profile(self, identifier: str, account_id: str,
                         linkedin_sections: str = None) -> str:
        """Cross-platform user profile lookup."""
        try:
            result = self.client.get_user_profile(
                identifier, account_id, linkedin_sections=linkedin_sections
            )
            profile = {
                "provider_id": result.get("provider_id", ""),
                "display_name": result.get("display_name", ""),
                "first_name": result.get("first_name", ""),
                "last_name": result.get("last_name", ""),
                "headline": result.get("headline", ""),
                "location": result.get("location", ""),
                "public_identifier": result.get("public_identifier", ""),
                "public_profile_url": result.get("public_profile_url", ""),
                "follower_count": result.get("follower_count"),
                "following_count": result.get("following_count"),
                "post_count": result.get("post_count"),
                "provider_messaging_id": result.get("provider_messaging_id", ""),
            }
            return json.dumps(profile, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)})

    def get_followers(self, account_id: str, identifier: str = None,
                      limit: int = 50, cursor: str = None) -> str:
        """List followers (IG/LI)."""
        try:
            result = self.client.get_followers(
                account_id, identifier=identifier, limit=limit, cursor=cursor
            )
            items = result.get("items", [])
            slimmed = [{"provider_id": u.get("provider_id", ""), "name": u.get("display_name", "") or u.get("name", ""), "username": u.get("public_identifier", ""), "profile_url": u.get("public_profile_url", "")} for u in items]
            return json.dumps({"items": slimmed, "cursor": result.get("cursor"), "count": len(slimmed)}, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)})

    def get_following(self, account_id: str, identifier: str = None,
                      limit: int = 50, cursor: str = None) -> str:
        """List accounts being followed (IG/LI)."""
        try:
            result = self.client.get_following(
                account_id, identifier=identifier, limit=limit, cursor=cursor
            )
            items = result.get("items", [])
            slimmed = [{"provider_id": u.get("provider_id", ""), "name": u.get("display_name", "") or u.get("name", ""), "username": u.get("public_identifier", ""), "profile_url": u.get("public_profile_url", "")} for u in items]
            return json.dumps({"items": slimmed, "cursor": result.get("cursor"), "count": len(slimmed)}, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)})

    def sync_chat(self, chat_id: str, account_id: str = None) -> str:
        """Trigger chat history sync (critical for WhatsApp)."""
        try:
            result = self.client.sync_chat(chat_id, account_id=account_id)
            return json.dumps(result, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)})

    def get_chat_attendees(self, chat_id: str, account_id: str = None) -> str:
        """List chat participants with platform-specific details."""
        try:
            result = self.client.get_chat_attendees(chat_id, account_id=account_id)
            items = result if isinstance(result, list) else result.get("items", [])
            slimmed = []
            for a in items:
                entry = {"provider_id": a.get("provider_id", ""), "name": a.get("name", "") or a.get("display_name", ""), "public_identifier": a.get("public_identifier", "")}
                specifics = a.get("specifics", {})
                if specifics.get("phone_number"): entry["phone_number"] = specifics["phone_number"]
                if specifics.get("provider"): entry["platform"] = specifics["provider"]
                slimmed.append(entry)
            return json.dumps({"attendees": slimmed}, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)})

async def main(dsn: Optional[str] = None, api_key: Optional[str] = None):
    """Run the Extended Unipile MCP server with send capabilities."""
    logger.info("Extended Unipile MCP Server starting")
    unipile = UnipileWrapperExtended(dsn, api_key)
    server = Server("unipile-extended")

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        """List all available tools including send capabilities"""
        return [
            # READ operations
            types.Tool(
                name="unipile_get_accounts",
                description="Get all connected messaging accounts (LinkedIn, Email, WhatsApp, etc.)",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="unipile_get_connections",
                description="Get LinkedIn connections list for an account. Returns profile details of your connections.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "string", "description": "Account ID (from get_accounts)"},
                        "limit": {"type": "integer", "description": "Max connections to return (default: 50)", "default": 50},
                        "cursor": {"type": "string", "description": "Pagination cursor from previous response"}
                    },
                    "required": ["account_id"]
                },
            ),
            types.Tool(
                name="unipile_get_chats",
                description="Get list of conversations/chats for an account",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "string", "description": "Account ID"},
                        "limit": {"type": "integer", "description": "Max chats (default: 10)", "default": 10}
                    },
                    "required": ["account_id"]
                },
            ),
            types.Tool(
                name="unipile_get_messages",
                description="Get messages from a specific chat/conversation",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "chat_id": {"type": "string", "description": "Chat ID (from get_chats)"},
                        "limit": {"type": "integer", "description": "Max messages (default: 20)", "default": 20}
                    },
                    "required": ["chat_id"]
                },
            ),

            # WRITE operations - LinkedIn
            types.Tool(
                name="unipile_send_linkedin_message",
                description="Send the FINAL crafted LinkedIn DM to a lead. ONLY use this tool when you have composed the actual message you want the lead to see in their inbox. Do NOT use for drafts, thinking, or status updates. The text parameter is sent word-for-word as a LinkedIn DM.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "string", "description": "LinkedIn account ID"},
                        "text": {"type": "string", "description": "Message text to send"},
                        "chat_id": {"type": "string", "description": "Existing chat ID (optional if attendee_id provided)"},
                        "attendee_id": {"type": "string", "description": "LinkedIn profile ID for new conversation (optional if chat_id provided)"}
                    },
                    "required": ["account_id", "text"]
                },
            ),
            types.Tool(
                name="unipile_send_connection_request",
                description="Send a LinkedIn connection request with optional personalized note",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "string", "description": "LinkedIn account ID"},
                        "profile_url": {"type": "string", "description": "LinkedIn profile URL (e.g., https://linkedin.com/in/username)"},
                        "message": {"type": "string", "description": "Optional personalized connection note (max 300 chars)"}
                    },
                    "required": ["account_id", "profile_url"]
                },
            ),
            types.Tool(
                name="unipile_create_linkedin_post",
                description="Create a LinkedIn post with optional image. Use image_generate first to create an image, then pass its file path here. Returns post_id for tracking engagement.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "string", "description": "LinkedIn account ID"},
                        "text": {"type": "string", "description": "Post content. Mentions: {{0}} referencing mentions array index."},
                        "image_path": {"type": "string", "description": "Local file path or URL of image to attach (max 6012x6012px). Optional."},
                        "external_link": {"type": "string", "description": "URL for preview card (must also appear in text). Optional."},
                    },
                    "required": ["account_id", "text"]
                },
            ),
            types.Tool(
                name="unipile_create_instagram_post",
                description="Create an Instagram feed post via Unipile. Requires the Instagram account_id and a local image/video path. Do not use OpenCLI for this account.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "string", "description": "Instagram Unipile account ID from unipile_get_accounts"},
                        "text": {"type": "string", "description": "Caption text. Instagram requires media; this is the caption only."},
                        "image_path": {"type": "string", "description": "Local absolute file path or URL of the image/video to publish. Required."},
                    },
                    "required": ["account_id", "text", "image_path"]
                },
            ),
            types.Tool(
                name="unipile_delete_post",
                description="Delete a LinkedIn or Instagram post on the account identified by account_id. LinkedIn: activity ID, social_id, or urn:li:activity:ID. Instagram: provider_id, not the /p/SHORTCODE.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "string", "description": "Unipile account ID that owns the post"},
                        "post_id": {"type": "string", "description": "Post ID to delete (LinkedIn activity/social_id or Instagram provider_id)"},
                    },
                    "required": ["account_id", "post_id"]
                },
            ),

            # POST ENGAGEMENT operations
            types.Tool(
                name="unipile_get_linkedin_post",
                description="Get a LinkedIn post's details including reaction and comment counts. Use social_id from response for further interactions.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "string", "description": "LinkedIn account ID"},
                        "post_id": {"type": "string", "description": "Post ID (numeric activity ID or urn:li:activity:ID format)"},
                    },
                    "required": ["account_id", "post_id"]
                },
            ),
            types.Tool(
                name="unipile_list_my_posts",
                description="List your own recent LinkedIn posts. Returns post IDs, engagement counters, and content. Use to check which posts are performing well.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "string", "description": "LinkedIn account ID"},
                        "identifier": {"type": "string", "description": "Your LinkedIn provider_id (starts with ACo/ADo). Get from unipile_get_accounts if needed."},
                        "limit": {"type": "integer", "description": "Max posts to return (default: 10)", "default": 10},
                        "cursor": {"type": "string", "description": "Pagination cursor from previous response"},
                    },
                    "required": ["account_id", "identifier"]
                },
            ),
            types.Tool(
                name="unipile_list_post_reactions",
                description="List who reacted to a LinkedIn post and their reaction types (like, celebrate, support, love, insightful, funny).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "string", "description": "LinkedIn account ID"},
                        "post_id": {"type": "string", "description": "Post social_id (from get_linkedin_post or list_my_posts)"},
                        "limit": {"type": "integer", "description": "Max reactions (default: 50)", "default": 50},
                        "cursor": {"type": "string", "description": "Pagination cursor"},
                    },
                    "required": ["account_id", "post_id"]
                },
            ),
            types.Tool(
                name="unipile_list_post_comments",
                description="List comments on a LinkedIn post. Use to see engagement and decide which comments to reply to.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "string", "description": "LinkedIn account ID"},
                        "post_id": {"type": "string", "description": "Post social_id"},
                        "limit": {"type": "integer", "description": "Max comments (default: 50)", "default": 50},
                        "sort_by": {"type": "string", "description": "MOST_RECENT or MOST_RELEVANT (default: MOST_RECENT)", "enum": ["MOST_RECENT", "MOST_RELEVANT"]},
                        "cursor": {"type": "string", "description": "Pagination cursor"},
                    },
                    "required": ["account_id", "post_id"]
                },
            ),
            types.Tool(
                name="unipile_reply_to_comment",
                description="Reply to a comment on a LinkedIn post, or add a new top-level comment. Use for engagement loops.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "string", "description": "LinkedIn account ID"},
                        "post_id": {"type": "string", "description": "Post social_id"},
                        "text": {"type": "string", "description": "Comment text (max 1250 chars). Use \\n for line breaks."},
                        "comment_id": {"type": "string", "description": "Parent comment ID for threaded reply. Omit for top-level comment."},
                    },
                    "required": ["account_id", "post_id", "text"]
                },
            ),
            # WRITE operations - Email
            types.Tool(
                name="unipile_send_email",
                description="Send an email from connected email account",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "string", "description": "Email account ID"},
                        "to": {"type": "array", "items": {"type": "string"}, "description": "Recipient email addresses"},
                        "subject": {"type": "string", "description": "Email subject"},
                        "body": {"type": "string", "description": "Email body (HTML or plain text)"},
                        "cc": {"type": "array", "items": {"type": "string"}, "description": "CC recipients (optional)"},
                        "bcc": {"type": "array", "items": {"type": "string"}, "description": "BCC recipients (optional)"}
                    },
                    "required": ["account_id", "to", "subject", "body"]
                },
            ),

            # SEARCH operations - LinkedIn
            types.Tool(
                name="unipile_search_linkedin_people",
                description="Search LinkedIn for people/profiles. Returns profile details, current positions, locations, etc. Use for lead generation and prospecting.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "string", "description": "LinkedIn account ID"},
                        "keywords": {"type": "string", "description": "Search keywords"},
                        "current_company": {"type": "array", "items": {"type": "string"}, "description": "Filter by current company names"},
                        "past_companies": {"type": "array", "items": {"type": "string"}, "description": "Filter by past company names"},
                        "schools": {"type": "array", "items": {"type": "string"}, "description": "Filter by schools"},
                        "industries": {"type": "array", "items": {"type": "string"}, "description": "Filter by industries"},
                        "locations": {"type": "array", "items": {"type": "string"}, "description": "Filter by locations"},
                        "title": {"type": "string", "description": "Filter by job title"},
                        "first_name": {"type": "string", "description": "Filter by first name"},
                        "last_name": {"type": "string", "description": "Filter by last name"},
                        "network": {"type": "array", "items": {"type": "string"}, "description": "Network degree: F (1st), S (2nd), O (3rd+)"},
                        "cursor": {"type": "string", "description": "Pagination cursor from previous response"}
                    },
                    "required": ["account_id"]
                },
            ),
            types.Tool(
                name="unipile_search_linkedin_companies",
                description="Search LinkedIn for companies. Returns company info, size, industry, location, etc. Use for account-based prospecting.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "string", "description": "LinkedIn account ID"},
                        "keywords": {"type": "string", "description": "Search keywords"},
                        "locations": {"type": "array", "items": {"type": "string"}, "description": "Filter by company locations"},
                        "industries": {"type": "array", "items": {"type": "string"}, "description": "Filter by industries"},
                        "company_size": {"type": "array", "items": {"type": "string"}, "description": "Filter by size: 1-10, 11-50, 51-200, 201-500, 501-1000, 1001-5000, 5001-10000, 10001+"},
                        "cursor": {"type": "string", "description": "Pagination cursor from previous response"}
                    },
                    "required": ["account_id"]
                },
            ),
            types.Tool(
                name="unipile_search_linkedin_jobs",
                description="Search LinkedIn job postings. Use to find companies hiring for specific roles (key buying signal for Open Computer).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "string", "description": "LinkedIn account ID"},
                        "keywords": {"type": "string", "description": "Job keywords (e.g., 'AI engineer', 'automation')"},
                        "locations": {"type": "array", "items": {"type": "string"}, "description": "Job locations"},
                        "companies": {"type": "array", "items": {"type": "string"}, "description": "Filter by company names"},
                        "experience_levels": {"type": "array", "items": {"type": "string"}, "description": "INTERNSHIP, ENTRY_LEVEL, ASSOCIATE, MID_SENIOR, DIRECTOR, EXECUTIVE"},
                        "job_types": {"type": "array", "items": {"type": "string"}, "description": "FULL_TIME, PART_TIME, CONTRACT, TEMPORARY, VOLUNTEER, INTERNSHIP"},
                        "remote": {"type": "array", "items": {"type": "string"}, "description": "ON_SITE, REMOTE, HYBRID"},
                        "date_posted": {"type": "string", "description": "PAST_24_HOURS, PAST_WEEK, PAST_MONTH, ANY_TIME"},
                        "industries": {"type": "array", "items": {"type": "string"}, "description": "Filter by industries"},
                        "cursor": {"type": "string", "description": "Pagination cursor from previous response"}
                    },
                    "required": ["account_id"]
                },
            ),

            # Helper tool for getting parameter IDs
            types.Tool(
                name="unipile_get_search_param_ids",
                description="Get LinkedIn search parameter IDs for locations, industries, skills, schools, or companies. Use this before searching to get proper IDs for filters.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "string", "description": "LinkedIn account ID"},
                        "param_type": {"type": "string", "description": "Type: LOCATION, INDUSTRY, SKILL, SCHOOL, COMPANY"},
                        "keywords": {"type": "string", "description": "Search keywords (e.g., 'los angeles' for location, 'software' for industry)"},
                        "limit": {"type": "integer", "description": "Max results (default: 100)", "default": 100}
                    },
                    "required": ["account_id", "param_type", "keywords"]
                },
            ),

            # WEBHOOK operations
            types.Tool(
                name="unipile_create_webhook",
                description="Register a webhook with Unipile to receive real-time events (new messages, connection accepted, email opened, etc.). Returns webhook ID.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "description": "Webhook source type: 'users' (connections/relations), 'chats' (messages), 'mails' (emails)"},
                        "request_url": {"type": "string", "description": "Your webhook endpoint URL (e.g., https://yourdomain.com/webhooks/unipile)"},
                        "name": {"type": "string", "description": "Webhook name for identification"},
                        "headers": {"type": "array", "items": {"type": "object"}, "description": "Optional custom headers to send with webhook requests"}
                    },
                    "required": ["source", "request_url", "name"]
                },
            ),
            types.Tool(
                name="unipile_list_webhooks",
                description="List all configured webhooks. Shows webhook IDs, URLs, event types, and status.",
                inputSchema={
                    "type": "object",
                    "properties": {}
                },
            ),
            types.Tool(
                name="unipile_delete_webhook",
                description="Delete a webhook by ID. Stops receiving events for that webhook.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "webhook_id": {"type": "string", "description": "Webhook ID to delete (from list_webhooks)"}
                    },
                    "required": ["webhook_id"]
                },
            ),

            # ── Cross-platform tools (NEW) ──────────────────────────────

            types.Tool(
                name="unipile_get_user_profile",
                description="Get a user profile by identifier across any platform. LinkedIn: use provider_id or public_identifier slug. Instagram: use USERNAME (not numeric ID). WhatsApp: use phone@s.whatsapp.net. For full LinkedIn profile, set linkedin_sections to *.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "identifier": {"type": "string", "description": "User identifier (format varies by platform)"},
                        "account_id": {"type": "string", "description": "Unipile account ID to query from"},
                        "linkedin_sections": {"type": "string", "description": "Set to * for full LinkedIn profile data"},
                    },
                    "required": ["identifier", "account_id"],
                },
            ),

            types.Tool(
                name="unipile_get_followers",
                description="List followers for an account (Instagram or LinkedIn). For Instagram, pass the user provider_id as identifier.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "string", "description": "Unipile account ID"},
                        "identifier": {"type": "string", "description": "User provider_id (for IG follower lookup)"},
                        "limit": {"type": "integer", "description": "Max followers to return (default 50)"},
                        "cursor": {"type": "string", "description": "Pagination cursor"},
                    },
                    "required": ["account_id"],
                },
            ),

            types.Tool(
                name="unipile_get_following",
                description="List accounts being followed (Instagram or LinkedIn). For Instagram, pass the user provider_id as identifier.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "string", "description": "Unipile account ID"},
                        "identifier": {"type": "string", "description": "User provider_id"},
                        "limit": {"type": "integer", "description": "Max results (default 50)"},
                        "cursor": {"type": "string", "description": "Pagination cursor"},
                    },
                    "required": ["account_id"],
                },
            ),

            types.Tool(
                name="unipile_sync_chat",
                description="Trigger chat history sync. CRITICAL for WhatsApp: messages are empty until sync completes. Uses GET (not POST). Returns status: SYNC_STARTED -> SYNC_RUNNING -> SYNC_DONE.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "chat_id": {"type": "string", "description": "Unipile chat ID to sync"},
                        "account_id": {"type": "string", "description": "Unipile account ID (optional)"},
                    },
                    "required": ["chat_id"],
                },
            ),

            types.Tool(
                name="unipile_get_chat_attendees",
                description="List participants of a chat. Returns provider_id, name, and platform-specific details (WhatsApp phone number, IG username). Useful for identifying who is in a conversation.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "chat_id": {"type": "string", "description": "Unipile chat ID"},
                        "account_id": {"type": "string", "description": "Unipile account ID (optional)"},
                    },
                    "required": ["chat_id"],
                },
            ),
        ]

    @server.call_tool()
    async def handle_call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> list[types.TextContent]:
        """Handle tool execution"""
        try:
            if name == "unipile_get_accounts":
                result = unipile.get_accounts()
            elif name == "unipile_get_connections":
                result = unipile.get_connections(
                    account_id=arguments["account_id"],
                    limit=arguments.get("limit", 50),
                    cursor=arguments.get("cursor")
                )
            elif name == "unipile_get_chats":
                result = unipile.get_chats(
                    account_id=arguments["account_id"],
                    limit=arguments.get("limit", 10)
                )
            elif name == "unipile_get_messages":
                result = unipile.get_messages(
                    chat_id=arguments["chat_id"],
                    limit=arguments.get("limit", 20)
                )
            elif name == "unipile_send_linkedin_message":
                result = unipile.send_message(
                    account_id=arguments["account_id"],
                    text=arguments["text"],
                    chat_id=arguments.get("chat_id"),
                    attendee_id=arguments.get("attendee_id")
                )
            elif name == "unipile_send_connection_request":
                result = unipile.send_connection_request(
                    account_id=arguments["account_id"],
                    profile_url=arguments["profile_url"],
                    message=arguments.get("message")
                )
            elif name == "unipile_create_linkedin_post":
                result = unipile.create_post(
                    account_id=arguments["account_id"],
                    text=arguments["text"],
                    image_path=arguments.get("image_path"),
                    external_link=arguments.get("external_link"),
                )
            elif name == "unipile_create_instagram_post":
                result = unipile.create_instagram_post(
                    account_id=arguments["account_id"],
                    text=arguments["text"],
                    image_path=arguments.get("image_path") or "",
                )
            elif name == "unipile_delete_post":
                result = unipile.delete_post(
                    account_id=arguments["account_id"],
                    post_id=arguments["post_id"],
                )
            elif name == "unipile_get_linkedin_post":
                result = unipile.get_post(
                    account_id=arguments["account_id"],
                    post_id=arguments["post_id"],
                )
            elif name == "unipile_list_my_posts":
                result = unipile.list_user_posts(
                    account_id=arguments["account_id"],
                    identifier=arguments["identifier"],
                    limit=arguments.get("limit", 10),
                    cursor=arguments.get("cursor"),
                )
            elif name == "unipile_list_post_reactions":
                result = unipile.list_post_reactions(
                    account_id=arguments["account_id"],
                    post_id=arguments["post_id"],
                    limit=arguments.get("limit", 50),
                    cursor=arguments.get("cursor"),
                )
            elif name == "unipile_list_post_comments":
                result = unipile.list_post_comments(
                    account_id=arguments["account_id"],
                    post_id=arguments["post_id"],
                    limit=arguments.get("limit", 50),
                    sort_by=arguments.get("sort_by", "MOST_RECENT"),
                    cursor=arguments.get("cursor"),
                )
            elif name == "unipile_reply_to_comment":
                result = unipile.reply_to_comment(
                    account_id=arguments["account_id"],
                    post_id=arguments["post_id"],
                    text=arguments["text"],
                    comment_id=arguments.get("comment_id"),
                )
            elif name == "unipile_send_email":
                result = unipile.send_email(
                    account_id=arguments["account_id"],
                    to=arguments["to"],
                    subject=arguments["subject"],
                    body=arguments["body"],
                    cc=arguments.get("cc"),
                    bcc=arguments.get("bcc")
                )
            elif name == "unipile_search_linkedin_people":
                result = unipile.search_linkedin_people(
                    account_id=arguments["account_id"],
                    keywords=arguments.get("keywords"),
                    current_company=arguments.get("current_company"),
                    past_companies=arguments.get("past_companies"),
                    schools=arguments.get("schools"),
                    industries=arguments.get("industries"),
                    locations=arguments.get("locations"),
                    title=arguments.get("title"),
                    first_name=arguments.get("first_name"),
                    last_name=arguments.get("last_name"),
                    network=arguments.get("network"),
                    cursor=arguments.get("cursor")
                )
            elif name == "unipile_search_linkedin_companies":
                result = unipile.search_linkedin_companies(
                    account_id=arguments["account_id"],
                    keywords=arguments.get("keywords"),
                    locations=arguments.get("locations"),
                    industries=arguments.get("industries"),
                    company_size=arguments.get("company_size"),
                    cursor=arguments.get("cursor")
                )
            elif name == "unipile_search_linkedin_jobs":
                result = unipile.search_linkedin_jobs(
                    account_id=arguments["account_id"],
                    keywords=arguments.get("keywords"),
                    locations=arguments.get("locations"),
                    companies=arguments.get("companies"),
                    experience_levels=arguments.get("experience_levels"),
                    job_types=arguments.get("job_types"),
                    remote=arguments.get("remote"),
                    date_posted=arguments.get("date_posted"),
                    industries=arguments.get("industries"),
                    cursor=arguments.get("cursor")
                )
            elif name == "unipile_get_search_param_ids":
                result = unipile.get_search_param_ids(
                    account_id=arguments["account_id"],
                    param_type=arguments["param_type"],
                    keywords=arguments["keywords"],
                    limit=arguments.get("limit", 100)
                )
            elif name == "unipile_create_webhook":
                result = unipile.create_webhook(
                    source=arguments["source"],
                    request_url=arguments["request_url"],
                    name=arguments["name"],
                    headers=arguments.get("headers")
                )
            elif name == "unipile_list_webhooks":
                result = unipile.list_webhooks()
            elif name == "unipile_delete_webhook":
                result = unipile.delete_webhook(
                    webhook_id=arguments["webhook_id"]
                )
            # ── Cross-platform tool dispatch (NEW) ──────────────────
            elif name == "unipile_get_user_profile":
                result = unipile.get_user_profile(
                    identifier=arguments["identifier"],
                    account_id=arguments["account_id"],
                    linkedin_sections=arguments.get("linkedin_sections"),
                )
            elif name == "unipile_get_followers":
                result = unipile.get_followers(
                    account_id=arguments["account_id"],
                    identifier=arguments.get("identifier"),
                    limit=arguments.get("limit", 50),
                    cursor=arguments.get("cursor"),
                )
            elif name == "unipile_get_following":
                result = unipile.get_following(
                    account_id=arguments["account_id"],
                    identifier=arguments.get("identifier"),
                    limit=arguments.get("limit", 50),
                    cursor=arguments.get("cursor"),
                )
            elif name == "unipile_sync_chat":
                result = unipile.sync_chat(
                    chat_id=arguments["chat_id"],
                    account_id=arguments.get("account_id"),
                )
            elif name == "unipile_get_chat_attendees":
                result = unipile.get_chat_attendees(
                    chat_id=arguments["chat_id"],
                    account_id=arguments.get("account_id"),
                )
            else:
                raise ValueError(f"Unknown tool: {name}")

            return [types.TextContent(
                type="text",
                text=result,
                mimeType="application/json"
            )]

        except Exception as e:
            logger.error(f"Error executing tool {name}: {str(e)}")
            return [types.TextContent(
                type="text",
                text=json.dumps({"error": str(e)}),
                mimeType="application/json"
            )]

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        logger.info("Server running with stdio transport")
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="unipile-extended",
                server_version="1.0.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
