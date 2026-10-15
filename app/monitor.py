"""Мониторинг новых улучшенных подарков через MTProto (Telethon).

У каждой коллекции улучшенные подарки нумеруются подряд (PlushPepe-1, -2, ...).
Храним последний известный номер и проверяем last+1. Если подарок существует - это новое улучшение.

Чтобы Telegram не сбрасывал сессию за частые запросы:
  * один глобальный лимитер запросов (RPS) на всё приложение;
  * адаптивные интервалы: "горячие" коллекции опрашиваются чаще, "холодные" реже;
  * случайный разброс (jitter) и штраф к скорости после FloodWait;
  * проверка по одной коллекции за раз, без пачек параллельных запросов.

Оценка рынка (valuate): для подарка, который кому-то подходит, берём цены текущих листингов
на маркете Telegram: флор коллекции, флор модели, флор фона, флор такой же пары модель+фон.
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
from telethon.tl import types as tl
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


def stars_of(amt) -> int | None:
    """resell_amount (список StarsAmount / число) -> цена в Stars."""
    if isinstance(amt, list):
        amt = next((a.amount for a in amt if type(a).__name__ == "StarsAmount"), None)
    return amt if isinstance(amt, int) else None


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
        "model": None, "model_rarity": None, "model_doc": None,
        "backdrop": None, "backdrop_rarity": None, "backdrop_id": None,
        "symbol": None, "symbol_rarity": None,
        "owner_id": getattr(oid, "user_id", None),
        "owner_name": getattr(g, "owner_name", None),
        "owner_address": getattr(g, "owner_address", None),
        "ask": stars_of(getattr(g, "resell_amount", None)),  # цена, если уже выставлен на продажу
        "est_usd": None,                                      # оценка самого Telegram, $
        "floor": None,
        "model_floor": None, "model_listed": None,
        "backdrop_floor": None, "backdrop_listed": None,
        "combo_floor": None, "combo_listed": None,
    }
    usd = getattr(g, "value_usd_amount", None)
    if isinstance(usd, int):
        d["est_usd"] = usd / 100  # предполагаем центы
    for a in g.attributes:
        kind = type(a).__name__
        r = getattr(a, "rarity_permille", None)
        r = r if isinstance(r, int) else None
        if kind == "StarGiftAttributeModel":
            d["model"], d["model_rarity"] = a.name, r
            d["model_doc"] = getattr(getattr(a, "document", None), "id", None)
        elif kind == "StarGiftAttributeBackdrop":
            d["backdrop"], d["backdrop_rarity"] = a.name, r
            d["backdrop_id"] = getattr(a, "backdrop_id", None)
        elif kind == "StarGiftAttributePattern":
            d["symbol"], d["symbol_rarity"] = a.name, r
    return d


def _attr_ids(d: dict):
    """Идентификаторы модели и фона для фильтра маркета (None, если схема Telethon старая)."""
    M = getattr(tl, "StarGiftAttributeIdModel", None)
    B = getattr(tl, "StarGiftAttributeIdBackdrop", None)
    model = M(document_id=d["model_doc"]) if M and d.get("model_doc") else None
    back = B(backdrop_id=d["backdrop_id"]) if B and d.get("backdrop_id") is not None else None
    return model, back


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
    def __init__(self, client: TelegramClient, on_gift: Callable[[dict], Awaitable[None]],
                 on_alert: Callable[[str], Awaitable[None]] | None = None):
        self.client = client
        self.on_gift = on_gift
        self.on_alert = on_alert
        self.started = time.monotonic()
        self.last_ok = time.monotonic()
        self.last_gift = time.monotonic()
        self.stalled = False
        self.limiter = Limiter(config.RPS)
        self.state: dict[str, dict] = {}  # prefix -> {cursor, interval, next}
        self._stats: dict[tuple, tuple[float, tuple]] = {}
        self._tasks: set[asyncio.Task] = set()
        self._last_refresh = 0.0

    # ---------- low level ----------
    async def fetch(self, slug: str):
        while True:
            await self.limiter.wait()
            try:
                r = await self.client(GetUniqueStarGiftRequest(slug=slug))
                self._touch()
                return r.gift
            except FATAL as e:
                await self.alert(
                    "fatal",
                    f"🛑 Сессия Telegram сброшена ({type(e).__name__}). Сгенерируй новый SESSION_STRING "
                    "и не запускай его в двух местах одновременно.")
                raise SystemExit(
                    f"Сессия Telegram сброшена ({type(e).__name__}). Сгенерируй новый SESSION_STRING "
                    "и не используй его одновременно в двух местах.")
            except FloodWaitError as e:
                self.limiter.punish()
                log.warning("FloodWait %ss - замедляюсь", e.seconds)
                await self._flood(e.seconds)
                await asyncio.sleep(e.seconds + 2)
            except RPCError as e:
                self._touch()  # сервер ответил - связь жива
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

    # ---------- рынок ----------
    async def stats(self, gift_id: int, attrs: list, key: str) -> tuple[int | None, int | None]:
        """(самая низкая цена, число листингов) по маркету Telegram для коллекции с фильтром по признакам."""
        ck = (gift_id, key)
        cached = self._stats.get(ck)
        if cached and time.time() - cached[0] < 600:
            return cached[1]
        price = count = None
        try:
            await self.limiter.wait()
            kw = {"attributes": attrs} if attrs else {}
            r = await self.client(GetResaleStarGiftsRequest(
                gift_id=gift_id, offset="", limit=1, sort_by_price=True, **kw))
            self._touch()
            count = getattr(r, "count", None)
            if r.gifts:
                price = stars_of(getattr(r.gifts[0], "resell_amount", None))
        except FATAL:
            raise
        except FloodWaitError as e:
            self.limiter.punish()
            log.warning("FloodWait %ss on market stats", e.seconds)
            await self._flood(e.seconds)
        except Exception as e:  # схема TL меняется - не роняем мониторинг
            log.debug("market stats failed: %s", e)
        res = (price, count)
        self._stats[ck] = (time.time(), res)
        return res

    async def floor(self, gift_id: int | None) -> int | None:
        """Флор коллекции (в Stars)."""
        if not gift_id:
            return None
        return (await self.stats(gift_id, [], "all"))[0]

    async def valuate(self, d: dict) -> None:
        """Дописывает в подарок флор модели, фона и пары модель+фон (с числом листингов)."""
        if d.get("valuated") or not d.get("gift_id"):
            return
        d["valuated"] = True
        gid = d["gift_id"]
        model, back = _attr_ids(d)
        if model:
            d["model_floor"], d["model_listed"] = await self.stats(gid, [model], f"m:{d['model_doc']}")
        if back:
            d["backdrop_floor"], d["backdrop_listed"] = await self.stats(
                gid, [back], f"b:{d['backdrop_id']}")
        if model and back:
            d["combo_floor"], d["combo_listed"] = await self.stats(
                gid, [model, back], f"mb:{d['model_doc']}:{d['backdrop_id']}")

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
            self.last_gift = time.monotonic()
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
        await self.alert("start", f"✅ Мониторинг запущен. Коллекций: {len(self.state)}.", cooldown=600)
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

    # ---------- контроль состояния ----------
    def _touch(self) -> None:
        self.last_ok = time.monotonic()

    async def alert(self, kind: str, text: str, cooldown: float = 1800) -> None:
        """Сообщение админам; одинаковые алерты не чаще, чем раз в cooldown секунд."""
        if self.on_alert is None:
            return
        try:
            if cooldown and not await db.should_alert(kind, cooldown):
                return
            await self.on_alert(text)
        except Exception:
            log.exception("alert failed")

    async def _flood(self, seconds: int) -> None:
        if seconds >= 120:
            await self.alert(
                "flood",
                f"⏳ Telegram просит подождать {seconds // 60} мин (FloodWait). Бот замедлился сам; "
                "если повторяется часто — уменьши RPS.", cooldown=3600)

    async def watchdog(self) -> None:
        """Раз в минуту проверяет, что мониторинг жив, и пишет админам при проблемах."""
        while True:
            await asyncio.sleep(60)
            now = time.monotonic()
            idle = now - self.last_ok
            if idle > config.STALL_SECONDS and not self.stalled:
                self.stalled = True
                await self.alert(
                    "stall",
                    f"⚠️ Мониторинг не получает ответов от Telegram уже {_ago(idle)}. "
                    "Возможен FloodWait или проблема с сессией. Состояние: /health")
            elif idle <= config.STALL_SECONDS and self.stalled:
                self.stalled = False
                await self.alert("recovered", "✅ Мониторинг снова получает ответы Telegram.", cooldown=0)
            quiet = now - self.last_gift
            if not self.stalled and quiet > config.NO_GIFTS_HOURS * 3600:
                await self.alert(
                    "quiet",
                    f"🤔 Новых улучшенных подарков нет уже {_ago(quiet)}. Проверь /health и логи.",
                    cooldown=config.NO_GIFTS_HOURS * 3600)

    def health(self) -> str:
        now = time.monotonic()
        pending = sum(1 for s in self.state.values() if s["cursor"] is None)
        rate = 1.0 / (self.limiter.gap * self.limiter.penalty)
        return (
            "🩺 <b>Состояние</b>\n"
            f"Работает: {_ago(now - self.started)}\n"
            f"Коллекций: {len(self.state)} (ждут стартовой настройки: {pending})\n"
            f"Последний ответ Telegram: {_ago(now - self.last_ok)} назад\n"
            f"Последний новый подарок: {_ago(now - self.last_gift)} назад\n"
            f"Скорость запросов: {rate:.1f}/с (штраф ×{self.limiter.penalty:.1f})"
        )


def _ago(sec: float) -> str:
    sec = int(sec)
    if sec < 60:
        return f"{sec} с"
    if sec < 3600:
        return f"{sec // 60} мин"
    h, m = divmod(sec // 60, 60)
    return f"{h} ч {m} мин"
