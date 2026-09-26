"""Allow ``python -m opencode_image_mcp`` to start the stdio MCP server."""

from __future__ import annotations

from .server import main

if __name__ == "__main__":
    main()
