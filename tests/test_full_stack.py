"""Настоящие HTTP + MCP транспорты, но бесплатный локальный double вместо LLM."""
import asyncio
import json
import socket
from contextlib import asynccontextmanager, nullcontext

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

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


def parse_events(text):
    events = []
    for block in text.strip().split("\n\n"):
        lines = block.splitlines()
        name = next(line[7:] for line in lines if line.startswith("event: "))
        events.append((name, json.loads(next(line[6:] for line in lines if line.startswith("data: ")))))
    return events


@asynccontextmanager
async def stack(tmp_path, answer):
    """Веб-приложение + MCP-сервер + двойник модели. answer(body) -> (status, choice)."""
    config = tmp_path / "resources.json"
    config.write_text(json.dumps([{"id": "materials", "title": "Тестовая база", "path": "Тест", "kind": "information"}]), encoding="utf-8")
    mcp_port, web_port, provider_port = free_port(), free_port(), free_port()
    settings = Settings(_env_file=None, data_dir=tmp_path / "data", history_path=tmp_path / "history.sqlite3",
        resources_path=config, mcp_port=mcp_port, mcp_url=f"http://127.0.0.1:{mcp_port}/mcp",
        openrouter_api_key="local-test-key", openrouter_model="local-test-model",
        openrouter_base_url=f"http://127.0.0.1:{provider_port}", auto_sync_on_start=False,
        iacpaas_username="", iacpaas_password="", live_reads=False)
    GraphStore(settings.data_dir / "search.sqlite3").import_graph("materials", {
        "id": 1, "name": "Каталог", "title": "Тестовая база", "type": "КОРЕНЬ", "json_type": "universal",
        "ontology": "Тестовая онтология", "successors": [{"id": 2, "name": "Тестовыйсплав", "type": "НЕТЕРМИНАЛ"}]})
    provider = FastAPI()
    requests = []

    @provider.get("/models")
    async def models():
        return {"data": [{"id": "local-test-model", "name": "Тестовая модель", "context_length": 200000,
                          "pricing": {"prompt": "0.000005", "completion": "0.00001"}}]}

    @provider.post("/chat/completions")
    async def complete(request: Request):
        body = await request.json()
        requests.append(body)
        status, choice = answer(body)
        if status != 200:
            return JSONResponse({"error": "test"}, status_code=status)

        async def chunks():
            yield "data: " + json.dumps({"choices": [choice]}, ensure_ascii=False) + "\n\n"
            yield "data: " + json.dumps({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}}) + "\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(chunks(), media_type="text/event-stream")

    async with serve(create_server(settings).streamable_http_app(), mcp_port), serve(provider, provider_port), \
            serve(create_app(settings), web_port):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{web_port}", timeout=15) as client:
            yield client, requests


def search_call(query):
    return {"delta": {"tool_calls": [{"index": 0, "id": "call_search", "type": "function", "function": {
        "name": "search_graph", "arguments": json.dumps({"query": query, "resource_id": "materials"}, ensure_ascii=False)}}]},
        "finish_reason": "tool_calls"}


@pytest.mark.asyncio
@pytest.mark.parametrize("found", [True, False])
async def test_http_chat_really_uses_mcp_and_counts_all_model_requests(tmp_path, found):
    def answer(body):
        if not any(message["role"] == "tool" for message in body["messages"]):
            return 200, search_call("Тестовыйсплав" if found else "несуществующий987654")
        text = "Найден Тестовыйсплав. Источник: Тестовая база, вершина 2." if found else "В доступных ресурсах информация не найдена."
        return 200, {"delta": {"content": text}, "finish_reason": "stop"}

    async with stack(tmp_path, answer) as (client, requests):
        session = (await client.post("/api/sessions")).json()
        response = await client.post(f"/api/sessions/{session['id']}/chat", json={"message": "Найди тестовый материал"})
        assert response.status_code == 200
        events = parse_events(response.text)
        trace = next(payload for name, payload in events if name == "tool_end")
        assert trace["name"] == "search_graph" and trace["result"]["ok"]
        assert bool(trace["result"]["data"]["hits"]) is found
        answer_text = "".join(payload["text"] for name, payload in events if name == "answer_delta")
        assert ("Найден Тестовыйсплав" if found else "не найдена") in answer_text
        assert ("answer_meta", {"grounded": True, "tool_calls": 1, "truncated": False}) in events
        usage = next(payload for name, payload in events if name == "usage")
        assert usage["turn"]["total_tokens"] == 26 and usage["session"]["total_tokens"] == 26
        # Цена из каталога модели: 5 и 10 за миллион токенов.
        assert usage["turn"]["cost"] == pytest.approx(2 * (10 * 5 + 3 * 10) / 1_000_000)
        assert usage["context_budget"] == 200000 - 4096
        saved = (await client.get(f"/api/sessions/{session['id']}")).json()
        assert saved["messages"][-1]["trace"][0]["name"] == "search_graph"
        assert saved["messages"][-1]["content"] == answer_text
        assert saved["messages"][-1]["grounded"] is True
        assert [body["tool_choice"] for body in requests] == ["auto", "auto"]


@pytest.mark.asyncio
async def test_provider_error_inside_mcp_session_keeps_its_message(tmp_path):
    # Раньше ошибка провайдера внутри MCP-сессии превращалась в «Проверьте доступность MCP».
    def answer(body):
        if not any(message["role"] == "tool" for message in body["messages"]):
            return 200, search_call("Тестовыйсплав")
        return 402, None

    async with stack(tmp_path, answer) as (client, _):
        session = (await client.post("/api/sessions")).json()
        response = await client.post(f"/api/sessions/{session['id']}/chat", json={"message": "Найди тестовый материал"})
        events = parse_events(response.text)
        error = next(payload for name, payload in events if name == "error")
        assert error["code"] == "http_402"
        assert "Недостаточно средств" in error["message"]
        assert events[-1][0] == "done"
        saved = (await client.get(f"/api/sessions/{session['id']}")).json()
        assert "Недостаточно средств" in saved["messages"][-1]["content"]
        # Диалог не остаётся заблокированным после ошибки.
        status = await client.post(f"/api/sessions/{session['id']}/chat", json={"message": "Ещё раз"})
        assert status.status_code == 200
