"""Один запуск поднимает MCP-процесс и веб-приложение."""
import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time

import uvicorn

from .config import get_settings
from .service import GraphService


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Локальный чат с материалами IACPaaS")
    parser.add_argument("command", nargs="?", choices=["serve", "sync"], default="serve")
    parser.add_argument("--web-only", action="store_true", help="Использовать уже запущенный MCP_URL")
    args = parser.parse_args()
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    if args.command == "sync":
        result = asyncio.run(GraphService(settings).refresh_all())
        print(json.dumps(result, ensure_ascii=True, indent=2))
        if result["status"] != "complete":
            sys.exit(1)
        return

    process = None
    try:
        if not args.web_only and settings.mcp_transport != "stdio":
            # Не подключаемся молча к чужому процессу на том же порту.
            with socket.socket() as probe:
                probe.bind((settings.mcp_host, settings.mcp_port))
            environment = os.environ.copy()
            environment["PYTHONUNBUFFERED"] = "1"
            process = subprocess.Popen([sys.executable, "-m", "materials_chat.mcp_server", "--transport", "streamable-http"], env=environment)
            deadline = time.monotonic()+15
            connect_host = "127.0.0.1" if settings.mcp_host == "0.0.0.0" else settings.mcp_host
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("MCP-сервер завершился при запуске. Проверьте сообщение выше.")
                try:
                    with socket.create_connection((connect_host, settings.mcp_port), timeout=0.3):
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("MCP-сервер не успел запуститься.")
        from .web import create_app
        print(f"\nОткройте http://127.0.0.1:{settings.app_port}  |  Ctrl+C — остановить\n", flush=True)
        uvicorn.run(create_app(settings), host=settings.app_host, port=settings.app_port, log_level="warning")
    except OSError:
        print("Порт занят или недоступен. Измените APP_PORT/MCP_PORT/MCP_URL в .env.", file=sys.stderr)
        sys.exit(1)
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()
