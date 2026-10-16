import asyncio
import html
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand, BotCommandScopeChat, BotCommandScopeDefault, CallbackQuery,
    InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, LabeledPrice, Message,
    PreCheckoutQuery, ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo,
)
from telethon import TelegramClient

from . import config, db, relay
from .relay import TXT_AUTO
from .stats import TXT_STATS

log = logging.getLogger("bot")
router = Router()

WELCOME = (
    "<b>Gift Track Bot</b>\n\n"
    "Приватный инструмент для выкупа только что улучшенных Telegram-подарков.\n\n"
    "<b>Что умеет:</b>\n"
    "— мгновенные уведомления о каждом улучшении\n"
    "— точный поиск владельца: пишешь ему первым\n"
    "— оценка рынка: флор коллекции, модели, фона и пары модель+фон\n"
    "— гибкие фильтры: коллекции, модели, фоны, цены и исключения\n"
    "— режим выгодных предложений: редкость, премия к рынку, красивые номера\n"
    "— избранное: сохраняй подарки одной кнопкой\n"
    "— сообщения владельцам с твоего аккаунта (привязка в приложении)\n"
    "— старт и стоп работы в любой момент\n\n"
    "Управление — кнопками меню внизу или в приложении."
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
    has = db.has_access(m.from_user.id, u)
    if not config.PAID_MODE and not has:
        return await m.answer("🔒 Приватный бот. Доступ только по приглашению.\n"
                              f"Твой ID: <code>{m.from_user.id}</code> — отправь его владельцу.",
                              reply_markup=ReplyKeyboardRemove())
    await m.answer(WELCOME, reply_markup=menu_kb(u, has))


# ---------- меню в чате ----------
TXT_START = "▶ Начать работу"
TXT_STOP = "⏹ Завершить работу"
TXT_FAVS = "⭐ Избранное"
TXT_STATUS = "📊 Статус"
TXT_PROFIT = "🔥 Выгода"


def menu_kb(u: dict | None, access: bool = True) -> ReplyKeyboardMarkup:
    """Постоянное меню внизу чата; подписи кнопок отражают текущее состояние."""
    app = ([KeyboardButton(text="⚙️ Приложение", web_app=WebAppInfo(url=config.WEBAPP_URL))]
           if config.WEBAPP_URL else [])
    if not access:
        rows = [app] if app else []
    else:
        on = bool(u and u["enabled"])
        profit = bool(u and u["profit_mode"])
        rows = [
            [KeyboardButton(text=TXT_STOP if on else TXT_START)],
            [KeyboardButton(text=f"{TXT_PROFIT}: {'вкл' if profit else 'выкл'}"),
             KeyboardButton(text=TXT_FAVS)],
            [KeyboardButton(text=TXT_STATUS)] + app,
        ]
    extra = []
    if access and db.auto_allowed(u):
        extra.append(KeyboardButton(text=TXT_AUTO))
    if access and u and u["user_id"] in config.ADMIN_IDS:
        extra.append(KeyboardButton(text=TXT_STATS))
    if extra:
        rows.append(extra)
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


async def status_text(uid: int) -> str:
    u = await db.get_user(uid)
    if db.has_access(uid, u):
        state = "работает" if u and u["enabled"] else "остановлен"
        mode = "включён" if u and u["profit_mode"] else "выключен"
        until = ""
        if config.PAID_MODE and u and db.sub_active(u):
            until = f" Подписка до {u['sub_until']:%d.%m.%Y %H:%M} UTC."
        acc = "привязан" if await db.get_account(uid) else "не привязан"
        return f"✅ Доступ открыт. Бот {state}. Режим выгодных: {mode}. Аккаунт: {acc}.{until}"
    if config.PAID_MODE:
        return "Подписки нет. Оформи её в приложении."
    return "🔒 Приватный бот. Доступа нет."


async def _gate(m: Message) -> tuple[dict | None, bool]:
    await db.upsert_user(m.from_user.id, m.from_user.username)
    u = await db.get_user(m.from_user.id)
    ok = db.has_access(m.from_user.id, u)
    if not ok:
        await m.answer("Нужна подписка — оформи её в приложении." if config.PAID_MODE else "🔒 Нет доступа.")
    return u, ok


@router.message(Command("status"))
@router.message(Command("menu"))
@router.message(F.text == TXT_STATUS)
async def status(m: Message):
    # заодно обновляем кнопки меню внизу чата (появляются новые кнопки после обновлений)
    u = await db.get_user(m.from_user.id)
    await m.answer(await status_text(m.from_user.id),
                   reply_markup=menu_kb(u, db.has_access(m.from_user.id, u)))


@router.message(F.text.in_({TXT_START, TXT_STOP}))
async def work_btn(m: Message):
    u, ok = await _gate(m)
    if not ok:
        return
    want = m.text == TXT_START  # кнопка не переключает, а задаёт состояние - без путаницы при устаревшей подписи
    await db.set_enabled(m.from_user.id, want)
    await db.log_event(m.from_user.id, "work_on" if want else "work_off")
    u["enabled"] = want
    await m.answer("▶ Работа начата — уведомления включены." if want
                   else "⏹ Работа завершена — уведомления выключены.", reply_markup=menu_kb(u))


@router.message(F.text.startswith(TXT_PROFIT))
async def profit_btn(m: Message):
    u, ok = await _gate(m)
    if not ok:
        return
    want = m.text.rstrip().endswith("выкл")  # на кнопке было "выкл" - значит включаем
    await db.set_profit(m.from_user.id, want)
    u["profit_mode"] = want
    await m.answer("🔥 Режим выгодных предложений включён. Порог и условия — в приложении." if want
                   else "Режим выгодных предложений выключен.", reply_markup=menu_kb(u))


@router.message(F.text == TXT_FAVS)
async def favs_btn(m: Message):
    _, ok = await _gate(m)
    if not ok:
        return
    items = await db.list_favs(m.from_user.id)
    if not items:
        return await m.answer("Избранное пусто. Жми «⭐ В избранное» под уведомлением.")
    shown = items[:10]
    lines = [f"{i}. {html.escape(g['collection'])} #{g['num']} — "
             f"{html.escape(' · '.join(x for x in (g.get('model'), g.get('backdrop')) if x))}"
             for i, g in enumerate(shown, 1)]
    more = f"\n\nПоказаны последние 10 из {len(items)}. Весь список — в приложении." if len(items) > 10 else ""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🎁 {g['collection']} #{g['num']}", url=f"https://t.me/nft/{g['slug']}")]
        for g in shown])
    await m.answer("⭐ <b>Избранное</b>\n\n" + "\n".join(lines) + more, reply_markup=kb)


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


@router.message(Command("autoallow"))
async def autoallow(m: Message, command: CommandObject):
    if m.from_user.id not in config.ADMIN_IDS:
        return
    try:
        uid = int((command.args or "").strip())
    except ValueError:
        return await m.answer("Использование: /autoallow <user_id>")
    await db.set_auto_ok(uid, True)
    await m.answer(f"🤖 Авто-режим разрешён: {uid}. Он появится в его меню.")


@router.message(Command("autodeny"))
async def autodeny(m: Message, command: CommandObject):
    if m.from_user.id not in config.ADMIN_IDS:
        return
    try:
        uid = int((command.args or "").strip())
    except ValueError:
        return await m.answer("Использование: /autodeny <user_id>")
    await db.set_auto_ok(uid, False)
    await m.answer(f"⛔ Авто-режим закрыт: {uid}")


# ---------- admin: пользователи и состояние ----------
@router.message(Command("users"))
async def users_cmd(m: Message):
    if m.from_user.id not in config.ADMIN_IDS:
        return
    rows = await db.list_users()
    seen = {r["user_id"] for r in rows}
    lines, working = [], 0
    for r in rows:
        uid = r["user_id"]
        if uid in config.ADMIN_IDS:
            acc = "админ"
        elif r["allowed"]:
            acc = "доступ"
        elif config.PAID_MODE and db.sub_active(r):
            acc = f"подписка до {r['sub_until']:%d.%m}"
        else:
            acc = "нет доступа"
        has = acc != "нет доступа"
        on = has and r["enabled"]
        working += 1 if on else 0
        icon = "🟢" if on else ("⚪" if has else "🔒")
        name = f"@{html.escape(r['username'])}" if r["username"] else "—"
        lines.append(f"{icon} {name} · <code>{uid}</code> · {acc}" + (" · 🤖" if r.get("auto_ok") else ""))
    for uid in sorted(config.ADMIN_IDS - seen):
        lines.append(f"⚪ — · <code>{uid}</code> · админ (не запускал бота)")
    if not lines:
        return await m.answer("Пользователей пока нет.")
    head = f"👥 Пользователей: {len(lines)} · работают: {working}\n🟢 работает · ⚪ остановлен · 🔒 нет доступа\n\n"
    chunk = head
    for line in lines:
        if len(chunk) + len(line) > 3800:
            await m.answer(chunk)
            chunk = ""
        chunk += line + "\n"
    await m.answer(chunk)


@router.message(Command("health"))
async def health_cmd(m: Message, monitor):
    if m.from_user.id not in config.ADMIN_IDS:
        return
    await m.answer(monitor.health())


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
    if added:
        await db.log_event(c.from_user.id, "fav", c.data[4:])
    await c.answer("⭐ Добавлено в избранное" if added else "Убрано из избранного")


@router.callback_query(F.data.startswith("hide:"))
async def hide(c: CallbackQuery):
    _, kind, slug = c.data.split(":", 2)
    g = await db.get_gift(slug)
    if not g:
        return await c.answer("Подарок не найден", show_alert=True)
    col, val = (("exclude_collections", g.get("collection")) if kind == "c"
                else ("exclude_models", g.get("model")))
    if not val:
        return await c.answer("Нечего скрывать", show_alert=True)
    await db.add_exclude(c.from_user.id, col, val)
    await db.log_event(c.from_user.id, "hide", slug)
    await c.answer(f"🚫 Больше не покажу: {val}")


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
        reasons.append(f"по рынку от {est} ⭐ — в {est / floor:.1f}× выше флора коллекции")
    if nice_number(d["num"]):
        reasons.append(f"красивый номер #{d['num']}")
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


def _mk(label: str, floor, n) -> str | None:
    if not floor:
        return None
    return f"{label} от {floor} ⭐" + (f" ({n} шт.)" if n else "")


def render(d: dict, reasons: list[str] | None = None) -> str:
    e = html.escape
    if d.get("ask") is not None:
        price = f"{d['ask']} ⭐ (цена в листинге)"
    elif d.get("floor") is not None:
        price = f"~{d['floor']} ⭐ (флор коллекции)"
    else:
        price = "нет данных"
    if d.get("owner_label"):
        owner = e(d["owner_label"])
    elif d.get("owner_name"):
        owner = e(d["owner_name"])
    elif d.get("owner_address"):
        owner = f"кошелёк <code>{e(d['owner_address'])}</code>"
    else:
        owner = "скрыт"
    total = f"/{d['total']}" if d.get("total") else ""
    offer = f"от {d['offer_min']} ⭐" if d.get("offer_min") else "—"
    text = (
        f"🎁 <b>{e(d['collection'])} #{d['num']}</b>\n\n"
        f"Модель: <b>{e(d['model'] or '—')}</b>{_pct(d['model_rarity'])}\n"
        f"Фон: <b>{e(d['backdrop'] or '—')}</b>{_pct(d['backdrop_rarity'])}\n"
        f"Узор: <b>{e(d['symbol'] or '—')}</b>{_pct(d['symbol_rarity'])}\n"
        f"Выпуск: {d['num']}{total}\n"
        f"Цена: {price}\n"
        f"Мин. оффер: {offer}\n"
        f"Владелец: {owner}"
    )
    market = [x for x in (
        _mk("Коллекция", d.get("floor"), None),
        _mk("Модель", d.get("model_floor"), d.get("model_listed")),
        _mk("Фон", d.get("backdrop_floor"), d.get("backdrop_listed")),
        _mk("Модель+фон", d.get("combo_floor"), d.get("combo_listed")),
    ) if x]
    if market:
        text += "\n\n📊 <b>Рынок</b>\n" + "\n".join(market)
    if d.get("est_usd"):
        text += f"\nОценка Telegram: ~${d['est_usd']:,.0f}"
    if reasons:
        text += "\n\n🔥 <b>Почему выгодно</b>\n" + "\n".join(f"— {e(r)}" for r in reasons)
    return text


async def owner_info(client: TelegramClient, d: dict) -> tuple[str | None, str | None]:
    """(ссылка для сообщения владельцу, подпись для уведомления)."""
    uid = d.get("owner_id")
    if not uid:
        return None, None
    url, label = f"tg://user?id={uid}", f"ID {uid}"
    try:
        ent = await client.get_entity(uid)
        name = " ".join(x for x in (getattr(ent, "first_name", None), getattr(ent, "last_name", None)) if x)
        uname = getattr(ent, "username", None)
        if uname:
            url = f"https://t.me/{uname}"
            label = f"{name} (@{uname})" if name else f"@{uname}"
        elif name:
            label = name
    except Exception:
        pass
    return url, label


async def broadcast(bot: Bot, client: TelegramClient, monitor, d: dict) -> None:
    base = [u for u in await db.active_users() if base_match(u, d)]
    if not base:
        return
    await monitor.valuate(d)  # запросы к маркету только если подарок вообще кому-то подходит
    rows = [[InlineKeyboardButton(text="🎁 Открыть подарок", url=f"https://t.me/nft/{d['slug']}")]]
    url, d["owner_label"] = await owner_info(client, d)
    if url:
        if config.RELAY and d.get("owner_id"):
            rows.append([InlineKeyboardButton(text="✉️ Написать", callback_data=f"msg:{d['slug']}"),
                         InlineKeyboardButton(text="👤 Профиль", url=url)])
        else:
            rows.append([InlineKeyboardButton(text="👤 Написать владельцу", url=url)])
    rows.append([InlineKeyboardButton(text="⭐ В избранное", callback_data=f"fav:{d['slug']}")])
    rows.append([
        InlineKeyboardButton(text="🚫 Коллекция", callback_data=f"hide:c:{d['slug']}"),
        InlineKeyboardButton(text="🚫 Модель", callback_data=f"hide:m:{d['slug']}"),
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    sent = 0
    for u in base:
        ok, reasons = profit_check(u, d)
        if not ok:
            continue
        try:
            await bot.send_message(u["user_id"], render(d, reasons), reply_markup=kb)
            sent += 1
            await db.log_event(u["user_id"], "notify", d["slug"])
            if u.get("auto_state") == "on" and db.auto_allowed(u) and d.get("owner_id"):
                relay.schedule_auto(bot, monitor, u, d)
        except Exception as e:
            log.debug("send to %s failed: %s", u["user_id"], e)
        await asyncio.sleep(0.04)
    log.info("%s -> %d users", d["slug"], sent)


async def setup_commands(bot: Bot) -> None:
    """Меню команд по кнопке «/»: обычным людям - короткое, админам - расширенное."""
    user_cmds = [
        BotCommand(command="start", description="Меню и список функций"),
        BotCommand(command="status", description="Статус работы"),
        BotCommand(command="menu", description="Обновить кнопки меню"),
    ]
    admin_cmds = user_cmds + [
        BotCommand(command="users", description="Список людей"),
        BotCommand(command="health", description="Состояние мониторинга"),
        BotCommand(command="allow", description="Выдать доступ: /allow <id>"),
        BotCommand(command="deny", description="Закрыть доступ: /deny <id>"),
        BotCommand(command="stats", description="Статистика: /stats или /stats <id>"),
        BotCommand(command="autoallow", description="Разрешить авто-режим: /autoallow <id>"),
        BotCommand(command="autodeny", description="Закрыть авто-режим: /autodeny <id>"),
    ]
    if config.PAID_MODE:
        admin_cmds.append(BotCommand(command="grant", description="Продлить подписку: /grant <id> <дней>"))
    try:
        await bot.set_my_commands(user_cmds, scope=BotCommandScopeDefault())
    except Exception as e:
        log.warning("set_my_commands failed: %s", e)
    for admin in config.ADMIN_IDS:
        try:
            await bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=admin))
        except Exception as e:  # админ ещё не писал боту - команды выставятся при следующем запуске
            log.debug("admin commands for %s failed: %s", admin, e)
