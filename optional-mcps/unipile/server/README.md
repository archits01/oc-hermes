# Extended Unipile MCP Server

Extended MCP server for Unipile with full send/write capabilities for sales outreach automation.

## Features

**READ Operations:**
- ✅ Get connected accounts (LinkedIn, Email, WhatsApp, etc.)
- ✅ Get LinkedIn connections list
- ✅ Get conversations/chats
- ✅ Get messages from chats

**WRITE Operations - LinkedIn:**
- ✅ Send LinkedIn direct messages
- ✅ Create LinkedIn posts

**WRITE Operations - Instagram:**
- ✅ Create Instagram feed posts (`unipile_create_instagram_post`, requires media + Instagram account_id)

**WRITE Operations - Email:**
- ✅ Send emails with CC/BCC support

## Installation

```bash
cd /tmp/mcp-unipile-extended
python3 -m venv venv
source venv/bin/activate
pip install -e .
```

## Configuration

Set environment variables:
```bash
export UNIPILE_DSN="your-dsn-here"
export UNIPILE_API_KEY="your-api-key-here"
```

## Tools Available

1. `unipile_get_accounts` - List all connected accounts
2. `unipile_get_connections` - Get LinkedIn connections
3. `unipile_get_chats` - List conversations
4. `unipile_get_messages` - Get chat messages
5. `unipile_send_linkedin_message` - Send LinkedIn DM
6. `unipile_create_linkedin_post` - Post to LinkedIn
7. `unipile_create_instagram_post` - Post to Instagram (account_id + image required)
8. `unipile_send_email` - Send email

Connection requests and post deletion are intentionally unavailable. The
provider routes previously used for those actions were not verified against
the supported Unipile API contract, so the client fails loudly without making
an outbound request if called directly by legacy code.

Perfect for sales automation, outreach campaigns, and lead engagement.
