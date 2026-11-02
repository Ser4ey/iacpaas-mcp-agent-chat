"""Запись в «Мой фонд» без сети: защита, подтверждение, запросы к подменённой платформе, чтение по пути."""
import copy
import json

import pytest

from materials_chat.config import PROJECT_ROOT, Settings
from materials_chat.platform import PlatformError
from materials_chat.service import GraphService, ToolResult, fail
from materials_chat.writes import WriteService

ACCOUNT = "user@example.ru / Мой Фонд / "
ONTOLOGY = {"title": "Онтология базы свойств материалов", "id": 100, "name": "Онтология базы свойств материалов",
            "type": "КОРЕНЬ", "json_type": "universal", "path": ACCOUNT + "Загрузки / Онтология базы свойств материалов$;",
            "successors": [{"id": 101, "name": "Класс свойств", "type": "НЕТЕРМИНАЛ", "specifier": "непустое упорядоченное множество",
                            "restrictor": "all", "meta": "описать элемент списка", "successors": [
                {"id": 102, "name": "Свойство", "type": "НЕТЕРМИНАЛ", "specifier": "возможно пустое упорядоченное множество",
                 "restrictor": "new-clone", "meta": "описать элемент списка", "successors": [
                    {"id": 103, "name": "Синонимы", "type": "НЕТЕРМИНАЛ", "specifier": "возможное отсутствие", "restrictor": "new",
                     "meta": "описать элемент списка", "successors": [
                        {"id": 104, "name": "синоним", "type": "ТЕРМИНАЛ-СОРТ", "valtype": "STRING",
                         "specifier": "непустое упорядоченное множество", "restrictor": "new", "meta": "описать элемент списка"}]}]}]}]}
MINE = {"title": "Мои свойства", "code": "77", "date": "02.11.2026", "path": ACCOUNT + "Демо агента / Мои свойства$;",
        "json_type": "universal", "ontology": ONTOLOGY["path"], "type": "КОРЕНЬ", "id": 1, "name": "Мои свойства",
        "meta": "Онтология базы свойств материалов", "successors": [
            {"id": 2, "type": "НЕТЕРМИНАЛ", "name": "Тепловые свойства", "meta": "Класс свойств", "successors": [
                {"id": 3, "type": "НЕТЕРМИНАЛ", "name": "Теплопроводность", "meta": "Свойство", "successors": [
                    {"id": 4, "type": "НЕТЕРМИНАЛ", "name": "Синонимы", "meta": "Синонимы", "successors": [
                        {"id": 5, "type": "ТЕРМИНАЛ-ЗНАЧЕНИЕ", "value": "λ", "valtype": "STRING", "meta": "синоним"},
                        {"id": 6, "type": "ТЕРМИНАЛ-ЗНАЧЕНИЕ", "value": "k", "valtype": "STRING", "meta": "синоним",
                         "original": ACCOUNT + "Загрузки / База свойств материалов$/Физические свойства/k;"}]}]},
                {"id": 7, "type": "НЕТЕРМИНАЛ", "name": "Дубль", "meta": "Свойство"},
                {"id": 8, "type": "НЕТЕРМИНАЛ", "name": "Дубль", "meta": "Свойство"}]}]}


class FakePlatform:
    """IACPaaS в памяти: ресурсы по пути «Папка / Ресурс»; записывающие вызовы запоминаются."""

    def __init__(self, resources=None, fail_import=None):
        self.resources = resources if resources is not None else {"Демо агента / Мои свойства": copy.deepcopy(MINE)}
        self.writes, self.exports, self.fail_import = [], [], fail_import

    async def export(self, resource_path, depth=None, root_path=None):
        self.exports.append((resource_path, depth, root_path))
        if resource_path not in self.resources:
            raise PlatformError("PLATFORM_REJECTED", "Платформа отклонила экспорт: проверьте путь и права на ресурс.")
        return copy.deepcopy(self.resources[resource_path])

    async def create_folder(self, parent, name, description=""):
        self.writes.append(("create_folder", parent, name))
        return {"success": True, "id": "9"}

    async def import_resource(self, folder, graph, clear):
        self.writes.append(("import", folder, copy.deepcopy(graph), clear))
        if self.fail_import and self.fail_import(graph):
            raise PlatformError("PLATFORM_REJECTED", "Платформа отклонила запрос: тест")
        self.resources[(folder + " / " if folder else "") + graph["title"]] = copy.deepcopy(graph)
        return {"success": True, "code": "77"}

    async def delete_resource(self, path, mode):
        self.writes.append(("delete", path, mode))
        self.resources.pop(path, None)
        return {"success": True}


class Confirmer:
    def __init__(self, answer=None):
        self.answer, self.messages = answer, []

    async def __call__(self, message, danger):
        self.messages.append((message, danger))
        return self.answer


DECLINED = fail("CONFIRMATION_DECLINED", "Пользователь отменил запись.")


def make(tmp_path, platform=None, **settings):
    service = GraphService(Settings(_env_file=None, data_dir=tmp_path / "data", resources_path=PROJECT_ROOT / "config" / "resources.json",
                                    iacpaas_username="", iacpaas_password="", openrouter_api_key="", **settings))
    service.store.import_graph("properties_ontology", copy.deepcopy(ONTOLOGY))
    service.client = platform or FakePlatform()
    return WriteService(service), service.client


DEMO_ITEMS = [{"concept": "Класс свойств", "name": "Тепловые свойства", "children": [
    {"concept": "Свойство", "name": "Теплопроводность", "children": [
        {"concept": "Синонимы", "children": [{"concept": "синоним", "value": "коэффициент теплопроводности"}]}]}]}]
BASE_PATHS = [item["path"] for item in json.loads((PROJECT_ROOT / "config" / "resources.json").read_text(encoding="utf-8"))]
TRICKY = ["Загрузки/Справочник по материалам", "мой фонд / ЗАГРУЗКИ / База свойств материалов",
          "//Загрузки//Новая папка", "user@example.ru/Мой Фонд/Загрузки$;", "Личное / Секрет / Вложенный"]


def requests_for(path):
    """Каждая записывающая операция, нацеленная на path."""
    return [
        ("write", {"operation": "create_folder", "parent_path": path, "name": "Новая"}),
        ("write", {"operation": "create_resource", "folder_path": path, "name": "Новый", "ontology": "properties_ontology",
                   "items": DEMO_ITEMS}),
        ("write", {"operation": "update_concept", "path": path, "concept_path": "/Физические свойства", "name": "X"}),
        ("delete", path),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [*BASE_PATHS, *TRICKY])
async def test_every_operation_on_protected_path_is_refused_before_platform(tmp_path, path):
    writes, platform = make(tmp_path, protected_paths="Личное / Секрет")
    confirm = Confirmer()
    for kind, argument in requests_for(path):
        result = await (writes.write(argument, confirm) if kind == "write" else writes.delete(argument, "trash", confirm))
        assert result.error.code == "PROTECTED_RESOURCE", (kind, argument)
    assert confirm.messages == [] and platform.writes == [] and platform.exports == []


@pytest.mark.asyncio
async def test_new_name_that_makes_protected_path_is_refused(tmp_path):
    writes, platform = make(tmp_path)
    result = await writes.write({"operation": "create_folder", "parent_path": "", "name": "загрузки"}, Confirmer())
    assert result.error.code == "PROTECTED_RESOURCE" and platform.writes == []


@pytest.mark.asyncio
async def test_create_folder_after_confirmation(tmp_path):
    writes, platform = make(tmp_path)
    confirm = Confirmer()
    result = await writes.write({"operation": "create_folder", "parent_path": "", "name": "Демо агента"}, confirm)
    assert result.ok and result.meta["served_from"] == "iacpaas_live"
    assert confirm.messages == [("Создать папку «Демо агента» в корне фонда.", False)]
    assert platform.writes == [("create_folder", "", "Демо агента")]


@pytest.mark.asyncio
async def test_declined_confirmation_sends_nothing(tmp_path):
    writes, platform = make(tmp_path)
    result = await writes.write({"operation": "create_folder", "parent_path": "Демо агента", "name": "X"}, Confirmer(DECLINED))
    assert result.error.code == "CONFIRMATION_DECLINED" and platform.writes == []


@pytest.mark.asyncio
async def test_create_resource_builds_json_and_describes_it(tmp_path):
    writes, platform = make(tmp_path)
    confirm = Confirmer()
    result = await writes.write({"operation": "create_resource", "folder_path": "Мой Фонд / Демо агента", "name": "Новые свойства",
                                 "ontology": "properties_ontology", "items": DEMO_ITEMS}, confirm)
    assert result.ok, result
    message, danger = confirm.messages[0]
    assert danger is False
    assert message == ("Создать ресурс «Новые свойства» в «Демо агента» по онтологии «Онтология базы свойств материалов»: "
                       "Класс свойств «Тепловые свойства» → Свойство «Теплопроводность» → Синонимы → "
                       "синоним «коэффициент теплопроводности».")
    kind, folder, graph, clear = platform.writes[0]
    assert (kind, folder, clear) == ("import", "Демо агента", False)
    assert graph["ontology"] == ONTOLOGY["path"] and graph["title"] == "Новые свойства"
    assert result.data["path"] == "Демо агента / Новые свойства"


@pytest.mark.asyncio
async def test_create_resource_refuses_existing_name_and_bad_tree(tmp_path):
    writes, platform = make(tmp_path)
    existing = await writes.write({"operation": "create_resource", "folder_path": "Демо агента", "name": "Мои свойства",
                                   "ontology": "properties_ontology", "items": []}, Confirmer())
    assert existing.error.code == "INVALID_ARGUMENTS" and "уже есть" in existing.error.message
    bad = await writes.write({"operation": "create_resource", "folder_path": "Демо агента", "name": "Новый",
                              "ontology": "properties_ontology", "items": [{"concept": "Плотность", "name": "П"}]}, Confirmer())
    assert bad.error.code == "CONCEPT_NOT_ALLOWED" and "Класс свойств" in bad.error.message
    other = await writes.write({"operation": "create_resource", "folder_path": "Демо агента", "name": "Новый",
                                "ontology": "units", "items": []}, Confirmer())
    assert other.error.code == "INVALID_ARGUMENTS"
    assert platform.writes == []


@pytest.mark.asyncio
async def test_update_concept_renames_by_reimport(tmp_path):
    writes, platform = make(tmp_path)
    confirm = Confirmer()
    result = await writes.write({"operation": "update_concept", "path": "Демо агента / Мои свойства",
                                 "concept_path": "/Тепловые свойства/Теплопроводность", "name": "Теплопроводность λ"}, confirm)
    assert result.ok, result
    assert "«Теплопроводность» (Свойство) → «Теплопроводность λ»" in confirm.messages[0][0]
    kind, folder, graph, clear = platform.writes[0]
    assert (kind, folder, clear) == ("import", "Демо агента", True)
    assert graph["successors"][0]["successors"][0]["name"] == "Теплопроводность λ"
    assert graph["successors"][0]["successors"][1:] == MINE["successors"][0]["successors"][1:]  # остальное без изменений
    assert result.data["concept_path"] == "/Тепловые свойства/Теплопроводность λ"


@pytest.mark.asyncio
async def test_update_concept_checks_target(tmp_path):
    writes, platform = make(tmp_path)
    base = {"operation": "update_concept", "path": "Демо агента / Мои свойства"}
    foreign = await writes.write({**base, "concept_path": "Тепловые свойства/Теплопроводность/Синонимы/k", "value": "x"}, Confirmer())
    assert foreign.error.code == "FOREIGN_CONCEPT"
    twin = await writes.write({**base, "concept_path": "Тепловые свойства/Дубль", "name": "x"}, Confirmer())
    assert twin.error.code == "AMBIGUOUS_PATH"
    missing = await writes.write({**base, "concept_path": "Тепловые свойства/Плотность", "name": "x"}, Confirmer())
    assert missing.error.code == "NODE_NOT_FOUND" and "Теплопроводность" in missing.error.message
    value_on_vertex = await writes.write({**base, "concept_path": "Тепловые свойства", "value": 5}, Confirmer())
    assert value_on_vertex.error.code == "INVALID_ARGUMENTS"
    assert platform.writes == []


@pytest.mark.asyncio
async def test_failed_reimport_restores_original(tmp_path):
    platform = FakePlatform(fail_import=lambda graph: graph["successors"][0]["successors"][0]["name"] != "Теплопроводность")
    writes, _ = make(tmp_path, platform)
    result = await writes.write({"operation": "update_concept", "path": "Демо агента / Мои свойства",
                                 "concept_path": "Тепловые свойства/Теплопроводность", "name": "Новое имя"}, Confirmer())
    assert result.error.code == "PLATFORM_REJECTED"
    assert [write[3] for write in platform.writes] == [True, True]  # попытка и откат к исходной выгрузке
    assert platform.writes[1][2]["successors"][0]["successors"][0]["name"] == "Теплопроводность"


@pytest.mark.asyncio
async def test_delete_to_trash_and_missing_resource(tmp_path):
    writes, platform = make(tmp_path)
    confirm = Confirmer()
    missing = await writes.delete("Демо агента / Нет такого", "trash", confirm)
    assert missing.error.code == "NODE_NOT_FOUND" and confirm.messages == []
    result = await writes.delete("Демо агента / Мои свойства", "trash", confirm)
    assert result.ok
    assert confirm.messages == [("Удалить в корзину ресурс «Демо агента / Мои свойства».", True)]
    assert platform.writes == [("delete", "Демо агента / Мои свойства", "trashIR")]
    bad_mode = await writes.delete("Демо агента / Мои свойства", "clear", confirm)
    assert bad_mode.error.code == "INVALID_ARGUMENTS"


@pytest.mark.asyncio
async def test_read_subgraph_by_path_is_live_with_concept_paths(tmp_path):
    writes, platform = make(tmp_path)
    result = await writes.read_subgraph("Мой Фонд / Демо агента / Мои свойства", None, 8, 50, 0)
    assert result.ok and result.meta["served_from"] == "iacpaas_live"
    paths = {node["label"]: node["concept_path"] for node in result.data["nodes"]}
    assert paths["Мои свойства"] == "" and paths["Теплопроводность"] == "/Тепловые свойства/Теплопроводность"
    assert paths["λ"] == "/Тепловые свойства/Теплопроводность/Синонимы/λ"
    assert platform.exports == [("Демо агента / Мои свойства", 8, None)]
    deeper = await writes.read_subgraph("Демо агента / Мои свойства", "/Тепловые свойства", 1, 50, 0)
    assert deeper.data["nodes"][0]["label"] == "Тепловые свойства"
    assert platform.exports[-1] == ("Демо агента / Мои свойства", 1, "/Тепловые свойства")


@pytest.mark.asyncio
async def test_read_by_path_reports_platform_errors(tmp_path):
    writes, _ = make(tmp_path)
    result = await writes.read_subgraph("Демо агента / Нет такого", None, 2, 50, 0)
    assert not result.ok and result.error.code == "PLATFORM_REJECTED"
    resource = await writes.read_resource("Демо агента / Мои свойства")
    assert resource.ok and resource.data["title"] == "Мои свойства" and resource.data["code"] == "77"


@pytest.mark.asyncio
async def test_write_resets_live_cache_of_resource(tmp_path):
    writes, platform = make(tmp_path)
    await writes.read_subgraph("Демо агента / Мои свойства", None, 8, 50, 0)
    await writes.read_subgraph("Демо агента / Мои свойства", None, 8, 50, 0)
    assert len(platform.exports) == 1  # повторное чтение — из короткого кэша
    await writes.write({"operation": "update_concept", "path": "Демо агента / Мои свойства",
                        "concept_path": "/Тепловые свойства/Теплопроводность", "name": "Т"}, Confirmer())
    after = await writes.read_subgraph("Демо агента / Мои свойства", None, 8, 50, 0)
    assert "Т" in {node["label"] for node in after.data["nodes"]}


def test_tool_result_type():
    assert isinstance(DECLINED, ToolResult)


@pytest.mark.asyncio
async def test_ontology_description_has_structure_only_with_writes(tmp_path):
    writes, _ = make(tmp_path)
    result = await writes.graph.get_inforesource("properties_ontology")
    assert result.data["structure"] == ["Класс свойств [вершина, нужно имя]", "  Свойство [вершина, нужно имя]",
                                        "    Синонимы [вершина]", "      синоним [значение STRING]"]
    assert "concept" in result.data["structure_legend"]
    readonly, _ = make(tmp_path / "ro", write_enabled=False)
    assert "structure" not in (await readonly.graph.get_inforesource("properties_ontology")).data
