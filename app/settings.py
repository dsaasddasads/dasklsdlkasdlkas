"""Настройки, которые админ меняет на ходу (в приложении, вкладка «Админ» -> «Управление»).

Значения лежат в таблице settings и обновляются раз в несколько секунд, поэтому читаются синхронно и без запросов к базе.
Пустое значение = берётся то, что задано переменными окружения.
"""
import asyncio
import logging

from . import config, db

log = logging.getLogger("settings")
_cache: dict[str, str] = {}

# ключ -> (тип, значение по умолчанию из окружения)
DEFAULTS = {
    "notify_paused": ("bool", False),   # приостановить все уведомления
    "relay_paused": ("bool", False),    # запретить отправку сообщений владельцам
    "auto_paused": ("bool", False),     # остановить авто-режим у всех
    "link_closed": ("bool", False),     # закрыть привязку новых аккаунтов
    "relay_daily_limit": ("int", config.RELAY_DAILY_LIMIT),
    "relay_min_gap": ("int", int(config.RELAY_MIN_GAP)),
    "auto_max": ("int", config.AUTO_MAX),
}


def get_bool(key: str) -> bool:
    return _cache.get(key, "") == "1" if key in _cache else bool(DEFAULTS[key][1])


def get_int(key: str) -> int:
    try:
        return int(_cache[key])
    except (KeyError, ValueError):
        return int(DEFAULTS[key][1])


def snapshot() -> dict:
    return {k: (get_bool(k) if t == "bool" else get_int(k)) for k, (t, _) in DEFAULTS.items()}


async def load() -> None:
    global _cache
    try:
        _cache = await db.load_settings()
    except Exception as e:  # база моргнула - остаёмся на прежних значениях
        log.debug("settings load failed: %s", e)


async def set_value(key: str, value) -> None:
    kind = DEFAULTS[key][0]
    if kind == "bool":
        v = "1" if value else "0"
    else:
        v = str(max(0, int(value)))
    await db.set_setting(key, v)
    _cache[key] = v


async def reset(key: str) -> None:
    await db.set_setting(key, None)
    _cache.pop(key, None)


async def run() -> None:
    """Фоновое обновление кэша."""
    while True:
        await load()
        await asyncio.sleep(5)
