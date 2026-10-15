"""Статистика для админов: кто и как пользуется ботом."""
import html
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from . import config, db

router = Router()
TXT_STATS = "📈 Статистика"
H24, H7D, HALL = 24, 24 * 7, 24 * 3650

LABELS = [
    ("notify", "🔔 уведомлений"),
    ("msg_manual", "✉️ сообщений вручную"),
    ("msg_auto", "🤖 сообщений авто"),
    ("reply_in", "💬 ответов от владельцев"),
    ("reply_out", "↩️ ответов владельцам"),
    ("fav", "⭐ в избранное"),
    ("hide", "🚫 скрыто коллекций/моделей"),
    ("work_on", "▶ запусков работы"),
    ("auto_start", "🤖 запусков авто"),
    ("app", "⚙️ открытий приложения"),
    ("filters", "🔧 сохранений фильтров"),
]


def ago(dt) -> str:
    if not dt:
        return "—"
    sec = int((datetime.now(timezone.utc) - dt).total_seconds())
    if sec < 60:
        return f"{sec} с назад"
    if sec < 3600:
        return f"{sec // 60} мин назад"
    if sec < 86400:
        return f"{sec // 3600} ч назад"
    return f"{sec // 86400} дн назад"


def _group(rows: list[dict]) -> dict[int, dict[str, int]]:
    out: dict[int, dict[str, int]] = {}
    for r in rows:
        out.setdefault(r["user_id"], {})[r["kind"]] = r["c"]
    return out


def _tot(groups: dict, kind: str) -> int:
    return sum(v.get(kind, 0) for v in groups.values())


def _chunks(lines: list[str], limit: int = 3800) -> list[str]:
    out, cur = [], ""
    for line in lines:
        if cur and len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = ""
        cur += line + "\n"
    if cur:
        out.append(cur)
    return out


def _name(r: dict) -> str:
    return f"@{html.escape(r['username'])}" if r.get("username") else "—"


async def summary() -> list[str]:
    users = await db.list_users()
    c24, c7 = _group(await db.event_counts(H24)), _group(await db.event_counts(H7D))
    seen = await db.last_seen()
    g24, g7 = await db.gifts_count(H24), await db.gifts_count(H7D)
    total, replied = await db.thread_totals()

    with_access = [r for r in users if db.has_access(r["user_id"], r)]
    working = sum(1 for r in with_access if r["enabled"])
    auto_on = sum(1 for r in with_access if r.get("auto_state") == "on")
    pct = f" ({replied * 100 // total}%)" if total else ""

    def m(c):
        return _tot(c, "msg_manual") + _tot(c, "msg_auto")

    lines = [
        "📈 <b>Статистика</b> (24 ч / 7 дн)",
        "",
        f"🎁 Подарков найдено: {g24} / {g7}",
        f"🔔 Уведомлений: {_tot(c24, 'notify')} / {_tot(c7, 'notify')}",
        f"✉️ Сообщений владельцам: {m(c24)} / {m(c7)} "
        f"(авто {_tot(c24, 'msg_auto')} / {_tot(c7, 'msg_auto')})",
        f"💬 Ответов от владельцев: {_tot(c24, 'reply_in')} / {_tot(c7, 'reply_in')}",
        f"🧵 Диалогов всего: {total}, с ответом: {replied}{pct}",
        f"👥 С доступом: {len(with_access)}, работают сейчас: {working}, авто включено: {auto_on}",
        "",
        "<b>По людям</b> (24 ч / 7 дн)",
    ]
    rows = sorted(with_access, key=lambda r: -c24.get(r["user_id"], {}).get("notify", 0))
    for r in rows:
        uid = r["user_id"]
        a, b = c24.get(uid, {}), c7.get(uid, {})
        icon = "🟢" if r["enabled"] else "⚪"
        lines.append(f"{icon} {_name(r)} · <code>{uid}</code>")
        lines.append(
            f"   🔔 {a.get('notify', 0)}/{b.get('notify', 0)} · "
            f"✉️ {a.get('msg_manual', 0) + a.get('msg_auto', 0)}/{b.get('msg_manual', 0) + b.get('msg_auto', 0)} "
            f"(🤖 {a.get('msg_auto', 0)}/{b.get('msg_auto', 0)}) · "
            f"💬 {a.get('reply_in', 0)}/{b.get('reply_in', 0)} · "
            f"⭐ {a.get('fav', 0)}/{b.get('fav', 0)} · был {ago(seen.get(uid))}")
    if not rows:
        lines.append("Пока никого.")
    lines += ["", "Подробно по человеку: /stats <id>"]
    return _chunks(lines)


async def user_detail(uid: int) -> list[str]:
    u = await db.get_user(uid)
    if not u:
        return ["Такого пользователя нет."]
    c24 = _group(await db.event_counts(H24, uid)).get(uid, {})
    c7 = _group(await db.event_counts(H7D, uid)).get(uid, {})
    call = _group(await db.event_counts(HALL, uid)).get(uid, {})
    seen = (await db.last_seen()).get(uid)
    access = "админ" if uid in config.ADMIN_IDS else ("доступ" if u["allowed"] else
             ("подписка" if config.PAID_MODE and db.sub_active(u) else "нет доступа"))
    lines = [
        f"👤 <b>{_name(u)}</b> · <code>{uid}</code>",
        f"Доступ: {access} · работа: {'вкл' if u['enabled'] else 'выкл'} · "
        f"режим выгоды: {'вкл' if u['profit_mode'] else 'выкл'}",
        f"Авто: {'разрешено' if db.auto_allowed(u) else 'нет'} · состояние: {u['auto_state']} "
        f"({u['auto_sent']} из {u['auto_limit']}, пауза {u['auto_gap']} с)",
        f"Последняя активность: {ago(seen)}",
        "",
        "<b>24 ч / 7 дн / всего</b>",
    ]
    for kind, label in LABELS:
        lines.append(f"{label}: {c24.get(kind, 0)} / {c7.get(kind, 0)} / {call.get(kind, 0)}")
    threads = await db.user_threads(uid, 5)
    if threads:
        lines += ["", "<b>Последние диалоги</b>"]
        for t in threads:
            state = "ответил ✅" if t["last_in"] else "ждём ответ"
            lines.append(f"• {html.escape(t['collection'] or t['slug'])} #{t['num']} — "
                         f"{ago(t['created_at'])} — {state}")
    return _chunks(lines)


async def _send(m: Message, chunks: list[str]) -> None:
    for c in chunks:
        await m.answer(c)


@router.message(Command("stats"))
async def stats_cmd(m: Message, command: CommandObject):
    if m.from_user.id not in config.ADMIN_IDS:
        return
    arg = (command.args or "").strip()
    if arg:
        try:
            return await _send(m, await user_detail(int(arg)))
        except ValueError:
            return await m.answer("Использование: /stats или /stats <user_id>")
    await _send(m, await summary())


@router.message(F.text == TXT_STATS)
async def stats_btn(m: Message):
    if m.from_user.id in config.ADMIN_IDS:
        await _send(m, await summary())
