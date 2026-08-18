from unittest.mock import MagicMock, patch

from mcp_server_unipile_extended.unipile_client_extended import UnipileClientExtended


class DummyClient(UnipileClientExtended):
    def __init__(self):
        self.base_url = "https://example.unipile.test"
        self.headers = {"X-API-KEY": "test-key", "accept": "application/json"}


def test_delete_post_empty_body():
    client = DummyClient()
    response = MagicMock()
    response.content = b""
    response.status_code = 204
    response.raise_for_status.return_value = None
    with patch("mcp_server_unipile_extended.unipile_client_extended.requests.delete", return_value=response) as mocked:
        result = client.delete_post("acc-1", "post-9")
    mocked.assert_called_once()
    assert mocked.call_args.kwargs["params"] == {"account_id": "acc-1"}
    assert result["object"] == "PostDeleted"
    assert result["post_id"] == "post-9"
    assert result["account_id"] == "acc-1"


def test_attachment_content_type_webp():
    assert DummyClient._attachment_content_type("/tmp/photo.webp") == "image/webp"
    assert DummyClient._attachment_content_type("/tmp/photo.jpg") == "image/jpeg"
