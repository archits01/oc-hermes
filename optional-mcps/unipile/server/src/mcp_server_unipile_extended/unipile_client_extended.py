import requests
import logging
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


class UnsupportedUnipileCapability(NotImplementedError):
    """Raised when this bridge cannot safely implement a requested action.

    The provider routes previously used for connection requests and post
    deletion are not part of the verified Unipile API contract.  Keeping a
    typed, loud failure here prevents callers that import the client directly
    from mistaking a disabled capability for a successful no-op.
    """


class UnipileClientExtended:
    def __init__(self, dsn: str, api_key: str):
        """Initialize the Unipile client with full capabilities"""
        self.base_url = f"https://{dsn}"
        self.headers = {
            'X-API-KEY': api_key,
            'accept': 'application/json',
            'Content-Type': 'application/json'
        }

    # READ operations
    def get_accounts(self) -> List[Dict]:
        """Get all connected accounts"""
        url = f"{self.base_url}/api/v1/accounts"
        response = requests.get(url, headers=self.headers)
        response.raise_for_status()
        data = response.json()
        if data.get("object") == "AccountList":
            return data.get("items", [])
        return []

    def get_connections(self, account_id: str, limit: int = 50, cursor: str = None) -> Dict:
        """Get LinkedIn connections/relations for an account"""
        url = f"{self.base_url}/api/v1/users/relations"
        params = {'account_id': account_id, 'limit': limit}
        if cursor:
            params['cursor'] = cursor
        response = requests.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_chats(self, account_id: str, limit: int = 10) -> List[Dict]:
        """Get available chats for a specific account"""
        url = f"{self.base_url}/api/v1/chats"
        params = {'account_id': account_id, 'limit': limit}
        response = requests.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        data = response.json()
        if data.get("object") == "ChatList":
            return data.get("items", [])
        return []

    def get_messages(self, chat_id: str, limit: int = 20) -> List[Dict]:
        """Get messages from a chat"""
        url = f"{self.base_url}/api/v1/chats/{chat_id}/messages"
        params = {'limit': limit}
        response = requests.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        data = response.json()
        if data.get("object") == "MessageList":
            return data.get("items", [])
        return []

    # WRITE operations - LinkedIn
    def send_message(self, account_id: str, text: str, chat_id: Optional[str] = None,
                    attendee_id: Optional[str] = None) -> Dict:
        """Send a LinkedIn message.

        - If chat_id is provided, sends to an existing conversation via
          POST /api/v1/chats/{chat_id}/messages.
        - If only attendee_id is provided (new contact), starts a new
          conversation via POST /api/v1/chats with attendees_ids.
        """
        if chat_id:
            # Existing conversation
            url = f"{self.base_url}/api/v1/chats/{chat_id}/messages"
            payload = {'text': text}
            response = requests.post(url, headers=self.headers, json=payload)
            response.raise_for_status()
            return response.json()
        elif attendee_id:
            # New conversation — create chat with first message
            url = f"{self.base_url}/api/v1/chats"
            payload = {
                'account_id': account_id,
                'attendees_ids': [attendee_id],
                'text': text,
            }
            response = requests.post(url, headers=self.headers, json=payload)
            response.raise_for_status()
            return response.json()
        else:
            raise ValueError("Either chat_id or attendee_id must be provided")

    def send_connection_request(self, account_id: str, profile_url: str,
                               message: Optional[str] = None) -> Dict:
        """Reject the unverified connection-request route without a request.

        ``/api/v1/hosted/accounts/{account_id}/users`` was previously treated
        as a LinkedIn connection-request endpoint, but it is not a verified
        public Unipile operation for this bridge.  Do not silently fall back
        to a guessed provider route.
        """
        del account_id, profile_url, message
        raise UnsupportedUnipileCapability(
            "LinkedIn connection requests are unsupported: no verified "
            "Unipile endpoint is configured; no provider request was sent"
        )

    @staticmethod
    def _attachment_content_type(path: str) -> str:
        ext = __import__("os").path.splitext(path)[1].lower()
        return {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".gif": "image/gif",
            ".mp4": "video/mp4",
        }.get(ext, "application/octet-stream")

    def create_post(self, account_id: str, text: str,
                   image_path: Optional[str] = None,
                   external_link: Optional[str] = None) -> Dict:
        """Create a LinkedIn or Instagram post for the given account_id.

        Uses POST /api/v1/posts with multipart/form-data.
        account_id selects the connected account/platform.
        Instagram requires image_path. LinkedIn image is optional.
        """
        import tempfile, os as _os
        url = f"{self.base_url}/api/v1/posts"
        headers = {"X-API-KEY": self.headers["X-API-KEY"], "accept": "application/json"}

        multipart_fields = {
            "account_id": (None, account_id),
            "text": (None, text),
        }
        if external_link:
            multipart_fields["external_link"] = (None, external_link)

        file_handle = None
        tmp_file = None
        try:
            if image_path:
                if image_path.startswith(("http://", "https://")):
                    r = requests.get(image_path, timeout=60)
                    r.raise_for_status()
                    ct = r.headers.get("content-type", "image/png")
                    ext = ".png"
                    if "jpeg" in ct or "jpg" in ct:
                        ext = ".jpg"
                    elif "webp" in ct:
                        ext = ".webp"
                    elif "mp4" in ct:
                        ext = ".mp4"
                    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
                    tmp.write(r.content)
                    tmp.close()
                    tmp_file = tmp.name
                    image_path = tmp_file

                file_handle = open(image_path, "rb")
                multipart_fields["attachments"] = (
                    _os.path.basename(image_path),
                    file_handle,
                    self._attachment_content_type(image_path),
                )

            response = requests.post(url, headers=headers, files=multipart_fields)
            response.raise_for_status()
            return response.json()
        finally:
            if file_handle:
                file_handle.close()
            if tmp_file and _os.path.exists(tmp_file):
                _os.unlink(tmp_file)

    def delete_post(self, account_id: str, post_id: str) -> Dict:
        """Reject the unverified post-deletion route without a request."""
        del account_id, post_id
        raise UnsupportedUnipileCapability(
            "Post deletion is unsupported: no verified Unipile endpoint is "
            "configured; no provider request was sent"
        )

    def send_email(self, account_id: str, to: List[str], subject: str, body: str,
                  cc: Optional[List[str]] = None, bcc: Optional[List[str]] = None) -> Dict:
        """Send an email"""
        url = f"{self.base_url}/api/v1/emails"
        payload = {'account_id': account_id, 'to': to, 'subject': subject, 'body': body}
        if cc:
            payload['cc'] = cc
        if bcc:
            payload['bcc'] = bcc
        response = requests.post(url, headers=self.headers, json=payload)
        response.raise_for_status()
        return response.json()

    # LINKEDIN SEARCH operations
    def linkedin_search(self, account_id: str, search_params: Dict) -> Dict:
        """
        Perform LinkedIn search (people, companies, jobs, posts)

        Args:
            account_id: LinkedIn account ID
            search_params: Search parameters:
                - api: "classic", "sales_navigator", or "recruiter"
                - category: "people", "companies", "jobs", or "posts"
                - keywords: search keywords
                - location: list of location IDs
                - industry: dict with "include" or "exclude" lists
                - url: alternatively, paste full LinkedIn search URL
        """
        url = f"{self.base_url}/api/v1/linkedin/search"
        params = {'account_id': account_id}
        response = requests.post(url, headers=self.headers, params=params, json=search_params)
        response.raise_for_status()
        return response.json()

    def search_linkedin_people(self, account_id: str, **kwargs) -> Dict:
        """Search for people on LinkedIn using flexible parameters"""
        search_params = {
            "api": "classic",
            "category": "people"
        }
        # Add all provided kwargs to search params
        for key, value in kwargs.items():
            if value is not None:
                search_params[key] = value

        return self.linkedin_search(account_id=account_id, search_params=search_params)

    def search_linkedin_companies(self, account_id: str, **kwargs) -> Dict:
        """Search for companies on LinkedIn using flexible parameters"""
        search_params = {
            "api": "classic",
            "category": "companies"
        }
        for key, value in kwargs.items():
            if value is not None:
                search_params[key] = value

        return self.linkedin_search(account_id=account_id, search_params=search_params)

    def search_linkedin_jobs(self, account_id: str, **kwargs) -> Dict:
        """Search for job postings on LinkedIn using flexible parameters"""
        search_params = {
            "api": "classic",
            "category": "jobs"
        }
        for key, value in kwargs.items():
            if value is not None:
                search_params[key] = value

        return self.linkedin_search(account_id=account_id, search_params=search_params)

    def get_linkedin_search_parameters(self, account_id: str, param_type: str,
                                       keywords: str, limit: int = 100) -> Dict:
        """
        Get LinkedIn search parameter IDs (for location, industry, skills, etc.)

        Args:
            account_id: LinkedIn account ID
            param_type: LOCATION, INDUSTRY, SKILL, SCHOOL, COMPANY, etc.
            keywords: Search keywords to find parameter IDs
            limit: Max results (default: 100)
        """
        url = f"{self.base_url}/api/v1/linkedin/search/parameters"
        params = {
            'account_id': account_id,
            'type': param_type,
            'keywords': keywords,
            'limit': limit
        }
        response = requests.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()


    # POST ENGAGEMENT operations
    def get_post(self, account_id: str, post_id: str) -> Dict:
        """Retrieve a single post's details (engagement counters, content, etc.).

        POST /api/v1/posts/{post_id}?account_id=X
        For LinkedIn, post_id can be numeric activity ID or URN format.
        """
        url = f"{self.base_url}/api/v1/posts/{post_id}"
        params = {"account_id": account_id}
        response = requests.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def list_user_posts(self, account_id: str, identifier: str,
                        limit: int = 10, cursor: Optional[str] = None) -> Dict:
        """List posts by a user or company.

        GET /api/v1/users/{identifier}/posts?account_id=X
        identifier: provider_id (ACo/ADo for users, numeric for companies).
        """
        url = f"{self.base_url}/api/v1/users/{identifier}/posts"
        params: Dict = {"account_id": account_id, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        response = requests.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def list_post_reactions(self, account_id: str, post_id: str,
                            limit: int = 50, cursor: Optional[str] = None) -> Dict:
        """List reactions on a post.

        GET /api/v1/posts/{post_id}/reactions?account_id=X
        post_id must be social_id from the post object.
        """
        url = f"{self.base_url}/api/v1/posts/{post_id}/reactions"
        params: Dict = {"account_id": account_id, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        response = requests.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def list_post_comments(self, account_id: str, post_id: str,
                           limit: int = 50, sort_by: str = "MOST_RECENT",
                           cursor: Optional[str] = None) -> Dict:
        """List comments on a post.

        GET /api/v1/posts/{post_id}/comments?account_id=X
        post_id must be social_id. sort_by: MOST_RECENT or MOST_RELEVANT.
        """
        url = f"{self.base_url}/api/v1/posts/{post_id}/comments"
        params: Dict = {"account_id": account_id, "limit": limit, "sort_by": sort_by}
        if cursor:
            params["cursor"] = cursor
        response = requests.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def reply_to_comment(self, account_id: str, post_id: str, text: str,
                         comment_id: Optional[str] = None) -> Dict:
        """Reply to a comment on a post (or add a top-level comment).

        POST /api/v1/posts/{post_id}/comments
        comment_id: if provided, creates a threaded reply to that comment.
        """
        url = f"{self.base_url}/api/v1/posts/{post_id}/comments"
        payload: Dict = {"account_id": account_id, "text": text}
        if comment_id:
            payload["comment_id"] = comment_id
        response = requests.post(url, headers=self.headers, json=payload)
        response.raise_for_status()
        return response.json()

    # WEBHOOK MANAGEMENT
    def create_webhook(self, source: str, request_url: str, name: str,
                      headers: Optional[List[Dict]] = None) -> Dict:
        """
        Create a webhook to receive real-time notifications.

        Args:
            source: Webhook source type ("messages", "users", "emails", "calendars", "tracking")
            request_url: Your webhook endpoint URL
            name: Webhook name
            headers: Optional headers to send with webhook (list of {key, value} dicts)

        Returns:
            Created webhook details
        """
        url = f"{self.base_url}/api/v1/webhooks"
        payload = {
            "source": source,
            "request_url": request_url,
            "name": name
        }
        if headers:
            payload["headers"] = headers

        response = requests.post(url, headers=self.headers, json=payload)
        response.raise_for_status()
        return response.json()

    def list_webhooks(self) -> Dict:
        """
        List all configured webhooks.

        Returns:
            List of webhooks
        """
        url = f"{self.base_url}/api/v1/webhooks"
        response = requests.get(url, headers=self.headers)
        response.raise_for_status()
        return response.json()

    def delete_webhook(self, webhook_id: str) -> Dict:
        """
        Delete a webhook.

        Args:
            webhook_id: The webhook ID to delete

        Returns:
            Deletion confirmation
        """
        url = f"{self.base_url}/api/v1/webhooks/{webhook_id}"
        response = requests.delete(url, headers=self.headers)
        response.raise_for_status()
        return response.json()

    # ── Cross-platform tools (added for multi-platform support) ────────

    def get_user_profile(self, identifier: str, account_id: str,
                         linkedin_sections: str = None) -> Dict:
        """Get user profile by identifier (cross-platform).
        LinkedIn: provider_id or slug. Instagram: username. WhatsApp: phone@s.whatsapp.net."""
        url = f"{self.base_url}/api/v1/users/{identifier}"
        params = {"account_id": account_id}
        if linkedin_sections:
            params["linkedin_sections"] = linkedin_sections
        response = requests.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_followers(self, account_id: str, identifier: str = None,
                      limit: int = 50, cursor: str = None) -> Dict:
        """List followers for an account (Instagram, LinkedIn, X)."""
        url = f"{self.base_url}/api/v1/users/followers"
        params = {"account_id": account_id, "limit": limit}
        if identifier:
            params["identifier"] = identifier
        if cursor:
            params["cursor"] = cursor
        response = requests.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_following(self, account_id: str, identifier: str = None,
                      limit: int = 50, cursor: str = None) -> Dict:
        """List accounts being followed (Instagram, LinkedIn, X)."""
        url = f"{self.base_url}/api/v1/users/following"
        params = {"account_id": account_id, "limit": limit}
        if identifier:
            params["identifier"] = identifier
        if cursor:
            params["cursor"] = cursor
        response = requests.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def sync_chat(self, chat_id: str, account_id: str = None) -> Dict:
        """Trigger chat history sync. Critical for WhatsApp. GET not POST."""
        url = f"{self.base_url}/api/v1/chats/{chat_id}/sync"
        params = {}
        if account_id:
            params["account_id"] = account_id
        response = requests.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_chat_attendees(self, chat_id: str, account_id: str = None) -> Dict:
        """List attendees of a specific chat."""
        url = f"{self.base_url}/api/v1/chats/{chat_id}/attendees"
        params = {}
        if account_id:
            params["account_id"] = account_id
        response = requests.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()
