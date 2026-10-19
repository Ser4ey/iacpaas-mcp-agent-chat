"""Общий Chat Completions клиент для RouterAI и OpenRouter (совместимый API)."""
from __future__ import annotations

import json
from typing import AsyncIterator

import httpx


class ProviderError(RuntimeError):
    def __init__(self, message: str, code: str = "provider_error"):
        super().__init__(message)
        self.code = code


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
                   "max_tokens": getattr(self.settings, "max_output_tokens", 1800)}
        if tools:
            payload.update(tools=tools, tool_choice=tool_choice, parallel_tool_calls=False)
        headers = {"Authorization": f"Bearer {self.settings.openrouter_api_key}",
                   "Content-Type": "application/json", "X-Title": "IACPaaS Materials Study"}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=20)) as client:
                async with client.stream("POST", base + "/chat/completions", json=payload, headers=headers) as response:
                    if response.status_code >= 400:
                        # Provider response bodies can echo user data or credentials.
                        # Status is enough for the local UI; do not expose raw bodies.
                        descriptions = {401: f"Ключ {provider} отклонён.",
                                        402: f"Недостаточно средств {provider}.",
                                        429: f"{provider} ограничил частоту запросов."}
                        raise ProviderError(descriptions.get(response.status_code,
                                            f"Ошибка {provider}: HTTP {response.status_code}."))
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
                            raise ProviderError("Провайдер прервал генерацию ответа.")
                        if event.get("usage"):
                            yield {"usage": event["usage"]}
                        for choice in event.get("choices", []):
                            yield {"delta": choice.get("delta", {}),
                                   "finish_reason": choice.get("finish_reason")}
        except httpx.TimeoutException as exc:
            raise ProviderError(f"Истекло время ожидания {provider}.", "provider_timeout") from exc
        except httpx.RequestError as exc:
            raise ProviderError(f"Не удалось связаться с {provider}.", "provider_connection") from exc
