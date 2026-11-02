"""Интеграция с VS Code: конфигурация MCP-сервера и свой агент согласованы с настоящим сервером."""
import json
import re

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from materials_chat.config import PROJECT_ROOT, Settings
from materials_chat.mcp_server import create_server

MCP_JSON = PROJECT_ROOT / ".vscode" / "mcp.json"
AGENT = PROJECT_ROOT / ".github" / "agents" / "iacpaas-materials.agent.md"
REAL_INDEX = PROJECT_ROOT / "data" / "search.sqlite3"


def server_config() -> dict:
    return json.loads(MCP_JSON.read_text(encoding="utf-8"))["servers"]["iacpaas"]


def expand(value: str) -> str:
    return value.replace("${workspaceFolder}", str(PROJECT_ROOT))


def agent_parts() -> tuple[str, str]:
    text = AGENT.read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
    assert match, "у агента должен быть YAML-заголовок"
    return match.group(1), match.group(2)


def test_workspace_config_starts_our_server_over_stdio():
    config = server_config()
    assert config["type"] == "stdio"
    assert config["args"] == ["-m", "materials_chat.mcp_server", "--transport", "stdio"]
    assert config["env"]["PYTHONIOENCODING"] == "utf-8"


@pytest.mark.asyncio
async def test_agent_allows_only_our_server_and_names_only_real_tools(tmp_path):
    header, body = agent_parts()
    assert re.search(r"^name: .+", header, re.M) and re.search(r"^description: .+", header, re.M)
    assert re.search(r"^tools: \['iacpaas/\*'\]$", header, re.M)  # имя сервера из .vscode/mcp.json, только его инструменты
    tools = {tool.name for tool in await create_server(Settings(_env_file=None, data_dir=tmp_path)).list_tools()}
    mentioned = set(re.findall(r"`([a-z]+_[a-z_]+)`", body))
    assert tools <= mentioned  # агент описывает каждый инструмент сервера
    prefixes = ("list_", "get_", "search_", "find_", "count_", "validate_", "write_", "delete_")
    assert {name for name in mentioned if name.startswith(prefixes)} <= tools  # и не называет несуществующих


@pytest.mark.asyncio
@pytest.mark.skipif(not REAL_INDEX.exists(), reason="нет локального индекса data/search.sqlite3")
async def test_server_launched_like_vscode_answers_in_one_call():
    """Запуск ровно по .vscode/mcp.json: команда, аргументы, рабочая папка, окружение."""
    config = server_config()
    command = expand(config["command"])
    if not (PROJECT_ROOT / ".venv").exists():
        pytest.skip("нет .venv проекта")
    import os
    params = StdioServerParameters(command=command, args=config["args"], cwd=expand(config["cwd"]),
                                   env={**os.environ, **config["env"]})
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        names = {tool.name for tool in (await session.list_tools()).tools}
        result = await session.call_tool("find_materials", {"conditions": [{"element": "Fe", "op": ">", "value": 5}],
                                                            "limit": 1})
    assert len(names) == 9
    assert result.structuredContent["ok"] and result.structuredContent["data"]["total"] > 1000
