# HTTP API веб-приложения

Контракт между сервером (`materials_chat/web.py`) и интерфейсом (`materials_chat/static/`).
Все ответы — JSON в UTF-8, кроме потока чата (Server-Sent Events).

## Общие объекты

### Usage — расход модели

Используется в ответе, в сообщении и в сумме по диалогу.

```json
{
  "input_tokens": 21256,
  "output_tokens": 376,
  "total_tokens": 21632,
  "requests": 4,
  "known": true,
  "unknown_requests": 0,
  "last_input_tokens": 6120,
  "cost": 0.1175,
  "currency": "RUB",
  "cost_known": true
}
```

- `requests` — сколько обращений к модели было (ответ с инструментами — это несколько обращений).
- `known: false` — провайдер не прислал usage хотя бы для одного обращения; числа тогда — нижняя оценка.
- `last_input_tokens` — входные токены последнего обращения, то есть фактический размер контекста.
- `cost` — стоимость в `currency` (`"RUB"` у RouterAI, `"USD"` у OpenRouter). `null`, если цены модели неизвестны.

### Результат инструмента (внутри trace)

```json
{
  "id": "call_1",
  "name": "search_graph",
  "arguments": {"query": "12Х18Н10Т"},
  "result": {
    "ok": true,
    "data": {"hits": [], "total": 24},
    "error": null,
    "meta": {
      "served_from": "local_index",
      "sources": [{"resource_id": "materials", "resource_title": "Справочник по материалам", "indexed_at": "…"}]
    }
  },
  "duration_ms": 34,
  "is_error": false
}
```

`meta.served_from` показывает, откуда данные:

| Значение | Смысл |
|---|---|
| `iacpaas_live` | Только что прочитано с платформы IACPaaS |
| `local_index` | Локальный поисковый индекс (снимок платформы) |
| `local_index_fallback` | Платформа недоступна, взято из индекса; причина в `meta.live_error` |

Для `iacpaas_live` в `meta` есть `fetched_at`, для индекса — `indexed_at`.
У ошибки: `ok: false`, `error: {"code": "NODE_NOT_FOUND", "message": "…", "retryable": false}`.

## Эндпоинты

### `GET /api/status`

```json
{
  "configured": true,
  "missing_settings": [],
  "provider": "RouterAI",
  "model": "deepseek/deepseek-v4.1-flash",
  "model_info": {
    "name": "DeepSeek: DeepSeek V4.1 Flash",
    "context_window": 1048576,
    "currency": "RUB",
    "price_input_per_million": 5.36,
    "price_output_per_million": 9.57,
    "source": "provider"
  },
  "limits": {"context_budget": 1044480, "max_output_tokens": 4096, "max_tool_steps": 8, "max_tool_calls": 16},
  "index": {
    "ready": true, "resources_indexed": 8, "total_resources": 8, "nodes": 94569,
    "last_synced": "2026-10-18T09:59:24+00:00", "busy": false, "current_resource": null,
    "errors": {}, "refresh_running": false, "refresh_error": null
  },
  "resources": [{"id": "materials", "title": "Справочник по материалам", "path": "Загрузки / …", "kind": "information"}]
}
```

`model_info.source`: `provider` — данные получены из каталога моделей провайдера, `settings` — из `.env`, `unknown` — неизвестны (тогда цены и окно `null`).

### `GET /api/data/overview`

Подготовленное описание данных для вкладки «Данные». Строится из локального индекса и `config/data_notes.json`.

```json
{
  "ready": true,
  "generated_at": "2026-10-19T12:00:00+00:00",
  "title": "Данные проекта",
  "intro": "Абзац о том, что за данные и откуда они.",
  "stats": [{"label": "Ресурсов", "value": "8"}, {"label": "Вершин", "value": "94 569"}],
  "sections": [
    {
      "id": "materials",
      "title": "Справочник по материалам",
      "kind": "data",
      "description": "Что содержит ресурс, простыми словами.",
      "stats": [{"label": "Вершин", "value": "93 593"}],
      "items": [
        {"title": "Сплавы на основе железа", "count": 512, "count_label": "марок",
         "children": [{"title": "Стали коррозионностойкие", "count": 40, "count_label": "марок"}]}
      ],
      "examples": ["12Х18Н10Т", "Д16"],
      "related": ["materials_ontology"],
      "source": {"path": "Загрузки / Справочник по материалам", "indexed_at": "…", "platform_modified": "28.08.2026"}
    }
  ],
  "suggested_questions": ["Какой химический состав у 12Х18Н10Т?"]
}
```

- `kind`: `data` (база или справочник) или `ontology` (структура, по которой описаны данные).
- `items` — дерево не глубже двух уровней; `count` и `children` необязательны.
- Если индекс пуст: `{"ready": false, "sections": [], …}` — интерфейс предлагает загрузить данные.

### `POST /api/index/refresh` → `202 {"ok": true, "status": "started" | "running"}`

Запускает перезагрузку ресурсов из IACPaaS. Ход виден в `GET /api/status` → `index`.

### Диалоги

- `GET /api/sessions` → `{"sessions": [{"id", "title", "updated_at", "usage"}]}` (новые сверху).
- `POST /api/sessions` → `201` объект диалога (как ниже, без сообщений).
- `GET /api/sessions/{id}` →

```json
{
  "id": "…", "title": "…", "updated_at": "…",
  "messages": [
    {"id": "…", "role": "user", "content": "…", "created_at": "…", "trace": [], "usage": {}, "grounded": null},
    {"id": "…", "role": "assistant", "content": "…", "created_at": "…", "trace": ["…"], "usage": {"…": "…"}, "grounded": true}
  ],
  "usage": {"…": "сумма по диалогу"},
  "context_tokens": 18498,
  "context_budget": 1044480,
  "context_estimated": true
}
```

- `grounded` (только у ответов): `true` — в этом ответе были успешные обращения к данным; `false` — ответ без обращения к базе (приветствие, вопрос о диалоге, отказ не по теме); `null` — старые сообщения, где признак не сохранялся.
- `context_tokens` — оценка размера истории, которая уйдёт в модель со следующим вопросом.

- `DELETE /api/sessions/{id}` → `{"ok": true}`; `409`, если в диалоге идёт ответ.

### `POST /api/sessions/{id}/chat` — поток ответа

Тело: `{"message": "текст"}`. Ответ — `text/event-stream`, события по порядку:

| Событие | Данные | Когда |
|---|---|---|
| `session` | `{"id", "title"}` | Сразу; title обновляется по первому вопросу |
| `status` | `{"message"}` | Что агент делает сейчас |
| `tool_start` | `{"id", "name", "arguments"}` | Начало вызова инструмента |
| `tool_end` | результат инструмента (см. выше) | Конец вызова |
| `answer_delta` | `{"text"}` | Очередной кусок ответа (склеивать) |
| `answer_meta` | `{"grounded", "tool_calls", "truncated"}` | После ответа; `truncated` — упёрлись в лимит длины |
| `usage` | `{"turn", "session", "context_tokens", "context_budget", "context_estimated"}` | Перед `done` |
| `error` | `{"message", "code"}` | Ошибка; текст уже понятен пользователю |
| `done` | `{"message_id"}` | Конец потока |

Ошибки до начала потока: `404` (нет диалога), `409` (ответ уже формируется), `503` (не настроен ключ или модель),
`422` (пустое сообщение). Тело: `{"detail": "текст"}`.
