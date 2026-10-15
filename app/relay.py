"""Диалоги с владельцами подарков от имени юзербота.

Работает ТОЛЬКО по нажатию кнопки «✉️ Написать» в уведомлении - никакой авто-рассылки.
Ответы владельцев пересылаются в бота с кнопкой «Ответить».

Защита аккаунта от спам-лимитов:
  * пауза между отправками (RELAY_MIN_GAP), лимит новых диалогов на человека и общий лимит в сутки;
  * одному владельцу первым сообщением не пишем повторно (72 часа);
  * при PeerFlood/FloodWait отправка автоматически ставится на паузу, админы получают сообщение;
  * пересылаются только ответы тех, кому мы писали, остальные личные сообщения аккаунта не трогаем.
"""
import asyncio
import html
import logging
import time

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, PeerFloodError, RPCError, UserIsBlockedError
from telethon.tl.types import InputPeerUser

from . import config, db

log = logging.getLogger("relay")
router = Router()

DEFAULT_TEMPLATES = [
    "Привет! Интересует ваш подарок {collection} #{num}. Готовы продать? Какая цена?",
    "Здравствуйте! Хочу купить {collection} #{num}. Могу отправить предложение через Offer to Buy, подойдёт?",
    "Привет! Подарок {collection} #{num} продаёте?",
]


class SendError(Exception):
    def __init__(self, text: str, alert: str | None = None):
        super().__init__(text)
        self.text = text
        self.alert = alert


class Compose(StatesGroup):
    text = State()


class Reply(StatesGroup):
    text = State()


_lock = asyncio.Lock()
_last = 0.0
_paused_until = 0.0
_inflight: set[tuple] = set()


# ---------- отправка ----------
async def deliver(client: TelegramClient, owner_id: int, owner_hash: int, text: str) -> None:
    """Одно сообщение владельцу: с паузой между отправками и защитой от лимитов."""
    global _last, _paused_until
    if time.time() < _paused_until:
        raise SendError("Отправка приостановлена из-за ограничений Telegram. Попробуй позже.")
    async with _lock:
        wait = _last + config.RELAY_MIN_GAP - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            await client.send_message(InputPeerUser(user_id=owner_id, access_hash=owner_hash), text)
        except PeerFloodError:
            _paused_until = time.time() + 86400
            raise SendError("Telegram ограничил аккаунту отправку сообщений незнакомым (спам-лимит). "
                            "Отправка отключена на 24 часа.", alert="peerflood")
        except FloodWaitError as e:
            _paused_until = time.time() + e.seconds + 5
            raise SendError(f"Telegram просит подождать {e.seconds} с.",
                            alert="relayflood" if e.seconds >= 300 else None)
        except UserIsBlockedError:
            raise SendError("Владелец заблокировал этот аккаунт.")
        except RPCError as e:
            m = str(e).upper()
            if "PRIVACY" in m:
                raise SendError("Владелец ограничил сообщения от незнакомых.")
            if "PREMIUM" in m:
                raise SendError("Владелец принимает сообщения только от Premium-пользователей.")
            if "PAYMENT" in m:
                raise SendError("Владелец берёт плату за сообщения от незнакомых — напиши ему сам.")
            raise SendError(f"Telegram отклонил отправку: {e}")
        finally:
            _last = time.monotonic()


async def first_message(client: TelegramClient, monitor, buyer_id: int, slug: str, text: str) -> int:
    """Первое сообщение владельцу подарка; возвращает id диалога."""
    g = await monitor.fetch(slug)  # заодно обновляет данные владельца у клиента
    if g is None:
        raise SendError("Подарок не найден.")
    uid = getattr(getattr(g, "owner_id", None), "user_id", None)
    if not uid:
        raise SendError("Владельцу сейчас нельзя написать (скрыт или подарок выведен).")
    try:
        owner_hash = (await client.get_input_entity(uid)).access_hash
    except Exception:
        raise SendError("Не удалось получить доступ к владельцу.")
    await deliver(client, uid, owner_hash, text)
    return await db.create_thread(buyer_id, uid, owner_hash, slug, getattr(g, "title", slug), g.num)


# ---------- входящие ответы ----------
def setup(client: TelegramClient, bot: Bot) -> None:
    async def on_message(event) -> None:
        try:
            if not event.is_private or event.out:
                return
            th = await db.latest_thread_for_owner(event.sender_id)
            if not th:
                return  # не наш собеседник - личные сообщения аккаунта не пересылаем
            await db.touch_thread_in(th["id"])
            await db.log_event(th["buyer_id"], "reply_in", th["slug"])
            sender = await event.get_sender()
            name = " ".join(x for x in (getattr(sender, "first_name", None),
                                        getattr(sender, "last_name", None)) if x)
            uname = getattr(sender, "username", None)
            label = f"{name} (@{uname})" if name and uname else (f"@{uname}" if uname else name or "владелец")
            text = event.raw_text or ""
            if event.message.media:
                text = (text + "\n" if text else "") + "📎 вложение (открой чат с владельцем, чтобы посмотреть)"
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="↩️ Ответить", callback_data=f"rep:{th['id']}")],
                [InlineKeyboardButton(text="🎁 Открыть подарок", url=f"https://t.me/nft/{th['slug']}")],
            ])
            await bot.send_message(
                th["buyer_id"],
                f"💬 <b>Ответ по {html.escape(th['collection'])} #{th['num']}</b>\n"
                f"от {html.escape(label)}:\n\n{html.escape(text[:3500])}",
                reply_markup=kb)
        except Exception:
            log.exception("incoming relay failed")

    client.add_event_handler(on_message, events.NewMessage(incoming=True))


# ---------- интерфейс в боте ----------
def _tpls() -> list[str]:
    return config.RELAY_TEMPLATES or DEFAULT_TEMPLATES


def _fmt(t: str, g: dict) -> str:
    try:
        return t.format(collection=g["collection"], num=g["num"])
    except (KeyError, IndexError, ValueError):
        return t


async def _allowed(uid: int) -> bool:
    return config.RELAY and db.has_access(uid, await db.get_user(uid))


async def _guard(c: CallbackQuery) -> bool:
    if not config.RELAY:
        await c.answer("Отправка сообщений выключена", show_alert=True)
        return False
    if not await _allowed(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return False
    return True


async def _send_first(message: Message, uid: int, slug: str, text: str, client, monitor) -> None:
    key = (uid, slug)
    if key in _inflight:
        return await message.answer("Уже отправляю…")
    _inflight.add(key)
    try:
        await first_message(client, monitor, uid, slug, text)
        await db.log_event(uid, "msg_manual", slug)
    except SendError as e:
        if e.alert:
            await monitor.alert(e.alert, f"⚠️ {e.text}", cooldown=3600)
        return await message.answer(f"❌ {e.text}")
    finally:
        _inflight.discard(key)
    await message.answer("✅ Сообщение отправлено владельцу. Если он ответит, я перешлю ответ сюда.")


@router.message(Command("cancel"))
async def cancel_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Отменено.")


@router.callback_query(F.data == "cancel")
async def cancel_cb(c: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await c.message.delete()
    except Exception:
        pass
    await c.answer("Отменено")


@router.callback_query(F.data.startswith("msg:"))
async def msg_start(c: CallbackQuery):
    if not await _guard(c):
        return
    slug = c.data[4:]
    g = await db.get_gift(slug)
    if not g or not g.get("owner_id"):
        return await c.answer("Владельцу нельзя написать", show_alert=True)
    th = await db.thread_for(c.from_user.id, g["owner_id"])
    if th:
        if th["last_in"]:
            await c.answer()
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="↩️ Продолжить диалог", callback_data=f"rep:{th['id']}")]])
            return await c.message.answer("Владелец уже отвечал. Продолжить диалог?", reply_markup=kb)
        return await c.answer("Ты уже писал этому владельцу — ждём ответ.", show_alert=True)
    if await db.count_threads(24, c.from_user.id) >= config.RELAY_DAILY_LIMIT:
        return await c.answer(f"Дневной лимит новых диалогов ({config.RELAY_DAILY_LIMIT}) исчерпан.", show_alert=True)
    if await db.count_threads(24, None) >= config.RELAY_GLOBAL_DAILY:
        return await c.answer("Общий дневной лимит новых диалогов исчерпан.", show_alert=True)
    await c.answer()
    tpls = _tpls()
    lines = [f"<b>{i}.</b> {html.escape(_fmt(t, g))}" for i, t in enumerate(tpls, 1)]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=str(i), callback_data=f"tpl:{i - 1}:{slug}") for i in range(1, len(tpls) + 1)],
        [InlineKeyboardButton(text="✍️ Свой текст", callback_data=f"own:{slug}")],
        [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
    ])
    await c.message.answer(f"✉️ <b>Сообщение владельцу {html.escape(g['collection'])} #{g['num']}</b>\n"
                           "Выбери шаблон — он уйдёт с твоего аккаунта:\n\n" + "\n\n".join(lines), reply_markup=kb)


@router.callback_query(F.data.startswith("tpl:"))
async def tpl_pick(c: CallbackQuery, client: TelegramClient, monitor):
    if not await _guard(c):
        return
    _, i, slug = c.data.split(":", 2)
    g = await db.get_gift(slug)
    tpls = _tpls()
    if not g or not i.isdigit() or int(i) >= len(tpls):
        return await c.answer("Устарело", show_alert=True)
    await c.answer("Отправляю…")
    await _send_first(c.message, c.from_user.id, slug, _fmt(tpls[int(i)], g), client, monitor)


@router.callback_query(F.data.startswith("own:"))
async def own_start(c: CallbackQuery, state: FSMContext):
    if not await _guard(c):
        return
    await state.set_state(Compose.text)
    await state.update_data(slug=c.data[4:])
    await c.answer()
    await c.message.answer("✍️ Напиши текст сообщения владельцу (до 1000 символов). /cancel — отмена.")


@router.message(Compose.text, F.text)
async def own_text(m: Message, state: FSMContext, client: TelegramClient, monitor):
    text = m.text.strip()
    if len(text) > 1000:
        return await m.answer("Слишком длинно, максимум 1000 символов. Сократи или /cancel.")
    data = await state.get_data()
    await state.clear()
    if not await _allowed(m.from_user.id):
        return await m.answer("Нет доступа.")
    await _send_first(m, m.from_user.id, data["slug"], text, client, monitor)


@router.callback_query(F.data.startswith("rep:"))
async def rep_start(c: CallbackQuery, state: FSMContext):
    if not await _guard(c):
        return
    tid = int(c.data[4:])
    th = await db.get_thread(tid)
    if not th or th["buyer_id"] != c.from_user.id:
        return await c.answer("Диалог не найден", show_alert=True)
    await state.set_state(Reply.text)
    await state.update_data(tid=tid)
    await c.answer()
    await c.message.answer("↩️ Напиши ответ (до 1000 символов). /cancel — отмена.")


@router.message(Reply.text, F.text)
async def rep_text(m: Message, state: FSMContext, client: TelegramClient, monitor):
    text = m.text.strip()
    if len(text) > 1000:
        return await m.answer("Слишком длинно, максимум 1000 символов. Сократи или /cancel.")
    data = await state.get_data()
    await state.clear()
    if not await _allowed(m.from_user.id):
        return await m.answer("Нет доступа.")
    th = await db.get_thread(data["tid"])
    if not th or th["buyer_id"] != m.from_user.id:
        return await m.answer("Диалог не найден.")
    try:
        await deliver(client, th["owner_id"], th["owner_hash"], text)
        await db.log_event(m.from_user.id, "reply_out", th["slug"])
    except SendError as e:
        if e.alert:
            await monitor.alert(e.alert, f"⚠️ {e.text}", cooldown=3600)
        return await m.answer(f"❌ {e.text}")
    await m.answer("✅ Отправлено.")


# ---------- авто-режим: N сообщений за запуск, с паузой между ними ----------
TXT_AUTO = "🤖 Авто"
STATE_LABEL = {"off": "выключен", "on": "работает", "paused": "на паузе"}

_auto_pending: dict[int, int] = {}
_auto_locks: dict[int, asyncio.Lock] = {}
_auto_last: dict[int, float] = {}
_auto_tasks: set[asyncio.Task] = set()


def auto_text(u: dict, d: dict) -> str:
    if u.get("auto_text"):
        return _fmt(u["auto_text"], d)
    tpls = _tpls()
    return _fmt(tpls[min(u.get("auto_tpl", 0), len(tpls) - 1)], d)


def schedule_auto(bot: Bot, client: TelegramClient, monitor, u: dict, d: dict) -> None:
    """Ставит задачу авто-сообщения владельцу подарка, который подошёл под фильтры пользователя."""
    uid = u["user_id"]
    if not d.get("owner_id") or d["owner_id"] == uid:
        return
    limit = min(u["auto_limit"], config.AUTO_MAX)
    if u["auto_sent"] + _auto_pending.get(uid, 0) >= limit:
        return  # уже запланировано достаточно
    _auto_pending[uid] = _auto_pending.get(uid, 0) + 1
    t = asyncio.create_task(_auto_job(bot, client, monitor, uid, d))
    _auto_tasks.add(t)
    t.add_done_callback(_auto_tasks.discard)


async def _tell(bot: Bot, uid: int, text: str) -> None:
    try:
        await bot.send_message(uid, text)
    except Exception as e:
        log.debug("tell %s failed: %s", uid, e)


async def _auto_job(bot: Bot, client: TelegramClient, monitor, uid: int, d: dict) -> None:
    title = f"{d['collection']} #{d['num']}"
    try:
        async with _auto_locks.setdefault(uid, asyncio.Lock()):
            u = await db.get_user(uid)
            if not (u and u["auto_state"] == "on" and db.auto_allowed(u) and db.has_access(uid, u)):
                return
            if u["auto_sent"] >= min(u["auto_limit"], config.AUTO_MAX):
                return
            if await db.thread_for(uid, d["owner_id"]):
                return  # этому владельцу уже писали
            if (await db.count_threads(24, uid) >= config.RELAY_DAILY_LIMIT
                    or await db.count_threads(24, None) >= config.RELAY_GLOBAL_DAILY):
                await db.auto_control(uid, "pause")
                return await _tell(bot, uid, "🤖 Авто поставлен на паузу: достигнут дневной лимит новых диалогов.")
            wait = _auto_last.get(uid, 0.0) + u["auto_gap"] - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            u = await db.get_user(uid)  # за время паузы режим могли остановить
            if not u or u["auto_state"] != "on":
                return
            try:
                await first_message(client, monitor, uid, d["slug"], auto_text(u, d))
            except SendError as e:
                if e.alert:
                    await monitor.alert(e.alert, f"⚠️ {e.text}", cooldown=3600)
                    await db.auto_control(uid, "pause")
                await _tell(bot, uid, f"🤖 Авто: не удалось написать владельцу {title}: {e.text}")
                return
            finally:
                _auto_last[uid] = time.monotonic()
            row = await db.auto_incr(uid)
            await db.log_event(uid, "msg_auto", d["slug"])
            msg = f"🤖 Авто: написал владельцу {title}. Отправлено {row['auto_sent']} из {row['auto_limit']}."
            if row["auto_state"] == "off":
                msg += "\n✅ Лимит достигнут, авто-режим выключен. Запусти снова при необходимости."
            await _tell(bot, uid, msg)
    except Exception:
        log.exception("auto job failed")
    finally:
        _auto_pending[uid] = max(0, _auto_pending.get(uid, 1) - 1)


def auto_panel(u: dict) -> tuple[str, InlineKeyboardMarkup]:
    st = u["auto_state"]
    text = (f"🤖 <b>Авто-режим:</b> {STATE_LABEL.get(st, st)}\n"
            f"Отправлено: {u['auto_sent']} из {min(u['auto_limit'], config.AUTO_MAX)}\n"
            f"Пауза между сообщениями: {u['auto_gap']} с\n\n"
            "Бот сам пишет владельцам подарков, которые подошли под твои фильтры, пока не наберёт лимит. "
            "Настройки (лимит, пауза, текст) — в приложении, вкладка «Авто».")
    if not u["enabled"]:
        text += "\n\n⚠️ Работа остановлена — нажми «▶ Начать работу», иначе подарки не приходят."
    B = InlineKeyboardButton
    if st == "on":
        rows = [[B(text="⏸ Пауза", callback_data="auto:pause"), B(text="⏹ Стоп", callback_data="auto:stop")]]
    elif st == "paused":
        rows = [[B(text="▶ Продолжить", callback_data="auto:resume"), B(text="⏹ Стоп", callback_data="auto:stop")]]
    else:
        rows = [[B(text=f"▶ Запустить на {min(u['auto_limit'], config.AUTO_MAX)}", callback_data="auto:start")]]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text == TXT_AUTO)
async def auto_btn(m: Message):
    u = await db.get_user(m.from_user.id)
    if not (u and db.has_access(m.from_user.id, u) and db.auto_allowed(u)):
        return await m.answer("🔒 Авто-режим не включён для тебя. Его выдаёт админ.")
    text, kb = auto_panel(u)
    await m.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("auto:"))
async def auto_cb(c: CallbackQuery):
    u = await db.get_user(c.from_user.id)
    if not (u and db.has_access(c.from_user.id, u) and db.auto_allowed(u)):
        return await c.answer("Нет доступа", show_alert=True)
    action = c.data[5:]
    if action not in ("start", "pause", "resume", "stop"):
        return await c.answer()
    u = await db.auto_control(c.from_user.id, action)
    await db.log_event(c.from_user.id, f"auto_{action}")
    text, kb = auto_panel(u)
    try:
        await c.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass
    await c.answer("Готово")
