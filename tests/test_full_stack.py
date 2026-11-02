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


CLOUD_CATALOG = {"data": [{"id": "local-test-model", "name": "Тестовая модель", "context_length": 200000,
                           "pricing": {"prompt": "0.000005", "completion": "0.00001"}}]}


@asynccontextmanager
async def stack(tmp_path, answer, platform=None, local=False, catalog=CLOUD_CATALOG, **overrides):
    """Веб-приложение + MCP-сервер + двойник модели. answer(body) -> (status, choice); platform — двойник IACPaaS.

    Двойник слушает 127.0.0.1; local=False — считать его облачным провайдером (с ключом и ценами),
    local=None — определить по адресу, как в приложении (локальный сервер без ключа).
    """
    config = tmp_path / "resources.json"
    config.write_text(json.dumps([{"id": "materials", "title": "Тестовая база", "path": "Тест", "kind": "information"}]), encoding="utf-8")
    mcp_port, web_port, provider_port = free_port(), free_port(), free_port()
    settings = Settings(_env_file=None, **({
        "data_dir": tmp_path / "data", "history_path": tmp_path / "history.sqlite3", "resources_path": config,
        "mcp_port": mcp_port, "mcp_url": f"http://127.0.0.1:{mcp_port}/mcp",
        "openrouter_api_key": "local-test-key", "openrouter_model": "local-test-model",
        "openrouter_base_url": f"http://127.0.0.1:{provider_port}", "auto_sync_on_start": False,
        "iacpaas_username": "", "iacpaas_password": "", "live_reads": False, "model_local": local} | overrides))
    GraphStore(settings.data_dir / "search.sqlite3").import_graph("materials", {
        "id": 1, "name": "Каталог", "title": "Тестовая база", "type": "КОРЕНЬ", "json_type": "universal",
        "ontology": "Тестовая онтология", "successors": [{"id": 2, "name": "Тестовыйсплав", "type": "НЕТЕРМИНАЛ"}]})
    provider = FastAPI()
    requests = []

    @provider.get("/models")
    async def models():
        return catalog

    @provider.post("/chat/completions")
    async def complete(request: Request):
        body = await request.json()
        body["authorization"] = request.headers.get("authorization")
        requests.append(body)
        status, choice = answer(body)
        if status != 200:
            return JSONResponse({"error": "test"}, status_code=status)

        async def chunks():
            yield "data: " + json.dumps({"choices": [choice]}, ensure_ascii=False) + "\n\n"
            yield "data: " + json.dumps({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}}) + "\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(chunks(), media_type="text/event-stream")

    mcp = create_server(settings)
    if platform is not None:
        mcp.graph_service.client = platform
    async with serve(mcp.streamable_http_app(), mcp_port), serve(provider, provider_port), \
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
        tools = {tool["function"]["name"]: tool["function"] for tool in requests[0]["tools"]}
        assert "include_unlisted" in json.dumps(tools["find_materials"]["parameters"], ensure_ascii=False)


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
        # Ошибка хранится отдельно и не выглядит как часть ответа после перезагрузки.
        assert saved["messages"][-1]["error"]["code"] == "http_402"
        assert "Недостаточно средств" not in saved["messages"][-1]["content"]
        # Диалог не остаётся заблокированным после ошибки.
        status = await client.post(f"/api/sessions/{session['id']}/chat", json={"message": "Ещё раз"})
        assert status.status_code == 200


def write_call():
    arguments = {"request": {"operation": "create_folder", "parent_path": "", "name": "Демо агента"}}
    return {"delta": {"tool_calls": [{"index": 0, "id": "call_write", "type": "function", "function": {
        "name": "write_inforesource", "arguments": json.dumps(arguments, ensure_ascii=False)}}]}, "finish_reason": "tool_calls"}


@pytest.mark.asyncio
@pytest.mark.parametrize("accept", [True, False])
async def test_write_waits_for_the_user_decision_from_the_stream(tmp_path, accept):
    from test_writes import FakePlatform

    def answer(body):
        if not any(message["role"] == "tool" for message in body["messages"]):
            return 200, write_call()
        return 200, {"delta": {"content": "Готово."}, "finish_reason": "stop"}

    platform = FakePlatform({})
    async with stack(tmp_path, answer, platform) as (client, requests):
        session = (await client.post("/api/sessions")).json()
        events, buffer, decision_url = [], "", None
        async with client.stream("POST", f"/api/sessions/{session['id']}/chat",
                                 json={"message": "Создай папку «Демо агента»"}) as response:
            async for chunk in response.aiter_text():
                buffer += chunk.replace("\r\n", "\n")
                while "\n\n" in buffer:
                    block, buffer = buffer.split("\n\n", 1)
                    events.extend(parse_events(block))
                    if events[-1][0] == "confirm_request":
                        assert platform.writes == []  # до решения пользователя ничего не записано
                        decision_url = f"/api/sessions/{session['id']}/confirmations/{events[-1][1]['id']}"
                        decision = await client.post(decision_url, json={"accept": accept})
                        assert decision.json() == {"ok": True, "status": "accepted" if accept else "declined"}
        request = next(payload for name, payload in events if name == "confirm_request")
        assert {key: request[key] for key in ("call_id", "tool", "message", "danger", "timeout_seconds")} == {
            "call_id": "call_write", "tool": "write_inforesource", "message": "Создать папку «Демо агента» в корне фонда.",
            "danger": False, "timeout_seconds": 120}
        end = next(payload for name, payload in events if name == "tool_end")
        assert end["confirmation"]["status"] == ("accepted" if accept else "declined")
        assert end["result"]["ok"] is accept
        if not accept:
            assert end["result"]["error"]["code"] == "CONFIRMATION_DECLINED"
        assert platform.writes == ([("create_folder", "", "Демо агента")] if accept else [])
        assert (await client.post(decision_url, json={"accept": True})).status_code == 404  # решение не повторить
        saved = (await client.get(f"/api/sessions/{session['id']}")).json()
        assert saved["messages"][-1]["trace"][0]["confirmation"]["status"] == end["confirmation"]["status"]
        assert "write_inforesource" in {tool["function"]["name"] for tool in requests[0]["tools"]}


@pytest.mark.asyncio
async def test_closed_page_while_waiting_for_confirmation_writes_nothing(tmp_path):
    from test_writes import FakePlatform

    def answer(body):
        if not any(message["role"] == "tool" for message in body["messages"]):
            return 200, write_call()
        return 200, {"delta": {"content": "Готово."}, "finish_reason": "stop"}

    platform = FakePlatform({})
    async with stack(tmp_path, answer, platform) as (client, _):
        session = (await client.post("/api/sessions")).json()
        confirmation = None
        async with client.stream("POST", f"/api/sessions/{session['id']}/chat", json={"message": "Создай папку"}) as response:
            async for chunk in response.aiter_text():
                if "event: confirm_request" in chunk:
                    confirmation = parse_events(chunk[chunk.index("event: confirm_request"):].split("\n\n")[0])[0][1]
                    break  # страницу закрыли, не ответив
        for _ in range(100):
            saved = (await client.get(f"/api/sessions/{session['id']}")).json()
            if saved["messages"]:
                break
            await asyncio.sleep(.05)
        assert saved["messages"][-1]["error"]["code"] == "cancelled"
        decision = await client.post(f"/api/sessions/{session['id']}/confirmations/{confirmation['id']}", json={"accept": True})
        assert decision.status_code == 404
        await asyncio.sleep(.3)
        assert platform.writes == []


# Каталог локального сервера (LM Studio, Ollama): только id модели — без окна контекста и без цен.
LOCAL_CATALOG = {"data": [{"id": "local-test-model", "object": "model", "owned_by": "organization_owner"}]}


async def loaded_status(client):
    """Статус после того, как приложение прочитало каталог модели (он грузится в фоне при старте)."""
    for _ in range(100):
        status = (await client.get("/api/status")).json()
        if status["model_info"]["source"] != "unknown":
            return status
        await asyncio.sleep(.05)
    return status


def search_then_answer(text):
    def answer(body):
        if any(tool["function"]["name"] == "submit_verdict" for tool in body.get("tools", [])):
            claims = [{"text": "Найден Тестовыйсплав", "quote": "Найден Тестовыйсплав", "status": "confirmed", "node_id": "2"}]
            return 200, {"delta": {"tool_calls": [{"index": 0, "id": "verdict", "type": "function", "function": {
                "name": "submit_verdict", "arguments": json.dumps({"claims": claims}, ensure_ascii=False)}}]},
                "finish_reason": "tool_calls"}
        if not any(message["role"] == "tool" for message in body["messages"]):
            return 200, search_call("Тестовыйсплав")
        return 200, {"delta": {"content": text}, "finish_reason": "stop"}
    return answer


@pytest.mark.asyncio
async def test_local_model_without_key_prices_and_window_answers_for_free(tmp_path):
    answer = search_then_answer("<think>Пользователь спрашивает о сплаве.</think>\n\nНайден Тестовыйсплав.")
    async with stack(tmp_path, answer, local=None, catalog=LOCAL_CATALOG, openrouter_api_key="") as (client, requests):
        status = await loaded_status(client)
        assert status["configured"] is True and status["missing_settings"] == []
        assert status["provider"] == "Локальная модель"
        info = status["model_info"]
        assert info["free"] is True and info["currency"] is None
        assert info["context_window"] is None and info["context_source"] == "default"
        assert status["limits"]["context_window"] == 32768
        assert status["limits"]["context_budget"] == 32768 - 4096

        session = (await client.post("/api/sessions")).json()
        response = await client.post(f"/api/sessions/{session['id']}/chat", json={"message": "Найди тестовый материал"})
        assert response.status_code == 200
        events = parse_events(response.text)
        assert not [payload for name, payload in events if name == "error"]
        assert "".join(payload["text"] for name, payload in events if name == "answer_delta") == "Найден Тестовыйсплав."
        usage = next(payload for name, payload in events if name == "usage")
        assert usage["turn"]["free"] is True and usage["turn"]["cost"] == 0 and usage["turn"]["currency"] is None
        assert usage["session"]["free"] is True
        # Ключа нет: клиент OpenAI отправляет заглушку, локальный сервер её не проверяет.
        assert {body["authorization"] for body in requests} == {"Bearer not-needed"}

        saved = (await client.get(f"/api/sessions/{session['id']}")).json()
        assert saved["messages"][-1]["content"] == "Найден Тестовыйсплав."  # рассуждение <think> не сохранено
        listed = (await client.get("/api/sessions")).json()["sessions"][0]
        assert listed["usage"]["free"] is True and listed["usage"]["requests"] == 2

        # Проверка ответа тоже не требует ключа; её расход — бесплатный.
        verify = await client.post(f"/api/sessions/{session['id']}/messages/{saved['messages'][-1]['id']}/verify")
        done = next(payload for name, payload in parse_events(verify.text) if name == "done")
        assert done["verification"]["verdict"] == "подтверждён" and done["verification"]["usage"]["free"] is True
        assert done["session_usage"]["free"] is True


@pytest.mark.asyncio
async def test_cloud_model_without_prices_has_unknown_cost_not_zero(tmp_path):
    async with stack(tmp_path, search_then_answer("Найден Тестовыйсплав."), catalog=LOCAL_CATALOG) as (client, _):
        status = await loaded_status(client)
        assert status["provider"] == "API модели" and status["model_info"]["free"] is False
        session = (await client.post("/api/sessions")).json()
        events = parse_events((await client.post(f"/api/sessions/{session['id']}/chat",
                                                 json={"message": "Найди тестовый материал"})).text)
        usage = next(payload for name, payload in events if name == "usage")
        assert usage["turn"]["cost"] is None and usage["turn"]["cost_known"] is False
        assert usage["session"]["cost"] is None and usage["turn"]["total_tokens"] == 26
        listed = (await client.get("/api/sessions")).json()["sessions"][0]
        assert listed["usage"]["cost"] is None


@pytest.mark.asyncio
async def test_stopped_local_server_gets_a_clear_message(tmp_path):
    async with stack(tmp_path, search_then_answer("—"), local=None, openrouter_api_key="",
                     openrouter_base_url=f"http://127.0.0.1:{free_port()}/v1") as (client, _):
        session = (await client.post("/api/sessions")).json()
        events = parse_events((await client.post(f"/api/sessions/{session['id']}/chat", json={"message": "Вопрос"})).text)
        error = next(payload for name, payload in events if name == "error")
        assert error["code"] == "provider_connection"
        assert error["message"].startswith("Локальный сервер модели не отвечает по адресу http://127.0.0.1:")
        assert events[-1][0] == "done"
