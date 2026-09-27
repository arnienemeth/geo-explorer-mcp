"""Entry point kept at the repository root.

The implementation lives in src/geo_explorer_mcp/server.py so the project can
be packaged and published. This shim keeps the paths that already work --
the Claude Desktop config, `fastmcp dev inspector server.py`, and the probe
scripts' `from server import mcp` -- pointing at the same server object.
"""

from geo_explorer_mcp.server import main, mcp

__all__ = ["mcp", "main"]

if __name__ == "__main__":
    main()
