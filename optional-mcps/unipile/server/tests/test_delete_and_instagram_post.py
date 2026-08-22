from unittest.mock import patch

import pytest

from mcp_server_unipile_extended.unipile_client_extended import (
    UnipileClientExtended,
    UnsupportedUnipileCapability,
)


class DummyClient(UnipileClientExtended):
    def __init__(self):
        self.base_url = "https://example.unipile.test"
        self.headers = {"X-API-KEY": "test-key", "accept": "application/json"}


def test_delete_post_is_explicitly_unsupported_without_provider_request():
    client = DummyClient()
    with patch("mcp_server_unipile_extended.unipile_client_extended.requests.delete") as mocked:
        with pytest.raises(UnsupportedUnipileCapability, match="Post deletion is unsupported"):
            client.delete_post("acc-1", "post-9")
    mocked.assert_not_called()


def test_connection_request_is_explicitly_unsupported_without_provider_request():
    with patch("mcp_server_unipile_extended.unipile_client_extended.requests.post") as mocked:
        with pytest.raises(UnsupportedUnipileCapability, match="connection requests are unsupported"):
            DummyClient().send_connection_request(
                "acc-1", "https://linkedin.example/in/person", "hello"
            )
    mocked.assert_not_called()


def test_attachment_content_type_webp():
    assert DummyClient._attachment_content_type("/tmp/photo.webp") == "image/webp"
    assert DummyClient._attachment_content_type("/tmp/photo.jpg") == "image/jpeg"
