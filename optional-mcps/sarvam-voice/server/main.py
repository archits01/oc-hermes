"""Sarvam Voice AI MCP Server for Hermes/OpenComputer Gateway.

Replaces the Bolna voice MCP. Tools for placing outbound AI calls and fetching
call data via Sarvam's Voice Agents ("samvaad") API.

Auth: X-API-Key with a Voice-Agents key created at
  indus.sarvam.ai -> Voice Agents -> Settings -> API Key
(the LLM/Speech "Sarvam API" key does NOT work here).

Config (env, never hardcode secrets):
  SARVAM_API_KEY        - the sk_samvaad_... key   (required)
  SARVAM_ORG_ID         - organisation id          (required)
  SARVAM_WORKSPACE_ID   - workspace id             (required)
  SARVAM_AGENT_ID       - default agent app_id     (optional; per-call override allowed)
  SARVAM_APP_VERSION    - default agent version    (optional)
  SARVAM_CONNECTION_ID  - telephony connection id  (optional)
  SARVAM_FROM_NUMBER    - agent_phone_number       (optional)

Design mirrors the old Bolna MCP: outputs are compact to save LLM context.
"""
from mcp.server.fastmcp import FastMCP
import os
import json
import httpx

mcp = FastMCP("SarvamVoiceAI")

API_KEY = os.environ.get("SARVAM_API_KEY", "")
ORG = os.environ.get("SARVAM_ORG_ID", "")
WS = os.environ.get("SARVAM_WORKSPACE_ID", "")
BASE = "https://apps.sarvam.ai/api"

# Defaults (per-call args override these)
DEF_AGENT = os.environ.get("SARVAM_AGENT_ID", "")
DEF_VERSION = os.environ.get("SARVAM_APP_VERSION", "")
DEF_CONNECTION = os.environ.get("SARVAM_CONNECTION_ID", "")
DEF_FROM = os.environ.get("SARVAM_FROM_NUMBER", "")

HEADERS = {"X-API-Key": API_KEY, "Content-Type": "application/json"}


def _cfg_error() -> dict | None:
    missing = [k for k, v in (("SARVAM_API_KEY", API_KEY), ("SARVAM_ORG_ID", ORG),
                              ("SARVAM_WORKSPACE_ID", WS)) if not v]
    if missing:
        return {"error": "not_configured", "missing": missing}
    return None


async def _request(method: str, url: str, *, json_body: dict = None, params: dict = None) -> dict | list:
    async with httpx.AsyncClient() as c:
        try:
            r = await c.request(method, url, headers=HEADERS, json=json_body, params=params, timeout=30.0)
            r.raise_for_status()
            return r.json() if r.content else {}
        except httpx.HTTPStatusError as e:
            return {"error": f"HTTP {e.response.status_code}", "detail": e.response.text[:300]}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}


@mcp.tool()
async def sarvam_list_agents() -> dict:
    """List voice-agent deployments (id, name, version) in the workspace."""
    err = _cfg_error()
    if err:
        return err
    url = f"{BASE}/app-authoring/v1/orgs/{ORG}/workspaces/{WS}/deployments"
    data = await _request("GET", url)
    if isinstance(data, dict) and "items" in data:
        return {"total": data.get("total", 0),
                "agents": [{"id": i.get("id") or i.get("app_id"),
                            "name": i.get("name"),
                            "version": i.get("app_version") or i.get("version")}
                           for i in data.get("items", [])]}
    return data


@mcp.tool()
async def sarvam_place_call(
    user_phone_number: str,
    agent_variables: dict | None = None,
    agent_id: str | None = None,
    app_version: str | None = None,
    connection_id: str | None = None,
    agent_phone_number: str | None = None,
    initial_bot_message: str | None = None,
    webhook_url: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Place a single outbound AI call (Sarvam "instant outbound").

    user_phone_number: destination (E.164). agent_variables: dynamic vars passed to
    the agent (e.g. {"customer_name": "..."}). agent_id/app_version/connection_id/
    agent_phone_number default to the SARVAM_* env values if omitted.
    Returns Sarvam's response incl. attempt_id.
    """
    err = _cfg_error()
    if err:
        return err
    aid = agent_id or DEF_AGENT
    ver = app_version or DEF_VERSION
    conn = connection_id or DEF_CONNECTION
    frm = agent_phone_number or DEF_FROM
    missing = [n for n, v in (("agent_id", aid), ("app_version", ver),
                              ("connection_id", conn), ("agent_phone_number", frm)) if not v]
    if missing:
        return {"error": "missing_agent_config", "missing": missing,
                "hint": "set SARVAM_AGENT_ID/APP_VERSION/CONNECTION_ID/FROM_NUMBER or pass them explicitly"}

    # Sarvam expects app_version as a number when it is numeric (e.g. 3), else a string.
    ver_val: object = int(ver) if str(ver).isdigit() else ver
    app_config: dict = {
        "app_id": aid,
        "app_version": ver_val,
        "app_type": "agent",
        "connection_config": {"connection_id": conn, "agent_phone_number": frm},
    }
    if agent_variables:
        app_config["agent_variables"] = agent_variables
    if initial_bot_message:
        app_config["app_overrides"] = {"initial_bot_message": initial_bot_message}

    body: dict = {"app_config": app_config, "user_config": {"user_phone_number": user_phone_number}}
    if webhook_url:
        body["webhook_config"] = {"url": webhook_url, "metadata": metadata or {}}

    url = f"{BASE}/outbounds/v1/orgs/{ORG}/workspaces/{WS}/outbounds"
    return await _request("POST", url, json_body=body)


@mcp.tool()
async def sarvam_list_call_attempts(start_datetime: str, end_datetime: str,
                                    limit: int = 20, offset: int = 0) -> dict:
    """List call attempts (outcomes/status) in an ISO8601 time window.

    start_datetime/end_datetime are REQUIRED ISO8601 strings. limit<=1000.
    """
    err = _cfg_error()
    if err:
        return err
    url = f"{BASE}/analytics/v1/{ORG}/{WS}/list-attempts"
    params = {"start_datetime": start_datetime, "end_datetime": end_datetime,
              "limit": limit, "offset": offset}
    return await _request("GET", url, params=params)


@mcp.tool()
async def sarvam_get_transcript(interaction_id: str, start_datetime: str, end_datetime: str) -> dict:
    """Fetch the transcript for a call/interaction within a time window."""
    err = _cfg_error()
    if err:
        return err
    url = f"{BASE}/analytics/v1/{ORG}/{WS}/get-transcript"
    params = {"start_datetime": start_datetime, "end_datetime": end_datetime,
              "filter_conditions": json.dumps([
                  {"id": "1", "field": "interaction_id", "operator": "equals", "value": interaction_id}
              ])}
    return await _request("GET", url, params=params)


if __name__ == "__main__":
    mcp.run()
