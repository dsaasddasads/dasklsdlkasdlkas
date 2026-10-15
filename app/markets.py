"""Внешние маркеты. Сейчас: Portals (публичные роуты, без авторизации и без юзербота).

Цены Portals - в GRAM (бывший TON, курс 1:1). Ничего здесь не должно ронять мониторинг: при любой ошибке возвращаем None,
а после 403/429/сбоя маркет на несколько минут отключается (backoff).

Формат ответов Portals не задокументирован официально, поэтому разбор терпимый к форме JSON.
Проверить вживую: python check_markets.py "Plush Pepe" "<модель>" "<фон>"
"""
import asyncio
import logging
import re
import time

import aiohttp

log = logging.getLogger("markets")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def num(v) -> float | None:
    try:
        x = float(v)
        return x if x > 0 else None
    except (TypeError, ValueError):
        return None


def items_of(j, *keys) -> list:
    """Достаём список из ответа, какой бы обёртки ни было: [..], {"results": [..]}, {"x": [..]}."""
    if isinstance(j, list):
        return j
    if isinstance(j, dict):
        for k in keys:
            if isinstance(j.get(k), list):
                return j[k]
        for v in j.values():
            if isinstance(v, list):
                return v
    return []


class Portals:
    BASE = "https://portal-market.com/api"

    def __init__(self, rps: float = 2.0, ttl: float = 600.0):
        self._sess: aiohttp.ClientSession | None = None
        self._gap = 1.0 / max(rps, 0.2)
        self._ttl = ttl
        self._next = 0.0
        self._lock = asyncio.Lock()
        self._down_until = 0.0
        self._warned = False
        self._cols: dict[str, dict] = {}
        self._cols_ts = 0.0
        self._cache: dict[tuple, tuple[float, float | None]] = {}

    # ---------- http ----------
    async def _get(self, path: str, params: dict):
        now = time.monotonic()
        if now < self._down_until:
            return None
        async with self._lock:
            wait = self._next - time.monotonic()
            self._next = max(time.monotonic(), self._next) + self._gap
        if wait > 0:
            await asyncio.sleep(wait)
        if self._sess is None or self._sess.closed:
            self._sess = aiohttp.ClientSession(
                headers={"User-Agent": UA, "Accept": "application/json",
                         "Origin": "https://portal-market.com", "Referer": "https://portal-market.com/"},
                timeout=aiohttp.ClientTimeout(total=12))
        try:
            async with self._sess.get(f"{self.BASE}{path}", params=params) as r:
                if r.status == 200:
                    self._warned = False
                    return await r.json(content_type=None)
                self._down_until = time.monotonic() + (900 if r.status in (401, 403) else 120)
                if not self._warned:
                    log.warning("Portals %s -> HTTP %s, пауза", path, r.status)
                    self._warned = True
        except Exception as e:
            self._down_until = time.monotonic() + 120
            if not self._warned:
                log.warning("Portals %s недоступен: %s", path, e)
                self._warned = True
        return None

    # ---------- collections ----------
    async def _collections(self) -> dict[str, dict]:
        if self._cols and time.monotonic() - self._cols_ts < 1800:
            return self._cols
        cols: dict[str, dict] = {}
        offset = 0
        for _ in range(5):
            j = await self._get("/collections", {"limit": 200, "offset": offset})
            if j is None:
                break
            page = items_of(j, "collections", "results")
            for c in page:
                if not isinstance(c, dict) or not c.get("id"):
                    continue
                for k in (norm(c.get("name")), norm(c.get("short_name"))):
                    if k:
                        cols[k] = c
            if len(page) < 200:
                break
            offset += 200
        if cols:
            self._cols, self._cols_ts = cols, time.monotonic()
        return self._cols

    async def floor(self, cid: str, model: str | None = None, backdrop: str | None = None) -> float | None:
        """Самая низкая цена листинга (GRAM) по коллекции с фильтром по модели и/или фону."""
        ck = (cid, model or "", backdrop or "")
        hit = self._cache.get(ck)
        if hit and time.monotonic() - hit[0] < self._ttl:
            return hit[1]
        params = {"offset": 0, "limit": 1, "collection_ids": cid, "status": "listed",
                  "sort_by": "price asc", "exclude_bundled": "true"}
        if model:
            params["filter_by_models"] = model
        if backdrop:
            params["filter_by_backdrops"] = backdrop
        j = await self._get("/nfts/search", params)
        if j is None:
            return None  # ошибка: не кэшируем
        lots = items_of(j, "results", "nfts", "items", "data")
        price = None
        if lots and isinstance(lots[0], dict):
            price = num(lots[0].get("price"))
        self._cache[ck] = (time.monotonic(), price)
        return price

    async def valuate(self, d: dict) -> None:
        """Дописывает в подарок цены Portals (GRAM): коллекция, модель, фон, пара."""
        col = (await self._collections()).get(norm(d.get("collection")))
        if not col:
            return
        cid = col["id"]
        d["portals_floor"] = num(col.get("floor_price"))
        model, back = d.get("model"), d.get("backdrop")
        if model:
            d["portals_model_floor"] = await self.floor(cid, model=model)
        if back:
            d["portals_backdrop_floor"] = await self.floor(cid, backdrop=back)
        if model and back:
            d["portals_combo_floor"] = await self.floor(cid, model=model, backdrop=back)

    async def close(self) -> None:
        if self._sess and not self._sess.closed:
            await self._sess.close()
