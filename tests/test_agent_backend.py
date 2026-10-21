"""Offline checks: no IACPaaS credentials or paid model requests are used."""
import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx2
import pytest
from langchain_core.tools import StructuredTool

from materials_chat.agent import Agent, context_budget
from materials_chat.openrouter import ProviderError, create_chat_model
from materials_chat.prompts import FINAL_STEP_NOTE
from materials_chat.sessions import COMPACTED_NOTE, SessionStore
from materials_chat.usage import add_usage, request_usage, with_cost

PRICES = {"price_input_per_million": 5.0, "price_output_per_million": 10.0,
          "price_cache_read_per_million": 1.0, "currency": "RUB", "context_window": 1_000_000}


def settings(**overrides):
    return SimpleNamespace(**({"max_context_tokens": None, "model_context_tokens": None,
                              "max_output_tokens": 1800, "max_tool_steps": 4, "max_tool_calls": 8,
                              "openrouter_api_key": "local-test", "openrouter_model": "local-model",
                              "openrouter_base_url": "http://provider.test/v1", "provider_name": "RouterAI"}
                             | overrides))


class Tools:
    """Инструмент search_graph в том виде, в каком его отдаёт langchain-mcp-adapters."""

    def __init__(self, result):
        self.output = result
        self.calls = []

    @asynccontextmanager
    async def __call__(self, _settings):
        async def search_graph(query: str = "") -> tuple:
            self.calls.append(("search_graph", {"query": query}))
            return json.dumps(self.output, ensure_ascii=False), {"structured_content": self.output}

        yield [StructuredTool.from_function(coroutine=search_graph, name="search_graph",
                                            description="Поиск по индексу", response_format="content_and_artifact")]


def event(delta, finish=None):
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def text_reply(text, finish="stop"):
    return [event({"role": "assistant", "content": text[:10]}), event({"content": text[10:]}), event({}, finish)]


def tool_reply(*names):
    calls = [{"index": index, "id": f"call{index}", "type": "function",
              "function": {"name": name, "arguments": '{"query":"порошок"}'}} for index, name in enumerate(names)]
    return [event({"role": "assistant", "tool_calls": calls}, "tool_calls")]


def sse(events):
    lines = [json.dumps({"id": "chatcmpl-test", "object": "chat.completion.chunk", "created": 0,
                         "model": "local-model", **item}, ensure_ascii=False) for item in events]
    return "".join(f"data: {line}\n\n" for line in lines) + "data: [DONE]\n\n"


class Provider:
    """Двойник RouterAI на уровне HTTP: ответы по очереди, последний повторяется."""

    usage = {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}

    def __init__(self, *replies):
        self.replies = list(replies) or [tool_reply("search_graph"), text_reply("По базе: свойство равно 25.")]
        self.requests = []

    def respond(self, number, events):
        return httpx2.Response(200, text=sse(events), headers={"Content-Type": "text/event-stream"})

    def handle(self, request):
        self.requests.append(json.loads(request.content))
        number = len(self.requests)
        reply = self.replies[min(number, len(self.replies)) - 1]
        return self.respond(number, [*reply, {"choices": [], "usage": self.usage}])

    def model(self, config):
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(self.handle))
        return create_chat_model(config, http_async_client=client, max_retries=0)


def run_agent(output=None, provider=None, model_info=None, history=(), **overrides):
    tools = Tools(output if output is not None else {"ok": True, "data": {"hits": [{"label": "Размер", "value": 25}]}})
    provider = provider or Provider()
    config = settings(**overrides)
    agent = Agent(config, tools, provider.model(config), model_info=model_info)

    async def run():
        return [item async for item in agent.run("Размер порошка?", list(history))]

    return agent, tools, provider, asyncio.run(run())


def test_tool_result_answer_usage_and_cost_are_preserved():
    agent, tools, provider, events = run_agent(model_info=PRICES)
    assert tools.calls == [("search_graph", {"query": "порошок"})]
    # Модель сама решает, нужен ли инструмент: принудительного вызова нет.
    assert [body["tool_choice"] for body in provider.requests] == ["auto", "auto"]
    assert "25" in agent.result.content
    assert agent.result.grounded is True
    assert agent.result.usage["total_tokens"] == 40
    assert agent.result.usage["requests"] == 2
    assert agent.result.usage["cost"] == pytest.approx(2 * (12 * 5 + 8 * 10) / 1_000_000)
    assert agent.result.usage["currency"] == "RUB"
    history = agent.result.model_messages
    assert history[0]["tool_calls"][0]["id"] == history[1]["tool_call_id"]
    assert json.loads(history[1]["content"])["data"]["hits"][0]["value"] == 25
    # Модель получает результат инструмента и инструкцию системы.
    second = provider.requests[1]["messages"]
    assert second[0]["role"] == "system" and second[-1]["role"] == "tool"
    meta = [payload for name, payload in events if name == "answer_meta"]
    assert meta == [{"grounded": True, "tool_calls": 1, "truncated": False}]
    names = [name for name, _ in events]
    assert names.index("tool_start") < names.index("tool_end") < names.index("answer_meta")


def test_request_uses_routerai_fields():
    _, _, provider, _ = run_agent()
    body = provider.requests[0]
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    # RouterAI ждёт max_tokens; ChatOpenAI сам по себе отправил бы max_completion_tokens.
    assert body["max_tokens"] == 1800 and "max_completion_tokens" not in body
    assert body["tools"][0]["function"]["name"] == "search_graph"


def test_provider_cost_and_reasoning_are_kept():
    fragments = [{"type": "reasoning.text", "text": "Ищу ", "format": "unknown", "index": 0},
                 {"type": "reasoning.text", "text": "порошок", "format": "unknown", "index": 0}]

    class ReasoningProvider(Provider):
        usage = {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20, "cost": 0.25}

        def handle(self, request):
            if not self.requests:
                self.requests.append(json.loads(request.content))
                thinking = [event({"role": "assistant", "reasoning_details": [item]}) for item in fragments]
                return self.respond(1, [*thinking, *tool_reply("search_graph"), {"choices": [], "usage": self.usage}])
            return super().handle(request)

    agent, _, provider, _ = run_agent(provider=ReasoningProvider(text_reply("Ответ")), model_info=PRICES)
    # Фактическое списание провайдера важнее расчёта по прайсу.
    assert agent.result.usage["cost"] == pytest.approx(0.25 + 0.25)
    # Фрагменты рассуждений не склеены LangChain и уходят модели в исходном виде.
    assert agent.result.model_messages[0]["reasoning_details"] == fragments
    sent = next(message for message in provider.requests[1]["messages"] if message["role"] == "assistant")
    assert sent["reasoning_details"] == fragments


def test_small_talk_is_answered_without_tools_and_not_replaced():
    provider = Provider(text_reply("Здравствуйте! Спросите меня о сплавах."))
    agent, tools, _, events = run_agent(provider=provider)
    assert tools.calls == []
    assert agent.result.content == "Здравствуйте! Спросите меня о сплавах."
    assert agent.result.grounded is False
    assert ("answer_meta", {"grounded": False, "tool_calls": 0, "truncated": False}) in events


def test_exhausted_steps_end_with_a_forced_final_answer():
    provider = Provider(tool_reply("search_graph"), tool_reply("search_graph"), text_reply("Итог по найденному."))
    agent, tools, provider, _ = run_agent(provider=provider, max_tool_steps=2)
    assert len(tools.calls) == 2
    assert [body["tool_choice"] for body in provider.requests] == ["auto", "auto", "none"]
    assert provider.requests[-1]["messages"][-1] == {"role": "system", "content": FINAL_STEP_NOTE}
    assert agent.result.content == "Итог по найденному."
    # Служебная подсказка последнего шага не сохраняется в историю диалога.
    assert all(message.get("content") != FINAL_STEP_NOTE for message in agent.result.model_messages)


def test_tool_request_on_the_final_step_is_not_executed():
    provider = Provider(tool_reply("search_graph"), tool_reply("search_graph"))
    agent, tools, provider, _ = run_agent(provider=provider, max_tool_steps=1)
    assert len(tools.calls) == 1
    assert provider.requests[-1]["tool_choice"] == "none"
    assert "tool_calls" not in agent.result.model_messages[-1]
    assert "Модель не дала ответа" in agent.result.content


def test_calls_over_the_limit_get_an_error_answer_each():
    provider = Provider(tool_reply("search_graph", "search_graph", "search_graph"), text_reply("Ответ по двум."))
    agent, tools, provider, _ = run_agent(provider=provider, max_tool_calls=2)
    assert len(tools.calls) == 2
    codes = [item["result"].get("error", {}).get("code") for item in agent.result.trace]
    assert codes == [None, None, "tool_limit"]
    tool_ids = {message["tool_call_id"] for message in agent.result.model_messages if message["role"] == "tool"}
    assert tool_ids == {"call0", "call1", "call2"}
    assert provider.requests[-1]["tool_choice"] == "none"


def test_length_limit_is_reported():
    provider = Provider(text_reply("Очень длинный ответ, который", finish="length"))
    agent, _, _, events = run_agent(provider=provider)
    assert agent.result.truncated
    assert "обрезан" in agent.result.content
    assert ("answer_meta", {"grounded": False, "tool_calls": 0, "truncated": True}) in events


def test_connection_failure_is_visible_in_trace():
    agent, _, _, _ = run_agent({"ok": False, "error": {"code": "connection", "message": "offline"}})
    assert agent.result.trace[0]["is_error"]
    assert agent.result.grounded is False


def test_previous_turns_reach_the_model():
    history = [{"role": "user", "content": "Что такое 12Х18Н10Т?"},
               {"role": "assistant", "content": "", "tool_calls": [{"id": "old", "type": "function", "function": {
                   "name": "search_graph", "arguments": '{"query": "12Х18Н10Т"}'}}]},
               {"role": "tool", "tool_call_id": "old", "content": '{"ok": true}'},
               {"role": "assistant", "content": "Это сталь."}]
    _, _, provider, _ = run_agent(provider=Provider(text_reply("Вы спрашивали о стали.")), history=history)
    sent = provider.requests[0]["messages"]
    assert [message["role"] for message in sent] == ["system", "user", "assistant", "tool", "assistant", "user"]
    assert sent[2]["tool_calls"][0]["id"] == sent[3]["tool_call_id"] == "old"


def test_provider_error_has_a_clear_message():
    class BrokeProvider(Provider):
        def handle(self, request):
            self.requests.append(json.loads(request.content))
            return httpx2.Response(402, json={"error": {"message": "no money"}})

    async def run():
        with pytest.raises(ProviderError) as error:
            async for _ in agent.run("Вопрос", []):
                pass
        return error.value

    provider = BrokeProvider()
    config = settings()
    agent = Agent(config, Tools({"ok": True}), provider.model(config))
    error = asyncio.run(run())
    assert error.code == "http_402" and "Недостаточно средств на счёте RouterAI" in str(error)
    assert agent.result.usage["requests"] == 1 and agent.result.usage["known"] is False


def test_cancelled_request_keeps_completed_usage_and_atomic_tool_history():
    class InterruptedProvider(Provider):
        def respond(self, number, events):
            if number == 1:
                return super().respond(number, events)

            async def hanging_stream():
                yield sse([event({"role": "assistant", "content": "Частичный ответ"})]).split("data: [DONE]")[0].encode()
                await asyncio.sleep(30)

            return httpx2.Response(200, content=hanging_stream(), headers={"Content-Type": "text/event-stream"})

    tools = Tools({"ok": True, "data": {"hits": [{"label": "Материал"}]}})
    provider = InterruptedProvider()
    config = settings()
    agent = Agent(config, tools, provider.model(config))

    async def run():
        # Как кнопка «Стоп»: веб-сервер отменяет задачу, пока модель ещё пишет ответ.
        task = asyncio.current_task()
        try:
            async for name, payload in agent.run("Вопрос", []):
                if name == "answer_delta":
                    task.cancel()
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert agent.result.interrupted
    assert agent.result.usage["total_tokens"] == 20
    assert agent.result.usage["requests"] == 2
    assert agent.result.usage["unknown_requests"] == 1
    assert agent.result.usage["known"] is False
    assert agent.result.model_messages[-1]["role"] == "tool"
    assert agent.result.content == "Частичный ответ"


def test_oversized_history_stops_before_any_provider_request():
    provider = Provider()
    tools = Tools({"ok": True, "data": {}})
    config = settings(max_context_tokens=20)
    agent = Agent(config, tools, provider.model(config))

    async def run():
        with pytest.raises(ProviderError) as error:
            async for _ in agent.run("Вопрос", []):
                pass
        assert error.value.code == "context_limit"

    asyncio.run(run())
    assert not provider.requests
    assert not tools.calls


def test_context_budget_follows_model_window_unless_overridden():
    assert context_budget(settings(max_output_tokens=4096), {"context_window": 1_048_576}) == 1_048_576 - 4096
    assert context_budget(settings(max_output_tokens=4096, max_context_tokens=100_000), {"context_window": 1_048_576}) == 100_000
    assert context_budget(settings(max_output_tokens=1000, model_context_tokens=8000), {"context_window": 1_048_576}) == 7000
    assert context_budget(settings(max_output_tokens=1000), None) == 32768 - 1000


def test_usage_cost_uses_cache_price_and_provider_cost():
    info = PRICES
    usage = request_usage({"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100,
                           "prompt_tokens_details": {"cached_tokens": 400}}, info)
    assert usage["cost"] == pytest.approx((600 * 5 + 400 * 1 + 100 * 10) / 1_000_000)
    assert usage["last_input_tokens"] == 1000
    assert request_usage({"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.5}, info)["cost"] == 0.5
    unknown = request_usage({"prompt_tokens": 1, "completion_tokens": 1}, {})
    assert unknown["cost"] is None and unknown["cost_known"] is False
    total = add_usage(usage, unknown)
    assert total["cost"] == pytest.approx(usage["cost"]) and total["cost_known"] is False
    legacy = with_cost({"input_tokens": 1_000_000, "output_tokens": 0, "total_tokens": 1_000_000,
                        "known": True, "requests": 1, "unknown_requests": 0}, info)
    assert legacy["cost"] == pytest.approx(5.0) and legacy["currency"] == "RUB"


def test_atomic_session_history_compaction_and_delete(tmp_path):
    store = SessionStore(tmp_path / "history.sqlite3", full_turns=1, tool_chars=50)
    session = store.create()
    big = json.dumps({"ok": True, "data": {"text": "х" * 500}}, ensure_ascii=False)
    for turn in range(2):
        stored = [{"role": "assistant", "content": "", "tool_calls": [{"id": f"a{turn}", "function": {"name": "read"}}],
                   "reasoning_details": [{"text": "внутреннее"}]},
                  {"role": "tool", "tool_call_id": f"a{turn}", "content": big},
                  {"role": "assistant", "content": "Ответ"}]
        store.save_turn(session["id"], f"Вопрос {turn}", "Ответ", [],
                        {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14, "known": True, "requests": 1},
                        stored, grounded=True)
    history = store.history(session["id"])
    tools = [message for message in history if message["role"] == "tool"]
    # Старый ход сжат и без рассуждений, последний — целиком.
    assert tools[0]["content"].endswith(COMPACTED_NOTE) and len(tools[0]["content"]) < 200
    assert tools[1]["content"] == big
    assert "reasoning_details" not in history[1]
    detail = store.get(session["id"])
    assert len(detail["messages"]) == 4
    assert detail["messages"][1]["grounded"] is True and detail["messages"][0]["grounded"] is None
    assert detail["usage"]["total_tokens"] == 28
    assert detail["title"] == "Вопрос 0"
    assert "model_messages" not in json.dumps(detail)
    store.delete(session["id"])
    assert store.get(session["id"]) is None
    store.close()


def test_old_history_database_is_migrated(tmp_path):
    path = tmp_path / "history.sqlite3"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE messages (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,
            content TEXT NOT NULL, created_at TEXT NOT NULL, trace TEXT NOT NULL,
            usage TEXT NOT NULL, model_messages TEXT NOT NULL);
        INSERT INTO sessions VALUES ('s', 'Старый', '2026-10-19');
        INSERT INTO messages VALUES ('m', 's', 'assistant', 'Ответ', '2026-10-19', '[]',
            '{"input_tokens": 1000000, "output_tokens": 0, "total_tokens": 1000000, "known": true, "requests": 1}', '[]');
    """)
    db.close()
    store = SessionStore(path)
    store.model_info = PRICES
    detail = store.get("s")
    assert detail["messages"][0]["grounded"] is None
    assert detail["usage"]["cost"] == pytest.approx(5.0)
    store.close()


def test_web_status_and_sessions_work_without_model_key(tmp_path):
    from fastapi.testclient import TestClient
    from materials_chat.config import Settings
    from materials_chat.web import create_app

    config = Settings(_env_file=None, data_dir=tmp_path, history_path=tmp_path / "history.sqlite3",
                      auto_sync_on_start=False, openrouter_api_key="", openrouter_model="")
    app = create_app(config)
    with TestClient(app) as client:
        status = client.get("/api/status").json()
        assert status["configured"] is False
        assert "OPENROUTER_API_KEY" in status["missing_settings"]
        assert status["model_info"]["source"] == "unknown"
        assert status["limits"]["context_budget"] > 0
        assert "iacpaas_password" not in json.dumps(status)
        # Интерфейс не берётся из кэша браузера после обновления приложения.
        assert client.get("/").headers["cache-control"] == "no-cache"
        assert client.get("/static/styles.css").headers["cache-control"] == "no-cache"
        session = client.post("/api/sessions").json()
        assert session["context_budget"] == status["limits"]["context_budget"]
        response = client.post(f"/api/sessions/{session['id']}/chat", json={"message": "Вопрос"})
        assert response.status_code == 503
        assert client.get(f"/api/sessions/{session['id']}").json()["messages"] == []
        assert client.delete(f"/api/sessions/{session['id']}").status_code == 200
        assert client.get(f"/api/sessions/{session['id']}").status_code == 404


def test_web_stream_saves_trace_grounding_and_hides_internal_reasoning(tmp_path):
    from fastapi.testclient import TestClient
    from materials_chat.agent import TurnResult
    from materials_chat.config import Settings
    from materials_chat.web import create_app

    class FakeAgent:
        def __init__(self, _settings, model_info=None):
            self.result = TurnResult()

        async def run(self, text, history):
            self.result.content = "Ответ по источнику."
            self.result.usage = {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10, "known": True,
                                 "requests": 1, "unknown_requests": 0, "cost": 0.01, "currency": "RUB", "cost_known": True}
            self.result.trace = [{"id": "t", "name": "search_graph", "result": {"ok": True}, "is_error": False}]
            self.result.model_messages = [{"role": "assistant", "content": self.result.content,
                                           "reasoning_details": [{"text": "private_internal"}]}]
            yield "tool_start", {"id": "t", "name": "search_graph", "arguments": {}}
            yield "tool_end", self.result.trace[0]
            yield "answer_delta", {"text": self.result.content}
            yield "answer_meta", {"grounded": True, "tool_calls": 1, "truncated": False}

    # Адрес провайдера недоступен: тест не должен ходить в сеть за каталогом моделей.
    config = Settings(_env_file=None, data_dir=tmp_path, history_path=tmp_path / "history.sqlite3",
                      auto_sync_on_start=False, openrouter_api_key="test-fake-never-sent", openrouter_model="fake",
                      openrouter_base_url="http://127.0.0.1:9")
    app = create_app(config)
    app.state.agent_factory = FakeAgent
    with TestClient(app) as client:
        session_id = client.post("/api/sessions").json()["id"]
        response = client.post(f"/api/sessions/{session_id}/chat", json={"message": "Вопрос"})
        assert response.status_code == 200
        assert "event: tool_start" in response.text
        assert "event: answer_meta" in response.text
        assert "event: done" in response.text
        assert "private_internal" not in response.text
        detail = client.get(f"/api/sessions/{session_id}").json()
        assert detail["usage"]["total_tokens"] == 10
        assert detail["usage"]["cost"] == pytest.approx(0.01)
        assert detail["messages"][1]["trace"][0]["name"] == "search_graph"
        assert detail["messages"][1]["grounded"] is True
        assert "private_internal" not in json.dumps(detail)
