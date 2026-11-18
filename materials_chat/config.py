"""Настройки из .env; секреты никогда не возвращаются браузеру или модели."""
import ipaddress
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLOUD_PROVIDERS = {"routerai.ru": "RouterAI", "openrouter.ai": "OpenRouter"}
LOCAL_PROVIDER = "Локальная модель"
ROUTERAI_BASE_URL = "https://routerai.ru/api/v1"


def is_local_endpoint(base_url: str) -> bool:
    """Адрес модели на этом компьютере или в локальной сети: такому серверу ключ API не нужен.

    Локальные — localhost, 127.0.0.0/8, ::1, частные сети (10.*, 172.16–31.*, 192.168.*),
    host.docker.internal, имена *.local и имена без точки (сервис docker compose, например «ollama»).
    """
    host = (urlsplit(str(base_url or "")).hostname or "").lower()
    if not host:
        return False
    if host in ("localhost", "host.docker.internal") or host.endswith((".localhost", ".local")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host
    return address.is_loopback or address.is_private or address.is_link_local


def public_endpoint(base_url: str) -> str:
    """Адрес сервера модели для показа: без логина, пароля и параметров запроса, которые могут быть в URL."""
    parts = urlsplit(str(base_url or ""))
    if not parts.hostname:
        return ""
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    try:
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        port = ""
    return f"{parts.scheme}://{host}{port}{parts.path}".rstrip("/")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8-sig", extra="ignore", populate_by_name=True)

    iacpaas_base_url: str = "https://iacpaas.dvo.ru"
    iacpaas_username: str = Field(default="", repr=False)
    iacpaas_password: str = Field(default="", repr=False)
    iacpaas_api_key: str = Field(default="", repr=False)
    iacpaas_access_token: str = Field(default="", repr=False)
    # Модель задаётся одним из двух блоков. Своя модель (LLM_*) — любой сервер с OpenAI-совместимым
    # API: если LLM_BASE_URL заполнен, работает она. Иначе — RouterAI (ROUTERAI_*).
    # Код читает выбранный блок через свойства model_base_url, model_name и model_api_key.
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = Field(default="", repr=False)
    routerai_api_key: str = Field(default="", repr=False,
        validation_alias=AliasChoices("ROUTERAI_API_KEY", "routerai_api_key"))
    routerai_model: str = Field(default="", validation_alias=AliasChoices("ROUTERAI_MODEL", "routerai_model"))
    # Адрес RouterAI постоянный; переменная оставлена, чтобы не ломать уже заполненные .env.
    routerai_base_url: str = Field(default=ROUTERAI_BASE_URL,
        validation_alias=AliasChoices("ROUTERAI_BASE_URL", "ROUTERAI_BASEURL", "routerai_base_url"))
    # Локальный ли сервер модели (оплаты нет, без повтора после тайм-аута). Пусто — по адресу, см. is_local_endpoint.
    model_local: bool | None = None
    # Модель проверки ответа; пусто — та же, что у агента.
    verifier_model: str = Field(default="", validation_alias=AliasChoices("VERIFIER_MODEL", "verifier_model"))
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
    # Сколько ждать ответа модели на одно обращение; локальной модели на процессоре нужно больше.
    model_timeout_seconds: float = Field(default=120, ge=10, le=3600)
    # Температура модели агента и проверяющего; 0 — ответы на одинаковые вопросы меньше расходятся.
    model_temperature: float | None = Field(default=0, ge=0, le=2)
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
    # Запись в «Мой фонд» (создание, изменение, удаление) и чтение любого ресурса по пути.
    write_enabled: bool = True
    # Дополнительные защищённые пути через «;» — к базовым ресурсам и папке «Загрузки».
    protected_paths: str = ""
    write_confirm_timeout_seconds: float = Field(default=120, ge=5, le=600)

    @property
    def model_settings_prefix(self) -> str:
        """Блок настроек, из которого взята модель: LLM (своя модель) или ROUTERAI."""
        return "LLM" if self.llm_base_url.strip() else "ROUTERAI"

    @property
    def model_base_url(self) -> str:
        own = self.model_settings_prefix == "LLM"
        return ((self.llm_base_url if own else self.routerai_base_url).strip() or ROUTERAI_BASE_URL).rstrip("/")

    @property
    def model_name(self) -> str:
        return (self.llm_model if self.model_settings_prefix == "LLM" else self.routerai_model).strip()

    @property
    def model_api_key(self) -> str:
        return (self.llm_api_key if self.model_settings_prefix == "LLM" else self.routerai_api_key).strip()

    @property
    def model_key_required(self) -> bool:
        """Ключ обязателен только RouterAI. Своей модели он нужен, если его требует сам сервер."""
        return self.model_settings_prefix == "ROUTERAI" and not self.model_is_local

    def with_model(self, name: str, **changes) -> "Settings":
        """Те же настройки с другой моделью на том же сервере (например, для проверяющего)."""
        field = "llm_model" if self.model_settings_prefix == "LLM" else "routerai_model"
        return self.model_copy(update={field: name, **changes})

    @property
    def model_is_local(self) -> bool:
        return self.model_local if self.model_local is not None else is_local_endpoint(self.model_base_url)

    @property
    def provider_name(self) -> str:
        """Имя сервера модели в интерфейсе и сообщениях: RouterAI, OpenRouter, «Локальная модель» или адрес сервера."""
        if self.model_is_local:
            return LOCAL_PROVIDER
        host = (urlsplit(self.model_base_url).hostname or "").lower()
        return CLOUD_PROVIDERS.get(host) or host or "API модели"

    @property
    def model_setting_names(self) -> tuple[str, str]:
        prefix = self.model_settings_prefix
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
