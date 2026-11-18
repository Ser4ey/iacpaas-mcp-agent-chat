"""RouterAI и своя модель (LLM_*): имена настроек, модель LangChain, каталог, ошибки; платных запросов нет."""
import json

import httpx
import httpx2
import openai
import pytest
from langchain_core.exceptions import ContextOverflowError

from materials_chat.config import Settings, public_endpoint
from materials_chat.agent import context_window
from materials_chat.openrouter import (ProviderError, ThinkFilter, as_provider_error, create_chat_model,
                                       fetch_model_info, missing_model_settings, strip_thinking)
from materials_chat.usage import add_usage, empty_usage, request_usage


def test_routerai_env_names_are_loaded_without_disclosing_key(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ROUTERAI_API_KEY=local-test\nROUTERAI_MODEL=deepseek/deepseek-v4.1-flash\nROUTERAI_BASEURL=https://routerai.ru/api/v1\n", encoding="utf-8")
    settings = Settings(_env_file=env)
    assert settings.model_settings_prefix == "ROUTERAI"
    assert settings.model_api_key == "local-test"
    assert settings.model_name == "deepseek/deepseek-v4.1-flash"
    assert settings.model_base_url == "https://routerai.ru/api/v1"
    assert settings.provider_name == "RouterAI"
    assert settings.model_setting_names == ("ROUTERAI_API_KEY", "ROUTERAI_MODEL")
    assert "local-test" not in repr(settings)


def test_routerai_address_is_built_in():
    settings = Settings(_env_file=None, ROUTERAI_API_KEY="local-test", ROUTERAI_MODEL="m")
    assert settings.model_base_url == "https://routerai.ru/api/v1"
    # Пустой ROUTERAI_BASE_URL в .env не ломает адрес.
    assert Settings(_env_file=None, ROUTERAI_BASE_URL="").model_base_url == "https://routerai.ru/api/v1"


def test_empty_settings_ask_for_routerai_and_name_both_blocks():
    settings = Settings(_env_file=None)
    assert settings.model_settings_prefix == "ROUTERAI"
    assert missing_model_settings(settings) == ["ROUTERAI_API_KEY", "ROUTERAI_MODEL"]


def test_own_model_block_wins_over_routerai(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LLM_BASE_URL=https://llm.example.com/v1/\nLLM_MODEL=corp-model\nLLM_API_KEY=own-test\n"
                   "ROUTERAI_API_KEY=router-test\nROUTERAI_MODEL=deepseek/deepseek-v4.1-flash\n", encoding="utf-8")
    settings = Settings(_env_file=env)
    assert settings.model_settings_prefix == "LLM"
    assert settings.model_base_url == "https://llm.example.com/v1"
    assert settings.model_name == "corp-model" and settings.model_api_key == "own-test"
    assert settings.model_setting_names == ("LLM_API_KEY", "LLM_MODEL")
    assert settings.provider_name == "llm.example.com" and not settings.model_is_local
    assert "own-test" not in repr(settings) and "router-test" not in repr(settings)
    # Проверяющему с другой моделью меняется модель своего блока, адрес остаётся тем же.
    other = settings.with_model("other-model")
    assert other.model_name == "other-model" and other.model_base_url == "https://llm.example.com/v1"
    assert settings.model_name == "corp-model"


def test_own_model_needs_only_its_name():
    # Ключ своему серверу не обязателен: если он нужен, сервер ответит 401, и сообщение назовёт LLM_API_KEY.
    remote = Settings(_env_file=None, LLM_BASE_URL="https://llm.example.com/v1", LLM_MODEL="m")
    assert missing_model_settings(remote) == []
    assert missing_model_settings(Settings(_env_file=None, LLM_BASE_URL="https://llm.example.com/v1")) == ["LLM_MODEL"]


def test_public_endpoint_hides_credentials_and_query():
    assert public_endpoint("https://user:secret@llm.example.com:8443/v1/?token=x") == "https://llm.example.com:8443/v1"
    assert public_endpoint("http://[::1]:1234/v1") == "http://[::1]:1234/v1"
    assert public_endpoint("") == ""


@pytest.mark.asyncio
async def test_own_remote_model_gets_its_key_and_errors_name_llm_settings(monkeypatch):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx2.Response(401, json={"error": {"message": "invalid key"}})

    settings = Settings(_env_file=None, LLM_BASE_URL="https://llm.example.com/v1", LLM_MODEL="corp-model",
                        LLM_API_KEY="own-test", ROUTERAI_API_KEY="router-test")
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
    with pytest.raises(openai.AuthenticationError) as error:
        await create_chat_model(settings, http_async_client=client, max_retries=0).ainvoke("Вопрос")
    assert str(seen[0].url) == "https://llm.example.com/v1/chat/completions"
    assert seen[0].headers["Authorization"] == "Bearer own-test"  # ключ RouterAI своему серверу не уходит
    problem = as_provider_error(error.value, settings.provider_name, settings)
    assert str(problem) == "Ключ llm.example.com отклонён. Проверьте LLM_API_KEY в .env."
    not_found = as_provider_error(openai.NotFoundError("nf", response=httpx.Response(
        404, request=httpx.Request("POST", "https://llm.example.com/v1/chat/completions")), body=None),
        settings.provider_name, settings)
    assert "LLM_MODEL" in str(not_found) and "LLM_BASE_URL" in str(not_found)

    # Каталог своего сервера запрашивается с тем же ключом: сервер может закрывать им и /models.
    catalog_requests = []
    actual_client = httpx.AsyncClient

    def catalog(request):
        catalog_requests.append(request)
        return httpx.Response(200, json={"data": [{"id": "corp-model", "context_length": 131072}]})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: actual_client(transport=httpx.MockTransport(catalog), **kwargs))
    info = await fetch_model_info(settings)
    assert info["context_window"] == 131072 and info["free"] is False
    assert str(catalog_requests[0].url) == "https://llm.example.com/v1/models"
    assert catalog_requests[0].headers["Authorization"] == "Bearer own-test"


@pytest.mark.asyncio
async def test_own_model_without_key_sends_a_stub():
    seen = []

    def handle(request):
        seen.append(request)
        payload = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m",
                   "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Да"}, "finish_reason": "stop"}]}
        return httpx2.Response(200, text="data: " + json.dumps(payload) + "\n\ndata: [DONE]\n\n",
                               headers={"Content-Type": "text/event-stream"})

    settings = Settings(_env_file=None, LLM_BASE_URL="https://llm.example.com/v1", LLM_MODEL="m")
    model = create_chat_model(settings, http_async_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle)))
    assert "".join([chunk.text async for chunk in model.astream("Вопрос")]) == "Да"
    assert seen[0].headers["Authorization"] == "Bearer not-needed"


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
    problem = as_provider_error(error.value, settings.provider_name, settings)
    assert str(problem) == "Ключ RouterAI отклонён. Проверьте ROUTERAI_API_KEY в .env." and problem.code == "http_401"


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


# ---------- Локальная модель (например, LM Studio, Ollama): без ключа, без цен, своё окно ----------

LOCAL = {"LLM_BASE_URL": "http://127.0.0.1:1234/v1", "LLM_MODEL": "qwen3-4b"}


@pytest.mark.parametrize("url, local", [
    ("http://127.0.0.1:1234/v1", True), ("http://localhost:11434/v1", True), ("http://[::1]:1234/v1", True),
    ("http://192.168.1.20:1234/v1", True), ("http://host.docker.internal:1234/v1", True),
    ("http://ollama:11434/v1", True), ("https://routerai.ru/api/v1", False), ("https://openrouter.ai/api/v1", False),
    ("https://api.example.com/v1", False)])
def test_local_endpoint_is_recognized_by_address(url, local):
    settings = Settings(_env_file=None, LLM_BASE_URL=url, LLM_MODEL="m")
    assert settings.model_is_local is local
    assert (settings.provider_name == "Локальная модель") is local
    assert missing_model_settings(settings) == []


def test_old_env_with_local_server_in_routerai_block_still_works():
    # .env, заполненный до появления LLM_*: локальный адрес в ROUTERAI_BASE_URL, ключ не нужен.
    settings = Settings(_env_file=None, ROUTERAI_BASE_URL="http://127.0.0.1:1234/v1", ROUTERAI_MODEL="qwen3-4b")
    assert settings.model_is_local and missing_model_settings(settings) == []
    # С адресом RouterAI ключ по-прежнему обязателен.
    assert missing_model_settings(Settings(_env_file=None, ROUTERAI_MODEL="m")) == ["ROUTERAI_API_KEY"]


def test_model_local_setting_overrides_the_address():
    assert Settings(_env_file=None, LLM_BASE_URL="https://llm.example.com/v1", MODEL_LOCAL="true").model_is_local
    forced = Settings(_env_file=None, LLM_BASE_URL="http://127.0.0.1:8080/v1", MODEL_LOCAL="false")
    assert not forced.model_is_local and forced.provider_name == "127.0.0.1"


@pytest.mark.asyncio
async def test_local_model_works_without_key_with_its_timeout():
    seen = []

    def handle(request):
        seen.append(request)
        payload = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "qwen3-4b",
                   "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Готово"}, "finish_reason": "stop"}]}
        return httpx2.Response(200, text="data: " + json.dumps(payload) + "\n\ndata: [DONE]\n\n",
                               headers={"Content-Type": "text/event-stream"})

    settings = Settings(_env_file=None, MODEL_TIMEOUT_SECONDS="600", **LOCAL)
    model = create_chat_model(settings, http_async_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle)))
    assert model.request_timeout == 600 and model.max_retries == 0
    chunks = [chunk async for chunk in model.astream("Вопрос")]
    assert "".join(chunk.text for chunk in chunks) == "Готово"
    assert str(seen[0].url) == "http://127.0.0.1:1234/v1/chat/completions"
    assert seen[0].headers["Authorization"] == "Bearer not-needed"  # заглушка, настоящий ключ не нужен


def catalog_transport(monkeypatch, routes):
    """httpx.AsyncClient приложения ходит в подменённый сервер: путь → (код, JSON)."""
    actual_client = httpx.AsyncClient

    def handle(request):
        status, body = routes.get(request.url.path, (404, {"error": "not found"}))
        return httpx.Response(status, json=body)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: actual_client(transport=httpx.MockTransport(handle), **kwargs))


@pytest.mark.asyncio
async def test_lm_studio_catalog_gives_loaded_window_and_no_prices(monkeypatch):
    catalog_transport(monkeypatch, {"/v1/models": (200, {"data": [{"id": "qwen3-4b", "object": "model"}]}),
                                    "/api/v0/models/qwen3-4b": (200, {"id": "qwen3-4b", "state": "loaded",
                                                                      "max_context_length": 262144,
                                                                      "loaded_context_length": 32768})})
    info = await fetch_model_info(Settings(_env_file=None, **LOCAL))
    assert info["context_window"] == 32768 and info["context_source"] == "lmstudio"
    assert info["free"] is True and info["currency"] is None and info["price_input_per_million"] is None


@pytest.mark.asyncio
async def test_local_window_unknown_falls_back_to_default_or_settings(monkeypatch):
    # Ollama: в каталоге нет окна, родного API LM Studio нет; у незагруженной модели LM Studio окна тоже нет.
    catalog_transport(monkeypatch, {"/v1/models": (200, {"data": [{"id": "qwen3-4b"}]})})
    info = await fetch_model_info(Settings(_env_file=None, **LOCAL))
    assert info["context_window"] is None and info["context_source"] == "default"
    assert context_window(Settings(_env_file=None, **LOCAL), info) == 32768
    manual = Settings(_env_file=None, MODEL_CONTEXT_TOKENS="16384", **LOCAL)
    info = await fetch_model_info(manual)
    assert info["context_window"] == 16384 and info["context_source"] == "settings"
    # Цены из .env — значит, стоимость считается, а не «бесплатно».
    priced = await fetch_model_info(Settings(_env_file=None, PRICE_INPUT_PER_MILLION="1",
                                             PRICE_OUTPUT_PER_MILLION="2", **LOCAL))
    assert priced["free"] is False


def test_local_usage_is_free_and_unknown_cloud_cost_is_null():
    free = request_usage({"prompt_tokens": 7000, "completion_tokens": 50}, {"free": True, "currency": None})
    assert free["cost"] == 0 and free["free"] and free["cost_known"] and free["currency"] is None
    unknown = request_usage({"prompt_tokens": 7000, "completion_tokens": 50}, {"free": False})
    assert unknown["cost"] is None and not unknown["free"] and not unknown["cost_known"]
    # Ни одной известной стоимости — сумма неизвестна, а не 0.
    total = add_usage(add_usage(empty_usage(), unknown), unknown)
    assert total["cost"] is None and total["cost_known"] is False and total["requests"] == 2
    assert add_usage(add_usage(empty_usage(), free), free)["free"] is True
    assert add_usage(free, unknown)["free"] is False


@pytest.mark.asyncio
async def test_local_server_errors_say_what_to_do():
    settings = Settings(_env_file=None, MODEL_TIMEOUT_SECONDS="300", **LOCAL)

    async def failure(handler):
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        with pytest.raises(openai.APIError) as error:
            await create_chat_model(settings, http_async_client=client).ainvoke("Вопрос")
        return as_provider_error(error.value, settings.provider_name, settings)

    def refuse(request):
        raise httpx2.ConnectError("connection refused", request=request)
    problem = await failure(refuse)
    assert problem.code == "provider_connection"
    assert "Локальный сервер модели не отвечает по адресу http://127.0.0.1:1234/v1" in str(problem)
    assert "LLM_BASE_URL" in str(problem)

    problem = await failure(lambda _: httpx2.Response(404, json={"error": "Model not found"}))
    assert problem.code == "http_404" and "«qwen3-4b» не найдена" in str(problem) and "LLM_MODEL" in str(problem)

    overflow = {"error": "Trying to keep the first 9000 tokens when context the overflows. However, the model is "
                         "loaded with context length of only 4096 tokens"}
    problem = await failure(lambda _: httpx2.Response(400, json=overflow))
    assert problem.code == "http_400" and "loaded with context length of only 4096" in str(problem)

    def slow(request):
        raise httpx2.ReadTimeout("timeout", request=request)
    problem = await failure(slow)
    assert problem.code == "provider_timeout" and "300 с" in str(problem) and "MODEL_TIMEOUT_SECONDS" in str(problem)


@pytest.mark.parametrize("pieces, visible", [
    (["<think>долго думаю</think>\n\nОтвет: 25."], "Ответ: 25."),
    (["<thi", "nk>думаю</th", "ink>", "\n\n", "Ответ: 25."], "Ответ: 25."),
    (["Ответ без рассуждений, 3 < 5."], "Ответ без рассуждений, 3 < 5."),
    (["Конец <", "b>"], "Конец <b>"),
    (["<think>оборвано на полуслове"], ""),
])
def test_think_blocks_are_removed_from_streamed_text(pieces, visible):
    stream = ThinkFilter()
    text = "".join(stream.feed(piece) for piece in pieces) + stream.flush()
    assert text == visible
    assert strip_thinking("".join(pieces)).strip() == visible
