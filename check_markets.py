"""Проверка Portals вживую (запускай ЛОКАЛЬНО, Telegram и сессия не нужны):

    pip install aiohttp
    python check_markets.py "Plush Pepe" "Название модели" "Название фона"

Модель и фон необязательны. Скрипт покажет, что вернул Portals и какие цены разобрал бот.
Если пишет HTTP 403 / пусто - у Portals изменился API или он закрыт для твоего IP.
"""
import asyncio
import json
import sys

import aiohttp

from app.markets import Portals, UA, norm


async def main() -> None:
    col = sys.argv[1] if len(sys.argv) > 1 else "Plush Pepe"
    model = sys.argv[2] if len(sys.argv) > 2 else None
    back = sys.argv[3] if len(sys.argv) > 3 else None

    async with aiohttp.ClientSession(headers={"User-Agent": UA, "Accept": "application/json",
                                              "Origin": "https://portal-market.com"}) as s:
        async with s.get(f"{Portals.BASE}/collections", params={"limit": 3, "offset": 0}) as r:
            print("GET /collections ->", r.status)
            print((await r.text())[:700], "\n")

    p = Portals()
    d = {"collection": col, "model": model, "backdrop": back}
    await p.valuate(d)
    print("Разобрано ботом:", json.dumps({k: v for k, v in d.items() if k.startswith("portals_")},
                                         ensure_ascii=False, indent=2))
    if "portals_floor" not in d:
        print(f"Коллекция '{col}' (ключ {norm(col)}) на Portals не найдена или маркет недоступен")
    await p.close()


asyncio.run(main())
