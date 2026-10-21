"""Агент на LangChain: граф create_agent, инструменты MCP через langchain-mcp-adapters.

Цикл «модель → инструменты → модель» строит LangChain: узлы model и tools и переход
между ними по наличию tool_calls. Правила приложения добавляет middleware TurnPolicy:
1. Модель сама решает, нужны ли инструменты (tool_choice="auto").
2. На последнем разрешённом шаге инструменты отключаются (tool_choice="none"):
   модель обязана ответить по найденному. Вызовы сверх лимита получают ответ-ошибку.
3. Каждый вызов инструмента и обращение к модели — событие для интерфейса,
   запись журнала и учёт расхода.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelResponse
from langchain_core.messages import (AIMessage, AIMessageChunk, HumanMessage, SystemMessage, ToolMessage,
                                     convert_to_messages)
from langchain_core.utils.function_calling import convert_to_openai_tool

from .openrouter import REASONING, ProviderError, as_provider_error, create_chat_model, reasoning_fragments
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


def context_budget(settings, model_info: dict | None) -> int:
    """Сколько токенов истории можно отправить: окно модели минус место под ответ."""
    window = getattr(settings, "model_context_tokens", None) or (model_info or {}).get("context_window") or 32768
    budget = window - getattr(settings, "max_output_tokens", 4096)
    soft_limit = getattr(settings, "max_context_tokens", None)
    return min(budget, soft_limit) if soft_limit else budget


def to_dicts(messages: list) -> list[dict]:
    """Сообщения LangChain → формат Chat Completions, в котором хранится история диалога."""
    result = []
    for message in messages:
        if isinstance(message, AIMessage):
            item = {"role": "assistant", "content": str(message.text)}
            calls = [{"id": call["id"], "type": "function", "function": {
                "name": call["name"], "arguments": json.dumps(call["args"], ensure_ascii=False)}}
                for call in message.tool_calls]
            # Вызов с нечитаемыми аргументами LangChain выносит в invalid_tool_calls и сам
            # отвечает на него ошибкой; пара «вызов → ответ» должна остаться в истории.
            calls += [{"id": call["id"], "type": "function", "function": {
                "name": call["name"] or "", "arguments": "{}"}} for call in message.invalid_tool_calls]
            if calls:
                item["tool_calls"] = calls
            if message.additional_kwargs.get(REASONING):
                item[REASONING] = reasoning_fragments(message.additional_kwargs[REASONING])
        elif isinstance(message, ToolMessage):
            item = {"role": "tool", "tool_call_id": message.tool_call_id, "content": str(message.text)}
        elif isinstance(message, SystemMessage):
            item = {"role": "system", "content": str(message.text)}
        else:
            item = {"role": "user", "content": str(message.text)}
        result.append(item)
    return result


def complete_groups(messages: list) -> list:
    """Сообщения без последнего запроса инструментов, если на него пришли не все ответы."""
    answered = {message.tool_call_id for message in messages if isinstance(message, ToolMessage)}
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, AIMessage) and message.tool_calls:
            return messages if all(call["id"] in answered for call in message.tool_calls) else messages[:index]
    return messages


def tool_output(message: ToolMessage) -> dict:
    """Конверт результата MCP-инструмента (ok, data, error, meta) из ответа LangChain."""
    artifact = message.artifact if isinstance(message.artifact, dict) else {}
    if isinstance(artifact.get("structured_content"), dict):
        return artifact["structured_content"]
    text = str(message.text)
    try:
        value = json.loads(text)
        if isinstance(value, dict) and "ok" in value:
            return value
    except ValueError:
        pass
    # Ответ без конверта: неверные аргументы или сбой внутри MCP-сервера.
    return {"ok": False, "error": {"code": "tool_error",
                                   "message": text[:500] or "MCP не вернул структурированный результат."}}


class TurnPolicy(AgentMiddleware):
    """Правила одного хода поверх стандартного агента LangChain."""

    def __init__(self, agent: Agent, budget: int):
        super().__init__()
        self.agent, self.budget = agent, budget
        self.max_steps = getattr(agent.settings, "max_tool_steps", 8)
        self.max_calls = getattr(agent.settings, "max_tool_calls", 16)
        self.steps = 0      # обращений к модели в этом ходе
        self.calls = 0      # выполненных вызовов инструментов

    async def awrap_model_call(self, request, handler):
        step, self.steps = self.steps, self.steps + 1
        final = step >= self.max_steps or self.calls >= self.max_calls
        request.runtime.stream_writer(("status", {"message": "Формулирую ответ…" if final else
                                                  "Думаю над вопросом…" if step == 0 else "Изучаю найденные данные…"}))
        if final:
            # Подсказка живёт только в этом запросе и не попадает в историю диалога.
            request = request.override(tool_choice="none",
                                       messages=[*request.messages, SystemMessage(FINAL_STEP_NOTE)])
        else:
            request = request.override(tool_choice="auto")
        self.agent.check_context(request, self.budget)
        usage = None
        try:
            response = await handler(request)
            message = next(item for item in reversed(response.result) if isinstance(item, AIMessage))
            usage = message.response_metadata.get("provider_usage")
        finally:
            # Расход учитываем и при обрыве: провайдер мог уже выставить счёт.
            self.agent.add_usage(usage)
        finish_reason = message.response_metadata.get("finish_reason")
        if finish_reason is None:
            raise ProviderError("Провайдер оборвал ответ до его завершения. Повторите вопрос.", "provider_incomplete")
        self.agent.result.truncated = finish_reason == "length"
        for call in message.tool_calls:
            call["id"] = call["id"] or uuid.uuid4().hex
        if final and (message.tool_calls or message.invalid_tool_calls):
            # Инструменты уже отключены; если модель всё равно их просит — не выполняем.
            message = message.model_copy(update={"tool_calls": [], "invalid_tool_calls": []})
            return ModelResponse(result=[message], structured_response=response.structured_response)
        return response

    async def awrap_tool_call(self, request, handler):
        call, write = request.tool_call, request.runtime.stream_writer
        entry = {"id": call["id"], "name": call["name"], "arguments": call.get("args") or {}}
        self.agent.result.trace.append(entry)  # порядок журнала — порядок вызовов, а не их завершения
        write(("tool_start", dict(entry)))
        started = time.monotonic()
        allowed = self.calls < self.max_calls
        if allowed:
            self.calls += 1
        try:
            if allowed:
                output = tool_output(await handler(request))
            else:
                output = {"ok": False, "error": {"code": "tool_limit",
                                                 "message": "Достигнут лимит вызовов инструментов на один вопрос."}}
        except asyncio.CancelledError:
            entry.update(result={"ok": False, "error": {"code": "cancelled", "message": "Вызов остановлен пользователем."}},
                         duration_ms=round((time.monotonic() - started) * 1000), is_error=True)
            raise
        except Exception:
            log.exception("Инструмент MCP %s завершился ошибкой", call["name"])
            output = {"ok": False, "error": {"code": "mcp_error", "message": "Не удалось выполнить инструмент MCP."}}
        entry.update(result=output, duration_ms=round((time.monotonic() - started) * 1000),
                     is_error=not output.get("ok", False))
        write(("tool_end", dict(entry)))
        return ToolMessage(json.dumps(output, ensure_ascii=False), tool_call_id=call["id"], name=call["name"],
                           status="error" if entry["is_error"] else "success")


class Agent:
    def __init__(self, settings, tools_factory=None, model=None, model_info: dict | None = None):
        self.settings = settings
        if tools_factory is None:
            from .mcp_client import mcp_tools
            tools_factory = mcp_tools
        self.tools_factory = tools_factory
        self.model = model
        self.model_info = model_info
        self.result = TurnResult()

    async def run(self, text: str, history: list[dict]):
        """Асинхронный генератор событий (имя, данные) для потока в браузер."""
        self.result = result = TurnResult()
        budget = context_budget(self.settings, self.model_info)
        self._check([{"role": "system", "content": SYSTEM_PROMPT}, *history, {"role": "user", "content": text}], budget)
        provider = getattr(self.settings, "provider_name", "API модели")
        failure = None
        try:
            model = self.model or create_chat_model(self.settings)
            yield "status", {"message": "Подключаю инструменты…"}
            async with self.tools_factory(self.settings) as tools:
                try:
                    async for event in self._loop(model, tools, [*convert_to_messages(history), HumanMessage(text)], budget):
                        yield event
                except Exception as error:
                    # Поднимаем после выхода из MCP-сессии: внутри неё библиотека anyio
                    # завернула бы ошибку в ExceptionGroup и потеряла понятный текст.
                    failure = as_provider_error(error, provider)
                    if failure is None:
                        raise
        except asyncio.CancelledError:
            result.interrupted = True
            raise
        if failure:
            raise failure

    async def _loop(self, model, tools: list, messages: list, budget: int):
        if not tools:
            raise ProviderError("MCP-сервер не предоставил инструменты.", "mcp_unavailable")
        result, start = self.result, len(messages)
        policy = TurnPolicy(self, budget)
        graph = create_agent(model, tools, system_prompt=SYSTEM_PROMPT, middleware=[policy])
        # На каждый шаг — узел модели и узел инструментов, плюс финальный ответ.
        config = {"recursion_limit": 2 * policy.max_steps + 5}
        reply_id, wrote_text = None, False
        async for mode, payload in graph.astream({"messages": messages}, config,
                                                 stream_mode=["messages", "custom", "values"]):
            if mode == "custom":            # события TurnPolicy: статус, начало и конец вызова
                yield payload
            elif mode == "values":          # состояние графа после каждого шага
                result.model_messages = to_dicts(complete_groups(payload["messages"][start:]))
            elif isinstance(payload[0], AIMessageChunk) and payload[0].text:
                chunk = payload[0]           # текст ответа модели по мере генерации
                if chunk.id != reply_id:
                    reply_id, wrote_text = chunk.id, False
                if not wrote_text and result.content:  # новый абзац после прошлого текста
                    result.content += "\n\n"
                    yield "answer_delta", {"text": "\n\n"}
                wrote_text = True
                result.content += chunk.text
                yield "answer_delta", {"text": chunk.text}

        # Ответ текстом — ход завершён.
        assistant = result.model_messages[-1]
        if result.truncated:
            note = "\n\n_(Ответ обрезан: достигнут лимит длины ответа.)_"
            result.content += note
            assistant["content"] += note
            yield "answer_delta", {"text": note}
        if not result.content.strip():
            note = "Модель не дала ответа. Попробуйте переформулировать вопрос."
            result.content = assistant["content"] = note
            yield "answer_delta", {"text": note}
        result.grounded = any(not item.get("is_error", True) for item in result.trace)
        yield "answer_meta", {"grounded": result.grounded, "tool_calls": len(result.trace),
                              "truncated": result.truncated}

    def check_context(self, request, budget: int):
        """Размер запроса к модели: системная инструкция, история и схемы инструментов."""
        messages = [{"role": "system", "content": request.system_prompt or ""}, *to_dicts(request.messages)]
        self._check(messages + [convert_to_openai_tool(tool) for tool in request.tools], budget)

    def _check(self, items: list, budget: int):
        self.result.context_tokens = SessionStore.estimate_tokens(items)
        if self.result.context_tokens > budget:
            raise ProviderError(f"Диалог превысил лимит контекста ({budget:,} токенов). Начните новый диалог."
                                .replace(",", " "), "context_limit")

    def add_usage(self, raw: dict | None):
        self.result.usage = add_usage(self.result.usage, request_usage(raw, self.model_info))
