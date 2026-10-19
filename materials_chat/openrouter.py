"""Общий Chat Completions клиент для RouterAI и OpenRouter (совместимый API)."""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator

import httpx

log = logging.getLogger(__name__)

# Валюта цен в каталоге моделей провайдера.
PROVIDER_CURRENCY = {"RouterAI": "RUB", "OpenRouter": "USD"}


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


class OpenRouterClient:
    def __init__(self, settings):
        self.settings = settings

    async def stream(self, messages: list[dict], tools: list[dict],
                     tool_choice: str = "auto") -> AsyncIterator[dict]:
        provider = getattr(self.settings, "provider_name", "API модели")
        key_setting, model_setting = getattr(self.settings, "model_setting_names", ("OPENROUTER_API_KEY", "OPENROUTER_MODEL"))
        if not getattr(self.settings, "openrouter_api_key", ""):
            raise ProviderError(f"Укажите {key_setting} в .env.", "missing_api_key")
        if not getattr(self.settings, "openrouter_model", ""):
            raise ProviderError(f"Укажите {model_setting} в .env.", "missing_model")
        base = str(getattr(self.settings, "openrouter_base_url", "https://openrouter.ai/api/v1")).rstrip("/")
        payload = {"model": self.settings.openrouter_model, "messages": messages,
                   "stream": True, "stream_options": {"include_usage": True},
                   "max_tokens": getattr(self.settings, "max_output_tokens", 4096)}
        if tools:
            payload.update(tools=tools, tool_choice=tool_choice)
        headers = {"Authorization": f"Bearer {self.settings.openrouter_api_key}",
                   "Content-Type": "application/json", "X-Title": "IACPaaS Materials Study"}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=20)) as client:
                async with client.stream("POST", base + "/chat/completions", json=payload, headers=headers) as response:
                    if response.status_code >= 400:
                        # Provider response bodies can echo user data or credentials.
                        # Status is enough for the local UI; do not expose raw bodies.
                        descriptions = {401: f"Ключ {provider} отклонён.",
                                        402: f"Недостаточно средств на счёте {provider}.",
                                        429: f"{provider} ограничил частоту запросов. Повторите чуть позже."}
                        raise ProviderError(descriptions.get(response.status_code,
                                            f"Ошибка {provider}: HTTP {response.status_code}."), f"http_{response.status_code}")
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            break
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if event.get("error"):
                            raise ProviderError(f"{provider} прервал генерацию ответа.", "provider_stream_error")
                        if event.get("usage"):
                            yield {"usage": event["usage"]}
                        for choice in event.get("choices", []):
                            yield {"delta": choice.get("delta", {}),
                                   "finish_reason": choice.get("finish_reason")}
        except httpx.TimeoutException as exc:
            raise ProviderError(f"Истекло время ожидания {provider}.", "provider_timeout") from exc
        except httpx.RequestError as exc:
            raise ProviderError(f"Не удалось связаться с {provider}.", "provider_connection") from exc
