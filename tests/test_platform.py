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
