"""MCP-клиент агента на langchain-mcp-adapters. Здесь нет прямого доступа к графу или API платформы."""
import os
import sys
from contextlib import asynccontextmanager
from datetime import timedelta

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from .config import Settings

SERVER = "iacpaas"


def connection(settings: Settings, elicitation=None) -> dict:
    """Как подключиться к нашему MCP-серверу: отдельный процесс по stdio или HTTP.

    elicitation — обработчик запросов подтверждения от сервера (MCP elicitation). Без него
    клиент не заявляет эту возможность, и сервер отказывает в записи (CONFIRMATION_UNAVAILABLE).
    """
    session_kwargs = {"elicitation_callback": elicitation} if elicitation else None
    if settings.mcp_transport == "stdio":
        environment = {**os.environ, "DATA_DIR": str(settings.data_dir),
                       "RESOURCES_PATH": str(settings.resources_path)}
        return {"transport": "stdio", "command": sys.executable, "env": environment,
                "args": ["-m", "materials_chat.mcp_server", "--transport", "stdio"], "session_kwargs": session_kwargs}
    # Вызов может ждать подтверждения пользователя (до WRITE_CONFIRM_TIMEOUT_SECONDS) и затем
    # платформу (до API_TIMEOUT_SECONDS; изменение вершины — выгрузка и импорт).
    wait = timedelta(seconds=settings.write_confirm_timeout_seconds + 2 * settings.api_timeout_seconds + 30)
    return {"transport": "streamable_http", "url": settings.mcp_url, "timeout": wait, "sse_read_timeout": wait,
            "session_kwargs": session_kwargs}


@asynccontextmanager
async def mcp_tools(settings: Settings, elicitation=None):
    """Одна MCP-сессия на вопрос; инструменты сервера — в виде инструментов LangChain."""
    client = MultiServerMCPClient({SERVER: connection(settings, elicitation)})
    async with client.session(SERVER) as session:
        # Схемы аргументов приходят из JSON Schema сервера (tools/list).
        yield await load_mcp_tools(session, server_name=SERVER)
