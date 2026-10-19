"""A deliberately small agent: model -> real MCP tools -> grounded response."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field

from .openrouter import OpenRouterClient, ProviderError
from .prompts import SYSTEM_PROMPT
from .sessions import SessionStore, add_usage, empty_usage


@dataclass
class TurnResult:
    content: str = ""
    trace: list = field(default_factory=list)
    usage: dict = field(default_factory=empty_usage)
    model_messages: list = field(default_factory=list)
    context_tokens: int = 0
    interrupted: bool = False


def has_evidence(result: dict) -> bool:
    """Empty data/error envelopes must not unlock an unsupported model answer."""
    if not result.get("ok", False):
        return False
    data = result.get("data")
    if not data:
        return False
    if isinstance(data, dict):
        # A search may be nonempty structurally while containing zero hits.
        for key in ("results", "hits", "matches", "nodes", "items"):
            if key in data and isinstance(data[key], list):
                return bool(data[key])
        if data.get("total") == 0:
            return False
    return True


def metadata_summary(trace: list) -> str:
    """Scope descriptions can be rendered exactly, without model extrapolation."""
    lines = []
    for item in trace:
        if not item["result"].get("ok"):
            continue
        data = item["result"].get("data", {})
        if item["name"] == "list_inforesources":
            for resource in data.get("resources", [])[:20]:
                state = "проиндексирован" if resource.get("indexed") else "ещё не загружен"
                lines.append(f"- {resource.get('title', resource.get('id', 'Ресурс'))} ({resource.get('id', '')}): {state}.")
        elif item["name"] == "get_inforesource":
            title = data.get("title", data.get("id", "Ресурс"))
            metadata = data.get("metadata", {})
            lines.append(f"- {title}: {data.get('nodes', '?')} вершин, {data.get('edges', '?')} дуг.")
            if metadata.get("ontology"):
                lines.append(f"  Онтология: {metadata['ontology']}.")
    if not lines:
        return ""
    return ("Сведения о доступных источниках IACPaaS:\n" + "\n".join(dict.fromkeys(lines)) +
            "\n\nЭто описание ресурсов. Для ответа о свойствах материала нужно найти соответствующие записи в графе.")


class Agent:
    def __init__(self, settings, bridge_factory=None, provider=None):
        self.settings = settings
        if bridge_factory is None:
            from .mcp_client import MCPBridge
            bridge_factory = MCPBridge
        self.bridge_factory = bridge_factory
        self.provider = provider or OpenRouterClient(settings)
        self.result = TurnResult()

    async def run(self, text: str, history: list[dict]):
        self.result = result = TurnResult()
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history,
                    {"role": "user", "content": text}]
        start_index = len(messages)
        max_context = getattr(self.settings, "max_context_tokens", 24000)
        model_context = getattr(self.settings, "model_context_tokens", 32768)
        limit = min(max_context, model_context - getattr(self.settings, "max_output_tokens", 1800))
        result.context_tokens = SessionStore.estimate_tokens(messages)
        if result.context_tokens > limit:
            raise ProviderError("Контекст диалога достиг лимита. Начните новый диалог.", "context_limit")
        good, failures, calls_made = 0, 0, 0
        retrieval_attempted, retrieval_good = False, False
        incomplete_scope = False
        answer_evidence = False
        max_calls = getattr(self.settings, "max_tool_calls", 12)
        try:
            yield "status", {"message": "Подключаю инструменты MCP…"}
            async with self.bridge_factory(self.settings) as bridge:
                tools = await bridge.list_tools()
                if not tools:
                    raise ProviderError("MCP-сервер не предоставил инструменты.", "mcp_unavailable")
                for step in range(getattr(self.settings, "max_tool_steps", 6)):
                    result.context_tokens = SessionStore.estimate_tokens(messages + tools)
                    if result.context_tokens > limit:
                        raise ProviderError("Данные превысили лимит контекста. Уточните запрос или начните новый диалог.", "context_limit")
                    yield "status", {"message": "Модель выбирает инструменты…" if not good else "Обрабатываю найденные данные…"}
                    assistant = {"role": "assistant", "content": ""}
                    collected, reasoning = {}, []
                    usage = None
                    completed = False
                    finish_reason = None
                    can_answer = answer_evidence and (not retrieval_attempted or retrieval_good)
                    try:
                        async for item in self.provider.stream(messages, tools, "required" if step == 0 else "auto"):
                            if "usage" in item:
                                usage = item["usage"]
                            delta = item.get("delta", {})
                            if item.get("finish_reason"):
                                finish_reason = item["finish_reason"]
                            if delta.get("reasoning_details"):
                                reasoning.extend(delta["reasoning_details"])
                            if delta.get("content"):
                                assistant["content"] += delta["content"]
                                # First response must contain a real tool call.
                                if can_answer:
                                    result.content += delta["content"]
                                    yield "answer_delta", {"text": delta["content"]}
                            for call in delta.get("tool_calls", []):
                                target = collected.setdefault(call.get("index", 0),
                                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                                if call.get("id"):
                                    target["id"] = call["id"]
                                function = call.get("function", {})
                                target["function"]["name"] += function.get("name", "")
                                target["function"]["arguments"] += function.get("arguments", "")
                        if finish_reason is None:
                            raise ProviderError("Провайдер завершил поток до подтверждения ответа.", "provider_incomplete")
                        completed = True
                    finally:
                        known = usage is not None and all(usage.get(key) is not None for key in
                                                         ("prompt_tokens", "completion_tokens", "total_tokens"))
                        current = {"input_tokens": int((usage or {}).get("prompt_tokens") or 0),
                                   "output_tokens": int((usage or {}).get("completion_tokens") or 0),
                                   "total_tokens": int((usage or {}).get("total_tokens") or 0),
                                   "known": known, "requests": 1, "unknown_requests": 0 if known else 1}
                        result.usage = add_usage(result.usage, current)
                    if reasoning:
                        assistant["reasoning_details"] = reasoning
                    if collected:
                        if len(collected) > max_calls:
                            raise ProviderError("Модель запросила слишком много инструментов одновременно. Уточните запрос.", "tool_limit")
                        if not can_answer:
                            # Do not carry an unsupported preamble into future turns.
                            assistant["content"] = ""
                        assistant["tool_calls"] = list(collected.values())
                        # Append each tool group atomically after all responses exist.
                        group = [assistant]
                        for call in assistant["tool_calls"]:
                            name = call["function"]["name"]
                            call_id = call["id"] or uuid.uuid4().hex
                            call["id"] = call_id
                            try:
                                arguments = json.loads(call["function"]["arguments"] or "{}")
                                if not isinstance(arguments, dict):
                                    raise ValueError()
                            except (ValueError, TypeError):
                                arguments = {}
                                output = {"ok": False, "error": {"code": "invalid_arguments", "message": "Некорректные аргументы инструмента."}}
                            else:
                                output = None
                            yield "tool_start", {"id": call_id, "name": name, "arguments": arguments}
                            started = time.monotonic()
                            if calls_made >= max_calls:
                                output = {"ok": False, "error": {"code": "tool_limit", "message": "Достигнут лимит вызовов."}}
                            elif output is None:
                                calls_made += 1
                                try:
                                    output = await bridge.call_tool(name, arguments)
                                except asyncio.CancelledError:
                                    result.trace.append({"id": call_id, "name": name, "arguments": arguments,
                                        "result": {"ok": False, "error": {"code": "cancelled", "message": "Вызов остановлен пользователем."}},
                                        "duration_ms": round((time.monotonic() - started) * 1000), "is_error": True})
                                    raise
                                except Exception:
                                    output = {"ok": False, "error": {"code": "mcp_error", "message": "Не удалось выполнить инструмент MCP."}}
                            useful = has_evidence(output)
                            if isinstance(output.get("data"), dict) and output["data"].get("coverage_complete") is False:
                                incomplete_scope = True
                            if name in ("search_graph", "get_subgraph"):
                                retrieval_attempted = True
                                retrieval_good = retrieval_good or useful
                            if useful:
                                good += 1
                                if name not in ("list_inforesources", "get_inforesource"):
                                    answer_evidence = True
                            if not output.get("ok"):
                                failures += 1
                            trace = {"id": call_id, "name": name, "arguments": arguments,
                                     "result": output, "duration_ms": round((time.monotonic() - started) * 1000),
                                     "is_error": not output.get("ok", False)}
                            result.trace.append(trace)
                            yield "tool_end", trace
                            group.append({"role": "tool", "tool_call_id": call_id,
                                          "content": json.dumps(output, ensure_ascii=False)})
                        messages.extend(group)
                        result.model_messages = messages[start_index:]
                        if calls_made >= max_calls:
                            break
                        continue
                    if can_answer and completed and assistant["content"].strip():
                        messages.append(assistant)
                        result.model_messages = messages[start_index:]
                        return
                    # A model may ignore tool_choice or stop after an empty search.
                    # Never expose its unsupported factual answer in that case.
                    break
                fallback = ("Не удалось надёжно проверить сведения: один или несколько инструментов завершились ошибкой. "
                            "Проверьте подключение и доступность индекса." if failures else
                            "В доступных ресурсах информация не найдена либо её недостаточно для ответа. Уточните название материала или характеристику.")
                if answer_evidence and (not retrieval_attempted or retrieval_good):
                    fallback = "Достигнут лимит поиска. Найденные фрагменты показаны в журнале; уточните запрос, чтобы получить ответ по более узкой выборке."
                elif incomplete_scope:
                    fallback = "В проиндексированной части информация не найдена. Некоторые ресурсы недоступны или ещё не загружены; вывод об отсутствии данных во всей базе сделать нельзя. Проверьте статус индекса."
                elif good and not retrieval_attempted and not failures:
                    fallback = metadata_summary(result.trace) or fallback
                result.content += ("\n\n" if result.content else "") + fallback
                yield "answer_delta", {"text": ("\n\n" if result.content != fallback else "") + fallback}
                result.model_messages.append({"role": "assistant", "content": fallback})
        except asyncio.CancelledError:
            result.interrupted = True
            raise
