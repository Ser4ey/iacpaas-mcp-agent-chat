"""Offline checks: no IACPaaS credentials or paid model requests are used."""
import asyncio
import json
from types import SimpleNamespace

from materials_chat.agent import Agent, has_evidence
from materials_chat.sessions import SessionStore
from materials_chat.openrouter import ProviderError


def settings(**overrides):
    return SimpleNamespace(**({"max_context_tokens": 24000, "model_context_tokens": 32768,
                              "max_output_tokens": 1800, "max_tool_steps": 4,
                              "max_tool_calls": 8} | overrides))


class Bridge:
    def __init__(self, result):
        self.output = result
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def list_tools(self):
        return [{"type": "function", "function": {"name": "search_graph", "parameters": {"type": "object"}}}]

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return self.output


class Provider:
    def __init__(self, final="По базе: свойство равно 25.", tool_name="search_graph"):
        self.requests = []
        self.final = final
        self.tool_name = tool_name

    async def stream(self, messages, tools, tool_choice):
        self.requests.append((messages.copy(), tool_choice))
        if len(self.requests) == 1:
            yield {"delta": {"tool_calls": [{"index": 0, "id": "call1", "function":
                                               {"name": self.tool_name, "arguments": '{"query":"порошок"}'}}]},
                   "finish_reason": "tool_calls"}
        else:
            for text in (self.final[:10], self.final[10:]):
                yield {"delta": {"content": text}, "finish_reason": None}
            yield {"delta": {}, "finish_reason": "stop"}
        yield {"usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}}


def run_agent(output):
    bridge, provider = Bridge(output), Provider()
    agent = Agent(settings(), lambda _: bridge, provider)

    async def run():
        return [event async for event in agent.run("Размер порошка?", [])]

    return agent, bridge, provider, asyncio.run(run())


def test_actual_tool_result_and_all_provider_usage_are_preserved():
    agent, bridge, provider, events = run_agent({"ok": True, "data": {"hits": [{"label": "Размер", "value": 25}]}})
    assert bridge.calls == [("search_graph", {"query": "порошок"})]
    assert provider.requests[0][1] == "required"
    assert any(event == "tool_start" for event, _ in events)
    assert any(event == "tool_end" for event, _ in events)
    assert "25" in agent.result.content
    assert agent.result.usage["total_tokens"] == 40
    assert agent.result.usage["known"] is True
    history = agent.result.model_messages
    assert history[0]["tool_calls"][0]["id"] == history[1]["tool_call_id"]


def test_empty_search_never_exposes_models_invented_answer():
    agent, _, _, events = run_agent({"ok": True, "data": {"hits": [], "total": 0}})
    assert "25" not in agent.result.content
    assert "не найдена" in agent.result.content
    assert all("25" not in value.get("text", "") for event, value in events if event == "answer_delta")


def test_connection_failure_is_not_reported_as_empty_search():
    agent, _, _, _ = run_agent({"ok": False, "error": {"code": "connection", "message": "offline"}})
    assert "ошибкой" in agent.result.content
    assert "не найдена" not in agent.result.content


def test_zero_count_is_a_valid_statistical_fact():
    assert has_evidence({"ok": True, "data": {"count": 0}})
    assert has_evidence({"ok": True, "data": {"nodes": 0, "edges": 0}})
    assert not has_evidence({"ok": True, "data": {"hits": [], "total": 0}})


def test_partial_index_is_not_mistaken_for_complete_negative_result():
    agent, _, _, _ = run_agent({"ok": True, "data": {"hits": [], "total": 0,
                                                      "coverage_complete": False, "unavailable_resources": ["catalog"]}})
    assert "во всей базе" in agent.result.content
    assert "не загружены" in agent.result.content


def test_metadata_cannot_unlock_invented_material_properties():
    bridge = Bridge({"ok": True, "data": {"resources": [
        {"id": "materials", "title": "Материалы", "indexed": True}], "total": 1}})
    agent = Agent(settings(), lambda _: bridge, Provider(tool_name="list_inforesources"))

    async def run():
        return [item async for item in agent.run("Размер порошка?", [])]

    events = asyncio.run(run())
    assert "25" not in agent.result.content
    assert "Материалы" in agent.result.content
    assert "описание ресурсов" in agent.result.content
    assert all("25" not in value.get("text", "") for event, value in events if event == "answer_delta")


def test_atomic_session_history_and_delete(tmp_path):
    store = SessionStore(tmp_path / "history.sqlite3")
    session = store.create()
    stored = [{"role": "assistant", "tool_calls": [{"id": "a", "function": {"name": "read"}}]},
              {"role": "tool", "tool_call_id": "a", "content": "{}"},
              {"role": "assistant", "content": "Ответ"}]
    store.save_turn(session["id"], "Вопрос", "Ответ", [],
                    {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14, "known": True}, stored)
    detail = store.get(session["id"])
    assert len(detail["messages"]) == 2
    assert detail["usage"]["total_tokens"] == 14
    assert detail["title"] == "Вопрос"
    assert len(store.history(session["id"])) == 4
    assert "model_messages" not in json.dumps(detail)
    store.delete(session["id"])
    assert store.get(session["id"]) is None
    store.close()


def test_cancelled_request_keeps_completed_usage_and_atomic_tool_history():
    class InterruptedProvider(Provider):
        async def stream(self, messages, tools, tool_choice):
            if self.requests:
                self.requests.append((messages.copy(), tool_choice))
                yield {"delta": {"content": "Частичный ответ"}}
                raise asyncio.CancelledError()
            async for event in super().stream(messages, tools, tool_choice):
                yield event

    bridge = Bridge({"ok": True, "data": {"hits": [{"label": "Материал"}]}})
    agent = Agent(settings(), lambda _: bridge, InterruptedProvider())

    async def run():
        try:
            async for _ in agent.run("Вопрос", []):
                pass
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
    bridge = Bridge({"ok": True, "data": {}})
    agent = Agent(settings(max_context_tokens=20), lambda _: bridge, provider)

    async def run():
        try:
            async for _ in agent.run("Вопрос", []):
                pass
        except ProviderError as error:
            assert error.code == "context_limit"
        else:
            raise AssertionError("Expected context limit")

    asyncio.run(run())
    assert not provider.requests
    assert not bridge.calls


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
        assert "iacpaas_password" not in json.dumps(status)
        session = client.post("/api/sessions").json()
        response = client.post(f"/api/sessions/{session['id']}/chat", json={"message": "Вопрос"})
        assert response.status_code == 503
        assert client.get(f"/api/sessions/{session['id']}").json()["messages"] == []
        assert client.delete(f"/api/sessions/{session['id']}").status_code == 200
        assert client.get(f"/api/sessions/{session['id']}").status_code == 404


def test_web_stream_saves_tool_trace_and_hides_internal_reasoning(tmp_path):
    from fastapi.testclient import TestClient
    from materials_chat.agent import TurnResult
    from materials_chat.config import Settings
    from materials_chat.web import create_app

    class FakeAgent:
        def __init__(self, _):
            self.result = TurnResult()

        async def run(self, text, history):
            self.result.content = "Ответ по источнику."
            self.result.usage = {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10,
                                 "known": True, "requests": 1, "unknown_requests": 0}
            self.result.trace = [{"id": "t", "name": "search_graph", "result": {"ok": True}, "is_error": False}]
            self.result.model_messages = [{"role": "assistant", "content": self.result.content,
                                           "reasoning_details": [{"text": "private_internal"}]}]
            yield "tool_start", {"id": "t", "name": "search_graph", "arguments": {}}
            yield "tool_end", self.result.trace[0]
            yield "answer_delta", {"text": self.result.content}

    config = Settings(_env_file=None, data_dir=tmp_path, history_path=tmp_path / "history.sqlite3",
                      auto_sync_on_start=False, openrouter_api_key="test-fake-never-sent", openrouter_model="fake")
    app = create_app(config)
    app.state.agent_factory = FakeAgent
    with TestClient(app) as client:
        session_id = client.post("/api/sessions").json()["id"]
        response = client.post(f"/api/sessions/{session_id}/chat", json={"message": "Вопрос"})
        assert response.status_code == 200
        assert "event: tool_start" in response.text
        assert "event: done" in response.text
        assert "private_internal" not in response.text
        detail = client.get(f"/api/sessions/{session_id}").json()
        assert detail["usage"]["total_tokens"] == 10
        assert detail["messages"][1]["trace"][0]["name"] == "search_graph"
        assert "private_internal" not in json.dumps(detail)
