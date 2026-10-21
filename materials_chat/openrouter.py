"""Модель для агента: ChatOpenAI из LangChain, настроенный на RouterAI или OpenRouter.

RouterAI и OpenRouter совместимы с Chat Completions, но добавляют в ответ два поля,
которые стандартный ChatOpenAI отбрасывает: фактическую стоимость (usage.cost) и
рассуждения модели (reasoning_details). RouterAIChat их сохраняет.
"""
from __future__ import annotations

import logging

import httpx
import openai
from langchain_core.exceptions import ContextOverflowError
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

log = logging.getLogger(__name__)

# Валюта цен в каталоге моделей провайдера.
PROVIDER_CURRENCY = {"RouterAI": "RUB", "OpenRouter": "USD"}
REASONING = "reasoning_details"


class ProviderError(RuntimeError):
    def __init__(self, message: str, code: str = "provider_error"):
        super().__init__(message)
        self.code = code


def _per_million(value) -> float | None:
    try:
        return float(value) * 1_000_000 if value is not None else None
    except (TypeError, ValueError):
        return None


async def fetch_model_info(settings) -> dict:
    """Окно контекста и цены выбранной модели из каталога провайдера (GET /models).

    Значения из .env (MODEL_CONTEXT_TOKENS, PRICE_*) имеют приоритет над каталогом.
    """
    provider = getattr(settings, "provider_name", "API модели")
    info = {"name": None, "context_window": None, "price_input_per_million": None,
            "price_output_per_million": None, "price_cache_read_per_million": None,
            "currency": getattr(settings, "price_currency", None) or PROVIDER_CURRENCY.get(provider),
            "source": "unknown"}
    model = getattr(settings, "openrouter_model", "")
    base = str(getattr(settings, "openrouter_base_url", "")).rstrip("/")
    if model and base:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=10)) as client:
                response = await client.get(base + "/models")
            response.raise_for_status()
            entry = next((item for item in response.json().get("data", []) if item.get("id") == model), None)
            if entry:
                pricing = entry.get("pricing") or {}
                info.update(name=entry.get("name"), context_window=entry.get("context_length"),
                            price_input_per_million=_per_million(pricing.get("prompt")),
                            price_output_per_million=_per_million(pricing.get("completion")),
                            price_cache_read_per_million=_per_million(pricing.get("input_cache_read")),
                            source="provider")
            else:
                log.warning("Модель %s не найдена в каталоге %s", model, provider)
        except (httpx.HTTPError, ValueError, AttributeError):
            log.warning("Не удалось получить каталог моделей %s", provider, exc_info=True)
    overrides = {"context_window": getattr(settings, "model_context_tokens", None),
                 "price_input_per_million": getattr(settings, "price_input_per_million", None),
                 "price_output_per_million": getattr(settings, "price_output_per_million", None)}
    for key, value in overrides.items():
        if value is not None:
            info[key] = value
            info["source"] = "settings" if info["source"] == "unknown" else info["source"]
    return info


def reasoning_fragments(stored: list) -> list:
    """Фрагменты рассуждений в том виде, в каком их прислал провайдер."""
    return [item.get("fragment", item) if isinstance(item, dict) else item for item in stored]


class RouterAIChat(ChatOpenAI):
    """ChatOpenAI, который не теряет стоимость и рассуждения из ответа RouterAI/OpenRouter."""

    def _convert_chunk_to_generation_chunk(self, chunk: dict, default_chunk_class: type,
                                           base_generation_info: dict | None):
        generation = super()._convert_chunk_to_generation_chunk(chunk, default_chunk_class, base_generation_info)
        if generation is None:
            return None
        message = generation.message
        if chunk.get("usage"):
            # Полный usage провайдера: в нём есть фактическое списание (cost).
            message.response_metadata["provider_usage"] = chunk["usage"]
        for choice in chunk.get("choices") or []:
            fragments = (choice.get("delta") or {}).get(REASONING)
            if fragments:
                # LangChain склеивает куски потока по полю index и при этом удваивает
                # строковые поля (format → «unknownunknown»). Обёртка без index
                # сохраняет фрагменты как есть, как их пересылал прежний клиент.
                message.additional_kwargs[REASONING] = [{"fragment": item} for item in fragments]
        return generation

    def _get_request_payload(self, input_, *, stop: list[str] | None = None, **kwargs) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        # ChatOpenAI переименовывает max_tokens в max_completion_tokens (так требует
        # OpenAI); совместимые провайдеры документируют прежнее имя.
        if "max_completion_tokens" in payload:
            payload["max_tokens"] = payload.pop("max_completion_tokens")
        # Рассуждения возвращаются модели вместе с её сообщениями: при вызове
        # инструментов модель продолжает с того места, где остановилась.
        messages = self._convert_input(input_).to_messages()
        for message, entry in zip(messages, payload.get("messages", [])):
            if isinstance(message, AIMessage) and message.additional_kwargs.get(REASONING):
                entry[REASONING] = reasoning_fragments(message.additional_kwargs[REASONING])
        return payload


def create_chat_model(settings, **overrides) -> RouterAIChat:
    """Модель из настроек .env. overrides — для тестов (например, свой HTTP-клиент)."""
    key_setting, model_setting = getattr(settings, "model_setting_names", ("OPENROUTER_API_KEY", "OPENROUTER_MODEL"))
    if not getattr(settings, "openrouter_api_key", ""):
        raise ProviderError(f"Укажите {key_setting} в .env.", "missing_api_key")
    if not getattr(settings, "openrouter_model", ""):
        raise ProviderError(f"Укажите {model_setting} в .env.", "missing_model")
    options = {"model": settings.openrouter_model, "api_key": settings.openrouter_api_key,
               "base_url": str(getattr(settings, "openrouter_base_url", "https://openrouter.ai/api/v1")).rstrip("/"),
               "max_tokens": getattr(settings, "max_output_tokens", 4096),
               "stream_usage": True, "timeout": 120, "max_retries": 2,
               "default_headers": {"X-Title": "IACPaaS Materials Study"}}
    return RouterAIChat(**(options | overrides))


def as_provider_error(error: BaseException, provider: str) -> ProviderError | None:
    """Ошибка SDK провайдера → понятное сообщение для интерфейса (тело ответа не раскрываем)."""
    if isinstance(error, ProviderError):
        return error
    if isinstance(error, ContextOverflowError):
        return ProviderError(f"Диалог не помещается в окно контекста модели {provider}. Начните новый диалог.",
                             "context_limit")
    if isinstance(error, openai.APITimeoutError):
        return ProviderError(f"Истекло время ожидания {provider}.", "provider_timeout")
    if isinstance(error, openai.APIConnectionError):
        return ProviderError(f"Не удалось связаться с {provider}.", "provider_connection")
    if isinstance(error, openai.APIStatusError):
        # Тело ответа провайдера может повторять данные пользователя или ключ;
        # для локального интерфейса достаточно кода статуса.
        descriptions = {401: f"Ключ {provider} отклонён.",
                        402: f"Недостаточно средств на счёте {provider}.",
                        429: f"{provider} ограничил частоту запросов. Повторите чуть позже."}
        return ProviderError(descriptions.get(error.status_code, f"Ошибка {provider}: HTTP {error.status_code}."),
                             f"http_{error.status_code}")
    if isinstance(error, openai.APIError):
        return ProviderError(f"{provider} прервал генерацию ответа.", "provider_stream_error")
    return None
