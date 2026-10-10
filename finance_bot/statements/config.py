"""Локальные настройки импорта выписок (файл statements_config.json, в git не попадает).

Пример см. в statements_config.example.json. Путь можно переопределить переменной STATEMENTS_CONFIG.

  phones          последние 4 цифры телефона -> "own" (свой счёт) или "family" (переводы семье)
  extra_rules     [["REGEX", "Категория"], ...] — свои правила по названию магазина, проверяются первыми
  analysis_start  дата начала анализа по умолчанию, YYYY-MM-DD
  people          "Имя Ф." как показывает банк -> "own" / "family" (переводы со скриншотов)
  big_purchase    порог «крупной разовой покупки» в рублях
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

DEFAULTS: dict = {"phones": {}, "extra_rules": [], "analysis_start": None, "big_purchase": 20000}


@lru_cache(maxsize=1)
def load() -> dict:
    path = Path(os.getenv("STATEMENTS_CONFIG", "statements_config.json"))
    cfg = dict(DEFAULTS)
    if path.is_file():
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    return cfg


def reset() -> None:
    """Сбросить кэш (нужно в тестах и после смены переменной STATEMENTS_CONFIG)."""
    load.cache_clear()
