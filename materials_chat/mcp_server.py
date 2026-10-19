"""Настоящий MCP-сервер на официальном SDK. Один набор tools, два транспорта."""
import argparse
import logging

from mcp.server.fastmcp import FastMCP

from .config import Settings, get_settings
from .service import GraphService, ToolResult


def create_server(settings: Settings | None = None) -> FastMCP:
    settings = settings or get_settings()
    service = GraphService(settings)
    mcp = FastMCP("IACPaaS Materials", host=settings.mcp_host, port=settings.mcp_port,
                  stateless_http=True, json_response=True)

    @mcp.tool()
    def list_inforesources(filter: str = "", limit: int = 20) -> ToolResult:
        """Список разрешённых баз и онтологий. Это метаданные, не сведения о свойствах материалов."""
        return service.list_inforesources(filter, limit)

    @mcp.tool()
    def get_inforesource(resource_id: str) -> ToolResult:
        """Метаданные базы: root, онтология, дата синхронизации и размер графа."""
        return service.get_inforesource(resource_id)

    @mcp.tool()
    def search_graph(query: str, resource_id: str | None = None, limit: int = 8, offset: int = 0) -> ToolResult:
        """Лексический поиск в локальном индексе реальных данных IACPaaS. Ищите короткими терминами/марками.
        Возвращает node_id, фрагменты, total и has_more. Уточняйте запрос вместо выгрузки всех результатов.
        Отсутствие совпадения относится только к указанному охвату и состоянию индекса.
        """
        return service.search_graph(query, resource_id, limit, offset)

    @mcp.tool()
    def get_subgraph(resource_id: str, root: str, depth: int = 2, max_nodes: int = 40, offset: int = 0) -> ToolResult:
        """Прочитать свойства/значения от найденной вершины. root берите из tool result, не угадывайте.
        Глубина до 5, до 60 вершин. При has_more используйте next_offset или уточните корень.
        """
        return service.get_subgraph(resource_id, root, depth, max_nodes, offset)

    @mcp.tool()
    def count_graph(resource_id: str, root: str | None = None) -> ToolResult:
        """Точное число уникальных вершин и дуг сохранённого графа/подграфа, без раскрытия внешних ссылок."""
        return service.count_graph(resource_id, root)

    @mcp.tool()
    def validate_resource(resource_id: str) -> ToolResult:
        """Базовая проверка структуры индекса; не выполняет предметные вычисления и не меняет ресурс."""
        return service.validate_resource(resource_id)

    return mcp


def main():
    parser = argparse.ArgumentParser(description="MCP-адаптер чтения IACPaaS")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="streamable-http")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    settings = get_settings()
    if args.host:
        settings.mcp_host = args.host
    if args.port:
        settings.mcp_port = args.port
    # stdout зарезервирован под JSON-RPC при stdio.
    logging.basicConfig(level=logging.WARNING)
    create_server(settings).run(transport=args.transport)


if __name__ == "__main__":
    main()
