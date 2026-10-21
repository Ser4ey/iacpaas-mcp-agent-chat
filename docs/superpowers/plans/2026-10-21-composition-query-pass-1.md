# Заход 1: поиск марок по составу и проверка ответов — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** MCP-инструмент `find_materials`, автоматическая сверка чисел под каждым ответом, кнопка «Проверить» с агентом-проверяющим и мелкие исправления — всё, что спецификация относит к заходу 1.

**Architecture:** Таблицу состава строит модуль `composition.py` из `nodes`/`edges` индекса: при импорте ресурса в той же транзакции, а для уже существующего индекса — при первом обращении. `GraphService.find_materials` отбирает и сортирует марки в Python и отдаёт страницу в общем конверте `ToolResult`. Сверка чисел — чистая функция `answer_check.check_numbers`: веб-слой вызывает её в конце хода и сохраняет результат в новой колонке `messages`. Проверяющий (`verifier.py`) — отдельное обращение к RouterAI без инструментов MCP. Он отвечает вызовом функции `submit_verdict`, а эндпоинт `POST …/verify` сохраняет вердикт в своей колонке.

**Tech Stack:** Python 3.12, FastAPI, FastMCP (`mcp` 1.30), LangChain 1.4 + langchain-openai 1.7, SQLite, pydantic 2, pytest + pytest-asyncio; фронтенд — ES-модули без сборки.

**Spec:** `docs/superpowers/specs/2026-10-21-composition-query-and-answer-checks-design.md` (части 1–4 и «Объём по заходам → Заход 1»).

## Global Constraints

- Python 3.12, зависимости только из `requirements-lock.txt`; новых пакетов не добавлять (pydantic, LangChain, httpx уже есть).
- Запуск Python — из корня проекта, всегда с `PYTHONIOENCODING=utf-8` (иначе вывод с кириллицей падает): `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest …` (bash) или `$env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m pytest …` (PowerShell).
- Тесты без сети и платных вызовов. Проверяющий тестируется тем же двойником RouterAI (`Provider` из `tests/test_agent_backend.py`), что и агент.
- Сохранённые диалоги продолжают открываться: колонки `messages` добавляются миграцией, как `grounded` и `error`; старые записи без новых полей показываются как раньше. Формат ответов существующих инструментов не меняется.
- Доступ к IACPaaS — только чтение.
- Комментарии, docstring, тексты интерфейса и ошибок — по-русски, в стиле окружающего кода: короткий docstring, комментарий объясняет «почему». Строки до ~120 символов.
- Документация описывает только текущее поведение, без пометок «раньше было».
- Реальный индекс `data/search.sqlite3` не в Git, и тесты его не меняют: работают с копией во временной папке и пропускаются, если файла нет.
- Работа идёт на ветке `composition-query`. Сообщение коммита — по-русски, в конце две строки (через `git commit -F -` с heredoc):
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_013kbgdJR2afziARDo1KgwuD
  ```

## Review Focus

1. **Старые диалоги и старые записи журнала.** У сообщений нет `number_check` и `verification`, а `meta.live_error` бывает строкой, а не объектом. Диалог открывается, строки под ответами прежние, кнопка «Проверить» есть. (Тесты миграции — задачи 7 и 10, подсказка — задача 5.)
2. **Элемент в «неудобной» записи:** `"CR"`, `" cr "`, `"Хром"`, `"железо"` распознаются как символ; неизвестное слово — не ошибка, а пустой результат с пометкой. (Тест — задача 3.)
3. **Числа в ответе в разных записях:** `0,5`, `2 670` с неразрывным пробелом, `17–19` и `17-19`, `−196` с типографским минусом, `19.` в конце предложения, `**17.0**`, id в обратных кавычках. (Тест — задача 6.)
4. **«Проверить» без данных и по чужим id:** ответ без вызовов инструментов, id сообщения пользователя, id из другого диалога дают понятный 4xx, и модель не вызывается. (Тест — задача 10.)
5. **Индекс обновился после построения таблицы состава:** после повторного импорта ресурса `find_materials` видит новые данные, а не старую таблицу. (Тест — задача 2.)

---

## Карта файлов

| Файл | Ответственность |
|---|---|
| `materials_chat/composition.py` (новый) | Формы значений → диапазоны; записи марок из графа; построение таблицы состава; условия, отбор и сортировка марок |
| `materials_chat/graph_store.py` | Таблицы состава, их построение при импорте и при первом обращении; русские названия элементов |
| `materials_chat/service.py` | `GraphService.find_materials`; исправление `_bounded_node` |
| `materials_chat/mcp_server.py` | Инструмент `find_materials`; описание `search_graph` |
| `materials_chat/prompts.py` | Правки `SYSTEM_PROMPT`; `VERIFIER_PROMPT` |
| `materials_chat/answer_check.py` (новый) | Сверка чисел ответа с данными |
| `materials_chat/verifier.py` (новый) | Агент-проверяющий |
| `materials_chat/sessions.py` | Колонки `number_check` и `verification`; выборки для сверки и проверки |
| `materials_chat/web.py` | Сверка в конце хода, событие `number_check`, эндпоинт `verify` |
| `materials_chat/config.py` | Настройка `VERIFIER_MODEL` |
| `materials_chat/static/trace.js` | Ярлык и сводка `find_materials`; текст подсказки `live_error` |
| `materials_chat/static/checks.js` (новый) | Строка сверки, подсветка чисел, блок вердикта |
| `materials_chat/static/app.js`, `styles.css` | Подключение строки сверки и кнопки «Проверить» |
| `config/data_notes.json` | Пример вопроса по составу |
| `tests/test_composition.py`, `test_real_index.py`, `test_answer_check.py`, `test_verifier.py` (новые) | Тесты |
| `README.md`, `docs/*.md`, `docs/guide.html`, `.env.example` | Документация |

---

### Task 1: `fields_truncated` только при реальном усечении

**Files:**
- Modify: `materials_chat/service.py:201-213` (`_bounded_node`)
- Test: `tests/test_graph_service.py`

**Interfaces:** без изменений сигнатур.

- [ ] **Step 1: Write the failing test** (в `tests/test_graph_service.py`)

```python
async def test_short_links_are_not_marked_as_truncated(service):
    data = graph_with_shared_cycle()
    data["successors"][0]["original"] = ("Pristup.SA@vvsu.ru / Мой Фонд / Загрузки / База свойств материалов$"
                                         "/Физические свойства/Плотность вещества;")
    service.store.import_graph("materials", data)
    node = (await service.get_subgraph("materials", "2", depth=0)).data["nodes"][0]
    # Ссылка переведена в короткий путь, но ничего не обрезано.
    assert node["links"] == ["Загрузки / База свойств материалов$/Физические свойства/Плотность вещества;"]
    assert "fields_truncated" not in node
    hit = service.search_graph("Алюминий", "materials").data["hits"][0]
    assert hit["node_id"] == "2" and hit["fields_truncated"] is False
```

- [ ] **Step 2: Run it — FAIL** (`fields_truncated` сейчас `True`)

`PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_graph_service.py -q -k short_links`

- [ ] **Step 3: Fix `_bounded_node`.** Признак ставится, если обрезано поле по длине, если ссылок больше трёх или если хотя бы одна ссылка после перевода в короткий путь длиннее `link_cap`. Само укорачивание пути через `_relative_path` усечением не считается.

- [ ] **Step 4: Run the whole file — PASS**, включая `test_small_result_budget_also_bounds_a_single_long_node` (там ссылка длиннее предела, признак остаётся).

`PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_graph_service.py -q`

- [ ] **Step 5: Commit** — `fields_truncated: признак только при реальном усечении`

---

### Task 2: Таблица состава в индексе

**Files:**
- Create: `materials_chat/composition.py`
- Modify: `materials_chat/graph_store.py` (схема в `__init__`, `import_graph`, новые методы)
- Test: `tests/test_composition.py` (новый)

**Interfaces:**
- Produces (`composition.py`):
  - `FORM_UNLISTED = "не указан в составе"`
  - `material_records(nodes: dict[str, dict], children: dict[str, list[str]]) -> list[dict]` — `nodes[id] = {"label": str, "meta": str, "value": Any}`. Результат: `[{"node_id": str, "name": str, "classes": list[str], "base": str | None, "contents": {symbol: {"form": str, "low": float | None, "high": float | None, "text": str}}}]`.
  - `rebuild_composition(db: sqlite3.Connection, resource_id: str, indexed_at: str) -> int` — переписывает строки ресурса в таблицах состава внутри транзакции вызывающего и возвращает число марок.
- Produces (`GraphStore`):
  - `composition(resource_id: str) -> list[dict]` — записи марок в форме `material_records`, по алфавиту `name`. Таблица строится, если её нет или её `indexed_at` не совпадает с `resources.indexed_at`. `KeyError`, если ресурс не проиндексирован.
  - `element_symbols() -> dict[str, str]` — `normalized(русское название) → символ`. Берётся из вершин `meta = "Химический элемент"` любого ресурса, у которых есть потомок `meta = "обозначение"`.
- Таблицы (имена закреплены тестом):
  ```sql
  composition_materials(resource_id, material_id, name, classes /* JSON-список */, base, PRIMARY KEY(resource_id, material_id))
  composition_elements(resource_id, material_id, element, form, low REAL, high REAL, text, PRIMARY KEY(resource_id, material_id, element))
  composition_state(resource_id PRIMARY KEY, indexed_at)
  ```

Как устроена запись марки (по реальному индексу, глубина 4–7 от вершины марки):
`Материал → «Элементный состав» → «Основа» → элемент` (символ основы) и `→ «Компонент» → одно из:`
- `meta «Химический элемент»` (символ — `label`) → потомки без `«%»`: `«Числовой интервал»` (`«Нижняя граница»`/`«Верхняя граница»` → `«Числовое значение»`), `«Не более»`/`«Не менее»` (→ `«Числовое значение»`), `«Числовое значение»` прямо под элементом, `«Расчетное»` или ничего;
- `«Совокупность химических элементов»` → несколько элементов (диапазон каждого неизвестен) и `«Содержание»`;
- `«Прочее»` — не элемент, пропускается.

Класс марки — цепочка вершин `meta «Класс материалов»` от корня (`["Редкоземельные сплавы", "Магнитные"]`). Число берётся из `value`, при его отсутствии — из `label`. Если символ повторяется в одной марке, остаётся первое вхождение (в реальном индексе повторов нет).

| Форма | `form` | `low`, `high` | `text` |
|---|---|---|---|
| Интервал a–b | `Числовой интервал` | a, b | `"17.0–19.0 %"` |
| Не более X | `Не более` | 0.0, X | `"не более 0.12 %"` |
| Не менее X | `Не менее` | X, 100.0 | `"не менее 21.0 %"` |
| Число X | `Числовое значение` | X, X | `"1.5 %"` |
| Расчетное | `Расчетное` | None, None | `"расчётное значение, см. запись марки"` |
| Совокупность | `Совокупность химических элементов` | None, None | `"в совокупности с другими элементами, см. запись марки"` |
| нет значения | `нет значения` | None, None | `"значение не указано"` |

Числа в `text` выводятся через `str(float)`, как в спецификации (`17.0–19.0 %`).

- [ ] **Step 1: Write the fixture and failing tests** — `tests/test_composition.py`. Фикстура повторяет форму universal JSON из реального справочника; задача 3 дописывает в этот же файл.

```python
"""Таблица состава и поиск марок по составу: синтетический справочник в форме universal JSON."""
import itertools
import json
import sqlite3

import pytest

from materials_chat.graph_store import GraphStore

_ids = itertools.count(10_000)


def node(name, meta, *successors, value=None):
    item = {"id": next(_ids), "name": name, "meta": meta, "type": "НЕТЕРМИНАЛ", "successors": list(successors)}
    if value is not None:
        item.update(type="ТЕРМИНАЛ-ЗНАЧЕНИЕ", value=value)
    return item


def number(value):
    return node(str(value), "Числовое значение", value=value)


def element(symbol, *form):
    # Как в справочнике: под символом элемента — «%» и форма значения.
    return node(symbol, "Химический элемент", node("%", "%", value="%"), *form)


def interval(low, high):
    return node("Числовой интервал", "Числовой интервал",
                node("Нижняя граница", "Нижняя граница", number(low)),
                node("Верхняя граница", "Верхняя граница", number(high)))


def at_most(value):
    return node("Не более", "Не более", node("≤", "≤", value="≤"), number(value))


def at_least(value):
    return node("Не менее", "Не менее", node("≥", "≥", value="≥"), number(value))


def material(material_id, name, base, *components):
    parts = [node(str(index), "Компонент", part) for index, part in enumerate(components, 1)]
    return {"id": material_id, "name": name, "meta": "Материал", "type": "НЕТЕРМИНАЛ", "successors": [
        node("Элементный состав", "Элементный состав",
             node("Основа", "Основа", node(base, "Химический элемент", value=base)), *parts)]}


def material_class(name, *items):
    return node(name, "Класс материалов", *items)


def catalog():
    return {"title": "Справочник по материалам", "id": 1, "name": "Справочник по материалам", "type": "КОРЕНЬ",
            "meta": "Онтология справочника по материалам", "json_type": "universal", "successors": [
        material_class("Сплавы на основе железа",
            material(100, "12Х18Н10Т", "Fe", element("Fe", interval(66.7, 70.0)), element("Cr", interval(17.0, 19.0)),
                     element("Ni", interval(9.0, 11.0)), element("C", at_most(0.12)),
                     element("Ti", node("Расчетное", "Расчетное"))),
            material(200, "Х25", "Fe", element("Fe", interval(70.0, 75.0)), element("Cr", interval(23.0, 27.0)),
                     element("Mo", number(1.5))),
            material(300, "Х20", "Fe", element("Fe", interval(76.0, 80.0)), element("Cr", interval(20.0, 22.0)),
                     element("Ni", at_most(0.6))),
            material(350, "Х21", "Fe", element("Fe", interval(72.0, 79.0)), element("Cr", at_least(21.0)))),
        material_class("Сплавы на основе алюминия",
            material(400, "АМг3", "Al", element("Al", interval(93.8, 96.0)), element("Mg", interval(3.2, 3.8)),
                     element("Fe", at_most(0.5)), element("Cr", at_most(0.05)))),
        material_class("Редкоземельные сплавы", material_class("Магнитные",
            material(500, "КС25", "Sm", element("Sm", number(25.0)), element("Ni"),
                     node("Совокупность химических элементов", "Совокупность химических элементов",
                          node("Nb", "Химический элемент", value="Nb"), node("Ti", "Химический элемент", value="Ti"),
                          node("Содержание", "Содержание", node("%", "%", value="%"), at_most(0.12))),
                     node("Прочее", "Прочее", node("%", "%", value="%"), number(0.1))))),
    ]}


def elements_base():
    return {"title": "База химических элементов и веществ", "id": 900, "name": "База химических элементов и веществ",
            "type": "КОРЕНЬ", "json_type": "universal", "successors": [
        node("Химические элементы", "Химические элементы",
             node("Хром", "Химический элемент", node("Cr", "обозначение", value="Cr"),
                  node("24", "порядковый номер", value=24)),
             node("Железо", "Химический элемент", node("Fe", "обозначение", value="Fe")),
             node("Бром", "Химический элемент", node("Br", "обозначение", value="Br")))]}


ALL = ["12Х18Н10Т", "АМг3", "КС25", "Х20", "Х21", "Х25"]


@pytest.fixture
def store(tmp_path):
    result = GraphStore(tmp_path / "graph.sqlite3")
    result.import_graph("materials", catalog())
    return result


def by_name(records):
    return {item["name"]: item for item in records}


def test_value_forms_become_ranges(store):
    records = store.composition("materials")
    assert [item["name"] for item in records] == ALL
    steel = by_name(records)["12Х18Н10Т"]
    assert (steel["node_id"], steel["base"], steel["classes"]) == ("100", "Fe", ["Сплавы на основе железа"])
    assert steel["contents"]["Cr"] == {"form": "Числовой интервал", "low": 17.0, "high": 19.0, "text": "17.0–19.0 %"}
    assert steel["contents"]["C"] == {"form": "Не более", "low": 0.0, "high": 0.12, "text": "не более 0.12 %"}
    assert steel["contents"]["Ti"] == {"form": "Расчетное", "low": None, "high": None,
                                       "text": "расчётное значение, см. запись марки"}
    assert by_name(records)["Х21"]["contents"]["Cr"] == {"form": "Не менее", "low": 21.0, "high": 100.0,
                                                         "text": "не менее 21.0 %"}
    assert by_name(records)["Х25"]["contents"]["Mo"] == {"form": "Числовое значение", "low": 1.5, "high": 1.5,
                                                         "text": "1.5 %"}


def test_unknown_forms_nested_classes_and_other_components(store):
    alloy = by_name(store.composition("materials"))["КС25"]
    assert alloy["classes"] == ["Редкоземельные сплавы", "Магнитные"] and alloy["base"] == "Sm"
    assert set(alloy["contents"]) == {"Sm", "Ni", "Nb", "Ti"}  # «Прочее» — не элемент
    assert alloy["contents"]["Ni"] == {"form": "нет значения", "low": None, "high": None, "text": "значение не указано"}
    assert alloy["contents"]["Nb"]["form"] == alloy["contents"]["Ti"]["form"] == "Совокупность химических элементов"
    assert alloy["contents"]["Ti"]["low"] is None


def test_existing_index_gets_the_table_on_first_use(tmp_path):
    path = tmp_path / "graph.sqlite3"
    GraphStore(path).import_graph("materials", catalog())
    db = sqlite3.connect(path)  # индекс, построенный до появления таблицы состава
    db.executescript("DROP TABLE composition_materials; DROP TABLE composition_elements; DROP TABLE composition_state;")
    db.close()
    assert [item["name"] for item in GraphStore(path).composition("materials")] == ALL


def test_stale_table_is_rebuilt(store):
    with store.connect() as db:
        db.execute("DELETE FROM composition_materials")
        db.execute("UPDATE composition_state SET indexed_at='2000-01-01T00:00:00+00:00'")
    assert len(store.composition("materials")) == 6


def test_reimport_replaces_the_table(store):
    changed = catalog()
    changed["successors"] = changed["successors"][1:2]  # остался только класс алюминиевых сплавов
    store.import_graph("materials", changed)
    assert [item["name"] for item in store.composition("materials")] == ["АМг3"]


def test_unindexed_resource_has_no_composition(store):
    with pytest.raises(KeyError):
        store.composition("other")


def test_element_names_come_from_the_elements_base(store):
    store.import_graph("elements", elements_base())
    assert store.element_symbols() == {"хром": "Cr", "железо": "Fe", "бром": "Br"}
```

- [ ] **Step 2: Run — FAIL** (`ImportError`/`AttributeError`)

`PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_composition.py -q`

- [ ] **Step 3: Implement `composition.py`** (`FORM_UNLISTED`, `material_records`, `rebuild_composition`). `material_records` спускается от корней (вершин без родителей) по вершинам `«Класс материалов»`, накапливая цепочку классов, и разбирает каждую вершину `«Материал»` по описанию выше. `rebuild_composition` читает `nodes` (значение — поле `value` из `payload`) и `edges` ресурса, удаляет строки ресурса в трёх таблицах, вставляет новые (`INSERT OR IGNORE`) и записывает `composition_state`. `composition.py` не импортирует `graph_store` — зависимость только обратная.

- [ ] **Step 4: Implement the `GraphStore` part.** Таблицы создаются в `__init__` (`CREATE TABLE IF NOT EXISTS`). `import_graph` вызывает `rebuild_composition(db, resource_id, indexed_at)` в той же транзакции, после вставки `nodes`/`edges`. `composition()` в одном соединении сверяет `composition_state.indexed_at` с `resources.indexed_at`, при расхождении перестраивает и читает записи. `element_symbols()` использует существующую `normalized()`.

- [ ] **Step 5: Run — PASS**, затем весь набор (импорт затрагивает все тесты индекса)

`PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q`

- [ ] **Step 6: Commit** — `Таблица состава марок в локальном индексе`

---

### Task 3: `GraphService.find_materials`

**Files:**
- Modify: `materials_chat/composition.py` (условия, отбор, сортировка)
- Modify: `materials_chat/service.py` (новый метод и константа)
- Test: `tests/test_composition.py` (дописать), `tests/test_real_index.py` (новый)

**Interfaces:**
- Consumes: `GraphStore.composition`, `GraphStore.element_symbols`, `FORM_UNLISTED` (задача 2); `GraphService._ready`, `_metadata`, `_fit_result`.
- Produces:
  - `composition.Condition(BaseModel)`: `element: str`, `op: str`, `value: float | None = None`, `include_unlisted: bool = False`; у полей `Field(description=…)` по-русски. Эта же модель — схема аргумента в MCP (задача 4).
  - `composition.RULES = "«> X» — нижняя граница диапазона больше X; «< X» — верхняя граница меньше X; марки без элемента в составе проходят «<» только с include_unlisted"`
  - `service.COMPOSITION_RESOURCE = "materials"`
  - `GraphService.find_materials(conditions: list[dict] | None = None, material_class: str | None = None, sort_by: str | None = None, order: str = "desc", limit: int = 20, offset: int = 0) -> ToolResult`
  - `data`: `resource_id`, `total`, `materials: [{"node_id", "name", "class", "base", "contents"}]` (`class` — цепочка через `" / "`), `limit`, `offset`, `has_more`, `next_offset` (`None`, если страниц больше нет), `rules`, `elements_not_in_compositions`. `meta = self._metadata(definition, indexed)`.

Правила (из спецификации, ниже они закреплены тестами):
- Ошибки: `INVALID_CONDITION` — неизвестный `op`; числовой `op` без `value`; `present`/`absent` с `value`; `value` вне 0–100; `include_unlisted` не у `<`/`<=`; нет `element`; больше 10 условий; `order` не `desc`/`asc`. `LIMIT_EXCEEDED` — `limit` вне 1–50 или `offset` вне 0–100000. `UNKNOWN_CLASS` — класс не найден; в сообщении список классов. `INDEX_NOT_READY` и `UNKNOWN_RESOURCE` — через `_ready(COMPOSITION_RESOURCE)`.
- Распознавание элемента: строка обрезается по краям; совпадение с известным символом без учёта регистра (известны символы из составов и из базы элементов); иначе `normalized()` русского названия по `element_symbols()`; иначе строка как есть.
- `> X`: известный диапазон и `low > X`; `>=`: `low >= X`; `<`: `high < X`; `<=`: `high <= X`. Элемент не указан в составе — `<`/`<=` проходят только с `include_unlisted`. Неизвестный диапазон не проходит ни одно числовое условие. `present` — элемент перечислен в любой форме; `absent` — не перечислен. Все условия — через «И».
- `material_class` — совпадение без учёта регистра с любым классом цепочки или со всей цепочкой через `" / "`.
- `sort_by` подразумевает `present`. `desc`: по `low` по убыванию, при равенстве — по `high` по убыванию. `asc` — обратный порядок: по `low`, затем по `high`, по возрастанию. Неизвестный диапазон — в конце при любом порядке; последний ключ — `name`. Без `sort_by` — по алфавиту `name`.
- `contents` — только элементы из условий и `sort_by` (в порядке упоминания, без повторов). Если элемента в составе марки нет: `{"form": FORM_UNLISTED, "low": None, "high": None, "text": FORM_UNLISTED}`.
- `elements_not_in_compositions` — элементы из запроса, которых нет ни в одном составе, в порядке упоминания.
- Страница: `materials[offset:offset+limit]`, затем `self._fit_result(result, "materials", offset)`, который соблюдает `MAX_RESULT_CHARS`.

- [ ] **Step 1: Write the failing service tests** — дописать в `tests/test_composition.py`

```python
from materials_chat.config import Settings
from materials_chat.service import GraphService


def make_service(tmp_path, imported=True):
    config = tmp_path / "resources.json"
    config.write_text(json.dumps([
        {"id": "materials", "title": "Справочник по материалам", "path": "Тест / Материалы", "kind": "information"},
        {"id": "elements", "title": "Элементы", "path": "Тест / Элементы", "kind": "information"},
    ], ensure_ascii=False), encoding="utf-8")
    result = GraphService(Settings(_env_file=None, resources_path=config, data_dir=tmp_path / "data", live_reads=False,
                                   iacpaas_username="", iacpaas_password="", openrouter_api_key=""))
    if imported:
        result.store.import_graph("materials", catalog())
        result.store.import_graph("elements", elements_base())
    return result


@pytest.fixture
def service(tmp_path):
    return make_service(tmp_path)


def find(service, *conditions, **options):
    return service.find_materials(list(conditions), **options)


def names(result):
    assert result.ok, result.error
    return [item["name"] for item in result.data["materials"]]


def test_thresholds_are_guaranteed_by_the_range(service):
    assert names(find(service, {"element": "Cr", "op": ">", "value": 20})) == ["Х21", "Х25"]
    assert names(find(service, {"element": "Cr", "op": ">=", "value": 20})) == ["Х20", "Х21", "Х25"]
    assert names(find(service, {"element": "Fe", "op": "<", "value": 1})) == ["АМг3"]
    assert names(find(service, {"element": "Fe", "op": "<=", "value": 0.5})) == ["АМг3"]
    result = find(service, {"element": "Cr", "op": ">", "value": 20})
    assert (result.data["total"], result.data["has_more"], result.data["next_offset"]) == (2, False, None)
    assert result.data["materials"][0] == {"node_id": "350", "name": "Х21", "class": "Сплавы на основе железа",
        "base": "Fe", "contents": {"Cr": {"form": "Не менее", "low": 21.0, "high": 100.0, "text": "не менее 21.0 %"}}}
    assert result.data["resource_id"] == "materials" and result.data["rules"].startswith("«> X» — нижняя граница")
    assert result.meta["served_from"] == "local_index" and result.meta["indexed_at"]


def test_presence_absence_and_unknown_ranges(service):
    assert names(find(service, {"element": "Cr", "op": "present"}, {"element": "Ni", "op": "absent"})) == \
        ["АМг3", "Х21", "Х25"]
    # «Расчетное» и «совокупность»: элемент есть, диапазон неизвестен — числовые условия не проходят.
    assert names(find(service, {"element": "Ti", "op": "present"})) == ["12Х18Н10Т", "КС25"]
    assert names(find(service, {"element": "Ti", "op": ">", "value": 0})) == []
    assert names(find(service, {"element": "Ni", "op": "<", "value": 1})) == ["Х20"]


def test_unlisted_element_passes_less_than_only_with_the_flag(service):
    result = find(service, {"element": "Ni", "op": "<", "value": 1, "include_unlisted": True})
    assert names(result) == ["АМг3", "Х20", "Х21", "Х25"]
    assert result.data["materials"][0]["contents"]["Ni"] == {"form": "не указан в составе", "low": None,
                                                             "high": None, "text": "не указан в составе"}


def test_sorting_by_element_puts_impurity_limits_last(service):
    top = find(service, sort_by="Cr", limit=3)
    assert names(top) == ["Х25", "Х21", "Х20"] and top.data["total"] == 5 and top.data["has_more"]
    assert names(find(service, sort_by="Cr", order="asc")) == ["АМг3", "12Х18Н10Т", "Х20", "Х21", "Х25"]
    assert names(find(service, sort_by="Ti")) == ["12Х18Н10Т", "КС25"]  # неизвестные диапазоны — в конце


def test_elements_absent_from_all_compositions(service):
    present = find(service, {"element": "Br", "op": "present"})
    assert present.ok and present.data["total"] == 0 and present.data["elements_not_in_compositions"] == ["Br"]
    assert find(service, {"element": "W", "op": "absent"}).data["total"] == 6
    assert find(service, {"element": "Cr", "op": "present"}).data["elements_not_in_compositions"] == []


def test_element_spellings_and_russian_names_are_resolved(service):
    for spelling in ("Cr", "CR", " cr ", "Хром", "хром"):
        result = find(service, {"element": spelling, "op": ">", "value": 20})
        assert names(result) == ["Х21", "Х25"], spelling
        assert set(result.data["materials"][0]["contents"]) == {"Cr"}
    assert names(find(service, {"element": "железо", "op": "<", "value": 1})) == ["АМг3"]
    unknown = find(service, {"element": "унобтаний", "op": "present"})
    assert unknown.ok and unknown.data["elements_not_in_compositions"] == ["унобтаний"]


def test_class_filter_includes_nested_classes(service):
    assert names(find(service, material_class="магнитные")) == ["КС25"]
    assert names(find(service, material_class="Редкоземельные сплавы")) == ["КС25"]
    assert names(find(service, {"element": "Cr", "op": ">", "value": 20}, material_class="сплавы на основе железа")) \
        == ["Х21", "Х25"]
    assert find(service, material_class="Магнитные").data["materials"][0]["class"] == "Редкоземельные сплавы / Магнитные"
    unknown = find(service, material_class="Бронзы")
    assert not unknown.ok and unknown.error.code == "UNKNOWN_CLASS"
    assert "Сплавы на основе алюминия" in unknown.error.message


def test_overview_without_conditions_is_alphabetical_and_paged(service):
    pages, offset = [], 0
    while offset is not None:
        page = find(service, limit=4, offset=offset)
        pages.append(names(page))
        offset = page.data["next_offset"]
    assert pages == [["12Х18Н10Т", "АМг3", "КС25", "Х20"], ["Х21", "Х25"]]
    assert find(service, limit=1).data["materials"][0]["contents"] == {}


@pytest.mark.parametrize("conditions, options", [
    ([{"element": "Cr", "op": ">"}], {}),
    ([{"element": "Cr", "op": "present", "value": 1}], {}),
    ([{"element": "Cr", "op": "~", "value": 1}], {}),
    ([{"element": "Cr", "op": ">", "value": 150}], {}),
    ([{"element": "Cr", "op": ">", "value": 1, "include_unlisted": True}], {}),
    ([{"op": ">", "value": 1}], {}),
    ([{"element": "Cr", "op": "present"}] * 11, {}),
    ([], {"order": "up"}),
])
def test_invalid_conditions_are_diagnostic(service, conditions, options):
    result = service.find_materials(conditions, **options)
    assert not result.ok and result.error.code == "INVALID_CONDITION"


@pytest.mark.parametrize("options", [{"limit": 0}, {"limit": 51}, {"offset": -1}])
def test_paging_limits(service, options):
    result = service.find_materials([], **options)
    assert not result.ok and result.error.code == "LIMIT_EXCEEDED"


def test_index_not_ready(tmp_path):
    result = make_service(tmp_path, imported=False).find_materials([])
    assert not result.ok and result.error.code == "INDEX_NOT_READY"


def test_page_fits_the_result_budget(service):
    service.settings.max_result_chars = 1500
    result = find(service, {"element": "Cr", "op": "present"}, {"element": "Fe", "op": "present"}, limit=50)
    assert result.ok and result.data["has_more"] and result.data["total"] == 5
    assert result.data["next_offset"] == len(result.data["materials"]) < 5
    assert len(json.dumps(result.model_dump(), ensure_ascii=False)) <= 1500
```

- [ ] **Step 2: Run — FAIL** (`AttributeError: find_materials`)

`PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_composition.py -q`

- [ ] **Step 3: Implement** `Condition`, `RULES` и чистые функции отбора в `composition.py` (разбор и проверка условий, распознавание элемента, отбор, сортировка, проекция `contents`), затем `GraphService.find_materials` в `service.py`. Метод синхронный, как `search_graph`; MCP-сервер (задача 4) вызывает его через `asyncio.to_thread`.

- [ ] **Step 4: Run — PASS**

- [ ] **Step 5: Write the real-index acceptance tests** — `tests/test_real_index.py`. Ожидаемое считается **независимо** от `composition.py`: прямым обходом `nodes`/`edges` копии индекса.

```python
"""Готовность find_materials на копии реального индекса; без индекса тесты пропускаются."""
import sqlite3
import time

import pytest

from materials_chat.config import PROJECT_ROOT, Settings
from materials_chat.service import GraphService

REAL_INDEX = PROJECT_ROOT / "data" / "search.sqlite3"
pytestmark = pytest.mark.skipif(not REAL_INDEX.exists(), reason="нет локального индекса data/search.sqlite3")
STEEL, AMG3 = "381405980786784", "381405980787068"  # 12Х18Н10Т и АМг3 — контрольные записи спецификации


@pytest.fixture(scope="module")
def copy(tmp_path_factory):
    folder = tmp_path_factory.mktemp("real-index")
    source = sqlite3.connect(REAL_INDEX.as_uri() + "?mode=ro", uri=True)
    target = sqlite3.connect(folder / "search.sqlite3")
    source.backup(target)  # копия с учётом WAL; рабочий индекс не меняется
    source.close()
    target.close()
    return folder


@pytest.fixture(scope="module")
def service(copy):
    return GraphService(Settings(_env_file=None, data_dir=copy, resources_path=PROJECT_ROOT / "config" / "resources.json",
                                 live_reads=False, iacpaas_username="", iacpaas_password="", openrouter_api_key=""))


@pytest.fixture(scope="module")
def ranges(copy):
    """{символ: {id марки: (низ, верх) или None}} — независимый обход, без composition.py."""
    ...  # см. Step 6


def pages(service, conditions, **options):
    items, offset = [], 0
    while offset is not None:
        result = service.find_materials(conditions, limit=50, offset=offset, **options)
        assert result.ok, result.error
        items += result.data["materials"]
        offset = result.data["next_offset"]
    assert len(items) == result.data["total"] == len({item["node_id"] for item in items})
    return items


def test_cr_greater_than_20_is_guaranteed(service, ranges):
    found = pages(service, [{"element": "Cr", "op": ">", "value": 20}])
    assert all(item["contents"]["Cr"]["low"] > 20 for item in found)
    assert STEEL not in {item["node_id"] for item in found}
    assert {item["node_id"] for item in found} == {key for key, value in ranges["Cr"].items() if value and value[0] > 20}


def test_top_three_by_chromium(service, ranges):
    names = {}  # id → имя, чтобы развести равные диапазоны так же, как сервис
    top = service.find_materials([], sort_by="Cr", limit=3).data["materials"]
    for item in pages(service, [{"element": "Cr", "op": "present"}]):
        names[item["node_id"]] = item["name"]
    known = [(key, value) for key, value in ranges["Cr"].items() if value]
    expected = sorted(known, key=lambda pair: (-pair[1][0], -pair[1][1], names[pair[0]]))[:3]
    assert [item["node_id"] for item in top] == [key for key, _ in expected]


def test_chromium_without_nickel(service, ranges):
    found = {item["node_id"] for item in pages(service, [{"element": "Cr", "op": "present"},
                                                         {"element": "Ni", "op": "absent"}])}
    assert found == set(ranges["Cr"]) - set(ranges["Ni"])


def test_iron_at_least_25_pages_add_up(service, ranges):
    found = pages(service, [{"element": "Fe", "op": ">=", "value": 25}])
    assert {item["node_id"] for item in found} == {key for key, value in ranges["Fe"].items() if value and value[0] >= 25}


def test_bromine_is_not_in_any_composition(service):
    result = service.find_materials([{"element": "Br", "op": "present"}])
    assert result.data["total"] == 0 and "Br" in result.data["elements_not_in_compositions"]


def test_control_records(service):
    steel = next(item for item in pages(service, [{"element": "Cr", "op": "present"}, {"element": "C", "op": "present"}])
                 if item["node_id"] == STEEL)
    assert (steel["contents"]["Cr"]["low"], steel["contents"]["Cr"]["high"]) == (17.0, 19.0)
    assert (steel["contents"]["C"]["low"], steel["contents"]["C"]["high"]) == (0.0, 0.12)
    amg3 = next(item for item in pages(service, [{"element": "Mg", "op": "present"}, {"element": "Fe", "op": "present"}])
                if item["node_id"] == AMG3)
    assert (amg3["contents"]["Mg"]["low"], amg3["contents"]["Mg"]["high"]) == (3.2, 3.8)
    assert (amg3["contents"]["Fe"]["low"], amg3["contents"]["Fe"]["high"]) == (0.0, 0.5)


def test_unlisted_nickel_adds_grades_without_nickel(service, ranges):
    listed = service.find_materials([{"element": "Ni", "op": "<", "value": 1}]).data["total"]
    flagged = service.find_materials([{"element": "Ni", "op": "<", "value": 1, "include_unlisted": True}]).data["total"]
    everything = service.find_materials([]).data["total"]
    assert flagged == listed + everything - len(ranges["Ni"])


def test_query_over_all_grades_is_fast(service):
    service.find_materials([])  # таблица построена
    started = time.perf_counter()
    service.find_materials([{"element": "Cr", "op": ">", "value": 20}], sort_by="Cr")
    assert time.perf_counter() - started < 1
```

- [ ] **Step 6: Implement the `ranges` fixture.** Независимый обход: для каждой вершины `meta «Химический элемент»` поднимаемся по `edges` до вершины `«Материал»` (`элемент → «Компонент»` или `элемент → «Совокупность…» → «Компонент»`, затем `→ «Элементный состав» → марка`). Вершины под `«Основа»` и `«Расчетное»` не считаются. Диапазон определяется по `meta` потомков, как в таблице спецификации; для совокупности, «Расчетного» и записи без значения — `None`.

- [ ] **Step 7: Run — PASS** (на машине с индексом; без индекса — `skipped`)

`PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_real_index.py tests/test_composition.py -q`

- [ ] **Step 8: Commit** — `find_materials: подбор марок по составу в GraphService`

---

### Task 4: MCP-инструмент, описания и промпт

**Files:**
- Modify: `materials_chat/mcp_server.py`, `materials_chat/prompts.py`, `config/data_notes.json`
- Test: `tests/test_composition.py` (дописать), `tests/test_full_stack.py` (одно утверждение)

**Interfaces:**
- Consumes: `composition.Condition`, `GraphService.find_materials` (задача 3).
- Produces: MCP-инструмент `find_materials(conditions: list[Condition] | None = None, material_class: str | None = None, sort_by: str | None = None, order: str = "desc", limit: int = 20, offset: int = 0) -> ToolResult`; внутри вызывает `asyncio.to_thread(service.find_materials, [item.model_dump() for item in conditions or []], …)`.

Docstring `find_materials` (это описание для модели) должен сказать:
- когда вызывать: подбор марок справочника по составу — пороги «больше/меньше X %», топ, максимум или минимум по элементу (`sort_by` + `limit`), есть или нет элемента, класс; количество марок — `total`;
- все условия соединяются через «И», для «или» нужно несколько вызовов;
- сравнение гарантированное: «> X» — нижняя граница диапазона больше X, «< X» — верхняя меньше X («не более 0.5» — это [0; 0.5]);
- `present` — элемент перечислен в любой форме, включая «не более»; гарантированное присутствие — `op ">"`, `value 0`;
- `include_unlisted` у `<`/`<=` — добавить марки, в составе которых элемент не указан;
- `elements_not_in_compositions` — элемент не встречается ни в одном составе;
- источник — локальный снимок (`indexed_at`); запись марки читается через `get_subgraph(resource_id="materials", root=node_id, depth=8, max_nodes=150)`;
- `limit` 1–50, при `has_more` продолжать с `next_offset`.

К docstring `search_graph` добавить: `limit` — до 20 (по умолчанию 8); поиск идёт по полной копии ресурсов, поэтому `has_more: false` значит «найдено всё по этому запросу»; при `has_more` продолжать с `next_offset`.

Правки `SYSTEM_PROMPT`:
- В «Когда нужны инструменты»: вопросы о марках по составу (больше или меньше X % элемента, топ, максимум, минимум или количество марок по элементу, есть или нет элемента, в каком классе) решаются через `find_materials`, а нужные записи потом читаются через `get_subgraph`. Остальные списки дочитываются по страницам до `has_more: false`.
- В «Как работать с данными»: правила `find_materials` (гарантированные пороги, `present` против `> 0`, когда ставить `include_unlisted` — например, «где никеля меньше 1 %», и что говорить при `elements_not_in_compositions`: «в составах не встречается»). Строку про `has_more` заменить: `has_more: true` — показана часть, `has_more: false` — найдено всё. Добавить: индекс — полная копия ресурсов, оговорку «записи могли не попасть в индекс» не писать; если сведения есть лишь у части записей — писать «заполнено у N записей».
- Примеры вопросов при неясном запросе и при отказе брать только из тех, на которые в данных есть ответ: «Какой химический состав у стали 12Х18Н10Т?», «Какие марки относятся к классу «Бронзы»?», «Какие марки содержат больше 20 % хрома?».

В `config/data_notes.json` → `suggested_questions` вставить вторым пунктом `{"text": "Какие марки содержат больше 20 % хрома?", "resources": ["materials"]}`.

- [ ] **Step 1: Write the failing tests** — дописать в `tests/test_composition.py`

```python
from materials_chat.mcp_server import create_server
from materials_chat.prompts import SYSTEM_PROMPT


async def test_mcp_lists_find_materials_with_its_rules(tmp_path):
    settings = make_service(tmp_path).settings
    tools = {tool.name: tool for tool in await create_server(settings).list_tools()}
    assert len(tools) == 7
    assert {"conditions", "material_class", "sort_by", "order", "limit", "offset"} <= \
        set(tools["find_materials"].inputSchema["properties"])
    assert "include_unlisted" in json.dumps(tools["find_materials"].inputSchema, ensure_ascii=False)
    description = tools["find_materials"].description
    assert "get_subgraph" in description and "include_unlisted" in description and "next_offset" in description
    assert "до 20" in tools["search_graph"].description and "has_more: false" in tools["search_graph"].description


async def test_mcp_find_materials_returns_the_envelope(tmp_path):
    settings = make_service(tmp_path).settings
    _, structured = await create_server(settings).call_tool(
        "find_materials", {"conditions": [{"element": "Cr", "op": ">", "value": 20}], "sort_by": "Cr"})
    assert structured["ok"] and [item["name"] for item in structured["data"]["materials"]] == ["Х25", "Х21"]


def test_prompt_routes_composition_questions_to_find_materials():
    assert "find_materials" in SYSTEM_PROMPT and "has_more: false" in SYSTEM_PROMPT
    assert "заполнено у N записей" in SYSTEM_PROMPT
```

В `tests/test_full_stack.py::test_http_chat_really_uses_mcp_and_counts_all_model_requests` после проверки `tool_choice` добавить — схема условий доходит до модели через `langchain-mcp-adapters`:

```python
        tools = {tool["function"]["name"]: tool["function"] for tool in requests[0]["tools"]}
        assert "include_unlisted" in json.dumps(tools["find_materials"]["parameters"], ensure_ascii=False)
```

- [ ] **Step 2: Run — FAIL**

`PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_composition.py tests/test_full_stack.py -q`

- [ ] **Step 3: Implement** — инструмент в `mcp_server.py`, docstring обоих инструментов, правки промпта и `data_notes.json`.

- [ ] **Step 4: Run all tests — PASS**

`PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q`

- [ ] **Step 5: Commit** — `MCP-инструмент find_materials, описание search_graph и правила промпта`

---

### Task 5: Журнал инструментов в интерфейсе

**Files:**
- Modify: `materials_chat/static/trace.js`

**Interfaces:**
- Consumes: аргументы и `data` `find_materials` (задачи 3–4).
- Produces: подписи журнала; экспортов не добавляется.

- [ ] **Step 1: `TOOL_ACTIVITY.find_materials = 'Подбирает марки по составу'`.**
- [ ] **Step 2: `describeArguments('find_materials', args)`** — части через `' · '`, в порядке: условия, класс, сортировка. Числовое условие: `Cr > 20`; `>=`/`<=` показываются как `≥`/`≤`; к условию с `include_unlisted` добавляется ` (и без элемента)`. `present` → `есть Mo`, `absent` → `нет Ni`. Класс — как есть. `sort_by` при `desc` → `топ по Cr`, при `asc` → `по возрастанию Cr`. Пример из спецификации: `Cr > 20 · нет Ni · Сплавы на основе железа`.
- [ ] **Step 3: `describeResult('find_materials', result)`** — `plural(total, ['марка', 'марки', 'марок'])` или `нет марок`, плюс `, есть ещё` при `has_more`.
- [ ] **Step 4: Fix `sourceLabel`** (`trace.js:195`): у `meta.live_error` берётся `message`, при его отсутствии — `code`; строка показывается как есть (старые записи).
- [ ] **Step 5: Verify syntax** — `node --check materials_chat/static/trace.js` → без вывода, код 0.
- [ ] **Step 6: Commit** — `Журнал: подпись find_materials и текст ошибки платформы в подсказке`

---

### Task 6: Сверка чисел ответа с данными

**Files:**
- Create: `materials_chat/answer_check.py`
- Test: `tests/test_answer_check.py` (новый)

**Interfaces:**
- Produces:
  - `extract_numbers(text: str) -> list[tuple[str, Decimal]]` — пары «как записано в тексте, абсолютное значение» в порядке появления.
  - `data_numbers(traces: list[dict]) -> set[Decimal]` — числа из полей данных успешных результатов (`result.ok`).
  - `check_numbers(answer: str, question: str, traces: list[dict]) -> dict` — `{"status": "checked" | "no_data" | "no_numbers", "checked": int, "confirmed": int, "unconfirmed": list[str]}`.

Правила:
- **Извлечение.** Числа с точкой или запятой; разделитель тысяч — пробел, ` ` или ` ` между группами по три цифры (`2 670`); диапазоны `17–19` и `17-19` дают два числа; знак отбрасывается (`−196`, `-196` → 196). Не считаются: маркеры нумерованного списка в начале строки (`1.`, `2)`), числа в «словах» с буквами (`12Х18Н10Т`, `АК4-1`, `Б83`, `Д16` — слово из букв и цифр, соединённых дефисом, пропускается целиком), даты `21.10.2026` и `2026-10-18`. Точка или запятая после числа без цифры за ней в число не входит.
- **Сравнение** — по значению (`Decimal`): `17`, `17.0`, `17,0` — одно число. Повторы одного значения считаются один раз, в `unconfirmed` попадает первая запись.
- **Данные.** Обход `result["data"]` успешных записей журнала (рекурсивно по словарям и спискам). Числа берутся только из значений ключей `DATA_KEYS = {"label", "value", "comment", "name", "path", "snippet", "node_id", "id", "root", "total", "nodes", "edges", "contents", "low", "high", "text", "base", "class", "title"}`. Скаляр в списке относится к ключу списка; строка разбирается через `extract_numbers`, число берётся как есть. Служебные поля (`limit`, `offset`, `next_offset`, `children_not_loaded`, `total_within_depth`, `indexed_at`, `fetched_at`), поля записи журнала (`duration_ms`) и `meta` не учитываются. Записи без `result` или с `result` не-словарём пропускаются.
- **Исключения.** Числа из вопроса и из аргументов всех переданных вызовов (рекурсивно: строки — через `extract_numbers`, числа — как есть) убираются из ответа до подсчёта.
- **Статус.** Чисел не осталось — `no_numbers` (`checked` 0). В журнале нет ни одного успешного результата — `no_data` (`checked` — сколько чисел, `confirmed` 0, `unconfirmed` пуст). Иначе `checked`.

- [ ] **Step 1: Write the failing tests** — `tests/test_answer_check.py`

```python
"""Сверка чисел ответа с данными инструментов: чистые функции, без сети."""
from decimal import Decimal

from materials_chat.answer_check import check_numbers, data_numbers, extract_numbers

SEARCH = {"id": "s", "name": "search_graph", "arguments": {"query": "12Х18Н10Т", "limit": 3}, "is_error": False,
          "duration_ms": 9500, "result": {"ok": True, "data": {
              "hits": [{"node_id": "381405980786784", "label": "12Х18Н10Т", "path": "Справочник/12Х18Н10Т",
                        "comment": "плотность 7900 кг/м³"}],
              "total": 24, "limit": 3, "offset": 0, "next_offset": 10300},
              "meta": {"indexed_at": "2026-10-18T09:59:24+00:00"}}}
FIND = {"id": "f", "name": "find_materials", "arguments": {"conditions": [{"element": "Cr", "op": ">", "value": 20}]},
        "is_error": False, "result": {"ok": True, "data": {"total": 2, "materials": [
            {"node_id": "200", "name": "Х25", "contents": {"Cr": {"form": "Числовой интервал", "low": 23.0,
                                                                  "high": 27.0, "text": "23.0–27.0 %"}}}]}}}
FAILED = {"id": "x", "name": "get_subgraph", "arguments": {}, "is_error": True,
          "result": {"ok": False, "error": {"code": "NODE_NOT_FOUND", "message": "Вершина 555 не найдена"}}}


def written(text):
    return [raw for raw, _ in extract_numbers(text)]


def test_numbers_are_extracted_as_written():
    assert written("Cr 17–19 %, C не более 0,12, плотность 7 900 кг/м³, всего 2 670 вершин.") == \
        ["17", "19", "0,12", "7 900", "2 670"]
    assert written("Диапазон 17-19 и −196 °C, итог 19.") == ["17", "19", "196", "19"]
    assert written("**17.0** в вершине `381405980786784`, Fe≤0,5, Ti до 5·C") == ["17.0", "381405980786784", "0,5", "5"]


def test_list_markers_grade_names_and_dates_are_skipped():
    assert written("1. Марка 12Х18Н10Т — хром 17.0\n2) АК4-1, Б83 и Д16\n- снимок от 21.10.2026 и 2026-10-18") == ["17.0"]


def test_values_compare_by_number():
    assert {value for _, value in extract_numbers("17 17.0 17,0")} == {Decimal("17")}
    assert extract_numbers("2 670")[0][1] == Decimal("2670")


def test_only_data_fields_count():
    numbers = data_numbers([SEARCH, FIND, FAILED, {"id": "old", "name": "search_graph"}])
    assert {Decimal(item) for item in ("381405980786784", "7900", "24", "23", "27", "2")} <= numbers
    assert not {Decimal(item) for item in ("3", "0", "10300", "9500", "555", "2026")} & numbers


def test_answer_numbers_are_checked_against_data():
    answer = ("Хрома больше 20 % у 2 марок: **Х25** (23–27 %). Плотность 12Х18Н10Т — 7 900 кг/м³, "
              "вершина 381405980786784. Показаны 3 из 24.")
    assert check_numbers(answer, "Где хрома больше 20 %? Покажи 3", [SEARCH, FIND]) == \
        {"status": "checked", "checked": 6, "confirmed": 6, "unconfirmed": []}


def test_missing_numbers_are_listed_once_as_written():
    result = check_numbers("Прочность 9500 МПа, после отжига 10 300 МПа; снова 9500.", "Прочность?", [SEARCH])
    assert result == {"status": "checked", "checked": 2, "confirmed": 0, "unconfirmed": ["9500", "10 300"]}


def test_no_data_and_no_numbers():
    assert check_numbers("Хром — 17 %.", "Хром?", []) == {"status": "no_data", "checked": 1, "confirmed": 0,
                                                          "unconfirmed": []}
    assert check_numbers("Хром — 17 %.", "Хром?", [FAILED])["status"] == "no_data"
    assert check_numbers("Здравствуйте! Спросите о сплавах.", "Привет", [SEARCH])["status"] == "no_numbers"
    assert check_numbers("Вы спрашивали про 20 %.", "Больше 20 %?", [SEARCH])["status"] == "no_numbers"
```

- [ ] **Step 2: Run — FAIL** (`ModuleNotFoundError`)

`PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_answer_check.py -q`

- [ ] **Step 3: Implement `answer_check.py`.** Подход: сначала закрыть пробелами даты и маркеры списков; затем разбить текст на «слова» — буквы и цифры (`[^\W_]`), соединённые `-`, `.` или `,`, с отдельной обработкой групп тысяч. Слово с буквами пропускается, остальные делятся по дефису на числа.

- [ ] **Step 4: Run — PASS**

- [ ] **Step 5: Commit** — `Сверка чисел ответа с данными инструментов`

---

### Task 7: Сверка в конце хода — хранение и поток

**Files:**
- Modify: `materials_chat/sessions.py`, `materials_chat/web.py`
- Test: `tests/test_agent_backend.py`

**Interfaces:**
- Consumes: `answer_check.check_numbers` (задача 6).
- Produces:
  - Колонка `messages.number_check TEXT` (миграция); `SessionStore.save_turn(..., error=None, number_check: dict | None = None)`; `get()` отдаёт `message["number_check"]` (словарь или `None`).
  - `SessionStore.traces(session_id: str) -> list[dict]` — записи журнала всех ответов диалога по порядку.
  - SSE-событие `number_check` с результатом `check_numbers` — после `error` (если он есть) и до `usage`.
  - Тестовые помощники в `tests/test_agent_backend.py` (их использует задача 10): `make_app(tmp_path) -> FastAPI`, `scripted_agent(turns: list[tuple[str, list]]) -> type`, `stream_events(text: str) -> list[tuple[str, dict]]`.

В `web.py::chat` в блоке `finally`, перед `save_turn`: `number_check = check_numbers(content, text, [*store.traces(session_id), *result.trace])`, затем сохранить вместе с ходом. Событие отправляется после блока `finally`, рядом с `error`.

- [ ] **Step 1: Write the failing tests** (в `tests/test_agent_backend.py`)

Помощники (рядом с существующими web-тестами):

```python
def make_app(tmp_path):
    from materials_chat.config import Settings
    from materials_chat.web import create_app
    # Адрес провайдера недоступен: тест не ходит в сеть за каталогом моделей.
    return create_app(Settings(_env_file=None, data_dir=tmp_path, history_path=tmp_path / "history.sqlite3",
                               auto_sync_on_start=False, openrouter_api_key="test-fake-never-sent",
                               openrouter_model="fake", openrouter_base_url="http://127.0.0.1:9"))


def scripted_agent(turns):
    """Подменяет Agent: каждый вызов отдаёт следующий заданный ход (текст ответа и журнал)."""
    from materials_chat.agent import TurnResult
    queue = list(turns)

    class ScriptedAgent:
        def __init__(self, _settings, model_info=None):
            self.result = TurnResult()

        async def run(self, text, history):
            self.result.content, self.result.trace = queue.pop(0)
            self.result.model_messages = [{"role": "assistant", "content": self.result.content}]
            yield "answer_delta", {"text": self.result.content}

    return ScriptedAgent


def stream_events(text):
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events
```

Тест:

```python
def test_number_check_uses_this_and_earlier_turns(tmp_path):
    from fastapi.testclient import TestClient
    data = [{"id": "t", "name": "search_graph", "arguments": {"query": "хром"}, "is_error": False,
             "result": {"ok": True, "data": {"hits": [{"label": "Cr", "value": "17.0–19.0 %"}]}}}]
    app = make_app(tmp_path)
    app.state.agent_factory = scripted_agent([("Хром 17–19 %, плотность 9500.", data),
                                              ("Напомню: хром 17–19 %.", [])])
    with TestClient(app) as client:
        session_id = client.post("/api/sessions").json()["id"]
        first = stream_events(client.post(f"/api/sessions/{session_id}/chat", json={"message": "Хром?"}).text)
        names = [name for name, _ in first]
        assert names.index("number_check") < names.index("usage")
        assert dict(first)["number_check"] == {"status": "checked", "checked": 3, "confirmed": 2, "unconfirmed": ["9500"]}
        second = stream_events(client.post(f"/api/sessions/{session_id}/chat", json={"message": "Повтори"}).text)
        # Ответ опирается на данные прошлого хода — числа подтверждены.
        assert dict(second)["number_check"] == {"status": "checked", "checked": 2, "confirmed": 2, "unconfirmed": []}
        saved = client.get(f"/api/sessions/{session_id}").json()["messages"]
        assert saved[1]["number_check"] == dict(first)["number_check"] and saved[0]["number_check"] is None
```

В `test_old_history_database_is_migrated` добавить: `assert detail["messages"][0]["number_check"] is None`.

- [ ] **Step 2: Run — FAIL**

`PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_agent_backend.py -q`

- [ ] **Step 3: Implement** — миграция, `save_turn`, `get`, `traces` в `sessions.py`; вызов сверки и событие в `web.py`.
- [ ] **Step 4: Run all tests — PASS**

`PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q`

- [ ] **Step 5: Commit** — `Сверка чисел сохраняется с ответом и приходит событием number_check`

---

### Task 8: Строка сверки и подсветка в интерфейсе

**Files:**
- Create: `materials_chat/static/checks.js`
- Modify: `materials_chat/static/app.js`, `materials_chat/static/styles.css`

**Interfaces:**
- Consumes: событие `number_check` и поле `message.number_check` (задача 7).
- Produces (`checks.js`; задача 11 дописывает сюда же):
  - `numberCheckNode(check: object | null) -> HTMLElement | null`
  - `highlightNumbers(container: HTMLElement, numbers: string[]) -> void`

Тексты:
- `checked` без ненайденных: `Числа сверены с данными: 12 из 12`;
- `checked` с ненайденными: `Не найдены в данных: 9500, 10 300 — проверьте`, класс предупреждения; `title` — `Сверено с данными: 10 из 12. Сюда попадают и числа, вычисленные моделью (пересчёт единиц, суммы), — это не обязательно ошибка.`;
- `no_data`: `нет данных для сверки`;
- `no_numbers` или `null` (старые сообщения) — ничего.

- [ ] **Step 1: `checks.js`.** `numberCheckNode` строит `<span>` по правилам выше. `highlightNumbers` обходит текстовые узлы контейнера (`TreeWalker`, `SHOW_TEXT`) и оборачивает каждое вхождение строки из `numbers` в `<mark class="number-unconfirmed">`. Совпадение — только целым числом: слева и справа нет буквы или цифры, справа нет `[.,]` с цифрой. Только DOM-узлы, без `innerHTML`, как в `markdown.js`.
- [ ] **Step 2: `app.js`.** Поле `view.numberCheck`; `case 'number_check'` в `handleStreamEvent`; `showSavedAnswer` берёт `message.number_check`. `renderAnswerMeta` ставит строку сверки перед «Копировать». После `renderMarkdown` в `finishAnswer` и `showSavedAnswer` — `highlightNumbers(view.body, view.numberCheck?.unconfirmed ?? [])`; событие `number_check` приходит до `done`, так что к `finishAnswer` оно уже известно. `finishAnswer` кладёт в `state.session.messages` и `number_check`. Шапку модулей в начале `app.js` дополнить строкой о `checks.js`.
- [ ] **Step 3: `styles.css`.** `.number-check.is-warning` — цвет `var(--warning)`; `mark.number-unconfirmed` — фон `var(--warning-soft)`, цвет текста наследуется. Для тёмной темы — те же токены (проверить, что они переопределены в тёмной схеме файла).
- [ ] **Step 4: Verify syntax** — `node --check materials_chat/static/checks.js` и `node --check materials_chat/static/app.js` → код 0.
- [ ] **Step 5: Commit** — `Интерфейс: строка сверки чисел и подсветка ненайденных`

---

### Task 9: Агент-проверяющий

**Files:**
- Create: `materials_chat/verifier.py`
- Modify: `materials_chat/config.py`, `materials_chat/prompts.py`, `tests/test_agent_backend.py` (помощник `verdict_reply`)
- Test: `tests/test_verifier.py` (новый)

**Interfaces:**
- Consumes: `create_chat_model`, `as_provider_error`, `ProviderError` (`openrouter.py`); `context_budget` (`agent.py`); `SessionStore.estimate_tokens`; `request_usage`.
- Produces:
  - `Settings.verifier_model: str = Field(default="", validation_alias=AliasChoices("VERIFIER_MODEL", "verifier_model"))`.
  - `prompts.VERIFIER_PROMPT`.
  - `verifier.Claim(BaseModel)`: `text: str` (непустой), `status: Literal["confirmed", "not_in_data", "contradicted"]`, `node_id: str | None = None`, `evidence: str = ""`; `verifier.VerdictReport(BaseModel)`: `claims: list[Claim]`.
  - `verifier_model_name(settings) -> str` — `getattr(settings, "verifier_model", "") or settings.openrouter_model`.
  - `summarize(claims: list[Claim]) -> dict` — `{"confirmed", "total", "percent", "verdict"}`. `percent = round(100 * confirmed / total)`. Вердикт по точной доле: все подтверждены — `«подтверждён»`, от 0,7 — `«частично подтверждён»`, ниже — `«не подтверждён»`. Нет утверждений — `percent: None`, вердикт `«нет утверждений о данных»`.
  - `verifier_messages(question: str, answer: str, traces: list[dict]) -> list[dict]` — `[system: VERIFIER_PROMPT, user: вопрос, ответ и данные]`. Данные — JSON только успешных записей: `{"tool", "arguments", "data", "served_from"}`.
  - `async verify_answer(settings, question: str, answer: str, traces: list[dict], model=None, model_info: dict | None = None) -> dict` — `{**summarize(...), "claims": [claim.model_dump() …], "usage": dict, "model": str, "created_at": ISO-время}`.
  - Коды `ProviderError`: `no_data` (нет успешных результатов — модель не вызывается), `context_limit` (оценка `estimate_tokens(messages)` больше `context_budget(settings, model_info)` — до запроса), `verifier_bad_output` (сообщение `«Проверяющий вернул ответ в неожиданном формате. Повторите проверку.»`); ошибки провайдера — через `as_provider_error`.
  - Помощник в `tests/test_agent_backend.py`: `verdict_reply(claims: list[dict]) -> list` — поток одного вызова `submit_verdict`.

Вызов: `model or create_chat_model(settings, model=verifier_model_name(settings))`, затем `bind_tools([REPORT_TOOL], tool_choice="required")`. `REPORT_TOOL` — `convert_to_openai_tool(VerdictReport)` с именем функции `submit_verdict`. Ответ собирается из `astream` сложением чанков, как в `test_model_provider.py`; так `RouterAIChat` сохраняет `provider_usage` со стоимостью. Ответ разбирается из первого `tool_calls`. Если вызова нет, текст разбирается как JSON (обрамление ```` ``` ```` снимается); не получилось — `verifier_bad_output`. Расход — `request_usage(message.response_metadata.get("provider_usage"), model_info)`.

`VERIFIER_PROMPT` задаёт правила:
- судить только по переданным данным из базы, своих знаний не использовать;
- разбить ответ на атомарные утверждения о данных: числа и диапазоны, принадлежность элемента или свойства марке, марки классу, наличие или отсутствие сведений («не найдено»), количество;
- реплики о диалоге, вежливость и предложения помощи пропускать;
- `confirmed` — следует из данных (опора: `node_id` и короткая выдержка); `not_in_data` — нет ни подтверждения, ни опровержения; `contradicted` — данные говорят другое (с опорой);
- вычисленное (пересчёт единиц, сумма, рейтинг, количество) — `confirmed`, если выводится из данных арифметически; в `evidence` — короткий расчёт;
- «не найдено» — `confirmed`, только если в данных есть успешный поиск без совпадений по этому запросу;
- отвечать только вызовом `submit_verdict`.

- [ ] **Step 1: Write the failing tests.** Помощник — в `tests/test_agent_backend.py`, рядом с `tool_reply`:

```python
def verdict_reply(claims):
    call = {"index": 0, "id": "verdict", "type": "function", "function": {
        "name": "submit_verdict", "arguments": json.dumps({"claims": claims}, ensure_ascii=False)}}
    return [event({"role": "assistant", "tool_calls": [call]}, "tool_calls")]
```

`tests/test_verifier.py`:

```python
"""Агент-проверяющий на двойнике RouterAI: без сети и платных запросов."""
import asyncio
import json

import pytest

from materials_chat.openrouter import ProviderError
from materials_chat.verifier import Claim, summarize, verifier_model_name, verify_answer
from test_agent_backend import PRICES, Provider, settings, text_reply, verdict_reply

TRACE = [{"id": "t1", "name": "find_materials", "is_error": False,
          "arguments": {"conditions": [{"element": "Cr", "op": ">", "value": 20}]},
          "result": {"ok": True, "meta": {"served_from": "local_index"}, "data": {"total": 1, "materials": [
              {"node_id": "200", "name": "Х25", "contents": {"Cr": {"low": 23.0, "high": 27.0, "text": "23.0–27.0 %"}}}]}}},
         {"id": "t2", "name": "get_subgraph", "arguments": {}, "is_error": True,
          "result": {"ok": False, "error": {"code": "NODE_NOT_FOUND", "message": "нет вершины"}}}]
CLAIMS = [{"text": "У Х25 хрома 23–27 %", "status": "confirmed", "node_id": "200", "evidence": "23.0–27.0 %"},
          {"text": "Х25 — нержавеющая сталь", "status": "not_in_data", "node_id": None, "evidence": ""},
          {"text": "Хрома больше 20 % у одной марки", "status": "confirmed", "node_id": None, "evidence": "total: 1"}]


def verify(provider, config, traces=TRACE, **kwargs):
    return asyncio.run(verify_answer(config, "Где хрома больше 20 %?", "Ответ про Х25.", traces,
                                     model=provider.model(config), **kwargs))


def test_verdict_counts_claims_and_cost():
    provider = Provider(verdict_reply(CLAIMS))
    report = verify(provider, settings(), model_info=PRICES)
    assert (report["confirmed"], report["total"], report["percent"], report["verdict"]) == (2, 3, 67, "не подтверждён")
    assert report["claims"][0] == CLAIMS[0] and report["model"] == "local-model" and report["created_at"]
    assert report["usage"]["total_tokens"] == 20
    assert report["usage"]["cost"] == pytest.approx((12 * 5 + 8 * 10) / 1_000_000)
    body = provider.requests[0]
    assert body["tool_choice"] == "required"
    assert [tool["function"]["name"] for tool in body["tools"]] == ["submit_verdict"]
    sent = json.dumps(body["messages"], ensure_ascii=False)
    assert "Где хрома больше 20 %?" in sent and "Ответ про Х25." in sent and "23.0–27.0 %" in sent
    assert "NODE_NOT_FOUND" not in sent  # неудачные вызовы — не данные


def test_summary_thresholds():
    def claims(*statuses):
        return [Claim(text="утверждение", status=status) for status in statuses]
    assert summarize(claims("confirmed", "confirmed"))["verdict"] == "подтверждён"
    assert summarize(claims(*["confirmed"] * 7, *["contradicted"] * 3)) == \
        {"confirmed": 7, "total": 10, "percent": 70, "verdict": "частично подтверждён"}
    assert summarize([]) == {"confirmed": 0, "total": 0, "percent": None, "verdict": "нет утверждений о данных"}


def test_verifier_model_defaults_to_the_agent_model():
    assert verifier_model_name(settings()) == "local-model"
    assert verifier_model_name(settings(verifier_model="other/model")) == "other/model"


def test_plain_json_answer_is_accepted():
    provider = Provider(text_reply(json.dumps({"claims": CLAIMS[:1]}, ensure_ascii=False)))
    assert verify(provider, settings())["verdict"] == "подтверждён"


@pytest.mark.parametrize("case, code", [("no_data", "no_data"), ("garbage", "verifier_bad_output"),
                                        ("oversized", "context_limit")])
def test_clear_errors(case, code):
    provider = Provider(text_reply("Всё верно!"))
    config = settings(max_context_tokens=50) if case == "oversized" else settings()
    with pytest.raises(ProviderError) as error:
        verify(provider, config, traces=[TRACE[1]] if case == "no_data" else TRACE)
    assert error.value.code == code
    if case == "garbage":
        assert "Повторите проверку" in str(error.value)
    else:
        assert not provider.requests  # до модели дело не дошло
```

- [ ] **Step 2: Run — FAIL**

`PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_verifier.py -q`

- [ ] **Step 3: Implement** — `verifier_model` в `config.py`, `VERIFIER_PROMPT` в `prompts.py`, модуль `verifier.py`.
- [ ] **Step 4: Run all tests — PASS**

`PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q`

- [ ] **Step 5: Commit** — `Агент-проверяющий ответа по данным диалога`

---

### Task 10: Эндпоинт «Проверить» и хранение вердикта

**Files:**
- Modify: `materials_chat/sessions.py`, `materials_chat/web.py`
- Test: `tests/test_agent_backend.py`

**Interfaces:**
- Consumes: `verify_answer`, `verifier_model_name` (задача 9); `make_app`, `scripted_agent`, `stream_events` (задача 7); `verdict_reply`, `Provider` (задача 9 и существующие).
- Produces:
  - Колонка `messages.verification TEXT` (миграция). `get()` отдаёт `message["verification"]` (словарь или `None`).
  - `SessionStore.turn(session_id: str, message_id: str) -> dict | None` — `{"question", "answer", "traces"}` для ответа ассистента этого диалога. `question` — ближайшее предыдущее сообщение пользователя, `traces` — журналы всех ответов до этого включительно. Для чужого id, id сообщения пользователя или несуществующего id — `None`.
  - `SessionStore.save_verification(message_id: str, verification: dict) -> None` — повторная проверка заменяет вердикт. Деньги за прежние проверки не теряются: в сохранённом словаре есть поле `usage_total = add_usage(прежний usage_total или usage, новый usage)`.
  - Сумма диалога в `get()` (и потому в `list()`) прибавляет `usage_total` вердиктов.
  - `app.state.verifier = verify_answer`, `app.state.locks = locks` — подмена в тестах.
  - `POST /api/sessions/{session_id}/messages/{message_id}/verify` → `200 {"message_id", "verification", "session_usage"}`. Ошибки: `404` «Ответ не найден.» (нет диалога или ответа); `503`, если не настроены ключ или модель (как в `chat`); `409` «В этом диалоге сейчас формируется ответ. Проверьте после его завершения.»; `ProviderError` — `422` (`no_data`), `413` (`context_limit`), иначе `502`; тело `{"detail": текст ошибки}`.
  - `model_info` для оценки цены — `await current_model_info()`, если `verifier_model_name(settings) == settings.openrouter_model`, иначе `None`: цены другой модели неизвестны, стоимость тогда — только из `usage.cost` провайдера.

- [ ] **Step 1: Write the failing tests** (в `tests/test_agent_backend.py`)

```python
FIND_ENTRY = {"id": "f", "name": "find_materials", "arguments": {"sort_by": "Cr", "limit": 1}, "is_error": False,
              "result": {"ok": True, "data": {"total": 5, "materials": [
                  {"node_id": "200", "name": "Х25", "contents": {"Cr": {"low": 23.0, "high": 27.0, "text": "23.0–27.0 %"}}}]}}}


def with_verifier(app, provider):
    from materials_chat.verifier import verify_answer
    app.state.verifier = lambda config, *args, **kwargs: verify_answer(config, *args, model=provider.model(config),
                                                                       **kwargs)


def test_verify_saves_the_verdict_and_adds_its_cost(tmp_path):
    from fastapi.testclient import TestClient
    app = make_app(tmp_path)
    app.state.agent_factory = scripted_agent([("Больше всего хрома у Х25: 23–27 %.", [FIND_ENTRY])])
    provider = Provider(verdict_reply([{"text": "У Х25 хрома 23–27 %", "status": "confirmed",
                                        "node_id": "200", "evidence": "23.0–27.0 %"}]))
    with_verifier(app, provider)
    with TestClient(app) as client:
        session_id = client.post("/api/sessions").json()["id"]
        client.post(f"/api/sessions/{session_id}/chat", json={"message": "У какой марки больше всего хрома?"})
        answer = client.get(f"/api/sessions/{session_id}").json()["messages"][1]
        assert answer["verification"] is None
        url = f"/api/sessions/{session_id}/messages/{answer['id']}/verify"
        body = client.post(url).json()
        assert (body["verification"]["verdict"], body["verification"]["percent"]) == ("подтверждён", 100)
        assert "У какой марки больше всего хрома?" in json.dumps(provider.requests[0], ensure_ascii=False)
        saved = client.get(f"/api/sessions/{session_id}").json()
        assert saved["messages"][1]["verification"]["claims"] == body["verification"]["claims"]
        assert saved["usage"]["total_tokens"] == body["session_usage"]["total_tokens"] == 20
        again = client.post(url).json()  # повторная проверка заменяет вердикт, но расход копится
        assert again["session_usage"]["total_tokens"] == 40
        assert client.get(f"/api/sessions/{session_id}").json()["usage"]["total_tokens"] == 40


def test_verify_rejects_wrong_targets_without_calling_the_model(tmp_path):
    from fastapi.testclient import TestClient
    app = make_app(tmp_path)
    app.state.agent_factory = scripted_agent([("Здравствуйте! Спросите о сплавах.", [])])
    provider = Provider(verdict_reply([]))
    with_verifier(app, provider)
    with TestClient(app) as client:
        session_id = client.post("/api/sessions").json()["id"]
        client.post(f"/api/sessions/{session_id}/chat", json={"message": "Привет"})
        user, answer = client.get(f"/api/sessions/{session_id}").json()["messages"]
        other = client.post("/api/sessions").json()["id"]
        assert client.post(f"/api/sessions/{other}/messages/{answer['id']}/verify").status_code == 404
        assert client.post(f"/api/sessions/{session_id}/messages/{user['id']}/verify").status_code == 404
        assert client.post(f"/api/sessions/missing/messages/{answer['id']}/verify").status_code == 404
        no_data = client.post(f"/api/sessions/{session_id}/messages/{answer['id']}/verify")
        assert no_data.status_code == 422 and "нет данных" in no_data.json()["detail"]
        lock = asyncio.Lock()
        asyncio.run(lock.acquire())  # как будто в диалоге идёт ответ
        app.state.locks[session_id] = lock
        assert client.post(f"/api/sessions/{session_id}/messages/{answer['id']}/verify").status_code == 409
    assert not provider.requests
```

В `test_old_history_database_is_migrated` добавить: `assert detail["messages"][0]["verification"] is None`.

Сообщение `ProviderError` с кодом `no_data` из задачи 9 должно содержать слова «нет данных», например: «В диалоге нет данных из базы — проверять ответ не с чем.»

- [ ] **Step 2: Run — FAIL**

`PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_agent_backend.py -q`

- [ ] **Step 3: Implement** — `sessions.py` (миграция, `turn`, `save_verification`, сумма в `get`), эндпоинт в `web.py`.
- [ ] **Step 4: Run all tests — PASS**

`PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q`

- [ ] **Step 5: Commit** — `Эндпоинт проверки ответа; вердикт и его стоимость сохраняются в диалоге`

---

### Task 11: Кнопка «Проверить» и блок вердикта

**Files:**
- Modify: `materials_chat/static/checks.js`, `materials_chat/static/app.js`, `materials_chat/static/styles.css`

**Interfaces:**
- Consumes: эндпоинт `verify` и поле `message.verification` (задача 10); `api` (`api.js`); `plural`, `formatCost`, `formatNumber` (`format.js`).
- Produces (`checks.js`): `verdictNode(verification: object) -> HTMLElement`.

Поведение:
- Кнопка `link-button` «Проверить» в строке под ответом, рядом с «Копировать». Показывается, если у ответа есть текст и id: для сохранённых — `message.id`, для нового — `message_id` из события `done` (`view.messageId`). Во время запроса — «Проверяю…» и `disabled`.
- Успех: блок вердикта под строкой расхода, внутри карточки ответа; повторная проверка заменяет блок. `state.session.usage = session_usage`, у сообщения в `state.session.messages` обновляется `verification`, затем `renderMeter()` и `loadSessions()` (стоимость в списке диалогов).
- Ошибка: в блоке вердикта `Не удалось проверить: <текст ошибки>` в стиле `answer-error`.
- Блок вердикта: итог `Подтверждено 11 из 12 утверждений (92 %) · частично подтверждён`, через `plural(total, ['утверждения', 'утверждений', 'утверждений'])` после «из». При `total: 0` — только вердикт. Затем расход: токены и `formatCost(usage.cost, usage.currency)`. Ниже — `<details>` «Утверждения»: список; у каждого статус («подтверждено» / «нет в данных» / «противоречит данным»), текст и опора (`вершина <node_id>` и `evidence`, что есть).
- Сохранённый вердикт показывается при открытии диалога (`showSavedAnswer`).
- Строится только из DOM-узлов, без `innerHTML`.

- [ ] **Step 1: `verdictNode` в `checks.js`.**
- [ ] **Step 2: `app.js`:** контейнер вердикта в `createAnswerView`; `view.messageId` (`done`, `showSavedAnswer`) и `view.verification`; кнопка в `renderAnswerMeta`; обработчик запроса.
- [ ] **Step 3: `styles.css`:** `.answer-verdict` и статусы утверждений на токенах `--success`/`--warning`/`--danger` и их `-soft`-вариантах.
- [ ] **Step 4: Verify syntax** — `node --check` для `checks.js` и `app.js` → код 0.
- [ ] **Step 5: Commit** — `Интерфейс: кнопка «Проверить» и блок вердикта`

---

### Task 12: Документация

**Files:**
- Modify: `README.md`, `docs/guide.html`, `docs/api-mapping.md`, `docs/http-api.md`, `docs/architecture.md`, `.env.example`

**Interfaces:**
- Consumes: поведение задач 1–11. Описывать по коду, а не по плану.

- [ ] **Step 1: `README.md`.** В таблицу «Откуда берутся данные» — строка `find_materials` (локальный индекс: таблица состава). В «Как устроен ответ» — вопросы по составу идут через `find_materials`. Новый раздел о проверке ответов: сверка чисел под каждым ответом; кнопка «Проверить», `VERIFIER_MODEL`; стоимость проверки входит в стоимость диалога. Раздел «План доработок» переписать под текущее состояние: правила подбора по составу из «Принятых решений» — в раздел о `find_materials` как описание поведения; в «Плане доработок» оставить только заход 2 со ссылкой на спецификацию.
- [ ] **Step 2: `docs/api-mapping.md`.** Строка `find_materials` в таблице сопоставления (`served_from: local_index`). Абзац о `query_graph`: из задания он реализован как `find_materials` — поддерживаемый запрос с ограниченными параметрами (условия на состав марок); произвольный язык запросов не заявляется.
- [ ] **Step 3: `docs/http-api.md`.** У сообщения — поля `number_check` и `verification` (форма, `null` у старых сообщений). Событие `number_check` в таблице потока. Эндпоинт `POST /api/sessions/{id}/messages/{message_id}/verify` с ответом и кодами ошибок. Сумма диалога включает проверки.
- [ ] **Step 4: `docs/guide.html`.** «6 инструментов» и «шестью» → 7 и семью (строки около 159, 198, 248, 318, 358, 900). Строка `find_materials` в таблице инструментов около 349 и 679. Число автотестов (около 175, 856, 871, 1024) — по фактическому выводу `pytest`. Новые модули — в перечень файлов около 248.
- [ ] **Step 5: `docs/architecture.md`** — если в нём есть перечень модулей или шагов хода, добавить `composition.py`, `answer_check.py`, `verifier.py` и сверку в конце хода. **`.env.example`** — закомментированная `# VERIFIER_MODEL=` с пояснением «модель проверяющего; по умолчанию ROUTERAI_MODEL».
- [ ] **Step 6: Verify** — `git grep -n "6 инструмент\|шесть инструмент\|шестью инструмент\|69 автотест"` ничего не находит; число тестов в документах совпадает с выводом `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q`.
- [ ] **Step 7: Commit** — `Документация: find_materials, сверка чисел и проверка ответов`

---

### Task 13: Итоговая проверка (выполняет координатор)

- [ ] **Step 1:** весь набор тестов — `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q`: всё зелёное, тесты по реальному индексу не пропущены.
- [ ] **Step 2:** приложение на свободных портах с реальными `.env` и индексом. В браузере: старый диалог открывается без ошибок в консоли; новый вопрос «Какие три марки содержат больше всего хрома?» вызывает `find_materials` (журнал: «Подбирает марки по составу», сводка аргументов); под ответом строка сверки; «Проверить» даёт блок вердикта, стоимость диалога растёт. Тестовый диалог после проверки удалить.
- [ ] **Step 3:** итоговое ревью всей ветки (subagent-driven-development), затем superpowers:finishing-a-development-branch.
