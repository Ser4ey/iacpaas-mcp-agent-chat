FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY materials_chat ./materials_chat
COPY config ./config
RUN pip install --no-cache-dir .
ENV APP_HOST=0.0.0.0 APP_PORT=8000 MCP_HOST=127.0.0.1 MCP_PORT=8001 MCP_URL=http://127.0.0.1:8001/mcp
EXPOSE 8000
CMD ["python", "-m", "materials_chat"]
