"""MCP-клиент агента на langchain-mcp-adapters. Здесь нет прямого доступа к графу или API платформы."""
import os
import sys
from contextlib import asynccontextmanager
from datetime import timedelta

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from .config import Settings

SERVER = "iacpaas"


def connection(settings: Settings) -> dict:
    """Как подключиться к нашему MCP-серверу: отдельный процесс по stdio или HTTP."""
    if settings.mcp_transport == "stdio":
        environment = {**os.environ, "DATA_DIR": str(settings.data_dir),
                       "RESOURCES_PATH": str(settings.resources_path)}
        return {"transport": "stdio", "command": sys.executable, "env": environment,
                "args": ["-m", "materials_chat.mcp_server", "--transport", "stdio"]}
    # Чтение с платформы может идти до API_TIMEOUT_SECONDS (по умолчанию 65 с).
    return {"transport": "streamable_http", "url": settings.mcp_url,
            "timeout": timedelta(seconds=90), "sse_read_timeout": timedelta(seconds=90)}


@asynccontextmanager
async def mcp_tools(settings: Settings):
    """Одна MCP-сессия на вопрос; инструменты сервера — в виде инструментов LangChain."""
    client = MultiServerMCPClient({SERVER: connection(settings)})
    async with client.session(SERVER) as session:
        # Схемы аргументов приходят из JSON Schema сервера (tools/list).
        yield await load_mcp_tools(session, server_name=SERVER)
