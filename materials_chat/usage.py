"""Учёт расхода модели: токены и стоимость одного обращения, ответа и диалога."""


def empty_usage() -> dict:
    return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "requests": 0,
            "known": True, "unknown_requests": 0, "last_input_tokens": 0,
            "cost": 0.0, "currency": None, "cost_known": True}


def price_tokens(input_tokens: int, output_tokens: int, model_info: dict | None, cached_tokens: int = 0) -> float | None:
    """Стоимость по ценам модели (за миллион токенов); None, если цены неизвестны."""
    info = model_info or {}
    price_in, price_out = info.get("price_input_per_million"), info.get("price_output_per_million")
    if price_in is None or price_out is None:
        return None
    price_cached = info.get("price_cache_read_per_million")
    if price_cached is None:
        price_cached = price_in
    fresh = max(0, input_tokens - cached_tokens)
    return (fresh * price_in + cached_tokens * price_cached + output_tokens * price_out) / 1_000_000


def request_usage(raw: dict | None, model_info: dict | None) -> dict:
    """Расход одного обращения по полю usage из ответа провайдера."""
    raw = raw or {}
    known = all(raw.get(key) is not None for key in ("prompt_tokens", "completion_tokens"))
    prompt, completion = int(raw.get("prompt_tokens") or 0), int(raw.get("completion_tokens") or 0)
    cached = int((raw.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    # Если провайдер сам посчитал стоимость — берём её, иначе считаем по прайсу.
    cost = float(raw["cost"]) if isinstance(raw.get("cost"), (int, float)) else price_tokens(prompt, completion, model_info, cached)
    return {"input_tokens": prompt, "output_tokens": completion,
            "total_tokens": int(raw.get("total_tokens") or prompt + completion), "requests": 1,
            "known": known, "unknown_requests": 0 if known else 1, "last_input_tokens": prompt,
            "cost": cost if known else None, "currency": (model_info or {}).get("currency"),
            "cost_known": known and cost is not None}


def with_cost(usage: dict, model_info: dict | None) -> dict:
    """Сообщения, сохранённые до появления учёта стоимости, оцениваем по текущему прайсу."""
    if not usage or "cost" in usage or not usage.get("requests"):
        return usage
    cost = price_tokens(int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0), model_info)
    return {**usage, "cost": cost, "currency": (model_info or {}).get("currency"),
            "cost_known": cost is not None and bool(usage.get("known", True))}


def add_usage(left: dict, right: dict) -> dict:
    result = empty_usage()
    for key in ("input_tokens", "output_tokens", "total_tokens", "requests", "unknown_requests"):
        result[key] = int(left.get(key, 0) or 0) + int(right.get(key, 0) or 0)
    result["known"] = bool(left.get("known", True) and right.get("known", True))
    # Размер контекста — у самого последнего обращения.
    result["last_input_tokens"] = right.get("last_input_tokens") or left.get("last_input_tokens") or 0
    costs = [item.get("cost") for item in (left, right) if item.get("requests")]
    result["cost"] = sum(cost for cost in costs if cost is not None)
    result["cost_known"] = all(item.get("cost_known", True) for item in (left, right) if item.get("requests"))
    result["currency"] = right.get("currency") or left.get("currency")
    return result
