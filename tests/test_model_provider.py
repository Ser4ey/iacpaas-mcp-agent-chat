"""RouterAI: имена настроек, модель LangChain и каталог моделей; платных запросов нет."""
import json

import httpx
import httpx2
import openai
import pytest
from langchain_core.exceptions import ContextOverflowError

from materials_chat.config import Settings
from materials_chat.openrouter import ProviderError, as_provider_error, create_chat_model, fetch_model_info


def test_routerai_env_names_are_loaded_without_disclosing_key(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ROUTERAI_API_KEY=local-test\nROUTERAI_MODEL=deepseek/deepseek-v4.1-flash\nROUTERAI_BASEURL=https://routerai.ru/api/v1\n", encoding="utf-8")
    settings = Settings(_env_file=env)
    assert settings.openrouter_api_key == "local-test"
    assert settings.openrouter_model == "deepseek/deepseek-v4.1-flash"
    assert settings.openrouter_base_url == "https://routerai.ru/api/v1"
    assert settings.provider_name == "RouterAI"
    assert settings.model_setting_names == ("ROUTERAI_API_KEY", "ROUTERAI_MODEL")
    assert "local-test" not in repr(settings)


def test_routerai_key_defaults_to_routerai_endpoint():
    settings = Settings(_env_file=None, ROUTERAI_API_KEY="local-test")
    assert settings.openrouter_base_url == "https://routerai.ru/api/v1"


def test_legacy_openrouter_config_stays_compatible():
    settings = Settings(_env_file=None, openrouter_api_key="legacy-test", openrouter_model="legacy-model")
    assert settings.openrouter_api_key == "legacy-test"
    assert settings.provider_name == "OpenRouter"


@pytest.mark.asyncio
async def test_routerai_model_receives_key_tools_and_returns_provider_usage():
    def handle(request):
        assert str(request.url) == "https://routerai.ru/api/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer local-test"
        assert request.headers["X-Title"] == "IACPaaS Materials Study"
        body = json.loads(request.content)
        assert body["model"] == "deepseek/deepseek-v4.1-flash"
        assert body["stream"] and body["tools"] and body["tool_choice"] == "required"
        payload = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": body["model"],
                   "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [{"index": 0, "id": "call1", "type": "function",
                                "function": {"name": "search_graph", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}],
                   "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16, "cost": 0.01}}
        return httpx2.Response(200, text="data: " + json.dumps(payload) + "\n\ndata: [DONE]\n\n",
                               headers={"Content-Type": "text/event-stream"})

    settings = Settings(_env_file=None, ROUTERAI_API_KEY="local-test", ROUTERAI_MODEL="deepseek/deepseek-v4.1-flash")
    model = create_chat_model(settings, http_async_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle)))
    tool = {"type": "function", "function": {"name": "search_graph", "parameters": {"type": "object", "properties": {}}}}
    chunks = [chunk async for chunk in model.bind_tools([tool], tool_choice="required").astream("Вопрос")]
    message = chunks[0]
    for chunk in chunks[1:]:
        message += chunk
    assert message.tool_calls[0]["name"] == "search_graph"
    assert message.response_metadata["provider_usage"]["cost"] == 0.01


@pytest.mark.asyncio
async def test_routerai_error_names_current_provider_not_previous_one():
    settings = Settings(_env_file=None, ROUTERAI_API_KEY="local-test", ROUTERAI_MODEL="model")
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(lambda _: httpx2.Response(401, json={"error": {}})))
    with pytest.raises(openai.AuthenticationError) as error:
        await create_chat_model(settings, http_async_client=client, max_retries=0).ainvoke("Вопрос")
    problem = as_provider_error(error.value, settings.provider_name)
    assert str(problem) == "Ключ RouterAI отклонён." and problem.code == "http_401"


def test_missing_key_is_named_in_the_error():
    with pytest.raises(ProviderError, match="ROUTERAI_API_KEY") as error:
        create_chat_model(Settings(_env_file=None, ROUTERAI_API_KEY="", ROUTERAI_MODEL="model", ROUTERAI_BASE_URL="https://routerai.ru/api/v1"))
    assert error.value.code == "missing_api_key"


@pytest.mark.asyncio
async def test_model_window_and_rub_prices_come_from_the_provider_catalog(monkeypatch):
    actual_client = httpx.AsyncClient
    catalog = {"data": [{"id": "deepseek/deepseek-v4.1-flash", "name": "DeepSeek V4.1 Flash", "context_length": 1048576,
                         "pricing": {"prompt": 5.3568843328e-06, "completion": 9.56586488e-06, "input_cache_read": 1.9e-07}}]}

    def handle(request):
        assert str(request.url) == "https://routerai.ru/api/v1/models"
        return httpx.Response(200, json=catalog)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: actual_client(transport=httpx.MockTransport(handle), **kwargs))
    settings = Settings(_env_file=None, ROUTERAI_API_KEY="local-test", ROUTERAI_MODEL="deepseek/deepseek-v4.1-flash")
    info = await fetch_model_info(settings)
    assert info["source"] == "provider" and info["currency"] == "RUB"
    assert info["context_window"] == 1048576
    assert info["price_input_per_million"] == pytest.approx(5.3568843328)
    assert info["price_output_per_million"] == pytest.approx(9.56586488)

    # Значения из .env важнее каталога.
    override = Settings(_env_file=None, ROUTERAI_API_KEY="local-test", ROUTERAI_MODEL="deepseek/deepseek-v4.1-flash",
                        model_context_tokens=64000, price_input_per_million=1.0)
    info = await fetch_model_info(override)
    assert info["context_window"] == 64000 and info["price_input_per_million"] == 1.0


@pytest.mark.asyncio
async def test_unreachable_catalog_leaves_prices_unknown(monkeypatch):
    actual_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: actual_client(
        transport=httpx.MockTransport(lambda _: httpx.Response(503)), **kwargs))
    info = await fetch_model_info(Settings(_env_file=None, ROUTERAI_API_KEY="local-test", ROUTERAI_MODEL="m"))
    assert info["source"] == "unknown" and info["price_input_per_million"] is None


def test_context_overflow_reported_by_provider_has_its_own_code():
    problem = as_provider_error(ContextOverflowError("prompt is too long"), "RouterAI")
    assert problem.code == "context_limit" and "окно контекста" in str(problem)
