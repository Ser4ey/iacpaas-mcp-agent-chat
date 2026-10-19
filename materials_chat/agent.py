"""Агент: модель сама решает, нужны ли MCP-инструменты, и отвечает по их результатам.

Цикл одного вопроса:
1. Модель получает историю, вопрос и описания инструментов (tool_choice="auto").
2. Если она запросила инструменты — выполняем их через MCP и возвращаем результаты.
3. Повторяем, пока модель не ответит текстом. На последнем разрешённом шаге
   инструменты отключаются (tool_choice="none"): модель обязана ответить по найденному.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field

from .openrouter import OpenRouterClient, ProviderError
from .prompts import FINAL_STEP_NOTE, SYSTEM_PROMPT
from .sessions import SessionStore
from .usage import add_usage, empty_usage, request_usage

log = logging.getLogger(__name__)


@dataclass
class TurnResult:
    content: str = ""
    trace: list = field(default_factory=list)
    usage: dict = field(default_factory=empty_usage)
    model_messages: list = field(default_factory=list)
    context_tokens: int = 0
    grounded: bool = False      # были ли успешные обращения к данным в этом ответе
    truncated: bool = False     # ответ обрезан лимитом длины
    interrupted: bool = False


@dataclass
class Reply:
    """Один ответ модели: текст, запрошенные инструменты и причина остановки."""
    content: str = ""
    tool_calls: dict = field(default_factory=dict)
    reasoning: list = field(default_factory=list)
    finish_reason: str | None = None


def context_budget(settings, model_info: dict | None) -> int:
    """Сколько токенов истории можно отправить: окно модели минус место под ответ."""
    window = getattr(settings, "model_context_tokens", None) or (model_info or {}).get("context_window") or 32768
    budget = window - getattr(settings, "max_output_tokens", 4096)
    soft_limit = getattr(settings, "max_context_tokens", None)
    return min(budget, soft_limit) if soft_limit else budget


class Agent:
    def __init__(self, settings, bridge_factory=None, provider=None, model_info: dict | None = None):
        self.settings = settings
        if bridge_factory is None:
            from .mcp_client import MCPBridge
            bridge_factory = MCPBridge
        self.bridge_factory = bridge_factory
        self.provider = provider or OpenRouterClient(settings)
        self.model_info = model_info
        self.result = TurnResult()

    async def run(self, text: str, history: list[dict]):
        """Асинхронный генератор событий (имя, данные) для потока в браузер."""
        self.result = result = TurnResult()
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history, {"role": "user", "content": text}]
        budget = context_budget(self.settings, self.model_info)
        self._check_context(messages, [], budget)
        failure = None
        try:
            yield "status", {"message": "Подключаю инструменты…"}
            async with self.bridge_factory(self.settings) as bridge:
                try:
                    async for event in self._loop(bridge, messages, budget):
                        yield event
                except ProviderError as error:
                    # Поднимаем после выхода из MCP-сессии: внутри неё библиотека anyio
                    # завернула бы ошибку в ExceptionGroup и потеряла понятный текст.
                    failure = error
        except asyncio.CancelledError:
            result.interrupted = True
            raise
        if failure:
            raise failure

    async def _loop(self, bridge, messages: list[dict], budget: int):
        result, start = self.result, len(messages)
        tools = await bridge.list_tools()
        if not tools:
            raise ProviderError("MCP-сервер не предоставил инструменты.", "mcp_unavailable")
        max_steps = getattr(self.settings, "max_tool_steps", 8)
        max_calls = getattr(self.settings, "max_tool_calls", 16)
        calls_made = 0
        for step in range(max_steps + 1):
            final = step == max_steps or calls_made >= max_calls
            request = messages + ([{"role": "system", "content": FINAL_STEP_NOTE}] if final else [])
            self._check_context(request, tools, budget)
            yield "status", {"message": "Формулирую ответ…" if final else
                             "Думаю над вопросом…" if step == 0 else "Изучаю найденные данные…"}
            reply = Reply()
            async for event in self._stream(request, tools, "none" if final else "auto", reply):
                yield event
            assistant = {"role": "assistant", "content": reply.content}
            if reply.reasoning:
                assistant["reasoning_details"] = reply.reasoning

            if reply.tool_calls and not final:
                assistant["tool_calls"] = list(reply.tool_calls.values())
                # Группа «запрос инструментов + все ответы» добавляется в историю целиком.
                group = [assistant]
                for call in assistant["tool_calls"]:
                    # На каждый запрошенный вызов нужен ответ; сверх лимита — ответ-ошибка.
                    allowed = calls_made < max_calls
                    if allowed:
                        calls_made += 1
                    async for event in self._call_tool(bridge, call, allowed, group):
                        yield event
                messages.extend(group)
                result.model_messages = messages[start:]
                continue

            # Ответ текстом — ход завершён.
            result.truncated = reply.finish_reason == "length"
            if result.truncated:
                note = "\n\n_(Ответ обрезан: достигнут лимит длины ответа.)_"
                result.content += note
                assistant["content"] += note
                yield "answer_delta", {"text": note}
            if not result.content.strip():
                note = "Модель не дала ответа. Попробуйте переформулировать вопрос."
                result.content = assistant["content"] = note
                yield "answer_delta", {"text": note}
            messages.append(assistant)
            result.model_messages = messages[start:]
            result.grounded = any(not item["is_error"] for item in result.trace)
            yield "answer_meta", {"grounded": result.grounded, "tool_calls": len(result.trace),
                                  "truncated": result.truncated}
            return

    async def _stream(self, messages: list[dict], tools: list[dict], tool_choice: str, reply: Reply):
        """Читает поток модели: текст сразу уходит в браузер, вызовы инструментов собираются."""
        result, usage, wrote_text = self.result, None, False
        try:
            async for item in self.provider.stream(messages, tools, tool_choice):
                if "usage" in item:
                    usage = item["usage"]
                if item.get("finish_reason"):
                    reply.finish_reason = item["finish_reason"]
                delta = item.get("delta") or {}
                if delta.get("reasoning_details"):
                    reply.reasoning.extend(delta["reasoning_details"])
                if delta.get("content"):
                    if not wrote_text and result.content:  # новый абзац после прошлого текста
                        result.content += "\n\n"
                        yield "answer_delta", {"text": "\n\n"}
                    wrote_text = True
                    reply.content += delta["content"]
                    result.content += delta["content"]
                    yield "answer_delta", {"text": delta["content"]}
                for call in delta.get("tool_calls") or []:
                    target = reply.tool_calls.setdefault(call.get("index", 0),
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    if call.get("id"):
                        target["id"] = call["id"]
                    function = call.get("function") or {}
                    target["function"]["name"] += function.get("name") or ""
                    target["function"]["arguments"] += function.get("arguments") or ""
            if reply.finish_reason is None:
                raise ProviderError("Провайдер оборвал ответ до его завершения. Повторите вопрос.", "provider_incomplete")
        finally:
            # Расход учитываем и при обрыве: провайдер мог уже выставить счёт.
            result.usage = add_usage(result.usage, request_usage(usage, self.model_info))

    async def _call_tool(self, bridge, call: dict, allowed: bool, group: list[dict]):
        """Выполняет один вызов инструмента и добавляет ответ в группу сообщений."""
        name = call["function"]["name"]
        call["id"] = call_id = call["id"] or uuid.uuid4().hex
        try:
            arguments = json.loads(call["function"]["arguments"] or "{}")
            if not isinstance(arguments, dict):
                raise ValueError
        except (ValueError, TypeError):
            arguments, output = {}, {"ok": False, "error": {"code": "invalid_arguments",
                                                             "message": "Некорректные аргументы инструмента."}}
        else:
            output = None
        yield "tool_start", {"id": call_id, "name": name, "arguments": arguments}
        started = time.monotonic()
        if output is None and not allowed:
            output = {"ok": False, "error": {"code": "tool_limit",
                                             "message": "Достигнут лимит вызовов инструментов на один вопрос."}}
        elif output is None:
            try:
                output = await bridge.call_tool(name, arguments)
            except asyncio.CancelledError:
                self.result.trace.append({"id": call_id, "name": name, "arguments": arguments,
                    "result": {"ok": False, "error": {"code": "cancelled", "message": "Вызов остановлен пользователем."}},
                    "duration_ms": round((time.monotonic() - started) * 1000), "is_error": True})
                raise
            except Exception:
                log.exception("Инструмент MCP %s завершился ошибкой", name)
                output = {"ok": False, "error": {"code": "mcp_error", "message": "Не удалось выполнить инструмент MCP."}}
        trace = {"id": call_id, "name": name, "arguments": arguments, "result": output,
                 "duration_ms": round((time.monotonic() - started) * 1000), "is_error": not output.get("ok", False)}
        self.result.trace.append(trace)
        yield "tool_end", trace
        group.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps(output, ensure_ascii=False)})

    def _check_context(self, messages: list[dict], tools: list[dict], budget: int):
        self.result.context_tokens = SessionStore.estimate_tokens(messages + tools)
        if self.result.context_tokens > budget:
            raise ProviderError(f"Диалог превысил лимит контекста ({budget:,} токенов). Начните новый диалог."
                                .replace(",", " "), "context_limit")
