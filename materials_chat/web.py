"""Local FastAPI chat API. SSE exposes tool execution, never hidden reasoning."""
from __future__ import annotations

import asyncio
import inspect
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import Agent
from .openrouter import ProviderError
from .sessions import SessionStore


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12000)


async def resolved(value):
    return await value if inspect.isawaitable(value) else value


def sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def create_app(settings=None) -> FastAPI:
    if settings is None:
        from .config import get_settings
        settings = get_settings()
    from .service import GraphService

    store = SessionStore(getattr(settings, "history_path", ".data/history.sqlite3"))
    graph = GraphService(settings)
    locks: dict[str, asyncio.Lock] = {}
    refresh_task = None
    refresh_error = None
    refresh_result = None

    @asynccontextmanager
    async def lifespan(app):
        if getattr(settings, "auto_sync_on_start", True):
            current_index = await resolved(graph.status())
            if not current_index.get("ready", False):
                await refresh_index()
        yield
        if refresh_task and not refresh_task.done():
            refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass
        store.close()

    app = FastAPI(title="Материалы · IACPaaS", lifespan=lifespan)
    # These attributes make the small app easy to test without paid model calls.
    app.state.settings, app.state.store, app.state.graph = settings, store, graph
    app.state.agent_factory = Agent

    def existing(session_id):
        session = store.get(session_id)
        if session is None:
            raise HTTPException(404, "Диалог не найден.")
        return session

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
                "missing_settings": missing, "mcp_transport": getattr(settings, "mcp_transport", "http"),
                "resources": await resolved(graph.resources()), "index": index,
                "limits": {name: getattr(settings, name, default) for name, default in
                           (("max_context_tokens", 24000), ("model_context_tokens", 32768),
                            ("max_output_tokens", 1800), ("max_tool_steps", 6), ("max_tool_calls", 12))}}

    @app.get("/api/sessions")
    async def sessions():
        return {"sessions": store.list()}

    @app.post("/api/sessions", status_code=201)
    async def create_session():
        return store.create()

    @app.get("/api/sessions/{session_id}")
    async def session_detail(session_id: str):
        return existing(session_id)

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
        # Acquire before returning the response to prevent two simultaneous turns.
        await lock.acquire()
        runner = app.state.agent_factory(settings)

        async def events():
            cancelled, error = False, None
            try:
                yield sse("session", {"id": session_id, "title": text[:70] if session["title"] == "Новый диалог" else session["title"]})
                async for event, payload in runner.run(text, store.history(session_id)):
                    yield sse(event, payload)
            except asyncio.CancelledError:
                cancelled = True
                runner.result.interrupted = True
                raise
            except ProviderError as exc:
                error = {"message": str(exc), "code": exc.code}
            except Exception:
                error = {"message": "Не удалось выполнить запрос. Проверьте доступность MCP и индекс ресурсов.", "code": "agent_error"}
            finally:
                result = runner.result
                if cancelled:
                    suffix = "Ответ остановлен пользователем."
                elif error:
                    suffix = error["message"]
                else:
                    suffix = ""
                content = result.content
                if suffix:
                    content += ("\n\n" if content else "") + suffix
                    # Persist only complete tool groups, then a visible status.
                    result.model_messages.append({"role": "assistant", "content": content})
                if not content:
                    content = "Модель не вернула ответ. Попробуйте уточнить запрос."
                    result.model_messages.append({"role": "assistant", "content": content})
                try:
                    message_id = store.save_turn(session_id, text, content, result.trace,
                                                 result.usage, result.model_messages)
                finally:
                    lock.release()
            if error:
                yield sse("error", error)
            updated = store.get(session_id)
            yield sse("usage", {"turn": runner.result.usage, "session": updated["usage"],
                                "context_tokens": updated["context_tokens"], "context_estimated": True})
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
                refresh_error = "Не удалось обновить индекс. Проверьте учётные данные и подключение к IACPaaS."
        refresh_task = asyncio.create_task(refresh())
        return {"ok": True, "status": "started"}

    static = Path(__file__).parent / "static"
    if static.is_dir():
        app.mount("/static", StaticFiles(directory=str(static)), name="static")

        @app.get("/")
        async def homepage():
            return FileResponse(static / "index.html")
    return app
