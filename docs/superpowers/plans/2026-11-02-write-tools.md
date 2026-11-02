# Запись в IACPaaS — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** два записывающих MCP-инструмента (`write_inforesource`, `delete_inforesource`) с защитой базовых ресурсов, сборкой universal JSON по онтологии и ручным подтверждением через MCP elicitation; чтение любого ресурса фонда по пути.

**Architecture:** политика путей и онтология — отдельные модули без сети; запись — `WriteService` поверх `GraphService` (общий индекс, клиент платформы и кэш живого чтения); MCP-сервер в HTTP-режиме с сессией вызывает `ctx.elicit`; агент передаёт `elicitation_callback`, который шлёт `confirm_request` в поток хода и ждёт решения из эндпоинта.

**Tech Stack:** Python 3.12, mcp 1.30 (FastMCP), langchain-mcp-adapters 0.3.2, FastAPI, httpx, SQLite-индекс, ванильный JS.

**Spec:** `docs/superpowers/specs/2026-10-28-write-tools-design.md`

## Global Constraints

- Тесты без сети и платных запросов; `.\.venv\Scripts\python.exe -m pytest -q`, сейчас 142 зелёных — должны остаться зелёными.
- `WRITE_ENABLED` (по умолчанию `true`), `PROTECTED_PATHS` (пусто, через `;`), `WRITE_CONFIRM_TIMEOUT_SECONDS` (120).
- Коды ошибок: `PROTECTED_RESOURCE`, `FOREIGN_CONCEPT`, `AMBIGUOUS_PATH`, `CONCEPT_NOT_ALLOWED`, `INVALID_VALUE`, `UNSUPPORTED_ONTOLOGY_FEATURE`, `NODE_NOT_FOUND`, `INVALID_ARGUMENTS`, `CONFIRMATION_DECLINED`, `CONFIRMATION_TIMEOUT`, `CONFIRMATION_UNAVAILABLE`, `PLATFORM_REJECTED`, `PLATFORM_UNAVAILABLE`, `PLATFORM_TIMEOUT`, `ACCESS_DENIED`.
- Защита проверяется до подтверждения и до любого запроса к платформе; секреты не попадают в результаты, журнал и текст подтверждения.
- Всего инструментов 9: 7 читающих (`readOnlyHint: true`), 2 записывающих (`readOnlyHint: false`, `destructiveHint: true`).
- Документация — только текущее поведение.

## Что показали пробы (02.11.2026) и как это меняет спецификацию

- `ctx.elicit` в режиме `stateless_http=True, json_response=True` до клиента не доходит; в режиме `stateless_http=False, json_response=False` и по stdio работает → HTTP переводится в режим с сессией.
- `POST /api/storage-unit/folder/create`: `path` — родительская папка («» — корень фонда), повтор имени → `success:false`, «В папке (или разделе) уже содержится объект с заданным именем».
- `POST /api/data/import`: минимальный JSON `{title, json_type:"universal", ontology:"<account> / Мой Фонд / Загрузки / Онтология …$;", name:title, type:"КОРЕНЬ", meta:<название онтологии>, successors}`; `id` не нужны; у вершин `type`, `name`, `meta` (= имя понятия), `comment`; у значений `type:"ТЕРМИНАЛ-ЗНАЧЕНИЕ"`, `value`, `valtype`, `meta`. Платформа сама проверяет ограничители: собственное значение по отношению с ограничителем `clone` (единица измерения в базе свойств) отклоняется → такие понятия сервер отклоняет заранее (`UNSUPPORTED_ONTOLOGY_FEATURE`); в сценарии демонстрации единица измерения заменяется синонимом.
- `PUT /api/data/update` на платформе не работает при любых аргументах («Неожиданное значение параметра action: updateVertice»). `update_concept` выполняется переимпортом: живая выгрузка ресурса → правка вершины → `import` с `clearIfExists: true` (тот же ресурс, тот же `code`); при сбое — повторный импорт исходной выгрузки. Ресурс с `link`/`original` или больше `LIVE_MAX_NODES` вершин так не меняется (`UNSUPPORTED_ONTOLOGY_FEATURE`). Вычислять `metaconcept_path` не нужно.
- `DELETE /api/data/delete` с `trashIR`/`deleteIR` работает; несуществующий путь → `success:false` с Java-сообщением.
- Отказ платформы: `success:false`, текст — в `explanationSafe` (у папки `code` нет). В ответ инструмента идёт `explanationSafe` без префиксов Java-исключений, не длиннее 400 символов.

## Review Focus

- Обход защиты написанием пути: `Загрузки/Справочник по материалам`, `мой фонд / ЗАГРУЗКИ / …`, `//Загрузки//X`, `Pristup…@…/Мой Фонд/Загрузки`, хвост `$;` — всё `PROTECTED_RESOURCE` (тест-матрица в задаче 4).
- Создание ресурса или папки с именем, которое делает итоговый путь защищённым (`parent_path=""`, `name="Загрузки"`) — отказ (задача 4).
- Имя вершины или значение с `/` в `concept_path` — `‴…‴` разбирается как один сегмент (задача 1).
- Решение по подтверждению после истечения времени или повторно — `404`, запись не выполняется (задача 6).
- `WRITE_ENABLED=false`: нет записывающих инструментов и параметра `path` у чтения (задача 5).

---

### Task 1: Пути фонда и защита

**Files:**
- Create: `materials_chat/fund_paths.py`
- Modify: `materials_chat/config.py` (три настройки), `.env.example`
- Test: `tests/test_write_policy.py`

**Interfaces:**
- Produces:
  - `split_path(text: str) -> list[str]` — сегменты: `‴…‴` — один сегмент, разделитель `/`, обрезка пробелов, без пустых, хвост `$;`/`$`/`;` снят, префикс `Мой Фонд` и `<e-mail> / Мой Фонд` снят; `Мой Фонд` в середине → `ValueError`.
  - `join_path(segments) -> str` — `" / ".join`, сегмент с `/` в `‴…‴` (формат путей экспорта).
  - `concept_path(segments) -> str` уже есть в `platform.py` (`/a/‴b/c‴`) — переиспользовать.
  - `class Protection(definitions: list[ResourceDefinition], extra: str)` с `rule(segments) -> str | None` — название правила («базовый ресурс „…“», «папка „Загрузки“», «PROTECTED_PATHS: …»); сравнение по сегментам без учёта регистра и `ё/е`.
  - `Settings.write_enabled: bool = True`, `protected_paths: str = ""`, `write_confirm_timeout_seconds: float = Field(120, ge=5, le=600)`.

- [ ] Тесты: `test_split_path_variants` (`"Мой Фонд / Демо агента"`, `"x@y.ru/Мой Фонд/Демо агента/"`, `"Демо агента/‴г/л‴"` → `["Демо агента","г/л"]`, `"A$;"`), `test_protection_matrix` (параметризовано: все 8 путей из `config/resources.json`, `Загрузки`, `Загрузки/Новый`, `загрузки//справочник по материалам`, путь из `PROTECTED_PATHS`) → правило не `None`; `Демо агента/Мои свойства` → `None`.
- [ ] Реализация, тесты зелёные, коммит.

### Task 2: Онтология проекта

**Files:**
- Create: `materials_chat/ontology.py`
- Test: `tests/test_ontology.py` (на настоящем `data/search.sqlite3`, `pytest.skip`, если индекса нет — как в `tests/test_real_index.py`)

**Interfaces:**
- Consumes: `GraphStore` (таблицы `nodes`, `edges`), `ResourceDefinition`.
- Produces:
  - `ONTOLOGIES = ("materials_ontology", "properties_ontology", "characteristics_ontology")`.
  - `class OntologyError(Exception)` с `code`, `message`.
  - `class Ontology` — `Ontology.load(store, definitions, resource_id) -> Ontology`; `structure(max_depth=8) -> list[dict]` — дерево `{concept, kind: "вершина с именем"|"значение"|"вариант альтернативы", valtype?, children?, unsupported?}`; `build(name: str, comment: str, items: list[dict]) -> dict` — universal JSON ресурса; `describe(items) -> str` — строка для подтверждения («Класс свойств „Тепловые свойства“ → Свойство „Теплопроводность“ → синоним „λ“»).
- Правила (из проб и индекса):
  - потомки понятия — его вершины в индексе; понятие без потомков и с `link` на одну из трёх онтологий проекта берёт потомков понятия по ссылке (поиск по `path` в индексе); `link` вне трёх онтологий → `UNSUPPORTED_ONTOLOGY_FEATURE`;
  - `specifier: "пропуск"` прозрачен: его потомки допустимы прямо под родителем;
  - ограничитель `ref`, `ref-clone`, `clone` → `UNSUPPORTED_ONTOLOGY_FEATURE` («значение должно ссылаться на вершину другого ресурса»);
  - `НЕТЕРМИНАЛ`: `name` обязателен у множеств и списков (в `specifier` есть «множеств», «спис», «последовательн»), иначе по умолчанию — имя понятия; `value` у вершины → `INVALID_ARGUMENTS`;
  - `ТЕРМИНАЛ-СОРТ`: нужен `value`; `REAL` — число или строка-число (`,` → `.`), `INTEGER` — целое, `STRING` — строка, иначе `INVALID_VALUE`; `DATE`, `BLOB`, `BOOLEAN` → `UNSUPPORTED_ONTOLOGY_FEATURE`;
  - `ТЕРМИНАЛ-ЗНАЧЕНИЕ` в онтологии (фиксированное значение, напр. «≤») → `UNSUPPORTED_ONTOLOGY_FEATURE`;
  - неизвестное понятие → `CONCEPT_NOT_ALLOWED` «В „X“ нельзя добавить „Y“; допустимы: …»;
  - корень JSON: `title=name=имя`, `json_type`, `ontology` = `metadata.path` онтологии из индекса, `meta` = `title` онтологии, `comment`.
- [ ] Тесты: `test_structure_of_properties_ontology` (есть «Класс свойств» → «Свойство» → «Синонимы» → «синоним» `STRING`; «Возможные значения» с вариантами «Числовое значение» `REAL`, «Числовой интервал»), `test_build_demo_resource` (JSON совпадает с принятым платформой в пробе: `meta` вершин, `ТЕРМИНАЛ-ЗНАЧЕНИЕ`/`valtype`), `test_link_children` («Числовой интервал» → «Нижняя граница»), `test_concept_not_allowed_lists_options` («Плотность» под «Свойство»), `test_invalid_real`, `test_clone_unit_is_unsupported` («единица измерения»), `test_set_requires_name`.
- [ ] Реализация, тесты, коммит.

### Task 3: Клиент платформы — запись

**Files:**
- Modify: `materials_chat/platform.py`
- Test: `tests/test_platform.py`

**Interfaces:**
- Produces: `PlatformClient.create_folder(parent: str, name: str, description: str) -> dict`, `import_resource(folder: str, graph: dict, clear: bool) -> dict`, `delete_resource(path: str, mode: str) -> dict` (`mode` — `trashIR`/`deleteIR`); общий `_send(method, url_path, body)` с той же авторизацией и повторным входом при 401, что у `export`. `success:false` → `PlatformError("PLATFORM_REJECTED", "Платформа отклонила запрос: <explanationSafe без Java-префиксов>")`; `platformUnavailable` → `PLATFORM_UNAVAILABLE`; 401/403 → `ACCESS_DENIED`; таймаут → `PLATFORM_TIMEOUT`.
- [ ] Тесты (`httpx.MockTransport`): метод, адрес и тело каждого запроса; `import` кладёт граф строкой в `json`; отказ с `explanationSafe` «ru.dvo…StorageException: …: Под узлом…» → сообщение без префикса.
- [ ] Реализация, тесты, коммит.

### Task 4: WriteService — операции, защита, подтверждение, чтение по пути

**Files:**
- Create: `materials_chat/writes.py`
- Modify: `materials_chat/service.py` (сброс `_live_cache` по пути; `get_inforesource` для онтологии добавляет `data.structure`)
- Test: `tests/test_writes.py`

**Interfaces:**
- Consumes: Task 1–3.
- Produces:
  - `Confirm = Callable[[str, bool], Awaitable[ToolResult | None]]` — текст, `danger`; `None` — подтверждено, иначе отказ.
  - `class WriteService(graph: GraphService)`: `async write(request: dict, confirm: Confirm) -> ToolResult` (`request.operation` ∈ `create_folder|create_resource|update_concept`), `async delete(path: str, mode: str, confirm: Confirm) -> ToolResult`, `async read_resource(path: str) -> ToolResult`, `async read_subgraph(path: str, root: str | None, depth, max_nodes, offset) -> ToolResult`.
  - Результат записи: `data = {operation, path, platform: {success, code}}`, `meta.served_from = "iacpaas_live"`; кэш живого чтения по пути сброшен.
  - `update_concept`: живая выгрузка ресурса, поиск вершины по `concept_path` по именам/значениям (одноимённые соседи → `AMBIGUOUS_PATH`, нет → `NODE_NOT_FOUND`, `original`/`link` у вершины → `FOREIGN_CONCEPT`), проверка значения по `valtype` вершины, переимпорт с `clearIfExists`, откат при сбое.
  - Журнал: одна строка `log.warning("Запись IACPaaS: %s %s → %s", operation, path, outcome)`.
- [ ] Тесты: матрица защиты (каждая операция × защищённый путь и обходные написания → `PROTECTED_RESOURCE`, подтверждение не вызывалось, у подменённой платформы нет записывающих вызовов); подтверждено → верный вызов платформы; отказ подтверждения → вызова нет, код из `confirm`; `update_concept` переименовывает через `import_resource(clear=True)`, чужая вершина → `FOREIGN_CONCEPT`; чтение по пути — `concept_path` у вершин, `served_from: iacpaas_live`, ошибка платформы → `ok:false`; текст подтверждения называет операцию и путь.
- [ ] Реализация, тесты, коммит.

### Task 5: MCP-сервер

**Files:**
- Modify: `materials_chat/mcp_server.py`
- Test: `tests/test_mcp_write.py` (`mcp.shared.memory.create_connected_server_and_client_session`)

**Interfaces:**
- Produces: `FastMCP(..., stateless_http=False, json_response=False)`; `write_inforesource(request: CreateFolder | CreateResource | UpdateConcept)` (pydantic-модели с `operation: Literal[...]` и своими обязательными полями), `delete_inforesource(path: str, mode: Literal["trash","delete"] = "trash")`; `get_inforesource(resource_id: str | None = None, path: str | None = None)`, `get_subgraph(resource_id=None, root=None, path=None, …)`; аннотации `ToolAnnotations`. Подтверждение: `ctx.elicit(message, ConfirmWrite)` в `asyncio.wait_for(…, WRITE_CONFIRM_TIMEOUT_SECONDS)`; клиент без возможности elicitation → `CONFIRMATION_UNAVAILABLE`; `accept` с `confirm: true` → `None`; `decline`/`cancel` → `CONFIRMATION_DECLINED`; таймаут → `CONFIRMATION_TIMEOUT`.
- [ ] Тесты: `list_tools` — 9 инструментов и аннотации; `WRITE_ENABLED=false` — 7, у `get_inforesource`/`get_subgraph` нет `path`; клиент без `elicitation_callback` → `CONFIRMATION_UNAVAILABLE`; клиент отвечает `decline` → `CONFIRMATION_DECLINED`, `accept` → запрос ушёл; таймаут (настройка 5 с, клиент молчит) → `CONFIRMATION_TIMEOUT`.
- [ ] Реализация, тесты, коммит.

### Task 6: Агент, MCP-клиент, веб

**Files:**
- Modify: `materials_chat/mcp_client.py`, `materials_chat/agent.py`, `materials_chat/web.py`
- Test: `tests/test_full_stack.py` (новый тест с записью), `tests/test_agent_backend.py` (эндпоинт решения)

**Interfaces:**
- Produces:
  - `mcp_tools(settings, elicitation=None)` — `session_kwargs={"elicitation_callback": elicitation}`; таймауты HTTP — `WRITE_CONFIRM_TIMEOUT_SECONDS + API_TIMEOUT_SECONDS + 30`.
  - `class Confirmations` (в `web.py`): `open(session_id, payload) -> (id, Future)`, `resolve(session_id, id, accept) -> bool`, `cancel_session(session_id)`.
  - `Agent(..., confirmations=None)`; при `confirmations` и стандартном `tools_factory` передаёт обработчик elicitation; событие `("confirm_request", {id, call_id, tool, message, danger, timeout_seconds})`; у записи журнала вызова — `confirmation: {id, message, status: "accepted"|"declined"|"expired"|"cancelled"}`.
  - `POST /api/sessions/{id}/confirmations/{confirmation_id}` `{"accept": bool}` → `{"ok": true, "status": …}`; неизвестный/решённый → `404`; работает при занятой блокировке; остановка хода отклоняет ожидающие.
- [ ] Тест сквозной: двойник модели вызывает `write_inforesource` (`create_folder`), подменённая платформа (через `GraphService.client`) принимает; клиент ловит `confirm_request` в потоке, шлёт `accept`, в `tool_end` — `ok: true` и `confirmation.status == "accepted"`, в сохранённом журнале то же; второй вариант — `accept: false` → `CONFIRMATION_DECLINED`, платформа без вызова; повторное решение → `404`.
- [ ] Реализация, тесты, коммит.

### Task 7: Интерфейс

**Files:**
- Modify: `materials_chat/static/trace.js`, `app.js`, `api.js`, `styles.css` (или текущий файл стилей)

- [ ] Карточка у вызова: текст, «Подтвердить»/«Отменить» (у удаления — цвет опасности), обратный отсчёт; после решения — итог («Подтверждено», «Отменено», «Время вышло»), восстанавливается из сохранённого журнала; `TOOL_ACTIVITY` для двух новых инструментов. Проверка — в браузере (задача 9).
- [ ] Коммит.

### Task 8: Промпт и документация

**Files:**
- Modify: `materials_chat/prompts.py`, `README.md`, `docs/api-mapping.md`, `docs/architecture.md`, `docs/http-api.md`, `docs/scenarios.md`, `docs/guide.html`, `.env.example`

- [ ] Промпт по части 4 спецификации; документация по списку спецификации с поправками из проб (переимпорт вместо `update`, `clone` не поддерживается); число инструментов 9 и число тестов в `docs/guide.html`.
- [ ] Полный прогон тестов, коммит.

### Task 9: Живая проверка

- [ ] Экземпляр на 8030/8031, `HISTORY_PATH` в scratchpad, `AUTO_SYNC_ON_START=false`; сценарий демонстрации в браузере внутри «Демо агента» (папка уже есть — создаётся вложенная); удалить созданное; раздел `docs/verification.md` с датой 02.11.2026; коммит.
