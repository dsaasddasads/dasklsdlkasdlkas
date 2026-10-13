import asyncio
import html
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice,
    Message, PreCheckoutQuery, WebAppInfo,
)
from telethon import TelegramClient

from . import config, db

log = logging.getLogger("bot")
router = Router()

WELCOME = (
    "<b>Добро пожаловать в Gift Track Bot!</b>\n\n"
    "<blockquote>Приватный инструмент для выкупа только что улучшенных Telegram-подарков.</blockquote>\n\n"
    "<i>— мгновенные уведомления об улучшениях\n"
    "— гибкие фильтры: модели, фона, коллекции, диапазон цен\n"
    "— инвайт в закрытый чат и полное сопровождение по выкупам</i>\n\n"
    "Чтобы открыть доступ — оформи подписку в приложении."
)

HELP = (
    "<b>What can this bot do?</b>\n\n"
    "Gift Track Bot — инструмент для выкупа только что улучшенных Telegram-подарков раньше остальных.\n\n"
    "— Моментальные уведомления о каждом улучшенном подарке\n"
    "— Точный поиск владельца, пишешь ему первым\n"
    "— Гибкая настройка фильтров: модели, фона, коллекции, диапазон цен\n"
    "— Полное сопровождение по выкупам и сделкам\n\n"
    "Ты видишь улучшенный подарок первым и забираешь его дешевле всех."
)


def main_kb() -> InlineKeyboardMarkup:
    rows = []
    if config.WEBAPP_URL:
        rows.append([InlineKeyboardButton(text="Открыть приложение",
                                          web_app=WebAppInfo(url=config.WEBAPP_URL))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def plans_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{d} дн. — {p} ⭐", callback_data=f"buy:{d}")]
        for d, p in config.PLANS.items()
    ])


def invoice_args(days: int) -> dict:
    return dict(
        title=f"Подписка Gift Track · {days} дн.",
        description="Доступ к мгновенным уведомлениям об улучшенных подарках, фильтрам и сопровождению сделок.",
        payload=f"sub:{days}",
        currency="XTR",
        prices=[LabeledPrice(label=f"{days} дней", amount=config.PLANS[days])],
        provider_token="",
    )


# ---------- commands ----------
@router.message(Command("start"))
async def start(m: Message):
    await db.upsert_user(m.from_user.id, m.from_user.username)
    await m.answer(WELCOME, reply_markup=main_kb())


@router.message(Command("help"))
async def help_(m: Message):
    await m.answer(HELP, reply_markup=main_kb())


@router.message(Command("sub"))
async def sub(m: Message):
    await db.upsert_user(m.from_user.id, m.from_user.username)
    await m.answer("Выбери срок подписки:", reply_markup=plans_kb())


@router.message(Command("status"))
async def status(m: Message):
    u = await db.get_user(m.from_user.id)
    if db.sub_active(u):
        await m.answer(f"✅ Подписка активна до {u['sub_until']:%d.%m.%Y %H:%M} UTC")
    else:
        await m.answer("Подписки нет. /sub — оформить.")


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


# ---------- payments (Telegram Stars) ----------
@router.callback_query(F.data.startswith("buy:"))
async def buy(c: CallbackQuery, bot: Bot):
    days = int(c.data.split(":")[1])
    if days not in config.PLANS:
        return await c.answer()
    await bot.send_invoice(chat_id=c.from_user.id, **invoice_args(days))
    await c.answer()


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)


@router.message(F.successful_payment)
async def paid(m: Message):
    days = int(m.successful_payment.invoice_payload.split(":")[1])
    u = await db.extend_sub(m.from_user.id, days)
    await m.answer(f"✅ Оплачено. Подписка активна до {u['sub_until']:%d.%m.%Y %H:%M} UTC.\n"
                   "Настрой фильтры в приложении.", reply_markup=main_kb())


# ---------- deals ----------
@router.callback_query(F.data.startswith("deal:"))
async def deal(c: CallbackQuery, bot: Bot, monitor):
    u = await db.get_user(c.from_user.id)
    if not db.sub_active(u):
        return await c.answer("Нужна активная подписка", show_alert=True)
    slug = c.data.split(":", 1)[1]
    await c.answer("Создаю чат…")
    deal_id = await db.create_deal(slug, c.from_user.id)
    try:
        link = await monitor.create_deal_chat(f"Сделка #{deal_id} · {slug}")
    except Exception as e:
        log.exception("deal chat failed")
        return await bot.send_message(c.from_user.id, f"Не удалось создать чат: {e}")
    await db.set_deal_link(deal_id, link)
    text = (f"🤝 <b>Сделка #{deal_id}</b>\n🎁 https://t.me/nft/{slug}\n"
            f"Закрытый чат: {link}\n\nПригласи владельца в чат — гарант подключится.")
    await bot.send_message(c.from_user.id, text)
    for admin in config.ADMIN_IDS:
        try:
            await bot.send_message(admin, text + f"\nПокупатель: <code>{c.from_user.id}</code>")
        except Exception:
            pass


# ---------- notifications ----------
def _ok(selected: list[str], value: str | None) -> bool:
    if not selected:
        return True
    return bool(value) and any(s.lower() in value.lower() for s in selected)


def matches(u: dict, d: dict) -> bool:
    if not (_ok(u["collections"], d["collection"]) and _ok(u["models"], d["model"])
            and _ok(u["backdrops"], d["backdrop"])):
        return False
    p = d.get("price")
    if p is not None:  # если цена неизвестна — не пропускаем подарок
        if u["price_min"] is not None and p < u["price_min"]:
            return False
        if u["price_max"] is not None and p > u["price_max"]:
            return False
    return True


def _pct(r: int | None) -> str:
    return f" ({r / 10:g}%)" if r is not None else ""


def render(d: dict) -> str:
    e = html.escape
    price = f"{d['price']} ⭐ (флор коллекции)" if d.get("price") else "нет данных"
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
        f"Ориентир цены: {price}\n"
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
    rows.append([InlineKeyboardButton(text="🤝 Сопроводить сделку", callback_data=f"deal:{d['slug']}")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    text = render(d)
    for u in users:
        try:
            await bot.send_message(u["user_id"], text, reply_markup=kb)
        except Exception as e:
            log.debug("send to %s failed: %s", u["user_id"], e)
        await asyncio.sleep(0.04)  # ~25 msg/s, в пределах лимита Bot API
    log.info("%s -> %d users", d["slug"], len(users))
