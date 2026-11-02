"""MCP-сервер записи через настоящий протокол MCP (сессия в памяти): инструменты, аннотации, подтверждение."""
import copy

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from materials_chat.config import PROJECT_ROOT, Settings
from materials_chat.mcp_server import create_server
from test_writes import ONTOLOGY, FakePlatform

READ_TOOLS = {"list_inforesources", "get_inforesource", "search_graph", "find_materials", "get_subgraph",
              "count_graph", "validate_resource"}
WRITE_TOOLS = {"write_inforesource", "delete_inforesource"}
CREATE = {"request": {"operation": "create_folder", "parent_path": "Демо агента", "name": "Сценарий"}}


def server(tmp_path, **settings):
    mcp = create_server(Settings(_env_file=None, data_dir=tmp_path / "data", resources_path=PROJECT_ROOT / "config" / "resources.json",
                                 iacpaas_username="", iacpaas_password="", openrouter_api_key="", **settings))
    platform = FakePlatform()
    mcp.graph_service.client = platform
    mcp.graph_service.store.import_graph("properties_ontology", copy.deepcopy(ONTOLOGY))
    return mcp, platform


def answering(action, confirm=True, seen=None):
    async def callback(context, params: types.ElicitRequestParams):
        if seen is not None:
            seen.append(params.message)
        if action == "silent":
            import asyncio
            await asyncio.sleep(7)  # дольше таймаута сервера (5 с)
        if action == "accept":
            return types.ElicitResult(action="accept", content={"confirm": confirm})
        return types.ElicitResult(action=action)
    return callback


@pytest.mark.asyncio
async def test_tools_and_annotations(tmp_path):
    mcp, _ = server(tmp_path)
    async with create_connected_server_and_client_session(mcp) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(tools) == READ_TOOLS | WRITE_TOOLS
    for name in READ_TOOLS:
        assert tools[name].annotations.readOnlyHint is True, name
    for name in WRITE_TOOLS:
        assert (tools[name].annotations.readOnlyHint, tools[name].annotations.destructiveHint) == (False, True)
    assert "path" in tools["get_inforesource"].inputSchema["properties"]
    assert "path" in tools["get_subgraph"].inputSchema["properties"]
    variants = tools["write_inforesource"].inputSchema["$defs"]
    assert set(variants["CreateFolder"]["required"]) == {"operation", "name"}
    assert set(variants["CreateResource"]["required"]) == {"operation", "folder_path", "name", "ontology"}
    assert set(variants["UpdateConcept"]["required"]) == {"operation", "path", "concept_path"}


@pytest.mark.asyncio
async def test_read_only_mode_is_the_old_tool_set(tmp_path):
    mcp, _ = server(tmp_path, write_enabled=False)
    async with create_connected_server_and_client_session(mcp) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(tools) == READ_TOOLS
    assert "path" not in tools["get_inforesource"].inputSchema["properties"]
    assert "path" not in tools["get_subgraph"].inputSchema["properties"]


@pytest.mark.asyncio
@pytest.mark.parametrize("callback, code", [
    (None, "CONFIRMATION_UNAVAILABLE"),
    (answering("decline"), "CONFIRMATION_DECLINED"),
    (answering("cancel"), "CONFIRMATION_DECLINED"),
    (answering("accept", confirm=False), "CONFIRMATION_DECLINED"),
    (answering("silent"), "CONFIRMATION_TIMEOUT"),
])
async def test_write_without_explicit_accept_does_nothing(tmp_path, callback, code):
    mcp, platform = server(tmp_path, write_confirm_timeout_seconds=5)
    async with create_connected_server_and_client_session(mcp, elicitation_callback=callback) as client:
        result = await client.call_tool("write_inforesource", CREATE)
    assert result.structuredContent["error"]["code"] == code
    assert platform.writes == []


@pytest.mark.asyncio
async def test_accepted_write_reaches_platform_and_message_is_built_by_server(tmp_path):
    mcp, platform = server(tmp_path)
    seen = []
    async with create_connected_server_and_client_session(mcp, elicitation_callback=answering("accept", seen=seen)) as client:
        result = await client.call_tool("write_inforesource", CREATE)
        protected = await client.call_tool("delete_inforesource", {"path": "Загрузки / Справочник по материалам"})
        read = await client.call_tool("get_subgraph", {"path": "Демо агента / Мои свойства", "depth": 2})
    assert result.structuredContent["ok"] is True
    assert seen == ["Создать папку «Сценарий» в «Демо агента»."]
    assert platform.writes == [("create_folder", "Демо агента", "Сценарий")]
    assert protected.structuredContent["error"]["code"] == "PROTECTED_RESOURCE"
    assert read.structuredContent["ok"] and read.structuredContent["meta"]["served_from"] == "iacpaas_live"
