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
    model_context_tokens: int = Field(default=32768, ge=4096)
    max_context_tokens: int = Field(default=24000, ge=2048)
    max_output_tokens: int = Field(default=1800, ge=128, le=8192)
    max_tool_steps: int = Field(default=6, ge=1, le=12)
    max_tool_calls: int = Field(default=12, ge=1, le=24)
    max_result_nodes: int = Field(default=60, ge=1, le=200)
    max_result_chars: int = Field(default=18000, ge=2000, le=50000)
    api_timeout_seconds: float = Field(default=65, ge=30, le=180)
    max_export_bytes: int = 64 * 1024 * 1024
    auto_sync_on_start: bool = True
    demo_mode: bool = False

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
