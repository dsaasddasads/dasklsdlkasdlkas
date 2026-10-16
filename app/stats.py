"""Статистика для админов: кто и как пользуется ботом (чат и приложение)."""
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
    ("notify", "уведомлений"),
    ("msg_manual", "сообщений вручную"),
    ("msg_auto", "сообщений авто"),
    ("reply_in", "ответов от владельцев"),
    ("reply_out", "ответов владельцам"),
    ("fav", "в избранное"),
    ("hide", "скрыто коллекций/моделей"),
    ("work_on", "запусков работы"),
    ("auto_start", "запусков авто"),
    ("account_link", "привязок аккаунта"),
    ("app", "открытий приложения"),
    ("filters", "сохранений фильтров"),
]


def _age(dt) -> int | None:
    return None if not dt else int((datetime.now(timezone.utc) - dt).total_seconds())


def ago(sec: int | None) -> str:
    if sec is None:
        return "—"
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


def _tot(groups: dict, *kinds: str) -> int:
    return sum(v.get(k, 0) for v in groups.values() for k in kinds)


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


def _access_label(uid: int, u: dict) -> str:
    if uid in config.ADMIN_IDS:
        return "админ"
    if u.get("allowed"):
        return "доступ"
    if config.PAID_MODE and db.sub_active(u):
        return "подписка"
    return "нет доступа"


async def collect() -> dict:
    """Все цифры одним словарём (для чата и для приложения)."""
    users = await db.list_users()
    linked = {a["user_id"] for a in await db.list_accounts()}
    c24, c7 = _group(await db.event_counts(H24)), _group(await db.event_counts(H7D))
    seen = await db.last_seen()
    total, replied = await db.thread_totals()
    rows = []
    for r in users:
        uid = r["user_id"]
        if not db.has_access(uid, r):
            continue
        a, b = c24.get(uid, {}), c7.get(uid, {})
        rows.append({
            "id": uid, "username": r["username"], "enabled": r["enabled"],
            "admin": uid in config.ADMIN_IDS, "allowed": bool(r["allowed"]),
            "auto_ok": bool(r.get("auto_ok")), "auto_state": r.get("auto_state"),
            "linked": uid in linked,
            "n": [a.get("notify", 0), b.get("notify", 0)],
            "m": [a.get("msg_manual", 0) + a.get("msg_auto", 0), b.get("msg_manual", 0) + b.get("msg_auto", 0)],
            "a": [a.get("msg_auto", 0), b.get("msg_auto", 0)],
            "r": [a.get("reply_in", 0), b.get("reply_in", 0)],
            "f": [a.get("fav", 0), b.get("fav", 0)],
            "seen": _age(seen.get(uid)),
        })
    rows.sort(key=lambda x: -x["n"][0])
    return {
        "gifts": [await db.gifts_count(H24), await db.gifts_count(H7D)],
        "notify": [_tot(c24, "notify"), _tot(c7, "notify")],
        "msgs": [_tot(c24, "msg_manual", "msg_auto"), _tot(c7, "msg_manual", "msg_auto")],
        "auto": [_tot(c24, "msg_auto"), _tot(c7, "msg_auto")],
        "replies": [_tot(c24, "reply_in"), _tot(c7, "reply_in")],
        "threads": [total, replied],
        "with_access": len(rows), "working": sum(1 for x in rows if x["enabled"]),
        "auto_on": sum(1 for x in rows if x["auto_state"] == "on"),
        "linked": sum(1 for x in rows if x["linked"]),
        "users": rows,
    }


async def user_info(uid: int) -> dict | None:
    u = await db.get_user(uid)
    if not u:
        return None
    c24 = _group(await db.event_counts(H24, uid)).get(uid, {})
    c7 = _group(await db.event_counts(H7D, uid)).get(uid, {})
    call = _group(await db.event_counts(HALL, uid)).get(uid, {})
    seen = (await db.last_seen()).get(uid)
    acc = await db.get_account(uid)
    threads = await db.user_threads(uid, 5)
    return {
        "id": uid, "username": u["username"], "access": _access_label(uid, u),
        "allowed": bool(u["allowed"]), "admin": uid in config.ADMIN_IDS,
        "enabled": u["enabled"], "profit_mode": u["profit_mode"],
        "auto_ok": db.auto_allowed(u), "auto_state": u["auto_state"], "auto_sent": u["auto_sent"],
        "auto_limit": u["auto_limit"], "auto_gap": u["auto_gap"],
        "linked": bool(acc), "account": (acc["username"] or acc["name"]) if acc else None,
        "seen": _age(seen),
        "counts": [{"label": label, "d1": c24.get(k, 0), "d7": c7.get(k, 0), "all": call.get(k, 0)}
                   for k, label in LABELS],
        "threads": [{"title": f"{t['collection'] or t['slug']} #{t['num']}", "age": _age(t["created_at"]),
                     "replied": bool(t["last_in"])} for t in threads],
    }


def _name(username) -> str:
    return f"@{html.escape(username)}" if username else "—"


async def summary() -> list[str]:
    d = await collect()
    total, replied = d["threads"]
    pct = f" ({replied * 100 // total}%)" if total else ""
    lines = [
        "📈 <b>Статистика</b> (24 ч / 7 дн)", "",
        f"🎁 Подарков найдено: {d['gifts'][0]} / {d['gifts'][1]}",
        f"🔔 Уведомлений: {d['notify'][0]} / {d['notify'][1]}",
        f"✉️ Сообщений владельцам: {d['msgs'][0]} / {d['msgs'][1]} (авто {d['auto'][0]} / {d['auto'][1]})",
        f"💬 Ответов от владельцев: {d['replies'][0]} / {d['replies'][1]}",
        f"🧵 Диалогов всего: {total}, с ответом: {replied}{pct}",
        f"👥 С доступом: {d['with_access']}, работают сейчас: {d['working']}, "
        f"аккаунтов привязано: {d['linked']}, авто включено: {d['auto_on']}",
        "", "<b>По людям</b> (24 ч / 7 дн)",
    ]
    for r in d["users"]:
        lines.append(f"{'🟢' if r['enabled'] else '⚪'} {_name(r['username'])} · <code>{r['id']}</code>"
                     f"{' · 🔗' if r['linked'] else ''}")
        lines.append(f"   🔔 {r['n'][0]}/{r['n'][1]} · ✉️ {r['m'][0]}/{r['m'][1]} (🤖 {r['a'][0]}/{r['a'][1]}) · "
                     f"💬 {r['r'][0]}/{r['r'][1]} · ⭐ {r['f'][0]}/{r['f'][1]} · был {ago(r['seen'])}")
    if not d["users"]:
        lines.append("Пока никого.")
    lines += ["", "Подробно по человеку: /stats <id>"]
    return _chunks(lines)


async def user_detail(uid: int) -> list[str]:
    i = await user_info(uid)
    if not i:
        return ["Такого пользователя нет."]
    lines = [
        f"👤 <b>{_name(i['username'])}</b> · <code>{uid}</code>",
        f"Доступ: {i['access']} · работа: {'вкл' if i['enabled'] else 'выкл'} · "
        f"режим выгоды: {'вкл' if i['profit_mode'] else 'выкл'}",
        f"Аккаунт: {('привязан ' + html.escape(str(i['account']))) if i['linked'] else 'не привязан'}",
        f"Авто: {'разрешено' if i['auto_ok'] else 'нет'} · состояние: {i['auto_state']} "
        f"({i['auto_sent']} из {i['auto_limit']}, пауза {i['auto_gap']} с)",
        f"Последняя активность: {ago(i['seen'])}", "",
        "<b>24 ч / 7 дн / всего</b>",
    ]
    lines += [f"{c['label']}: {c['d1']} / {c['d7']} / {c['all']}" for c in i["counts"]]
    if i["threads"]:
        lines += ["", "<b>Последние диалоги</b>"]
        lines += [f"• {html.escape(t['title'])} — {ago(t['age'])} — {'ответил ✅' if t['replied'] else 'ждём ответ'}"
                  for t in i["threads"]]
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
