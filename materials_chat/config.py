"""Настройки из .env; секреты никогда не возвращаются браузеру или модели."""
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8-sig", extra="ignore", populate_by_name=True)

    iacpaas_base_url: str = "https://iacpaas.dvo.ru"
    iacpaas_username: str = Field(default="", repr=False)
    iacpaas_password: str = Field(default="", repr=False)
    iacpaas_api_key: str = Field(default="", repr=False)
    iacpaas_access_token: str = Field(default="", repr=False)
    # Внутренние имена оставлены для совместимости с первой версией.
    # При наличии обоих наборов настроек ROUTERAI_* имеет приоритет.
    openrouter_api_key: str = Field(default="", repr=False,
        validation_alias=AliasChoices("ROUTERAI_API_KEY", "OPENROUTER_API_KEY"))
    openrouter_model: str = Field(default="",
        validation_alias=AliasChoices("ROUTERAI_MODEL", "OPENROUTER_MODEL"))
    openrouter_base_url: str = Field(default="https://openrouter.ai/api/v1",
        validation_alias=AliasChoices("ROUTERAI_BASE_URL", "ROUTERAI_BASEURL", "OPENROUTER_BASE_URL"))
    app_host: str = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1, le=65535)
    mcp_host: str = "127.0.0.1"
    mcp_port: int = Field(default=8001, ge=1, le=65535)
    mcp_url: str = "http://127.0.0.1:8001/mcp"
    mcp_transport: str = "streamable-http"
    data_dir: Path = PROJECT_ROOT / "data"
    resources_path: Path = PROJECT_ROOT / "config" / "resources.json"
    history_path: Path = PROJECT_ROOT / "data" / "history.sqlite3"
    # Окно контекста и цены модели по умолчанию берутся из каталога моделей провайдера
    # (GET /models). Эти настройки нужны, только чтобы их переопределить.
    model_context_tokens: int | None = Field(default=None, ge=4096)
    max_context_tokens: int | None = Field(default=None, ge=2048)  # мягкий лимит истории
    price_input_per_million: float | None = Field(default=None, ge=0)
    price_output_per_million: float | None = Field(default=None, ge=0)
    price_currency: str | None = None
    max_output_tokens: int = Field(default=4096, ge=128, le=32768)
    max_tool_steps: int = Field(default=8, ge=1, le=16)
    max_tool_calls: int = Field(default=16, ge=1, le=32)
    # Старые ходы диалога уходят в модель со сжатыми результатами инструментов.
    history_full_turns: int = Field(default=2, ge=0, le=20)
    history_tool_chars: int = Field(default=1500, ge=200, le=50000)
    max_result_nodes: int = Field(default=150, ge=1, le=300)
    # Самая большая запись марки (07ГФБ, 120 вершин) занимает около 19,5 тысячи символов.
    max_result_chars: int = Field(default=22000, ge=2000, le=50000)
    # Живое чтение вершин и подграфов с IACPaaS (поиск всегда идёт по индексу).
    live_reads: bool = True
    live_max_nodes: int = Field(default=5000, ge=1)
    live_cache_seconds: float = Field(default=60, ge=0)
    api_timeout_seconds: float = Field(default=65, ge=30, le=180)
    max_export_bytes: int = 64 * 1024 * 1024
    auto_sync_on_start: bool = True

    @model_validator(mode="before")
    @classmethod
    def choose_routerai_endpoint(cls, values):
        # Ключ RouterAI без явно заданного адреса отправляем только в RouterAI.
        # У старых настроек OPENROUTER_* сохраняется прежний адрес по умолчанию.
        if isinstance(values, dict) and values.get("ROUTERAI_API_KEY") and not any(
            values.get(name) for name in ("ROUTERAI_BASE_URL", "ROUTERAI_BASEURL", "OPENROUTER_BASE_URL", "openrouter_base_url")
        ):
            values["ROUTERAI_BASE_URL"] = "https://routerai.ru/api/v1"
        return values

    @property
    def provider_name(self) -> str:
        host = (urlsplit(self.openrouter_base_url).hostname or "").lower()
        return {"routerai.ru": "RouterAI", "openrouter.ai": "OpenRouter"}.get(host, "API модели")

    @property
    def model_setting_names(self) -> tuple[str, str]:
        prefix = "ROUTERAI" if self.provider_name == "RouterAI" else "OPENROUTER"
        return prefix + "_API_KEY", prefix + "_MODEL"

    @model_validator(mode="after")
    def resolve_paths(self):
        for field in ("data_dir", "resources_path", "history_path"):
            value = getattr(self, field)
            if not value.is_absolute():
                setattr(self, field, PROJECT_ROOT / value)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
