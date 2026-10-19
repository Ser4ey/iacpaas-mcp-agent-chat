"""RouterAI environment aliases and compatible streaming API; no paid requests."""
import json

import httpx
import pytest

from materials_chat.config import Settings
from materials_chat.openrouter import OpenRouterClient, ProviderError, fetch_model_info


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
async def test_routerai_receives_streaming_tools_and_returns_usage(monkeypatch):
    actual_client = httpx.AsyncClient
    def handle(request):
        assert str(request.url) == "https://routerai.ru/api/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer local-test"
        body = json.loads(request.content)
        assert body["model"] == "deepseek/deepseek-v4.1-flash"
        assert body["stream"] and body["tools"] and body["tool_choice"] == "required"
        payload = {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call1", "function": {"name": "search_graph", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}],
                   "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}}
        return httpx.Response(200, text="data: " + json.dumps(payload) + "\n\ndata: [DONE]\n\n", headers={"Content-Type": "text/event-stream"})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: actual_client(transport=httpx.MockTransport(handle), **kwargs))
    settings = Settings(_env_file=None, ROUTERAI_API_KEY="local-test", ROUTERAI_MODEL="deepseek/deepseek-v4.1-flash")
    chunks = [chunk async for chunk in OpenRouterClient(settings).stream([], [{"type": "function", "function": {"name": "search_graph"}}], "required")]
    assert chunks[0]["usage"]["total_tokens"] == 16
    assert chunks[1]["delta"]["tool_calls"][0]["function"]["name"] == "search_graph"


@pytest.mark.asyncio
async def test_routerai_error_names_current_provider_not_previous_one(monkeypatch):
    actual_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: actual_client(transport=httpx.MockTransport(lambda _: httpx.Response(401)), **kwargs))
    settings = Settings(_env_file=None, ROUTERAI_API_KEY="local-test", ROUTERAI_MODEL="model")
    with pytest.raises(ProviderError, match="Ключ RouterAI отклонён"):
        _ = [chunk async for chunk in OpenRouterClient(settings).stream([], [])]


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
