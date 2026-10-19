"""Настоящие HTTP + MCP транспорты, но бесплатный локальный double вместо LLM."""
import asyncio
import json
import socket
from contextlib import asynccontextmanager, nullcontext

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from materials_chat.config import Settings
from materials_chat.graph_store import GraphStore
from materials_chat.mcp_server import create_server
from materials_chat.web import create_app


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@asynccontextmanager
async def serve(app, port):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    server.capture_signals = lambda: nullcontext()
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(.01)
        assert server.started
        yield
    finally:
        server.should_exit = True
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("found", [True, False])
async def test_http_chat_really_uses_mcp_and_counts_all_model_requests(tmp_path, found):
    config = tmp_path / "resources.json"
    config.write_text(json.dumps([{"id": "materials", "title": "Тестовая база", "path": "Тест", "kind": "information"}]), encoding="utf-8")
    mcp_port, web_port, provider_port = free_port(), free_port(), free_port()
    settings = Settings(_env_file=None, data_dir=tmp_path / "data", history_path=tmp_path / "history.sqlite3",
        resources_path=config, mcp_port=mcp_port, mcp_url=f"http://127.0.0.1:{mcp_port}/mcp",
        openrouter_api_key="local-test-key", openrouter_model="local-test-model",
        openrouter_base_url=f"http://127.0.0.1:{provider_port}", auto_sync_on_start=False,
        iacpaas_username="", iacpaas_password="")
    GraphStore(settings.data_dir / "search.sqlite3").import_graph("materials", {
        "id": 1, "name": "Каталог", "title": "Тестовая база", "type": "КОРЕНЬ", "json_type": "universal",
        "ontology": "Тестовая онтология", "successors": [{"id": 2, "name": "Тестовыйсплав", "type": "НЕТЕРМИНАЛ"}]})
    provider = FastAPI()
    requests = []

    @provider.post("/chat/completions")
    async def complete(request: Request):
        body = await request.json()
        requests.append(body)
        tool_messages = [message for message in body["messages"] if message["role"] == "tool"]
        if not tool_messages:
            choice = {"delta": {"tool_calls": [{"index": 0, "id": "call_search", "type": "function", "function": {
                "name": "search_graph", "arguments": json.dumps({"query": "Тестовыйсплав" if found else "несуществующий987654", "resource_id": "materials"}, ensure_ascii=False)}}]}, "finish_reason": "tool_calls"}
        else:
            choice = {"delta": {"content": "Найден Тестовыйсплав. Источник: Тестовая база, вершина 2." if found else "НЕПОДТВЕРЖДЁННОЕ УТВЕРЖДЕНИЕ"}, "finish_reason": "stop"}
        async def chunks():
            yield "data: " + json.dumps({"choices": [choice]}, ensure_ascii=False) + "\n\n"
            yield "data: " + json.dumps({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}}) + "\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(chunks(), media_type="text/event-stream")

    async with serve(create_server(settings).streamable_http_app(), mcp_port), serve(provider, provider_port), serve(create_app(settings), web_port):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{web_port}", timeout=15) as client:
            session = (await client.post("/api/sessions")).json()
            response = await client.post(f"/api/sessions/{session['id']}/chat", json={"message": "Найди тестовый материал"})
            assert response.status_code == 200
            events = []
            for block in response.text.strip().split("\n\n"):
                lines = block.splitlines()
                name = next(line[7:] for line in lines if line.startswith("event: "))
                value = json.loads(next(line[6:] for line in lines if line.startswith("data: ")))
                events.append((name, value))
            trace = next(payload for name, payload in events if name == "tool_end")
            assert trace["name"] == "search_graph" and trace["result"]["ok"]
            assert bool(trace["result"]["data"]["hits"]) is found
            answer = "".join(payload["text"] for name, payload in events if name == "answer_delta")
            assert "НЕПОДТВЕРЖДЁННОЕ" not in answer
            assert ("Найден Тестовыйсплав" if found else "не найдена") in answer
            usage = next(payload for name, payload in events if name == "usage")
            assert usage["turn"]["total_tokens"] == 26
            assert usage["session"]["total_tokens"] == 26
            saved = (await client.get(f"/api/sessions/{session['id']}")).json()
            assert saved["messages"][-1]["trace"][0]["name"] == "search_graph"
            assert saved["messages"][-1]["content"] == answer
            assert len(requests) == 2
