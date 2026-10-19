"""MCP-клиент агента. Здесь нет прямого доступа к графу или API платформы."""
import json
import os
import sys
from contextlib import AsyncExitStack

import httpx

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from .config import Settings


class MCPBridge:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.stack = AsyncExitStack()
        self.session = None

    async def __aenter__(self):
        try:
            if self.settings.mcp_transport == "stdio":
                environment = {**os.environ, "DATA_DIR": str(self.settings.data_dir),
                               "RESOURCES_PATH": str(self.settings.resources_path)}
                params = StdioServerParameters(command=sys.executable, args=["-m", "materials_chat.mcp_server", "--transport", "stdio"], env=environment)
                read, write = await self.stack.enter_async_context(stdio_client(params))
            else:
                client = await self.stack.enter_async_context(httpx.AsyncClient(timeout=httpx.Timeout(90, connect=10)))
                read, write, _ = await self.stack.enter_async_context(streamable_http_client(
                    self.settings.mcp_url, http_client=client))
            self.session = await self.stack.enter_async_context(ClientSession(read, write))
            await self.session.initialize()
            return self
        except BaseException:
            await self.stack.aclose()
            raise

    async def __aexit__(self, *args):
        return await self.stack.__aexit__(*args)

    async def list_tools(self) -> list[dict]:
        result = await self.session.list_tools()
        return [{"type": "function", "function": {"name": tool.name, "description": tool.description or "",
                 "parameters": tool.inputSchema}} for tool in result.tools]

    async def call_tool(self, name: str, arguments: dict) -> dict:
        result = await self.session.call_tool(name, arguments)
        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, dict):
            return structured
        for block in result.content:
            if getattr(block, "type", "") == "text":
                try:
                    value = json.loads(block.text)
                    if isinstance(value, dict):
                        return value
                except ValueError:
                    pass
        return {"ok": False, "data": {}, "error": {"code": "MCP_ERROR", "message": "MCP не вернул структурированный результат.", "retryable": False}, "meta": {}}
