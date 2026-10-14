import asyncio
import html
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice,
    Message, PreCheckoutQuery,
)
from telethon import TelegramClient

from . import config, db

log = logging.getLogger("bot")
router = Router()

WELCOME = (
    "<b>Gift Track Bot</b>\n\n"
    "Приватный инструмент для выкупа только что улучшенных Telegram-подарков.\n\n"
    "<b>Что умеет:</b>\n"
    "— мгновенные уведомления о каждом улучшении\n"
    "— точный поиск владельца: пишешь ему первым\n"
    "— гибкие фильтры: коллекции, модели, фоны, диапазон цен\n"
    "— режим выгодных предложений: только редкие подарки\n"
    "— избранное: сохраняй подарки одной кнопкой\n"
    "— старт и стоп работы в любой момент\n\n"
    "Управление и подписка — в приложении (кнопка меню рядом с полем ввода)."
)


def invoice_args(days: int) -> dict:
    return dict(
        title=f"Подписка Gift Track · {days} дн.",
        description="Доступ к мгновенным уведомлениям об улучшенных подарках и фильтрам.",
        payload=f"sub:{days}",
        currency="XTR",
        prices=[LabeledPrice(label=f"{days} дней", amount=config.PLANS[days])],
        provider_token="",
    )


# ---------- commands ----------
@router.message(Command("start"))
async def start(m: Message):
    await db.upsert_user(m.from_user.id, m.from_user.username)
    u = await db.get_user(m.from_user.id)
    if not config.PAID_MODE and not db.has_access(m.from_user.id, u):
        return await m.answer("🔒 Приватный бот. Доступ только по приглашению.\n"
                              f"Твой ID: <code>{m.from_user.id}</code> — отправь его владельцу.")
    await m.answer(WELCOME)


@router.message(Command("status"))
async def status(m: Message):
    u = await db.get_user(m.from_user.id)
    if db.has_access(m.from_user.id, u):
        state = "работает" if u and u["enabled"] else "остановлен"
        until = ""
        if config.PAID_MODE and u and db.sub_active(u):
            until = f" Подписка до {u['sub_until']:%d.%m.%Y %H:%M} UTC."
        await m.answer(f"✅ Доступ открыт. Бот {state}.{until}")
    elif config.PAID_MODE:
        await m.answer("Подписки нет. Оформи её в приложении.")
    else:
        await m.answer("🔒 Приватный бот. Доступа нет.")


@router.message(Command("grant"))
async def grant(m: Message, command: CommandObject):
    if m.from_user.id not in config.ADMIN_IDS:
        return
    try:
        uid, days = (int(x) for x in (command.args or "").split())
    except ValueError:
        return await m.answer("Использование: /grant <user_id> <дней>")
    u = await db.extend_sub(uid, days)
    await m.answer(f"Ок. {uid} — до {u['sub_until']:%d.%m.%Y %H:%M} UTC")


@router.message(Command("allow"))
async def allow(m: Message, command: CommandObject):
    if m.from_user.id not in config.ADMIN_IDS:
        return
    try:
        uid = int((command.args or "").strip())
    except ValueError:
        return await m.answer("Использование: /allow <user_id>\nЧеловек должен сначала нажать /start у бота.")
    await db.set_allowed(uid, True)
    await m.answer(f"✅ Доступ выдан: {uid}")


@router.message(Command("deny"))
async def deny(m: Message, command: CommandObject):
    if m.from_user.id not in config.ADMIN_IDS:
        return
    try:
        uid = int((command.args or "").strip())
    except ValueError:
        return await m.answer("Использование: /deny <user_id>")
    await db.set_allowed(uid, False)
    await db.set_enabled(uid, False)
    await m.answer(f"⛔ Доступ закрыт: {uid}")


# ---------- payments (Telegram Stars, только PAID_MODE=1) ----------
@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    if not config.PAID_MODE:
        return await q.answer(ok=False, error_message="Платный режим выключен")
    await q.answer(ok=True)


@router.message(F.successful_payment)
async def paid(m: Message):
    days = int(m.successful_payment.invoice_payload.split(":")[1])
    u = await db.extend_sub(m.from_user.id, days)
    await m.answer(f"✅ Оплачено. Подписка активна до {u['sub_until']:%d.%m.%Y %H:%M} UTC.\n"
                   "Открой приложение, настрой фильтры и нажми «Начать работу».")


# ---------- favorites ----------
@router.callback_query(F.data.startswith("fav:"))
async def fav(c: CallbackQuery):
    added = await db.toggle_fav(c.from_user.id, c.data[4:])
    await c.answer("⭐ Добавлено в избранное" if added else "Убрано из избранного")


# ---------- matching ----------
def _ok(selected: list[str], value: str | None) -> bool:
    if not selected:
        return True
    return bool(value) and any(s.lower() in value.lower() for s in selected)


def eff_price(d: dict) -> int | None:
    return d["ask"] if d.get("ask") is not None else d.get("floor")


def min_rarity(d: dict) -> int | None:
    vals = [d[k] for k in ("model_rarity", "backdrop_rarity", "symbol_rarity") if d.get(k) is not None]
    return min(vals) if vals else None


def matches(u: dict, d: dict) -> bool:
    if not (_ok(u["collections"], d["collection"]) and _ok(u["models"], d["model"])
            and _ok(u["backdrops"], d["backdrop"])):
        return False
    p = eff_price(d)
    if p is not None:  # если цена неизвестна — подарок не отсекаем
        if u["price_min"] is not None and p < u["price_min"]:
            return False
        if u["price_max"] is not None and p > u["price_max"]:
            return False
    if u["profit_mode"]:
        r = min_rarity(d)  # в промилле: 10 = 1%
        if r is None or r > u["profit_rarity"] * 10:
            return False
        ask, floor = d.get("ask"), d.get("floor")
        if ask is not None and floor is not None and ask > floor * 1.5:
            return False  # выставлен сильно дороже флора - не выгодно
    return True


# ---------- notifications ----------
def _pct(r: int | None) -> str:
    return f" ({r / 10:g}%)" if r is not None else ""


def render(d: dict) -> str:
    e = html.escape
    if d.get("ask") is not None:
        price = f"{d['ask']} ⭐ (цена в листинге)"
    elif d.get("floor") is not None:
        price = f"~{d['floor']} ⭐ (флор коллекции)"
    else:
        price = "нет данных"
    if d.get("owner_name"):
        owner = e(d["owner_name"])
    elif d.get("owner_address"):
        owner = f"кошелёк <code>{e(d['owner_address'])}</code>"
    else:
        owner = "скрыт"
    total = f"/{d['total']}" if d.get("total") else ""
    return (
        f"🎁 <b>{e(d['collection'])} #{d['num']}</b>\n\n"
        f"Модель: <b>{e(d['model'] or '—')}</b>{_pct(d['model_rarity'])}\n"
        f"Фон: <b>{e(d['backdrop'] or '—')}</b>{_pct(d['backdrop_rarity'])}\n"
        f"Узор: <b>{e(d['symbol'] or '—')}</b>{_pct(d['symbol_rarity'])}\n"
        f"Выпуск: {d['num']}{total}\n"
        f"Цена: {price}\n"
        f"Владелец: {owner}"
    )


async def owner_url(client: TelegramClient, d: dict) -> str | None:
    uid = d.get("owner_id")
    if not uid:
        return None
    try:
        ent = await client.get_entity(uid)
        if getattr(ent, "username", None):
            return f"https://t.me/{ent.username}"
    except Exception:
        pass
    return f"tg://user?id={uid}"


async def broadcast(bot: Bot, client: TelegramClient, d: dict) -> None:
    users = [u for u in await db.active_users() if matches(u, d)]
    if not users:
        return
    rows = [[InlineKeyboardButton(text="🎁 Открыть подарок", url=f"https://t.me/nft/{d['slug']}")]]
    url = await owner_url(client, d)
    if url:
        rows.append([InlineKeyboardButton(text="👤 Написать владельцу", url=url)])
    rows.append([InlineKeyboardButton(text="⭐ В избранное", callback_data=f"fav:{d['slug']}")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    text = render(d)
    for u in users:
        try:
            await bot.send_message(u["user_id"], text, reply_markup=kb)
        except Exception as e:
            log.debug("send to %s failed: %s", u["user_id"], e)
        await asyncio.sleep(0.04)
    log.info("%s -> %d users", d["slug"], len(users))
