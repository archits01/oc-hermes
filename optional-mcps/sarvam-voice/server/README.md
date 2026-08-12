# Sarvam Voice MCP

Replaces `bolna-voice`. Drives Sarvam Voice Agents ("samvaad") to place outbound
AI calls and fetch call data/transcripts.

Auth: `X-API-Key` with a Voice-Agents key (`sk_samvaad_...`) from
indus.sarvam.ai → Voice Agents → Settings → API Key. The LLM/Speech "Sarvam API"
key does NOT work here.

Env (written to `~/.hermes/.env` at install): `SARVAM_API_KEY`, `SARVAM_ORG_ID`,
`SARVAM_WORKSPACE_ID`, and optional agent defaults `SARVAM_AGENT_ID`,
`SARVAM_APP_VERSION`, `SARVAM_CONNECTION_ID`, `SARVAM_FROM_NUMBER`.

Tools: `sarvam_list_agents`, `sarvam_place_call`, `sarvam_list_call_attempts`,
`sarvam_get_transcript`.
