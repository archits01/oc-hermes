"""Regression tests for provider-account scoping on live chat calls."""

import asyncio
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "unipile_common_account_scope",
    "/opt/opencomputer-v2/plugins/platforms/_unipile_common.py",
)
_module = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_module)
UnipileClient = _module.UnipileClient

class _Response:
    def __init__(self, payload):
        self._payload = payload
    def raise_for_status(self):
        return None
    def json(self):
        return self._payload

class _HTTP:
    def __init__(self):
        self.get_calls = []
        self.post_calls = []
    async def get(self, url, headers=None, params=None):
        self.get_calls.append((url, headers, params))
        return _Response({"items": []})
    async def post(self, url, headers=None, json=None):
        self.post_calls.append((url, headers, json))
        return _Response({"id": "provider-receipt"})

def test_send_and_history_include_exact_account_scope():
    async def run():
        http = _HTTP()
        client = UnipileClient("dsn", "key", http)
        await client.send_message(
            "chat-scope-test", "scope-test-unique", account_id="account-scope-test"
        )
        await client.get_messages(
            "chat-scope-test", limit=5, account_id="account-scope-test"
        )
        assert http.post_calls[0][2]["account_id"] == "account-scope-test"
        assert http.get_calls[0][2] == {
            "limit": 5, "account_id": "account-scope-test"
        }
    asyncio.run(run())
