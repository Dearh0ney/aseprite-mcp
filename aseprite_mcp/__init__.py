"""Aseprite MCP - Model Context Protocol implementation for Aseprite."""

from mcp.server import MCPServer
from .core.security import guard_tool


class SafeMCPServer(MCPServer):
    def tool(self, *args, **kwargs):
        register = super().tool(*args, **kwargs)
        return lambda function: register(guard_tool(function))


mcp = SafeMCPServer("aseprite")

__version__ = "0.1.0"
__author__ = "Divyansh Singh"
