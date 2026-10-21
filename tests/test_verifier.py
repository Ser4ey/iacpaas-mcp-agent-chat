"""Агент-проверяющий на двойнике RouterAI: без сети и платных запросов."""
import asyncio
import json

import pytest

from materials_chat.openrouter import ProviderError
from materials_chat.verifier import Claim, summarize, verifier_model_name, verify_answer
from test_agent_backend import PRICES, Provider, settings, text_reply, verdict_reply

TRACE = [{"id": "t1", "name": "find_materials", "is_error": False,
          "arguments": {"conditions": [{"element": "Cr", "op": ">", "value": 20}]},
          "result": {"ok": True, "meta": {"served_from": "local_index"}, "data": {"total": 1, "materials": [
              {"node_id": "200", "name": "Х25", "contents": {"Cr": {"low": 23.0, "high": 27.0, "text": "23.0–27.0 %"}}}]}}},
         {"id": "t2", "name": "get_subgraph", "arguments": {}, "is_error": True,
          "result": {"ok": False, "error": {"code": "NODE_NOT_FOUND", "message": "нет вершины"}}}]
CLAIMS = [{"text": "У Х25 хрома 23–27 %", "status": "confirmed", "node_id": "200", "evidence": "23.0–27.0 %"},
          {"text": "Х25 — нержавеющая сталь", "status": "not_in_data", "node_id": None, "evidence": ""},
          {"text": "Хрома больше 20 % у одной марки", "status": "confirmed", "node_id": None, "evidence": "total: 1"}]


def verify(provider, config, traces=TRACE, **kwargs):
    return asyncio.run(verify_answer(config, "Где хрома больше 20 %?", "Ответ про Х25.", traces,
                                     model=provider.model(config), **kwargs))


def test_verdict_counts_claims_and_cost():
    provider = Provider(verdict_reply(CLAIMS))
    report = verify(provider, settings(), model_info=PRICES)
    assert (report["confirmed"], report["total"], report["percent"], report["verdict"]) == (2, 3, 67, "не подтверждён")
    assert report["claims"][0] == CLAIMS[0] and report["model"] == "local-model" and report["created_at"]
    assert report["usage"]["total_tokens"] == 20
    assert report["usage"]["cost"] == pytest.approx((12 * 5 + 8 * 10) / 1_000_000)
    body = provider.requests[0]
    assert body["tool_choice"] == "required"
    assert [tool["function"]["name"] for tool in body["tools"]] == ["submit_verdict"]
    sent = json.dumps(body["messages"], ensure_ascii=False)
    assert "Где хрома больше 20 %?" in sent and "Ответ про Х25." in sent and "23.0–27.0 %" in sent
    assert "NODE_NOT_FOUND" not in sent  # неудачные вызовы — не данные


def test_summary_thresholds():
    def claims(*statuses):
        return [Claim(text="утверждение", status=status) for status in statuses]
    assert summarize(claims("confirmed", "confirmed"))["verdict"] == "подтверждён"
    assert summarize(claims(*["confirmed"] * 7, *["contradicted"] * 3)) == \
        {"confirmed": 7, "total": 10, "percent": 70, "verdict": "частично подтверждён"}
    assert summarize([]) == {"confirmed": 0, "total": 0, "percent": None, "verdict": "нет утверждений о данных"}


def test_verifier_model_defaults_to_the_agent_model():
    assert verifier_model_name(settings()) == "local-model"
    assert verifier_model_name(settings(verifier_model="other/model")) == "other/model"


def test_plain_json_answer_is_accepted():
    provider = Provider(text_reply(json.dumps({"claims": CLAIMS[:1]}, ensure_ascii=False)))
    assert verify(provider, settings())["verdict"] == "подтверждён"


@pytest.mark.parametrize("case, code", [("no_data", "no_data"), ("garbage", "verifier_bad_output"),
                                        ("oversized", "context_limit")])
def test_clear_errors(case, code):
    provider = Provider(text_reply("Всё верно!"))
    config = settings(max_context_tokens=50) if case == "oversized" else settings()
    with pytest.raises(ProviderError) as error:
        verify(provider, config, traces=[TRACE[1]] if case == "no_data" else TRACE)
    assert error.value.code == code
    if case == "garbage":
        assert "Повторите проверку" in str(error.value)
    else:
        assert not provider.requests  # до модели дело не дошло
