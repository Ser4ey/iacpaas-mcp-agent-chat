"""Агент-проверяющий: отдельный вызов модели без инструментов сверяет ответ чата с данными, которые вернули инструменты."""
import json
import re
from datetime import datetime, timezone
from typing import Literal

from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, Field, ValidationError

from .agent import context_budget
from .openrouter import ProviderError, as_provider_error, create_chat_model
from .prompts import VERIFIER_PROMPT
from .sessions import SessionStore
from .usage import request_usage


class Claim(BaseModel):
    text: str = Field(min_length=1)
    quote: str = ""  # дословный фрагмент ответа: по нему интерфейс находит место утверждения в тексте
    status: Literal["confirmed", "not_in_data", "contradicted"]
    node_id: str | None = None
    evidence: str = ""


class VerdictReport(BaseModel):
    """Утверждения ответа с вердиктом по каждому."""
    claims: list[Claim]


REPORT_TOOL = convert_to_openai_tool(VerdictReport)
REPORT_TOOL["function"]["name"] = "submit_verdict"
BAD_OUTPUT = "Проверяющий вернул ответ в неожиданном формате. Повторите проверку."
# Отчёт длиннее ответа чата: ~150 токенов на утверждение, у ответа о 20 марках их десятки.
VERIFIER_MAX_TOKENS = 16384
SOURCE_KEYS = ("resource_id", "resource_title", "served_from", "indexed_at", "fetched_at", "live_error", "sources")


def verifier_model_name(settings) -> str:
    """Модель проверки; если не задана отдельно — та же, что у агента."""
    return getattr(settings, "verifier_model", "") or settings.openrouter_model


def summarize(claims: list[Claim]) -> dict:
    """Доля подтверждённых утверждений и словесный вердикт по точной доле, а не по округлённому проценту."""
    confirmed = sum(claim.status == "confirmed" for claim in claims)
    total = len(claims)
    if not total:
        return {"confirmed": 0, "total": 0, "percent": None, "verdict": "нет утверждений о данных"}
    verdict = ("подтверждён" if confirmed == total
               else "частично подтверждён" if confirmed * 10 >= total * 7 else "не подтверждён")
    return {"confirmed": confirmed, "total": total, "percent": round(100 * confirmed / total), "verdict": verdict}


def successful_data(traces: list[dict]) -> list[dict]:
    """Только удачные вызовы: ошибки инструментов данными из базы не являются.

    source — откуда данные (ресурс, дата снимка, живое чтение): ответ обязан назвать источник.
    """
    records = []
    for trace in traces:
        result = trace.get("result")
        if trace.get("is_error") or not isinstance(result, dict) or result.get("ok") is False:
            continue
        meta = result.get("meta") or {}
        records.append({"tool": trace.get("name"), "arguments": trace.get("arguments"), "data": result.get("data"),
                        "source": {key: meta[key] for key in SOURCE_KEYS if meta.get(key) is not None}})
    return records


def verifier_messages(question: str, answer: str, traces: list[dict]) -> list[dict]:
    data = json.dumps(successful_data(traces), ensure_ascii=False)
    user = f"Вопрос пользователя:\n{question}\n\nОтвет ассистента:\n{answer}\n\nДанные из базы (JSON):\n{data}"
    return [{"role": "system", "content": VERIFIER_PROMPT}, {"role": "user", "content": user}]


def parse_report(message) -> VerdictReport:
    """Отчёт из вызова submit_verdict; если модель ответила текстом — из JSON в тексте (возможно, в ```)."""
    try:
        if message.tool_calls:
            return VerdictReport.model_validate(message.tool_calls[0]["args"])
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(message.text).strip())
        return VerdictReport.model_validate_json(text)
    except (ValidationError, ValueError, TypeError, KeyError):
        raise ProviderError(BAD_OUTPUT, "verifier_bad_output") from None


def announce_claims(message, shown: int, on_claim) -> int:
    """Сообщить о дописанных утверждениях, пока отчёт ещё приходит; возвращает, сколько уже сообщено.

    Последнее утверждение в частично разобранном отчёте может быть недописано, поэтому оно ждёт следующего.
    """
    args = message.tool_calls[0]["args"] if message.tool_calls else {}
    claims = args.get("claims") if isinstance(args, dict) else None
    if not isinstance(claims, list):
        return shown
    for index in range(shown, len(claims) - 1):
        try:
            claim = Claim.model_validate(claims[index])
        except ValidationError:
            break
        on_claim(index, claim.model_dump())
        shown = index + 1
    return shown


async def verify_answer(settings, question: str, answer: str, traces: list[dict], model=None,
                        model_info: dict | None = None, on_claim=None) -> dict:
    """Проверить ответ по данным диалога; ошибки — ProviderError с кодом для HTTP-слоя.

    on_claim(index, claim) вызывается для каждого утверждения по мере того, как модель его дописывает.
    """
    if not successful_data(traces):
        raise ProviderError("В диалоге нет данных из базы — проверять ответ не с чем.", "no_data")
    messages = verifier_messages(question, answer, traces)
    # Под отчёт проверяющего в окне оставлено больше места, чем под ответ чата.
    reserve = max(0, VERIFIER_MAX_TOKENS - getattr(settings, "max_output_tokens", 4096))
    if SessionStore.estimate_tokens(messages) > context_budget(settings, model_info) - reserve:
        raise ProviderError("Данные диалога не помещаются в окно контекста проверяющего.", "context_limit")
    name = verifier_model_name(settings)
    model = model or create_chat_model(settings, model=name, max_tokens=VERIFIER_MAX_TOKENS)
    message, shown = None, 0
    try:
        # Стриминг нужен, чтобы RouterAIChat сохранил стоимость провайдера в provider_usage,
        # и чтобы интерфейс видел утверждения по одному, а не весь отчёт в конце.
        async for chunk in model.bind_tools([REPORT_TOOL], tool_choice="required").astream(messages):
            message = chunk if message is None else message + chunk
            if on_claim:
                shown = announce_claims(message, shown, on_claim)
    except Exception as error:
        problem = as_provider_error(error, getattr(settings, "provider_name", "API модели"))
        raise problem or error
    if message is None:
        raise ProviderError(BAD_OUTPUT, "verifier_bad_output")
    if message.response_metadata.get("finish_reason") == "length":
        # Обрезанные аргументы LangChain дочитывает как неполный JSON: часть утверждений пропала бы молча.
        raise ProviderError("Проверяющий не уложился в лимит длины ответа. Проверка не выполнена — "
                            "повторите её или задайте вопрос уже.", "verifier_truncated")
    report = parse_report(message)
    if on_claim:
        for index, claim in enumerate(report.claims[shown:], start=shown):
            on_claim(index, claim.model_dump())
    return {**summarize(report.claims), "claims": [claim.model_dump() for claim in report.claims],
            "usage": request_usage(message.response_metadata.get("provider_usage"), model_info), "model": name,
            "created_at": datetime.now(timezone.utc).isoformat()}
