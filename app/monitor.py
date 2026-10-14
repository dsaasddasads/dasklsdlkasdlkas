"""Мониторинг новых улучшенных подарков через MTProto (Telethon).

У каждой коллекции улучшенные подарки нумеруются подряд (PlushPepe-1, -2, ...).
Храним последний известный номер и проверяем last+1. Если подарок существует - это новое улучшение.

Чтобы Telegram не сбрасывал сессию за частые запросы:
  * один глобальный лимитер запросов (RPS) на всё приложение;
  * адаптивные интервалы: "горячие" коллекции опрашиваются чаще, "холодные" реже;
  * случайный разброс (jitter) и штраф к скорости после FloodWait;
  * проверка по одной коллекции за раз, без пачек параллельных запросов.
"""
import asyncio
import logging
import random
import re
import time
from typing import Awaitable, Callable

from telethon import TelegramClient
from telethon.errors import (
    AuthKeyDuplicatedError, AuthKeyUnregisteredError, FloodWaitError, RPCError,
    SessionExpiredError, SessionRevokedError, UserDeactivatedBanError, UserDeactivatedError,
)
from telethon.tl.functions.payments import (
    GetResaleStarGiftsRequest,
    GetStarGiftsRequest,
    GetUniqueStarGiftRequest,
)

from . import config, db

log = logging.getLogger("monitor")

FATAL = (AuthKeyDuplicatedError, AuthKeyUnregisteredError, SessionRevokedError,
         SessionExpiredError, UserDeactivatedError, UserDeactivatedBanError)


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


class Limiter:
    """Глобальный ограничитель скорости запросов с штрафом после FloodWait."""

    def __init__(self, rps: float):
        self.gap = 1.0 / max(rps, 0.1)
        self.penalty = 1.0
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next - now)
            self._next = max(now, self._next) + self.gap * self.penalty
            self.penalty = max(1.0, self.penalty * 0.98)  # штраф постепенно спадает
        if delay:
            await asyncio.sleep(delay)

    def punish(self) -> None:
        self.penalty = min(self.penalty * 2, 8.0)


class Monitor:
    def __init__(self, client: TelegramClient, on_gift: Callable[[dict], Awaitable[None]]):
        self.client = client
        self.on_gift = on_gift
        self.limiter = Limiter(config.RPS)
        self.state: dict[str, dict] = {}  # prefix -> {cursor, interval, next}
        self._floor: dict[int, tuple[float, int | None]] = {}
        self._last_refresh = 0.0
        self._tasks: set[asyncio.Task] = set()

    # ---------- low level ----------
    async def fetch(self, slug: str):
        while True:
            await self.limiter.wait()
            try:
                r = await self.client(GetUniqueStarGiftRequest(slug=slug))
                return r.gift
            except FATAL as e:
                raise SystemExit(
                    f"Сессия Telegram сброшена ({type(e).__name__}). Сгенерируй новый SESSION_STRING "
                    "и не используй его одновременно в двух местах.")
            except FloodWaitError as e:
                self.limiter.punish()
                log.warning("FloodWait %ss - замедляюсь", e.seconds)
                await asyncio.sleep(e.seconds + 2)
            except RPCError as e:
                msg = str(e).upper()
                if "STARGIFT" not in msg and "SLUG" not in msg:
                    log.warning("RPC error on %s: %s", slug, e)
                return None  # такого номера ещё нет

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
            await self.limiter.wait()
            r = await self.client(GetResaleStarGiftsRequest(
                gift_id=gift_id, offset="", limit=1, sort_by_price=True))
            if r.gifts:
                amt = getattr(r.gifts[0], "resell_amount", None)
                if isinstance(amt, list):
                    amt = next((a.amount for a in amt if type(a).__name__ == "StarsAmount"), None)
                if isinstance(amt, int):
                    val = amt
        except FATAL:
            raise
        except Exception as e:  # схема TL меняется - не роняем мониторинг
            log.debug("floor failed: %s", e)
        self._floor[gift_id] = (time.time(), val)
        return val

    # ---------- collections ----------
    async def refresh_collections(self) -> None:
        titles = set(config.COLLECTIONS)
        if not titles:
            try:
                await self.limiter.wait()
                r = await self.client(GetStarGiftsRequest(hash=0))
                titles = {g.title for g in getattr(r, "gifts", []) if getattr(g, "title", None)}
            except FATAL:
                raise
            except Exception as e:
                log.warning("discovery failed: %s", e)
        cursors = await db.get_cursors()
        prefixes = {prefix_of(t) for t in titles if prefix_of(t)} | set(cursors)
        for p in prefixes:
            if p not in self.state:
                self.state[p] = {
                    "cursor": cursors.get(p),  # None = нужен стартовый поиск максимума
                    "interval": config.MIN_INTERVAL,
                    "next": time.monotonic() + random.uniform(0, config.MIN_INTERVAL),
                }
        self._last_refresh = time.monotonic()
        log.info("tracking %d collections", len(self.state))

    # ---------- main loop ----------
    async def _emit(self, data: dict) -> None:
        try:
            data["floor"] = await self.floor(data.get("gift_id"))
            await self.on_gift(data)
        except FATAL:
            raise
        except Exception:
            log.exception("on_gift failed")

    async def _advance(self, prefix: str, s: dict) -> bool:
        found = False
        n = s["cursor"] + 1
        while True:
            g = await self.fetch(f"{prefix}-{n}")
            if g is None:
                return found
            data = parse(g, prefix)
            s["cursor"] = n
            await db.set_cursor(prefix, n)
            if await db.save_gift(data):
                t = asyncio.create_task(self._emit(data))
                self._tasks.add(t)
                t.add_done_callback(self._tasks.discard)
            found = True
            n += 1

    async def check(self, prefix: str) -> None:
        s = self.state[prefix]
        found = False
        if s["cursor"] is None:
            s["cursor"] = await self.find_max(prefix)
            await db.set_cursor(prefix, s["cursor"])
            log.info("baseline %s=%s", prefix, s["cursor"])
        else:
            found = await self._advance(prefix, s)
        # горячая коллекция - часто, пустая - всё реже
        s["interval"] = config.MIN_INTERVAL if found else min(s["interval"] * 1.5, config.MAX_INTERVAL)
        s["next"] = time.monotonic() + s["interval"] * random.uniform(0.8, 1.2)

    async def run(self) -> None:
        await self.refresh_collections()
        while True:
            if time.monotonic() - self._last_refresh > 1800:
                await self.refresh_collections()
            if not self.state:
                await asyncio.sleep(10)
                continue
            p = min(self.state, key=lambda k: self.state[k]["next"])
            wait = self.state[p]["next"] - time.monotonic()
            if wait > 0:
                await asyncio.sleep(min(wait, 5))
                continue
            try:
                await self.check(p)
            except SystemExit:
                raise
            except Exception:
                log.exception("check %s failed", p)
                self.state[p]["next"] = time.monotonic() + config.MAX_INTERVAL
