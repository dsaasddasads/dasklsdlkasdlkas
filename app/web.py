import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web
from aiogram import Bot

from . import bot as botmod, config, db

INDEX = Path(__file__).parent / "webapp" / "index.html"


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


def make_app(bot: Bot) -> web.Application:
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
        return web.FileResponse(INDEX)

    async def health(_):
        return web.Response(text="ok")

    async def me(request):
        u = await auth(request)
        row = await db.get_user(u["id"])
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
        })

    async def toggle(request):
        u = await auth(request)
        row = await db.get_user(u["id"])
        want = bool((await request.json()).get("enabled"))
        if want and not db.has_access(u["id"], row):
            raise web.HTTPPaymentRequired()
        await db.set_enabled(u["id"], want)
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
    ])
    return app
