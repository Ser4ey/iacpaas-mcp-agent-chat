"""Подписи стоимости в интерфейсе (static/format.js) выполняются в Node.js; без Node.js тест пропускается."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

FORMAT_JS = Path(__file__).resolve().parent.parent / "materials_chat" / "static" / "format.js"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node.js не установлен")
def test_cost_labels_never_show_a_misleading_zero():
    cases = {
        "free": {"requests": 2, "cost": 0, "free": True, "cost_known": True, "currency": None},
        "unknown": {"requests": 2, "cost": None, "cost_known": False, "currency": None},
        # Так сохранены старые ответы без цен: 0 при cost_known=false — это не «0,00 ₽».
        "zero_unknown": {"requests": 1, "cost": 0, "cost_known": False, "currency": "RUB"},
        "rub": {"requests": 1, "cost": 0.1234, "cost_known": True, "currency": "RUB"},
        "partial": {"requests": 2, "cost": 0.5, "cost_known": False, "currency": "RUB"},
        "no_currency": {"requests": 1, "cost": 0.5, "cost_known": True, "currency": None},
        "no_requests": {"requests": 0, "cost": 0, "cost_known": True, "currency": None},
    }
    script = (f"import {{ formatUsageCost }} from {json.dumps(FORMAT_JS.as_uri())};\n"
              f"const cases = {json.dumps(cases)};\n"
              "const out = Object.fromEntries(Object.entries(cases).map(([key, usage]) => [key, formatUsageCost(usage)]));\n"
              "out.meter = formatUsageCost(cases.unknown, { unknown: '—' });\n"
              "out.fallback_currency = formatUsageCost(cases.no_currency, { currency: 'USD' });\n"
              "console.log(JSON.stringify(out));")
    result = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True,
                            encoding="utf-8", timeout=60, check=True)
    assert json.loads(result.stdout) == {
        "free": "бесплатно", "unknown": "стоимость неизвестна", "zero_unknown": "стоимость неизвестна",
        "rub": "0,12\xa0₽", "partial": "≥\xa00,50\xa0₽", "no_currency": "0,50", "no_requests": "",
        "meter": "—", "fallback_currency": "0,50\xa0$"}
