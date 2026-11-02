"""Контракт реального API: data-строка и обновление JWT при HTTP 401."""
import httpx
import pytest

from materials_chat.config import Settings
from materials_chat.platform import PlatformClient, PlatformError


@pytest.mark.asyncio
async def test_expired_jwt_reauthenticates_and_decodes_data(monkeypatch):
    actual_client = httpx.AsyncClient
    calls = []
    def handle(request):
        calls.append(request)
        if request.url.path == "/api/signin":
            return httpx.Response(200, json={"accessToken": "new-token"})
        if request.headers["Authorization"] == "Bearer expired-token":
            return httpx.Response(401)
        return httpx.Response(200, json={"filename": "test.json", "data": '{"json_type":"universal","title":"Test","type":"КОРЕНЬ","id":1}'})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: actual_client(transport=httpx.MockTransport(handle), **kwargs))
    settings = Settings(_env_file=None, iacpaas_access_token="expired-token", iacpaas_username="test", iacpaas_password="test")
    result = await PlatformClient(settings).export("Загрузки / Тест")
    assert result["title"] == "Test"
    assert [request.url.path for request in calls] == ["/api/data/export/user-item", "/api/signin", "/api/data/export/user-item"]


@pytest.mark.asyncio
async def test_denied_access_is_not_an_empty_resource(monkeypatch):
    actual_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: actual_client(transport=httpx.MockTransport(lambda _: httpx.Response(403)), **kwargs))
    settings = Settings(_env_file=None, iacpaas_api_key="test", iacpaas_access_token="")
    with pytest.raises(PlatformError) as result:
        await PlatformClient(settings).export("Тест")
    assert result.value.code == "ACCESS_DENIED"


def recording_platform(monkeypatch, reply):
    """PlatformClient с подменённым HTTP: запоминает запросы, отвечает reply(request)."""
    actual_client = httpx.AsyncClient
    calls = []

    def handle(request):
        calls.append(request)
        return reply(request)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: actual_client(transport=httpx.MockTransport(handle), **kwargs))
    settings = Settings(_env_file=None, iacpaas_api_key="secret-key", iacpaas_access_token="")
    return PlatformClient(settings), calls


@pytest.mark.asyncio
async def test_write_requests_have_documented_method_address_and_body(monkeypatch):
    import json
    client, calls = recording_platform(monkeypatch, lambda _: httpx.Response(200, json={"success": True, "code": "42"}))
    await client.create_folder("", "Демо агента", "проверка")
    graph = {"title": "Мои свойства", "json_type": "universal", "type": "КОРЕНЬ"}
    result = await client.import_resource("Демо агента", graph, clear=False)
    await client.import_resource("Демо агента", graph, clear=True)
    await client.delete_resource("Демо агента / Мои свойства", "trashIR")
    assert result == {"success": True, "code": "42"}
    assert [(call.method, call.url.path) for call in calls] == [
        ("POST", "/api/storage-unit/folder/create"), ("POST", "/api/data/import"),
        ("POST", "/api/data/import"), ("DELETE", "/api/data/delete")]
    bodies = [json.loads(call.content) for call in calls]
    assert bodies[0] == {"path": "", "name": "Демо агента", "description": "проверка"}
    assert bodies[1]["path"] == "Демо агента" and bodies[1]["clearIfExists"] is False
    assert json.loads(bodies[1]["json"]) == graph  # ресурс уходит строкой JSON
    assert bodies[2]["clearIfExists"] is True
    assert bodies[3] == {"path": "Демо агента / Мои свойства", "mode": "trashIR"}
    assert all(call.headers["X-API-KEY"] == "secret-key" for call in calls)


@pytest.mark.asyncio
async def test_platform_refusal_keeps_only_the_safe_explanation(monkeypatch):
    explanation = ("ru.dvo.iacp.is.iacpaas.storage.exceptions.StorageException: ru.dvo.iacp.is.iacpaas.storage."
                   "exceptions.StorageException: Под узлом `X´ не найдено метаотношение")
    client, _ = recording_platform(monkeypatch, lambda _: httpx.Response(200, json={
        "success": False, "code": "", "error": "Не удалось импортировать", "explanationSafe": explanation}))
    with pytest.raises(PlatformError) as result:
        await client.import_resource("Демо агента", {"title": "X"}, clear=False)
    assert result.value.code == "PLATFORM_REJECTED"
    assert result.value.message == "Платформа отклонила запрос: Под узлом `X´ не найдено метаотношение"


@pytest.mark.asyncio
@pytest.mark.parametrize("reply, code", [
    (lambda _: httpx.Response(403), "ACCESS_DENIED"),
    (lambda _: httpx.Response(200, json={"success": False, "platformUnavailable": True}), "PLATFORM_UNAVAILABLE"),
    (lambda _: httpx.Response(502), "PLATFORM_UNAVAILABLE"),
])
async def test_write_errors_map_to_codes(monkeypatch, reply, code):
    client, _ = recording_platform(monkeypatch, reply)
    with pytest.raises(PlatformError) as result:
        await client.delete_resource("Демо агента / X", "trashIR")
    assert result.value.code == code
