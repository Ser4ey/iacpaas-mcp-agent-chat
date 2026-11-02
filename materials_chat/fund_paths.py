"""Пути в «Моём фонде» IACPaaS и защита базовых ресурсов от записи.

Путь сравнивается по сегментам без учёта регистра, лишних пробелов и «/», поэтому
обходные написания («загрузки//Справочник», «Мой Фонд / Загрузки / …») защищены так же.
"""
import re

QUOTE = "‴"  # имя с «/» платформа заключает в тройные кавычки (Json.html, 7.2)
MY_FUND = "мой фонд"
UPLOADS = "Загрузки"


def segment_key(segment: str) -> str:
    return re.sub(r"\s+", " ", segment).strip().casefold().replace("ё", "е")


def split_path(text: str) -> list[str]:
    """Сегменты пути без префикса «Мой Фонд» (и «<e-mail> / Мой Фонд») и хвоста «$;»."""
    text = (text or "").strip()
    text = re.sub(r"\$?;?\s*$", "", text) if text.endswith(("$;", "$", ";")) else text
    segments, current, quoted = [], [], False
    for char in text:
        if char == QUOTE:
            quoted = not quoted
        elif char == "/" and not quoted:
            segments.append("".join(current))
            current = []
        else:
            current.append(char)
    segments.append("".join(current))
    segments = [re.sub(r"\s+", " ", segment).strip() for segment in segments]
    segments = [segment for segment in segments if segment]
    keys = [segment_key(segment) for segment in segments]
    if MY_FUND in keys:
        position = keys.index(MY_FUND)
        if position > 1 or (position == 1 and "@" not in segments[0]):
            raise ValueError("«Мой Фонд» может стоять только в начале пути.")
        segments = segments[position + 1:]
    if any(segment_key(segment) == MY_FUND for segment in segments):
        raise ValueError("«Мой Фонд» может стоять только в начале пути.")
    return segments


def join_path(segments: list[str]) -> str:
    """Путь в формате запросов платформы: «Папка / Ресурс», имя с «/» — в ‴…‴."""
    return " / ".join(f"{QUOTE}{segment}{QUOTE}" if "/" in segment else segment for segment in segments)


class Protection:
    """Защищённые пути: базовые ресурсы проекта, папка «Загрузки» целиком, PROTECTED_PATHS."""

    def __init__(self, definitions, extra: str = ""):
        self.rules: list[tuple[tuple[str, ...], str]] = []
        for item in definitions:
            self.rules.append((self._key(item.path), f"базовый ресурс «{item.title}» ({item.path})"))
        self.rules.append(((segment_key(UPLOADS),), "папка «Загрузки» с базовыми ресурсами"))
        for raw in (extra or "").split(";"):
            if raw.strip():
                self.rules.append((self._key(raw), f"PROTECTED_PATHS: {raw.strip()}"))

    @staticmethod
    def _key(path: str) -> tuple[str, ...]:
        return tuple(segment_key(segment) for segment in split_path(path))

    def rule(self, segments: list[str]) -> str | None:
        """Название правила, если путь совпадает с защищённым или лежит внутри него."""
        key = tuple(segment_key(segment) for segment in segments)
        for protected, name in self.rules:
            if protected and key[:len(protected)] == protected:
                return name
        return None
