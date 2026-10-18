import asyncio
import hashlib
import hmac
import html
import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web
from aiogram import Bot

from . import accounts, bot as botmod, config, db, relay, settings, stats

INDEX = Path(__file__).parent / "webapp" / "index.html"
BUILD = "2026-10-17"  # метка версии: видна внизу главной и по адресу /health


def verify(init_data: str) -> dict | None:
    """Проверка подписи Telegram WebApp initData."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            return None
        if time.time() - int(pairs.get("auth_date", 0)) > 86400:
            return None
        return json.loads(pairs["user"])
    except Exception:
        return None


def _list(v) -> list[str]:
    if isinstance(v, str):
        v = v.split(",")
    return [str(x).strip() for x in (v or []) if str(x).strip()][:50]


def _int(v) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _rarity(v) -> float:
    try:
        return min(max(float(v), 0.1), 100.0)
    except (TypeError, ValueError):
        return 1.0


def _disc(v) -> float:
    try:
        return min(max(float(v), 0.0), 95.0)
    except (TypeError, ValueError):
        return 0.0


_app_log: dict[int, float] = {}


async def _log_app(uid: int) -> None:
    """Открытия приложения - не чаще раза в 10 минут на человека."""
    if time.time() - _app_log.get(uid, 0) > 600:
        _app_log[uid] = time.time()
        await db.log_event(uid, "app")


def make_app(bot: Bot, monitor=None) -> web.Application:
    app = web.Application()

    async def auth(request: web.Request) -> dict:
        u = verify(request.headers.get("X-Init-Data", ""))
        if not u:
            raise web.HTTPUnauthorized()
        await db.upsert_user(u["id"], u.get("username"))
        if not config.PAID_MODE and not db.has_access(u["id"], await db.get_user(u["id"])):
            raise web.HTTPForbidden()
        return u

    async def index(_):
        return web.FileResponse(INDEX, headers={"Cache-Control": "no-store"})  # чтобы Telegram не держал старую страницу

    async def health(_):
        return web.Response(text=f"ok build {BUILD}")

    async def me(request):
        u = await auth(request)
        row = await db.get_user(u["id"])
        await _log_app(u["id"])
        return web.json_response({
            "active": db.has_access(u["id"], row),
            "paid_mode": config.PAID_MODE,
            "sub_until": row["sub_until"].isoformat() if row["sub_until"] else None,
            "enabled": row["enabled"],
            "collections": row["collections"], "models": row["models"],
            "backdrops": row["backdrops"],
            "price_min": row["price_min"], "price_max": row["price_max"],
            "profit_mode": row["profit_mode"], "profit_rarity": row["profit_rarity"],
            "exclude_collections": row["exclude_collections"], "exclude_models": row["exclude_models"],
            "exclude_backdrops": row["exclude_backdrops"], "min_discount": row["min_discount"],
            "plans": config.PLANS,
            "auto_available": db.has_access(u["id"], row) and db.auto_allowed(row),
            "auto_state": row["auto_state"], "auto_sent": row["auto_sent"],
            "auto_limit": min(row["auto_limit"], settings.get_int("auto_max")), "auto_gap": row["auto_gap"],
            "auto_tpl": row["auto_tpl"], "auto_text": row["auto_text"],
            "auto_max": settings.get_int("auto_max"), "auto_min_gap": config.AUTO_MIN_GAP, "auto_max_gap": config.AUTO_MAX_GAP,
            "templates": relay._tpls(),
            "account": await accounts.manager.info(u["id"]),
            "is_admin": u["id"] in config.ADMIN_IDS,
            "link_ok": accounts.link_ok(),
            "flags": {k: settings.get_bool(k) for k in ("notify_paused", "relay_paused", "auto_paused", "link_closed")},
        })

    async def toggle(request):
        u = await auth(request)
        row = await db.get_user(u["id"])
        want = bool((await request.json()).get("enabled"))
        if want and not db.has_access(u["id"], row):
            raise web.HTTPPaymentRequired()
        await db.set_enabled(u["id"], want)
        await db.log_event(u["id"], "work_on" if want else "work_off")
        return web.json_response({"enabled": want})

    async def save(request):
        u = await auth(request)
        b = await request.json()
        await db.set_filters(u["id"], {
            "collections": _list(b.get("collections")),
            "models": _list(b.get("models")),
            "backdrops": _list(b.get("backdrops")),
            "price_min": _int(b.get("price_min")),
            "price_max": _int(b.get("price_max")),
            "profit_mode": bool(b.get("profit_mode")),
            "profit_rarity": _rarity(b.get("profit_rarity")),
            "exclude_collections": _list(b.get("exclude_collections")),
            "exclude_models": _list(b.get("exclude_models")),
            "exclude_backdrops": _list(b.get("exclude_backdrops")),
            "min_discount": _disc(b.get("min_discount")),
        })
        await db.log_event(u["id"], "filters")
        return web.json_response({"ok": True})

    async def favorites(request):
        u = await auth(request)
        items = await db.list_favs(u["id"])
        keys = ("slug", "collection", "num", "model", "backdrop", "symbol")
        return web.json_response({"items": [{k: i.get(k) for k in keys} for i in items]})

    async def fav_remove(request):
        u = await auth(request)
        await db.remove_fav(u["id"], str((await request.json()).get("slug", "")))
        return web.json_response({"ok": True})

    async def _auto_user(request) -> dict:
        u = await auth(request)
        row = await db.get_user(u["id"])
        if not (db.has_access(u["id"], row) and db.auto_allowed(row)):
            raise web.HTTPForbidden()
        return u

    async def auto_ctl(request):
        u = await _auto_user(request)
        action = (await request.json()).get("action")
        if action not in ("start", "pause", "resume", "stop"):
            raise web.HTTPBadRequest()
        if action == "start" and not accounts.manager.get(u["id"]):
            return web.json_response({"error": "Сначала привяжи свой аккаунт (вкладка «Аккаунт»)."}, status=400)
        row = await db.auto_control(u["id"], action)
        await db.log_event(u["id"], f"auto_{action}")
        return web.json_response({"auto_state": row["auto_state"], "auto_sent": row["auto_sent"]})

    async def auto_settings(request):
        u = await _auto_user(request)
        b = await request.json()
        await db.set_auto_settings(
            u["id"], _int(b.get("limit")) or settings.get_int("auto_max"), _int(b.get("gap")) or 60,
            _int(b.get("tpl")) or 0, str(b.get("text") or ""))
        return web.json_response({"ok": True})

    # ----- привязка личного аккаунта -----
    async def _acc_user(request) -> dict:
        u = await auth(request)
        if not db.has_access(u["id"], await db.get_user(u["id"])):
            raise web.HTTPForbidden()
        return u

    def _err(e: Exception) -> web.Response:
        return web.json_response({"error": accounts.explain(e)}, status=400)

    async def acc_phone(request):
        u = await _acc_user(request)
        try:
            await accounts.manager.link_phone(u["id"], str((await request.json()).get("phone", "")))
        except Exception as e:
            return _err(e)
        return web.json_response({"step": "code"})

    async def acc_code(request):
        u = await _acc_user(request)
        try:
            step = await accounts.manager.link_code(u["id"], str((await request.json()).get("code", "")))
        except Exception as e:
            return _err(e)
        return web.json_response({"step": step})

    async def acc_password(request):
        u = await _acc_user(request)
        try:
            await accounts.manager.link_password(u["id"], str((await request.json()).get("password", "")))
        except Exception as e:
            return _err(e)
        return web.json_response({"step": "done"})

    async def acc_unlink(request):
        u = await _acc_user(request)
        await accounts.manager.cancel(u["id"])
        await accounts.manager.unlink(u["id"])
        return web.json_response({"ok": True})

    # ----- админ-панель -----
    async def _admin(request) -> dict:
        u = await auth(request)
        if u["id"] not in config.ADMIN_IDS:
            raise web.HTTPForbidden()
        return u

    async def admin_stats(request):
        await _admin(request)
        return web.json_response(await stats.collect())

    async def admin_user(request):
        await _admin(request)
        uid = _int(request.query.get("id"))
        info = await stats.user_info(uid) if uid else None
        if not info:
            raise web.HTTPNotFound()
        return web.json_response(info)

    async def admin_access(request):
        admin = await _admin(request)
        b = await request.json()
        uid, allowed = _int(b.get("id")), bool(b.get("allowed"))
        if not uid or uid in config.ADMIN_IDS:
            raise web.HTTPBadRequest()
        await db.set_allowed(uid, allowed)
        if not allowed:
            await db.set_enabled(uid, False)
            await db.auto_control(uid, "stop")
        await db.log_event(admin["id"], "admin", f"access:{uid}:{'on' if allowed else 'off'}")
        return web.json_response({"ok": True})

    async def admin_autook(request):
        admin = await _admin(request)
        b = await request.json()
        uid = _int(b.get("id"))
        if not uid:
            raise web.HTTPBadRequest()
        await db.set_auto_ok(uid, bool(b.get("ok")))
        await db.log_event(admin["id"], "admin", f"autook:{uid}:{'on' if b.get('ok') else 'off'}")
        return web.json_response({"ok": True})

    # ----- расширенное управление (только админам) -----
    LIMITS = {"relay_daily_limit": (0, 100), "relay_min_gap": (5, 600), "auto_max": (1, 20)}

    async def admin_control(request):
        await _admin(request)
        now = time.time()
        accs = await accounts.manager.health()
        for a in accs:
            a["paused"] = max(0, int(relay.paused_until(a["id"]) - now))
        log = [{"age": int(now - r["ts"].timestamp()), "admin": r["user_id"], "what": r["slug"] or ""}
               for r in await db.admin_log(20)]
        return web.json_response({
            "settings": settings.snapshot(), "link_ok": accounts.link_ok(), "link_problem": accounts.problem(),
            "accounts": accs, "log": log,
            "health": re.sub(r"<[^>]+>", "", monitor.health()) if monitor else "",
        })

    async def admin_setting(request):
        admin = await _admin(request)
        b = await request.json()
        key = str(b.get("key", ""))
        if key not in settings.DEFAULTS:
            raise web.HTTPBadRequest()
        kind = settings.DEFAULTS[key][0]
        if b.get("value") is None:
            await settings.reset(key)
            what = "сброшено"
        elif kind == "bool":
            await settings.set_value(key, bool(b["value"]))
            what = "вкл" if b["value"] else "выкл"
        else:
            lo, hi = LIMITS[key]
            n = _int(b["value"])
            if n is None:
                raise web.HTTPBadRequest()
            n = min(max(n, lo), hi)
            await settings.set_value(key, n)
            what = str(n)
        await db.log_event(admin["id"], "admin", f"set:{key}:{what}")
        return web.json_response({"settings": settings.snapshot()})

    async def admin_user_action(request):
        admin = await _admin(request)
        b = await request.json()
        uid, action = _int(b.get("id")), str(b.get("action", ""))
        target = await db.get_user(uid) if uid else None
        if not target:
            raise web.HTTPNotFound()
        is_admin = uid in config.ADMIN_IDS
        note = action
        if action == "message":
            text = str(b.get("text", "")).strip()[:1000]
            if not text:
                raise web.HTTPBadRequest()
            try:
                await bot.send_message(uid, "📣 <b>Сообщение от админа</b>\n\n" + html.escape(text))
            except Exception:
                return web.json_response({"error": "Не удалось отправить: человек не запускал бота или заблокировал его."}, status=400)
        elif is_admin:
            raise web.HTTPForbidden()  # админов этими действиями не трогаем
        elif action == "stop_work":
            await db.set_enabled(uid, False)
        elif action == "stop_auto":
            await db.auto_control(uid, "stop")
        elif action == "reset_auto":
            await db.auto_reset(uid)
        elif action == "unlink":
            await accounts.manager.cancel(uid)
            await accounts.manager.unlink(uid, by_admin=True)
        elif action == "limit":
            n = _int(b.get("value"))
            await db.set_daily_limit(uid, None if n is None else min(max(n, 0), 200))
            note = f"limit={'по умолчанию' if n is None else min(max(n, 0), 200)}"
        else:
            raise web.HTTPBadRequest()
        await db.log_event(admin["id"], "admin", f"user:{uid}:{note}")
        return web.json_response({"ok": True})

    _bg: set = set()

    async def _send_all(text: str, ids: list[int]) -> None:
        for uid in ids:
            try:
                await bot.send_message(uid, "📣 <b>Объявление</b>\n\n" + html.escape(text))
            except Exception:
                pass
            await asyncio.sleep(0.05)

    async def admin_broadcast(request):
        admin = await _admin(request)
        text = str((await request.json()).get("text", "")).strip()[:1000]
        if not text:
            raise web.HTTPBadRequest()
        ids = [r["user_id"] for r in await db.list_users() if db.has_access(r["user_id"], r)]
        t = asyncio.create_task(_send_all(text, ids))
        _bg.add(t)
        t.add_done_callback(_bg.discard)
        await db.log_event(admin["id"], "admin", f"broadcast:{len(ids)}")
        return web.json_response({"queued": len(ids)})

    async def invoice(request):
        await auth(request)
        if not config.PAID_MODE:
            raise web.HTTPNotFound()
        days = _int((await request.json()).get("days"))
        if days not in config.PLANS:
            raise web.HTTPBadRequest()
        link = await bot.create_invoice_link(**botmod.invoice_args(days))
        return web.json_response({"link": link})

    app.add_routes([
        web.get("/", index), web.get("/health", health),
        web.get("/api/me", me), web.post("/api/toggle", toggle),
        web.post("/api/filters", save),
        web.get("/api/favorites", favorites), web.post("/api/favorites/remove", fav_remove),
        web.post("/api/invoice", invoice),
        web.post("/api/auto", auto_ctl), web.post("/api/auto/settings", auto_settings),
        web.post("/api/account/phone", acc_phone), web.post("/api/account/code", acc_code),
        web.post("/api/account/password", acc_password), web.post("/api/account/unlink", acc_unlink),
        web.get("/api/admin/stats", admin_stats), web.get("/api/admin/user", admin_user),
        web.post("/api/admin/access", admin_access), web.post("/api/admin/autook", admin_autook),
        web.get("/api/admin/control", admin_control), web.post("/api/admin/setting", admin_setting),
        web.post("/api/admin/user_action", admin_user_action), web.post("/api/admin/broadcast", admin_broadcast),
    ])
    return app
