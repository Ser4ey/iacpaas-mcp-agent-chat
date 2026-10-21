"""Local FastAPI chat API. SSE exposes tool execution, never hidden reasoning.

Контракт эндпоинтов и событий описан в docs/http-api.md.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import Agent, context_budget
from .openrouter import ProviderError, fetch_model_info
from .sessions import SessionStore

log = logging.getLogger(__name__)

UNKNOWN_MODEL = {"name": None, "context_window": None, "price_input_per_million": None,
                 "price_output_per_million": None, "price_cache_read_per_million": None,
                 "currency": None, "source": "unknown"}


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12000)


async def resolved(value):
    return await value if inspect.isawaitable(value) else value


def sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def leaf_exception(error: BaseException) -> BaseException:
    """anyio (внутри MCP SDK) заворачивает ошибки в ExceptionGroup; пользователю нужна исходная."""
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return error


def describe_error(error: BaseException) -> dict:
    if isinstance(error, ProviderError):
        return {"message": str(error), "code": error.code}
    log.error("Ошибка при ответе агента", exc_info=error)
    return {"message": f"Внутренняя ошибка агента ({type(error).__name__}). Подробности — в окне сервера.",
            "code": "agent_error"}


def create_app(settings=None) -> FastAPI:
    if settings is None:
        from .config import get_settings
        settings = get_settings()
    from .service import GraphService

    store = SessionStore(getattr(settings, "history_path", ".data/history.sqlite3"),
                         getattr(settings, "history_full_turns", 2), getattr(settings, "history_tool_chars", 1500))
    graph = GraphService(settings)
    locks: dict[str, asyncio.Lock] = {}
    refresh_task = None
    refresh_error = None
    refresh_result = None
    model_task = None
    overview_cache: dict = {}

    async def load_model_info():
        info = await fetch_model_info(settings)
        app.state.model_info = store.model_info = info
        return info

    async def current_model_info() -> dict:
        """Каталог модели грузится при старте; первый вопрос дождётся его не дольше 20 с."""
        if model_task and not model_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(model_task), 20)
            except (asyncio.TimeoutError, Exception):
                pass
        return app.state.model_info

    @asynccontextmanager
    async def lifespan(app):
        nonlocal model_task
        model_task = asyncio.create_task(load_model_info())
        if getattr(settings, "auto_sync_on_start", True):
            current_index = await resolved(graph.status())
            if not current_index.get("ready", False):
                await refresh_index()
        yield
        for task in (refresh_task, model_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        store.close()

    app = FastAPI(title="Материалы · IACPaaS", lifespan=lifespan)
    # These attributes make the small app easy to test without paid model calls.
    app.state.settings, app.state.store, app.state.graph = settings, store, graph
    app.state.agent_factory = Agent
    app.state.model_info = dict(UNKNOWN_MODEL)

    def existing(session_id):
        session = store.get(session_id)
        if session is None:
            raise HTTPException(404, "Диалог не найден.")
        return session

    def budget() -> int:
        return context_budget(settings, app.state.model_info)

    @app.get("/api/status")
    async def status():
        key_name, model_name = getattr(settings, "model_setting_names", ("OPENROUTER_API_KEY", "OPENROUTER_MODEL"))
        missing = [name for name, value in ((key_name, getattr(settings, "openrouter_api_key", "")),
                                            (model_name, getattr(settings, "openrouter_model", ""))) if not value]
        index = await resolved(graph.status())
        if not isinstance(index, dict):
            index = {"status": str(index)}
        index = {**index, "refresh_running": bool(refresh_task and not refresh_task.done()),
                 "refresh_error": refresh_error, "refresh_result": refresh_result}
        return {"configured": not missing, "model": getattr(settings, "openrouter_model", ""),
                "provider": getattr(settings, "provider_name", "API модели"),
                "model_info": app.state.model_info,
                "missing_settings": missing, "mcp_transport": getattr(settings, "mcp_transport", "http"),
                "resources": await resolved(graph.resources()), "index": index,
                "limits": {"context_budget": budget(),
                           "max_output_tokens": getattr(settings, "max_output_tokens", 4096),
                           "max_tool_steps": getattr(settings, "max_tool_steps", 8),
                           "max_tool_calls": getattr(settings, "max_tool_calls", 16)}}

    @app.get("/api/data/overview")
    async def data_overview():
        from .overview import build_overview
        notes_path = Path(settings.resources_path).parent / "data_notes.json"
        indexed = [graph.store.resource(item["id"]) for item in graph.resources()]
        # Описание пересобирается только после обновления индекса или заметок.
        key = (tuple(item and item["indexed_at"] for item in indexed),
               notes_path.stat().st_mtime if notes_path.exists() else None)
        if overview_cache.get("key") != key:
            notes = json.loads(notes_path.read_text(encoding="utf-8")) if notes_path.exists() else {}
            value = await asyncio.to_thread(build_overview, Path(settings.data_dir) / "search.sqlite3",
                                            graph.resources(), notes)
            overview_cache.update(key=key, value=value)
        return overview_cache["value"]

    @app.get("/api/sessions")
    async def sessions():
        return {"sessions": store.list()}

    @app.post("/api/sessions", status_code=201)
    async def create_session():
        return {**store.create(), "context_budget": budget()}

    @app.get("/api/sessions/{session_id}")
    async def session_detail(session_id: str):
        return {**existing(session_id), "context_budget": budget()}

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str):
        existing(session_id)
        if locks.get(session_id) and locks[session_id].locked():
            raise HTTPException(409, "Дождитесь завершения или остановите текущий ответ.")
        store.delete(session_id)
        locks.pop(session_id, None)
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/chat")
    async def chat(session_id: str, body: ChatRequest):
        session = existing(session_id)
        text = body.message.strip()
        if not text:
            raise HTTPException(422, "Сообщение не может быть пустым.")
        if not getattr(settings, "openrouter_api_key", "") or not getattr(settings, "openrouter_model", ""):
            names = getattr(settings, "model_setting_names", ("OPENROUTER_API_KEY", "OPENROUTER_MODEL"))
            raise HTTPException(503, f"Заполните {' и '.join(names)} в .env и перезапустите приложение.")
        lock = locks.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            raise HTTPException(409, "В этом диалоге уже формируется ответ.")
        model_info = await current_model_info()
        runner = app.state.agent_factory(settings, model_info=model_info)

        async def events():
            # Блокировка берётся внутри потока: если клиент отключится до его начала,
            # диалог не останется заблокированным навсегда.
            if lock.locked():
                yield sse("error", {"message": "В этом диалоге уже формируется ответ.", "code": "busy"})
                return
            async with lock:
                cancelled, error = False, None
                try:
                    title = text[:70] if session["title"] == "Новый диалог" else session["title"]
                    yield sse("session", {"id": session_id, "title": title})
                    async for event, payload in runner.run(text, store.history(session_id)):
                        yield sse(event, payload)
                except asyncio.CancelledError:
                    cancelled = True
                    runner.result.interrupted = True
                    raise
                except BaseExceptionGroup as group:
                    leaf = leaf_exception(group)
                    if isinstance(leaf, asyncio.CancelledError):
                        cancelled = True
                        runner.result.interrupted = True
                        raise
                    error = describe_error(leaf)
                except Exception as failure:
                    error = describe_error(failure)
                finally:
                    result = runner.result
                    problem = {"message": "Ответ остановлен пользователем.", "code": "cancelled"} if cancelled else error
                    content = result.content
                    if problem:
                        # Модель в следующем ходе должна знать, что этот ход прервался.
                        # В историю модели попадают только завершённые группы инструментов.
                        result.model_messages.append({"role": "assistant", "content":
                                                      content + ("\n\n" if content else "") + f"[{problem['message']}]"})
                    elif not content:
                        content = "Модель не вернула ответ. Попробуйте уточнить запрос."
                        result.model_messages.append({"role": "assistant", "content": content})
                    grounded = any(not item.get("is_error") for item in result.trace)
                    message_id = store.save_turn(session_id, text, content, result.trace, result.usage,
                                                 result.model_messages, grounded, problem)
                if error:
                    yield sse("error", error)
                updated = store.get(session_id)
                yield sse("usage", {"turn": runner.result.usage, "session": updated["usage"],
                                    "context_tokens": updated["context_tokens"], "context_budget": budget(),
                                    "context_estimated": True})
                yield sse("done", {"message_id": message_id})

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/index/refresh", status_code=202)
    async def refresh_index():
        nonlocal refresh_task, refresh_error, refresh_result
        if refresh_task and not refresh_task.done():
            return {"ok": True, "status": "running"}
        refresh_error = None
        refresh_result = None

        async def refresh():
            nonlocal refresh_error, refresh_result
            try:
                refresh_result = await resolved(graph.refresh_all())
                if refresh_result.get("status") == "partial_failure":
                    refresh_error = "Индекс обновлён частично. Список недоступных ресурсов показан в статусе индекса."
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Обновление индекса не удалось")
                refresh_error = "Не удалось обновить индекс. Проверьте учётные данные и подключение к IACPaaS."
        refresh_task = asyncio.create_task(refresh())
        return {"ok": True, "status": "started"}

    static = Path(__file__).parent / "static"
    if static.is_dir():
        app.mount("/static", RevalidatedStaticFiles(directory=str(static)), name="static")

        @app.get("/")
        async def homepage():
            return FileResponse(static / "index.html", headers={"Cache-Control": "no-cache"})
    return app


class RevalidatedStaticFiles(StaticFiles):
    """Браузер сверяет файлы интерфейса с сервером (ответ 304, если не менялись).

    Без этого после обновления приложения стили и скрипты берутся из кэша браузера.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response
