import asyncio
import html
import logging
from urllib.parse import quote

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
    "— уведомления только по подаркам, на которые можно отправить оффер\n"
    "— показываем минимальный оффер, кнопка «Написать владельцу» открывает чат с готовым текстом\n"
    "— оценка рынка: Telegram и Portals — флор коллекции, модели, фона и пары\n"
    "— гибкие фильтры: коллекции, модели, фоны, цены и исключения\n"
    "— режим выгодных предложений: редкость, премия к рынку, красивые номера\n"
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


def _hit(selected: list[str], value: str | None) -> bool:
    return bool(selected) and bool(value) and any(s.lower() in value.lower() for s in selected)


def eff_price(d: dict) -> int | None:
    return d["ask"] if d.get("ask") is not None else d.get("floor")


def min_rarity(d: dict) -> int | None:
    vals = [d[k] for k in ("model_rarity", "backdrop_rarity", "symbol_rarity") if d.get(k) is not None]
    return min(vals) if vals else None


def nice_number(n: int) -> bool:
    s = str(n)
    if n <= 100:
        return True
    if len(s) >= 3 and len(set(s)) == 1:  # 777, 8888
        return True
    if n % 1000 == 0:                      # 1000, 25000
        return True
    return len(s) >= 3 and s == s[::-1]    # 12321


def est_value(d: dict) -> int | None:
    """Оценка по рынку: максимум из флоров пары, модели и фона, где листингов достаточно (не тонкий рынок)."""
    cands = []
    for k in ("combo", "model", "backdrop"):
        fl, n = d.get(f"{k}_floor"), d.get(f"{k}_listed")
        if fl and (n or 0) >= config.MIN_LISTED:
            cands.append(fl)
    return max(cands) if cands else None


def base_match(u: dict, d: dict) -> bool:
    """Базовые фильтры: исключения, коллекции, модели, фоны, цена."""
    if (_hit(u["exclude_collections"], d["collection"]) or _hit(u["exclude_models"], d["model"])
            or _hit(u["exclude_backdrops"], d["backdrop"])):
        return False
    if not (_ok(u["collections"], d["collection"]) and _ok(u["models"], d["model"])
            and _ok(u["backdrops"], d["backdrop"])):
        return False
    p = eff_price(d)
    if p is not None:  # если цена неизвестна — подарок не отсекаем
        if u["price_min"] is not None and p < u["price_min"]:
            return False
        if u["price_max"] is not None and p > u["price_max"]:
            return False
    return True


def ton_to_stars(d: dict, ton: float | None) -> int | None:
    k = d.get("ton_per_star")
    return round(ton / k) if ton and k else None


def gram(d: dict, stars: float | None) -> float | None:
    """⭐ -> GRAM (бывший TON) по курсу из листингов Telegram."""
    k = d.get("ton_per_star")
    return stars * k if stars and k else None


def fmt(x: float) -> str:
    t = f"{x:.0f}" if x >= 100 else f"{x:.1f}" if x >= 10 else f"{x:.2f}"
    return t.rstrip("0").rstrip(".") if "." in t else t


def price_str(d: dict, stars: float | None) -> str:
    """'627 ⭐ (≈6.6 GRAM)'."""
    g = gram(d, stars)
    return f"{stars:.0f} ⭐" + (f" (≈{fmt(g)} GRAM)" if g else "")


def portals_ref(d: dict) -> float | None:
    """Самая точная цена Portals (GRAM): пара модель+фон, иначе модель, иначе фон."""
    for k in ("portals_combo_floor", "portals_model_floor", "portals_backdrop_floor"):
        if d.get(k):
            return d[k]
    return None


def portals_discount(d: dict) -> float | None:
    """На сколько % цена в листинге Telegram ниже цены Portals (нужен листинг и курс GRAM/⭐)."""
    ask, ref = d.get("ask"), ton_to_stars(d, portals_ref(d))
    if ask is None or not ref:
        return None
    return (1 - ask / ref) * 100


def profit_check(u: dict, d: dict) -> tuple[bool, list[str]]:
    """Режим выгодных предложений. Возвращает (проходит, причины)."""
    if not u["profit_mode"]:
        return True, []
    reasons = []
    r = min_rarity(d)  # в промилле: 10 = 1%
    if r is not None and r <= u["profit_rarity"] * 10:
        reasons.append(f"редкий признак ({r / 10:g}%)")
    est, floor = est_value(d), d.get("floor")
    if est and floor and est >= floor * config.PREMIUM_X:
        reasons.append(f"по рынку от {price_str(d, est)} — в {est / floor:.1f}× выше флора коллекции")
    if nice_number(d["num"]):
        reasons.append(f"красивый номер #{d['num']}")
    cheap = portals_discount(d)
    if cheap is not None and cheap >= config.PORTALS_DISCOUNT:
        reasons.append(f"в листинге на {cheap:.0f}% дешевле, чем на Portals")
    if not reasons:
        return False, []
    ask = d.get("ask")
    if ask is not None:  # уже выставлен на продажу - сравниваем с оценкой рынка
        ref = est or (floor * 1.5 if floor else None)
        if ref:
            disc = (1 - ask / ref) * 100
            if disc < u["min_discount"] or (disc < 0 and not est):
                return False, []
            if est and disc > 0:
                reasons.append(f"выставлен со скидкой {disc:.0f}% к оценке рынка")
    return True, reasons


# ---------- notifications ----------
def _pct(r: int | None) -> str:
    return f" ({r / 10:g}%)" if r is not None else ""


def _row(label: str, d: dict, tg_stars, tg_n, pt_gram) -> str | None:
    """Строка сравнения маркетов: Telegram (⭐ -> GRAM) и Portals (GRAM)."""
    parts = []
    if tg_stars:
        g = gram(d, tg_stars)
        t = fmt(g) if g else f"{tg_stars} ⭐"
        parts.append(f"Telegram {t}" + (f" ({tg_n} шт.)" if tg_n else ""))
    if pt_gram:
        parts.append(f"Portals {fmt(pt_gram)}")
    return f"{label}: " + " · ".join(parts) if parts else None


def render(d: dict, reasons: list[str] | None = None) -> str:
    e = html.escape
    if d.get("ask") is not None:
        price = f"{price_str(d, d['ask'])}, цена в листинге"
    elif d.get("floor") is not None:
        price = f"~{price_str(d, d['floor'])}, флор коллекции"
    else:
        price = "нет данных"
    if d.get("owner_name"):
        owner = e(d["owner_name"])
    elif d.get("owner_address"):
        owner = f"кошелёк <code>{e(d['owner_address'])}</code>"
    else:
        owner = "скрыт"
    total = f"/{d['total']}" if d.get("total") else ""
    text = (
        f"🎁 <b>{e(d['collection'])} #{d['num']}</b>\n\n"
        f"Модель: <b>{e(d['model'] or '—')}</b>{_pct(d['model_rarity'])}\n"
        f"Фон: <b>{e(d['backdrop'] or '—')}</b>{_pct(d['backdrop_rarity'])}\n"
        f"Узор: <b>{e(d['symbol'] or '—')}</b>{_pct(d['symbol_rarity'])}\n"
        f"Выпуск: {d['num']}{total}\n"
        f"Цена: {price}\n"
        f"Владелец: {owner}"
    )
    if d.get("offer_min") is not None:
        text += f"\nМин. оффер: {price_str(d, d['offer_min'])}"
    rows = [x for x in (
        _row("Коллекция", d, d.get("floor"), None, d.get("portals_floor")),
        _row("Модель", d, d.get("model_floor"), d.get("model_listed"), d.get("portals_model_floor")),
        _row("Фон", d, d.get("backdrop_floor"), d.get("backdrop_listed"), d.get("portals_backdrop_floor")),
        _row("Модель+фон", d, d.get("combo_floor"), d.get("combo_listed"), d.get("portals_combo_floor")),
    ) if x]
    if rows:
        unit = "GRAM" if d.get("ton_per_star") else "Telegram в ⭐, Portals в GRAM"
        text += f"\n\n📊 <b>Цены маркетов ({unit})</b>\n" + "\n".join(rows)
    if reasons:
        text += "\n\n🔥 <b>Почему выгодно</b>\n" + "\n".join(f"— {e(r)}" for r in reasons)
    return text


def message_text(d: dict) -> str:
    """Текст, который подставится в чат с владельцем (шаблон MSG_TEMPLATE)."""
    t = config.MSG_TEMPLATE
    for k, v in (("{collection}", d["collection"]), ("{num}", str(d["num"])),
                 ("{link}", f"https://t.me/nft/{d['slug']}")):
        t = t.replace(k, v)
    return t


def contact_url(d: dict) -> str | None:
    """Ссылка на чат с владельцем и готовым текстом. None - если написать нельзя."""
    name = d.get("owner_username")
    if name and (d.get("owner_reachable") or not config.ONLY_REACHABLE):
        return f"https://t.me/{name}?text={quote(message_text(d), safe='')}"
    if not config.ONLY_REACHABLE and d.get("owner_id"):
        return f"tg://user?id={d['owner_id']}"  # запасной вариант: работает не у всех
    return None


async def broadcast(bot: Bot, client: TelegramClient, monitor, d: dict) -> None:
    base = [u for u in await db.active_users() if base_match(u, d)]
    if not base:
        return
    if config.ONLY_OFFERABLE and monitor.offer_supported and d.get("offer_min") is None:
        log.info("%s пропущен: оффер на этот подарок недоступен", d["slug"])
        return
    await monitor.resolve_owner(d)  # профиль владельца: только если подарок кому-то подходит
    url = contact_url(d)
    if config.ONLY_REACHABLE and not url:
        log.info("%s пропущен: %s", d["slug"], d.get("owner_note") or "написать владельцу нельзя")
        return
    await monitor.valuate(d)  # запросы к маркетам только если подарок вообще кому-то подходит
    rows = [[InlineKeyboardButton(text="🎁 Открыть подарок", url=f"https://t.me/nft/{d['slug']}")]]
    if url:
        rows.append([InlineKeyboardButton(text="👤 Написать владельцу", url=url)])
    rows.append([InlineKeyboardButton(text="⭐ В избранное", callback_data=f"fav:{d['slug']}")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    sent = 0
    for u in base:
        ok, reasons = profit_check(u, d)
        if not ok:
            continue
        try:
            await bot.send_message(u["user_id"], render(d, reasons), reply_markup=kb)
            sent += 1
        except Exception as e:
            log.debug("send to %s failed: %s", u["user_id"], e)
        await asyncio.sleep(0.04)
    log.info("%s -> %d users", d["slug"], sent)
