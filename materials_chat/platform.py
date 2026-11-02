"""HTTP-клиент IACPaaS: вход, чтение (экспорт) и запись (папка, импорт, удаление)."""
import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from .config import Settings

log = logging.getLogger(__name__)


class PlatformError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


class PlatformClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._token = settings.iacpaas_access_token
        self._login_lock = asyncio.Lock()

    async def _login(self) -> None:
        if not self.settings.iacpaas_username or not self.settings.iacpaas_password:
            raise PlatformError("AUTH_REQUIRED", "Укажите логин и пароль IACPaaS либо действующий API-ключ/JWT в .env.")
        try:
            async with httpx.AsyncClient(timeout=self.settings.api_timeout_seconds) as client:
                response = await client.post(self.settings.iacpaas_base_url.rstrip("/") + "/api/signin", json={
                    "username": self.settings.iacpaas_username, "password": self.settings.iacpaas_password})
            if response.status_code in (401, 403):
                raise PlatformError("AUTH_FAILED", "IACPaaS отклонила логин или пароль.")
            response.raise_for_status()
            token = response.json().get("accessToken")
            if not isinstance(token, str) or not token:
                raise PlatformError("AUTH_FAILED", "IACPaaS не вернула токен доступа.")
            self._token = token  # JWT остаётся только в памяти процесса.
        except PlatformError:
            raise
        except (httpx.HTTPError, ValueError):
            raise PlatformError("PLATFORM_UNAVAILABLE", "Не удалось войти в IACPaaS. Проверьте сеть и доступность платформы.", True) from None

    async def export(self, resource_path: str, depth: int | None = None, root_path: str | None = None) -> dict[str, Any]:
        """Экспортируем universal JSON. В реальном API граф лежит в строке data."""
        if not self.settings.iacpaas_api_key and not self._token:
            async with self._login_lock:
                if not self._token:
                    await self._login()
        params = {"path": resource_path, "json-type": "universal", "compress": "false", "no-blob-data": "true"}
        if depth is not None:
            params["export-depth"] = str(depth)
        if root_path:
            params["start-target-concept-path"] = root_path

        for attempt in range(2):
            headers = {"Accept": "application/json"}
            if self.settings.iacpaas_api_key:
                headers["X-API-KEY"] = self.settings.iacpaas_api_key
            else:
                headers["Authorization"] = "Bearer " + self._token
            try:
                async with httpx.AsyncClient(timeout=self.settings.api_timeout_seconds) as client:
                    async with client.stream("GET", self.settings.iacpaas_base_url.rstrip("/") + "/api/data/export/user-item", params=params, headers=headers) as response:
                        if response.status_code == 401 and attempt == 0 and not self.settings.iacpaas_api_key:
                            async with self._login_lock:
                                await self._login()
                            continue
                        if response.status_code in (401, 403):
                            raise PlatformError("ACCESS_DENIED", "Нет доступа к ресурсу либо истёк токен IACPaaS.")
                        if response.status_code == 404:
                            raise PlatformError("RESOURCE_NOT_FOUND", "Ресурс не найден на платформе.")
                        response.raise_for_status()
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw) > self.settings.max_export_bytes:
                                raise PlatformError("EXPORT_TOO_LARGE", "Экспорт превышает лимит загрузки. Ограничьте область ресурсом меньшего размера.")
                body = json.loads(raw)
                if isinstance(body, dict) and body.get("success") is False:
                    # Технические сообщения платформы могут содержать пользовательские пути,
                    # поэтому в ответ инструмента не попадают, а пишутся только в лог сервера.
                    log.warning("IACPaaS отклонила экспорт %s: %s", resource_path, body.get("explanationSafe") or body.get("error"))
                    raise PlatformError("PLATFORM_REJECTED", "Платформа отклонила экспорт: проверьте путь и права на ресурс.")
                graph = body.get("data", body) if isinstance(body, dict) else body
                if isinstance(graph, str):
                    graph = json.loads(graph)
                if not isinstance(graph, dict) or graph.get("json_type") != "universal" or "type" not in graph:
                    raise PlatformError("INVALID_PLATFORM_RESPONSE", "Платформа вернула неожиданный формат графа.")
                return graph
            except PlatformError:
                raise
            except httpx.TimeoutException:
                raise PlatformError("PLATFORM_TIMEOUT", "IACPaaS не ответила вовремя. Повторите синхронизацию позже.", True) from None
            except (httpx.HTTPError, ValueError):
                raise PlatformError("PLATFORM_UNAVAILABLE", "Не удалось прочитать IACPaaS. Проверьте сеть и состояние платформы.", True) from None
        raise PlatformError("AUTH_FAILED", "Не удалось обновить доступ IACPaaS.")


    # ---------- Запись ----------

    async def _authorized(self) -> dict:
        if not self.settings.iacpaas_api_key and not self._token:
            async with self._login_lock:
                if not self._token:
                    await self._login()
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.settings.iacpaas_api_key:
            headers["X-API-KEY"] = self.settings.iacpaas_api_key
        else:
            headers["Authorization"] = "Bearer " + self._token
        return headers

    async def _send(self, method: str, url_path: str, body: dict) -> dict:
        """Записывающий запрос; ответ платформы — Result или CodeResult ({success, code, explanationSafe…})."""
        for attempt in range(2):
            headers = await self._authorized()
            try:
                async with httpx.AsyncClient(timeout=self.settings.api_timeout_seconds) as client:
                    response = await client.request(method, self.settings.iacpaas_base_url.rstrip("/") + url_path,
                                                    json=body, headers=headers)
            except httpx.TimeoutException:
                raise PlatformError("PLATFORM_TIMEOUT", "IACPaaS не ответила вовремя; запись могла не выполниться — "
                                    "перечитайте ресурс перед повтором.", True) from None
            except httpx.HTTPError:
                raise PlatformError("PLATFORM_UNAVAILABLE", "Не удалось обратиться к IACPaaS. Проверьте сеть.", True) from None
            if response.status_code == 401 and attempt == 0 and not self.settings.iacpaas_api_key:
                async with self._login_lock:
                    await self._login()
                continue
            if response.status_code in (401, 403):
                raise PlatformError("ACCESS_DENIED", "Нет прав на запись в этот ресурс либо истёк токен IACPaaS.")
            try:
                response.raise_for_status()
                result = response.json()
            except (httpx.HTTPError, ValueError):
                raise PlatformError("PLATFORM_UNAVAILABLE", f"IACPaaS ответила ошибкой {response.status_code}.", True) from None
            if not isinstance(result, dict):
                raise PlatformError("INVALID_PLATFORM_RESPONSE", "Платформа вернула неожиданный ответ.")
            if result.get("platformUnavailable"):
                raise PlatformError("PLATFORM_UNAVAILABLE", "Платформа сообщила, что временно недоступна.", True)
            if not result.get("success"):
                raise PlatformError("PLATFORM_REJECTED", "Платформа отклонила запрос: " + safe_explanation(result))
            return {key: result[key] for key in ("success", "code", "id") if result.get(key) not in (None, "")}
        raise PlatformError("AUTH_FAILED", "Не удалось обновить доступ IACPaaS.")

    async def create_folder(self, parent: str, name: str, description: str = "") -> dict:
        """parent — путь родительской папки в «Моём фонде» («» — корень)."""
        return await self._send("POST", "/api/storage-unit/folder/create",
                                {"path": parent, "name": name, "description": description})

    async def import_resource(self, folder: str, graph: dict, clear: bool) -> dict:
        """folder — папка-контейнер; имя ресурса — title графа. clear — очистить существующий ресурс перед импортом."""
        return await self._send("POST", "/api/data/import", {
            "path": folder, "json": json.dumps(graph, ensure_ascii=False), "clearIfExists": clear})

    async def delete_resource(self, path: str, mode: str) -> dict:
        """mode: trashIR — в корзину, deleteIR — совсем."""
        return await self._send("DELETE", "/api/data/delete", {"path": path, "mode": mode})


def safe_explanation(result: dict) -> str:
    """Пояснение платформы без имён Java-исключений и не длиннее 400 символов."""
    text = str(result.get("explanationSafe") or result.get("explanation") or result.get("error") or "причина не указана")
    text = re.sub(r"(?:[a-z_][\w$]*\.)+[A-Z]\w*(?:Exception|Error):\s*", "", text).strip()
    return text[:400] + ("…" if len(text) > 400 else "")


def concept_path(labels: list[str]) -> str | None:
    """Значение start-target-concept-path: имена от корня (без него) через «/».

    Имя, в котором есть «/», платформа ждёт в тройных кавычках: /…/‴кг/м³‴.
    """
    if not labels:
        return None
    return "/" + "/".join(f"‴{label}‴" if "/" in label else label for label in labels)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
