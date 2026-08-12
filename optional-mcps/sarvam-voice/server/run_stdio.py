"""Stdio entry point for Sarvam Voice MCP server (used by the Hermes/OpenComputer gateway)."""
from main import mcp

if __name__ == "__main__":
    mcp.run(transport="stdio")
