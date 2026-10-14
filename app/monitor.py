"""Мониторинг новых улучшенных подарков через MTProto (Telethon).

Принцип: у каждой коллекции улучшенные подарки нумеруются подряд (PlushPepe-1, -2, ...).
Храним последний известный номер и каждый цикл проверяем last+1. Если подарок существует -
это новое улучшение.
"""
import asyncio
import logging
import re
import time
from typing import Awaitable, Callable

from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.functions.payments import (
    GetResaleStarGiftsRequest,
    GetStarGiftsRequest,
    GetUniqueStarGiftRequest,
)

from . import config, db

log = logging.getLogger("monitor")


def prefix_of(title: str) -> str:
    """'Plush Pepe' -> 'PlushPepe' (так строится slug)."""
    return re.sub(r"[^A-Za-z0-9]", "", title)


def parse(g, prefix: str) -> dict:
    oid = getattr(g, "owner_id", None)
    d = {
        "slug": g.slug,
        "prefix": prefix,
        "collection": getattr(g, "title", prefix),
        "num": g.num,
        "issued": getattr(g, "availability_issued", None),
        "total": getattr(g, "availability_total", None),
        "gift_id": getattr(g, "gift_id", None),
        "model": None, "model_rarity": None,
        "backdrop": None, "backdrop_rarity": None,
        "symbol": None, "symbol_rarity": None,
        "owner_id": getattr(oid, "user_id", None),
        "owner_name": getattr(g, "owner_name", None),
        "owner_address": getattr(g, "owner_address", None),
        "ask": None,
        "floor": None,
    }
    for a in g.attributes:
        kind = type(a).__name__
        r = getattr(a, "rarity_permille", None)
        r = r if isinstance(r, int) else None
        if kind == "StarGiftAttributeModel":
            d["model"], d["model_rarity"] = a.name, r
        elif kind == "StarGiftAttributeBackdrop":
            d["backdrop"], d["backdrop_rarity"] = a.name, r
        elif kind == "StarGiftAttributePattern":
            d["symbol"], d["symbol_rarity"] = a.name, r
    amt = getattr(g, "resell_amount", None)  # цена, если подарок уже выставлен на продажу
    if isinstance(amt, list):
        amt = next((x.amount for x in amt if type(x).__name__ == "StarsAmount"), None)
    d["ask"] = amt if isinstance(amt, int) else None
    return d


class Monitor:
    def __init__(self, client: TelegramClient, on_gift: Callable[[dict], Awaitable[None]]):
        self.client = client
        self.on_gift = on_gift
        self.sem = asyncio.Semaphore(config.CONCURRENCY)
        self.cursors: dict[str, int] = {}
        self._floor: dict[int, tuple[float, int | None]] = {}
        self._last_refresh = 0.0
        self._tasks: set[asyncio.Task] = set()

    # ---------- low level ----------
    async def fetch(self, slug: str):
        while True:
            try:
                r = await self.client(GetUniqueStarGiftRequest(slug=slug))
                return r.gift
            except FloodWaitError as e:
                log.warning("FloodWait %ss", e.seconds)
                await asyncio.sleep(e.seconds + 1)
            except RPCError:
                return None  # STARGIFT_SLUG_INVALID и т.п. - такого номера ещё нет

    async def exists(self, slug: str) -> bool:
        return await self.fetch(slug) is not None

    async def find_max(self, prefix: str) -> int:
        """Текущий максимальный номер (экспоненциальный + бинарный поиск)."""
        if not await self.exists(f"{prefix}-1"):
            return 0
        lo, hi = 1, 2
        while hi < 2_000_000 and await self.exists(f"{prefix}-{hi}"):
            lo, hi = hi, hi * 2
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if await self.exists(f"{prefix}-{mid}"):
                lo = mid
            else:
                hi = mid
        return lo

    async def floor(self, gift_id: int | None) -> int | None:
        """Ориентир цены: самый дешёвый лот коллекции на маркете Telegram (в Stars)."""
        if not gift_id:
            return None
        cached = self._floor.get(gift_id)
        if cached and time.time() - cached[0] < 600:
            return cached[1]
        val = None
        try:
            r = await self.client(GetResaleStarGiftsRequest(
                gift_id=gift_id, offset="", limit=1, sort_by_price=True))
            if r.gifts:
                amt = getattr(r.gifts[0], "resell_amount", None)
                if isinstance(amt, list):
                    amt = next((a.amount for a in amt if type(a).__name__ == "StarsAmount"), None)
                if isinstance(amt, int):
                    val = amt
        except Exception as e:  # схема TL меняется - не роняем мониторинг
            log.debug("floor failed: %s", e)
        self._floor[gift_id] = (time.time(), val)
        return val

    # ---------- collections ----------
    async def refresh_collections(self) -> None:
        titles = set(config.COLLECTIONS)
        if not titles:
            try:
                r = await self.client(GetStarGiftsRequest(hash=0))
                titles = {g.title for g in getattr(r, "gifts", []) if getattr(g, "title", None)}
            except Exception as e:
                log.warning("discovery failed: %s", e)
        self.cursors = await db.get_cursors()
        for t in titles:
            p = prefix_of(t)
            if p and p not in self.cursors:
                n = await self.find_max(p)
                await db.set_cursor(p, n)
                self.cursors[p] = n
                log.info("new collection %s baseline=%s", p, n)
        self._last_refresh = time.monotonic()
        log.info("tracking %d collections", len(self.cursors))

    # ---------- main loop ----------
    async def _emit(self, data: dict) -> None:
        try:
            data["floor"] = await self.floor(data.get("gift_id"))
            await self.on_gift(data)
        except Exception:
            log.exception("on_gift failed")

    async def check(self, prefix: str) -> None:
        async with self.sem:
            n = self.cursors[prefix] + 1
            while True:
                g = await self.fetch(f"{prefix}-{n}")
                if g is None:
                    break
                data = parse(g, prefix)
                self.cursors[prefix] = n
                await db.set_cursor(prefix, n)
                if await db.save_gift(data):
                    t = asyncio.create_task(self._emit(data))
                    self._tasks.add(t)
                    t.add_done_callback(self._tasks.discard)
                n += 1

    async def run(self) -> None:
        await self.refresh_collections()
        while True:
            if time.monotonic() - self._last_refresh > 1800:
                await self.refresh_collections()
            res = await asyncio.gather(*(self.check(p) for p in list(self.cursors)),
                                       return_exceptions=True)
            for r in res:
                if isinstance(r, Exception):
                    log.error("check error: %r", r)
            await asyncio.sleep(config.POLL_INTERVAL)
