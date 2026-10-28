"""Агент-проверяющий на двойнике RouterAI: без сети и платных запросов."""
import asyncio
import json

import httpx2
import pytest

from materials_chat import verifier
from materials_chat.openrouter import ProviderError
from materials_chat.sessions import SessionStore
from materials_chat.verifier import Claim, successful_data, summarize, verifier_model_name, verify_answer
from test_agent_backend import PRICES, Provider, event, settings, sse, text_reply, verdict_reply

META = {"resource_id": "materials", "resource_title": "Справочник по материалам", "resource_path": "Мой Фонд/…",
        "indexed_at": "2026-10-18T09:59:24+00:00", "served_from": "local_index", "last_sync_error": None}
TRACE = [{"id": "t1", "name": "find_materials", "is_error": False,
          "arguments": {"conditions": [{"element": "Cr", "op": ">", "value": 20}]},
          "result": {"ok": True, "meta": META, "data": {"total": 1, "materials": [
              {"node_id": "200", "name": "Х25", "contents": {"Cr": {"low": 23.0, "high": 27.0, "text": "23.0–27.0 %"}}}]}}},
         {"id": "t2", "name": "get_subgraph", "arguments": {}, "is_error": True,
          "result": {"ok": False, "error": {"code": "NODE_NOT_FOUND", "message": "нет вершины"}}}]
CLAIMS = [{"text": "У Х25 хрома 23–27 %", "quote": "хрома 23–27 %", "status": "confirmed", "node_id": "200",
           "evidence": "23.0–27.0 %"},
          {"text": "Х25 — нержавеющая сталь", "quote": "нержавеющая сталь Х25", "status": "not_in_data",
           "node_id": None, "evidence": ""},
          {"text": "Хрома больше 20 % у одной марки", "quote": "только у одной марки", "status": "confirmed",
           "node_id": None, "evidence": "total: 1"}]


def verify(provider, config, traces=TRACE, **kwargs):
    return asyncio.run(verify_answer(config, "Где хрома больше 20 %?", "Ответ про Х25.", traces,
                                     model=provider.model(config), **kwargs))


def streamed_verdict(claims, pieces=9):
    """Отчёт submit_verdict, аргументы которого приходят кусками, как в настоящем потоке."""
    text = json.dumps({"claims": claims}, ensure_ascii=False)
    size = -(-len(text) // pieces)
    parts = [text[start:start + size] for start in range(0, len(text), size)]
    first = {"index": 0, "id": "verdict", "type": "function",
             "function": {"name": "submit_verdict", "arguments": parts[0]}}
    return ([event({"role": "assistant", "tool_calls": [first]})]
            + [event({"tool_calls": [{"index": 0, "function": {"arguments": part}}]}) for part in parts[1:]]
            + [event({}, "tool_calls")])


class TrickleProvider(Provider):
    """Двойник, который отдаёт поток по одному событию и считает, сколько уже отправлено."""

    sent = 0

    def respond(self, number, events):
        blocks = [block + "\n\n" for block in sse(events).split("\n\n") if block]

        async def body():
            for block in blocks:
                self.sent += 1
                yield block.encode()

        self.total = len(blocks)
        return httpx2.Response(200, content=body(), headers={"Content-Type": "text/event-stream"})


def test_claims_arrive_one_by_one_while_the_report_streams():
    provider, seen = TrickleProvider(streamed_verdict(CLAIMS)), []
    report = verify(provider, settings(), on_claim=lambda index, claim: seen.append((index, claim, provider.sent)))
    assert [(index, claim) for index, claim, _ in seen] == list(enumerate(CLAIMS))
    # Первое утверждение показано, пока отчёт ещё приходит, а не после него.
    assert seen[0][2] < provider.total
    assert report["claims"] == CLAIMS


def test_claim_without_quote_is_kept():
    claim = {key: value for key, value in CLAIMS[0].items() if key != "quote"}
    report = verify(Provider(verdict_reply([claim])), settings())
    assert report["claims"] == [{**claim, "quote": ""}]
    assert "quote" in verifier.VERIFIER_PROMPT and "дословн" in verifier.VERIFIER_PROMPT


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


def test_verifier_sees_where_the_data_came_from():
    provider = Provider(verdict_reply(CLAIMS))
    verify(provider, settings())
    sent = json.dumps(provider.requests[0]["messages"], ensure_ascii=False)
    # Ответ обязан назвать ресурс и дату снимка: без meta проверяющий счёл бы это «нет в данных».
    assert "Справочник по материалам" in sent and "2026-10-18T09:59:24+00:00" in sent
    assert "Поле source" in provider.requests[0]["messages"][0]["content"]
    fallback = {"id": "s", "name": "search_graph", "arguments": {"query": "Х25"}, "is_error": False,
                "result": {"ok": True, "data": {"hits": []}, "meta": {
                    "served_from": "local_index_fallback", "indexed_at": "2026-10-18T09:59:24+00:00",
                    "live_error": {"code": "timeout", "message": "нет ответа"}, "search_type": "lexical_prefix",
                    "sources": [{"resource_id": "materials", "indexed_at": "2026-10-18T09:59:24+00:00"}]}}}
    first, second = successful_data([TRACE[0], fallback])
    assert first["source"] == {"resource_id": "materials", "resource_title": "Справочник по материалам",
                               "served_from": "local_index", "indexed_at": "2026-10-18T09:59:24+00:00"}
    assert second["source"] == {key: fallback["result"]["meta"][key]
                                for key in ("served_from", "indexed_at", "live_error", "sources")}


def test_report_cut_by_the_length_limit_is_an_error_not_a_verdict():
    claims = [CLAIMS[0], {"text": "У Х25 никеля 5 %", "status": "contradicted", "node_id": "200", "evidence": "нет Ni"}]
    full = json.dumps({"claims": claims}, ensure_ascii=False)
    cut = full[:full.index(', {"text": "У Х25 никеля')]  # второе утверждение не успело прийти
    call = {"index": 0, "id": "verdict", "type": "function", "function": {"name": "submit_verdict", "arguments": cut}}
    provider = Provider([event({"role": "assistant", "tool_calls": [call]}, "length")])
    with pytest.raises(ProviderError) as error:
        verify(provider, settings())
    assert error.value.code == "verifier_truncated" and "лимит" in str(error.value)


def test_verifier_has_its_own_output_cap(monkeypatch):
    provider = Provider(verdict_reply(CLAIMS))
    model = provider.model(settings())
    created = []

    def create(config, **overrides):
        created.append(overrides)
        return model

    monkeypatch.setattr(verifier, "create_chat_model", create)
    asyncio.run(verify_answer(settings(verifier_model="other/model"), "Вопрос?", "Ответ.", TRACE))
    # Отчёт по ответу на 20 марок длиннее обычного ответа чата: ~150 токенов на утверждение.
    assert created == [{"model": "other/model", "max_tokens": 16384}]
    assert "80 символов" in verifier.VERIFIER_PROMPT  # короткие выдержки — отчёт короче


def test_window_keeps_room_for_the_report():
    provider = Provider(verdict_reply(CLAIMS))
    tokens = SessionStore.estimate_tokens(verifier.verifier_messages("Где хрома больше 20 %?", "Ответ про Х25.", TRACE))
    # Ответ чата (1800 токенов) поместился бы, отчёт проверяющего (16 384) — нет.
    with pytest.raises(ProviderError) as error:
        verify(provider, settings(), model_info={"context_window": tokens + 1800 + 100})
    assert error.value.code == "context_limit" and not provider.requests


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
