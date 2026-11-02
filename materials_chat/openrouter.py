"""Модель для агента: ChatOpenAI из LangChain, настроенный на RouterAI, OpenRouter или локальный сервер.

Все они совместимы с Chat Completions. RouterAI и OpenRouter добавляют в ответ два поля,
которые стандартный ChatOpenAI отбрасывает: фактическую стоимость (usage.cost) и
рассуждения модели (reasoning_details). RouterAIChat их сохраняет. Локальный сервер
(LM Studio, Ollama) ключа не требует, цен и окна контекста в каталоге не сообщает.
"""
from __future__ import annotations

import logging
import re

import httpx
import openai
from langchain_core.exceptions import ContextOverflowError
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from .config import LOCAL_PROVIDER, is_local_endpoint

log = logging.getLogger(__name__)

# Валюта цен в каталоге моделей провайдера.
PROVIDER_CURRENCY = {"RouterAI": "RUB", "OpenRouter": "USD"}
REASONING = "reasoning_details"
# Локальному серверу ключ не нужен, но клиент OpenAI без ключа не создаётся.
LOCAL_API_KEY = "not-needed"
THINK_OPEN, THINK_CLOSE = "<think>", "</think>"
THINK_BLOCK = re.compile(r"<think>.*?(?:</think>\s*|$)", re.S)


class ProviderError(RuntimeError):
    def __init__(self, message: str, code: str = "provider_error"):
        super().__init__(message)
        self.code = code


def endpoint_is_local(settings) -> bool:
    """Модель на локальном сервере (адрес на этом компьютере или в локальной сети)."""
    value = getattr(settings, "model_is_local", None)
    return value if isinstance(value, bool) else is_local_endpoint(getattr(settings, "openrouter_base_url", ""))


def missing_model_settings(settings) -> list[str]:
    """Каких настроек модели не хватает. Ключ API нужен только облачному провайдеру."""
    key_name, model_name = getattr(settings, "model_setting_names", ("OPENROUTER_API_KEY", "OPENROUTER_MODEL"))
    missing = [] if endpoint_is_local(settings) or getattr(settings, "openrouter_api_key", "") else [key_name]
    return missing + ([] if getattr(settings, "openrouter_model", "") else [model_name])


def _per_million(value) -> float | None:
    try:
        return float(value) * 1_000_000 if value is not None else None
    except (TypeError, ValueError):
        return None


async def lmstudio_context(client: httpx.AsyncClient, base: str, model: str) -> int | None:
    """Окно, с которым модель загружена в LM Studio: его отдаёт родной REST API (/api/v0/models/{id}).

    У незагруженной модели окна нет (LM Studio загрузит её со своим окном по умолчанию) — None.
    """
    root = base[:-3] if base.endswith("/v1") else base
    try:
        response = await client.get(f"{root}/api/v0/models/{model}")
        entry = response.json() if response.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        return None
    value = entry.get("loaded_context_length") if isinstance(entry, dict) else None
    return value if isinstance(value, int) and value > 0 else None


async def fetch_model_info(settings) -> dict:
    """Окно контекста и цены выбранной модели из каталога провайдера (GET /models).

    Значения из .env (MODEL_CONTEXT_TOKENS, PRICE_*) имеют приоритет над каталогом.
    context_source — откуда окно: provider (каталог), lmstudio (окно загруженной модели),
    settings (MODEL_CONTEXT_TOKENS), default (никто не сообщил — берётся окно по умолчанию).
    free — локальная модель без цен: за токены никто не платит.
    """
    provider = getattr(settings, "provider_name", "API модели")
    local = endpoint_is_local(settings)
    info = {"name": None, "context_window": None, "price_input_per_million": None,
            "price_output_per_million": None, "price_cache_read_per_million": None,
            "currency": getattr(settings, "price_currency", None) or PROVIDER_CURRENCY.get(provider),
            "source": "unknown", "context_source": "default", "free": False}
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
                    if info["context_window"]:
                        info["context_source"] = "provider"
                    elif local and (window := await lmstudio_context(client, base, model)):
                        info.update(context_window=window, context_source="lmstudio")
                else:
                    log.warning("Модель %s не найдена в каталоге %s", model, provider)
        except (httpx.HTTPError, ValueError, AttributeError):
            # Локальный сервер может быть ещё не запущен: это не ошибка приложения, трассировка не нужна.
            log.warning("Не удалось получить каталог моделей %s", provider, exc_info=not local)
    overrides = {"context_window": getattr(settings, "model_context_tokens", None),
                 "price_input_per_million": getattr(settings, "price_input_per_million", None),
                 "price_output_per_million": getattr(settings, "price_output_per_million", None)}
    for key, value in overrides.items():
        if value is not None:
            info[key] = value
            info["source"] = "settings" if info["source"] == "unknown" else info["source"]
    if overrides["context_window"] is not None:
        info["context_source"] = "settings"
    info["free"] = local and info["price_input_per_million"] is None and info["price_output_per_million"] is None
    return info


def strip_thinking(text: str) -> str:
    """Текст ответа без рассуждений <think>…</think>: так их присылают некоторые локальные модели."""
    return THINK_BLOCK.sub("", text) if THINK_OPEN in text else text


class ThinkFilter:
    """Убирает <think>…</think> из потока текста по кускам; тег может прийти разрезанным между кусками."""

    def __init__(self):
        self.inside, self.pending, self.after = False, "", False

    @staticmethod
    def _partial(text: str, tag: str) -> int:
        """Длина конца text, который может оказаться началом тега."""
        return next((size for size in range(min(len(tag) - 1, len(text)), 0, -1) if text.endswith(tag[:size])), 0)

    def feed(self, text: str) -> str:
        data, self.pending, visible = self.pending + text, "", []
        while data:
            tag = THINK_CLOSE if self.inside else THINK_OPEN
            position = data.find(tag)
            if position < 0:
                keep = self._partial(data, tag)
                if not self.inside:
                    visible.append(data[:len(data) - keep])
                self.pending = data[len(data) - keep:] if keep else ""
                break
            if not self.inside:
                visible.append(data[:position])
            else:
                self.after = True  # после рассуждения модель обычно ставит пустые строки — ответу они не нужны
            self.inside = not self.inside
            data = data[position + len(tag):]
        result = "".join(visible)
        if self.after:
            result = result.lstrip()
            self.after = not result
        return result

    def flush(self) -> str:
        """Остаток в конце сообщения: недописанный тег вне рассуждения — обычный текст."""
        rest, self.pending = self.pending, ""
        return "" if self.inside else rest


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
    local = endpoint_is_local(settings)
    key = getattr(settings, "openrouter_api_key", "") or (LOCAL_API_KEY if local else "")
    if not key:
        raise ProviderError(f"Укажите {key_setting} в .env.", "missing_api_key")
    if not getattr(settings, "openrouter_model", ""):
        raise ProviderError(f"Укажите {model_setting} в .env.", "missing_model")
    options = {"model": settings.openrouter_model, "api_key": key,
               "base_url": str(getattr(settings, "openrouter_base_url", "https://openrouter.ai/api/v1")).rstrip("/"),
               "max_tokens": getattr(settings, "max_output_tokens", 4096),
               "stream_usage": True, "timeout": getattr(settings, "model_timeout_seconds", 120),
               # Повтор после тайм-аута локальной модели снова занял бы всё время ожидания.
               "max_retries": 0 if local else 2,
               "default_headers": {"X-Title": "IACPaaS Materials Study"}}
    temperature = getattr(settings, "model_temperature", None)
    if temperature is not None:
        # 0 — меньше разброса между одинаковыми вопросами: модель выбирает самые вероятные шаги и формулировки.
        options["temperature"] = temperature
    return RouterAIChat(**(options | overrides))


def _server_message(error: openai.APIStatusError) -> str:
    """Текст ошибки из ответа локального сервера (например, о переполнении окна), не длиннее 300 символов."""
    body = error.body
    if isinstance(body, dict):
        body = body.get("error", body)
    if isinstance(body, dict):
        body = body.get("message") or ""
    text = " ".join(str(body or "").split())
    return text[:300] + ("…" if len(text) > 300 else "")


def local_provider_error(error: BaseException, settings) -> ProviderError | None:
    """Ошибки локального сервера модели — с подсказкой, что сделать, чтобы он ответил."""
    base = str(getattr(settings, "openrouter_base_url", "")).rstrip("/")
    model = getattr(settings, "openrouter_model", "")
    if isinstance(error, ContextOverflowError):
        return ProviderError("Диалог не помещается в окно контекста локальной модели. Начните новый диалог "
                             "или загрузите модель с большим окном и укажите его в MODEL_CONTEXT_TOKENS.", "context_limit")
    if isinstance(error, openai.APITimeoutError):
        timeout = getattr(settings, "model_timeout_seconds", 120)
        return ProviderError(f"Локальная модель не ответила за {timeout:g} с. Увеличьте MODEL_TIMEOUT_SECONDS "
                             "или возьмите модель меньше.", "provider_timeout")
    if isinstance(error, openai.APIConnectionError):
        return ProviderError(f"Локальный сервер модели не отвечает по адресу {base}. Запустите его "
                             "(LM Studio: «lms server start», Ollama: «ollama serve») и повторите вопрос.",
                             "provider_connection")
    if isinstance(error, openai.APIStatusError):
        if error.status_code == 404:
            return ProviderError(f"Модель «{model}» не найдена на локальном сервере {base}. Загрузите её "
                                 "(LM Studio: «lms load …», Ollama: «ollama pull …») или исправьте ROUTERAI_MODEL.",
                                 "http_404")
        # Тело ответа локального сервера не уходит за пределы компьютера; в нём причина (например, переполнение окна).
        detail = _server_message(error)
        hint = " Возможно, запрос не помещается в окно, с которым загружена модель." if error.status_code == 400 else ""
        return ProviderError(f"Локальный сервер модели отклонил запрос (HTTP {error.status_code})"
                             + (f": {detail}" if detail else ".") + hint, f"http_{error.status_code}")
    if isinstance(error, openai.APIError):
        return ProviderError("Локальный сервер модели прервал генерацию ответа.", "provider_stream_error")
    return None


def as_provider_error(error: BaseException, provider: str, settings=None) -> ProviderError | None:
    """Ошибка SDK провайдера → понятное сообщение для интерфейса (тело ответа облачного провайдера не раскрываем)."""
    if isinstance(error, ProviderError):
        return error
    if provider == LOCAL_PROVIDER:
        return local_provider_error(error, settings)
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
