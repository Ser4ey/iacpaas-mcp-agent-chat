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
    status: Literal["confirmed", "not_in_data", "contradicted"]
    node_id: str | None = None
    evidence: str = ""


class VerdictReport(BaseModel):
    """Утверждения ответа с вердиктом по каждому."""
    claims: list[Claim]


REPORT_TOOL = convert_to_openai_tool(VerdictReport)
REPORT_TOOL["function"]["name"] = "submit_verdict"
BAD_OUTPUT = "Проверяющий вернул ответ в неожиданном формате. Повторите проверку."


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
    """Только удачные вызовы: ошибки инструментов данными из базы не являются."""
    records = []
    for trace in traces:
        result = trace.get("result")
        if trace.get("is_error") or not isinstance(result, dict) or result.get("ok") is False:
            continue
        records.append({"tool": trace.get("name"), "arguments": trace.get("arguments"), "data": result.get("data"),
                        "served_from": (result.get("meta") or {}).get("served_from")})
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


async def verify_answer(settings, question: str, answer: str, traces: list[dict], model=None,
                        model_info: dict | None = None) -> dict:
    """Проверить ответ по данным диалога; ошибки — ProviderError с кодом для HTTP-слоя."""
    if not successful_data(traces):
        raise ProviderError("В диалоге нет данных из базы — проверять ответ не с чем.", "no_data")
    messages = verifier_messages(question, answer, traces)
    if SessionStore.estimate_tokens(messages) > context_budget(settings, model_info):
        raise ProviderError("Данные диалога не помещаются в окно контекста проверяющего.", "context_limit")
    name = verifier_model_name(settings)
    model = model or create_chat_model(settings, model=name)
    try:
        # Стриминг нужен, чтобы RouterAIChat сохранил стоимость провайдера в provider_usage.
        chunks = [chunk async for chunk in model.bind_tools([REPORT_TOOL], tool_choice="required").astream(messages)]
    except Exception as error:
        problem = as_provider_error(error, getattr(settings, "provider_name", "API модели"))
        raise problem or error
    if not chunks:
        raise ProviderError(BAD_OUTPUT, "verifier_bad_output")
    message = chunks[0]
    for chunk in chunks[1:]:
        message += chunk
    report = parse_report(message)
    return {**summarize(report.claims), "claims": [claim.model_dump() for claim in report.claims],
            "usage": request_usage(message.response_metadata.get("provider_usage"), model_info), "model": name,
            "created_at": datetime.now(timezone.utc).isoformat()}
