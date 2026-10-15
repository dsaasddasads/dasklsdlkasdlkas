import json
from datetime import datetime, timezone

import asyncpg

from . import config

pool: asyncpg.Pool | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     BIGINT PRIMARY KEY,
    username    TEXT,
    sub_until   TIMESTAMPTZ,
    enabled     BOOLEAN NOT NULL DEFAULT FALSE,
    collections TEXT[] NOT NULL DEFAULT '{}',
    models      TEXT[] NOT NULL DEFAULT '{}',
    backdrops   TEXT[] NOT NULL DEFAULT '{}',
    price_min   BIGINT,
    price_max   BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE users ADD COLUMN IF NOT EXISTS profit_mode   BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS profit_rarity REAL    NOT NULL DEFAULT 1.0;
ALTER TABLE users ALTER COLUMN enabled SET DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS allowed BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS exclude_collections TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE users ADD COLUMN IF NOT EXISTS exclude_models      TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE users ADD COLUMN IF NOT EXISTS exclude_backdrops   TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE users ADD COLUMN IF NOT EXISTS min_discount        REAL   NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS cursors (
    prefix   TEXT PRIMARY KEY,
    last_num INT NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS gifts (
    slug       TEXT PRIMARY KEY,
    prefix     TEXT NOT NULL,
    num        INT NOT NULL,
    data       JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    ts  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS favorites (
    user_id    BIGINT NOT NULL,
    slug       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, slug)
);
"""


async def init() -> None:
    global pool
    pool = await asyncpg.create_pool(config.DATABASE_URL, min_size=1, max_size=5)
    await pool.execute(SCHEMA)


# ---------- users ----------
async def upsert_user(uid: int, username: str | None) -> None:
    await pool.execute(
        "INSERT INTO users(user_id, username) VALUES($1, $2) "
        "ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username",
        uid, username,
    )


async def get_user(uid: int) -> dict | None:
    row = await pool.fetchrow("SELECT * FROM users WHERE user_id = $1", uid)
    return dict(row) if row else None


async def set_enabled(uid: int, enabled: bool) -> None:
    await pool.execute("UPDATE users SET enabled=$2 WHERE user_id=$1", uid, enabled)


async def set_filters(uid: int, f: dict) -> None:
    await pool.execute(
        "UPDATE users SET collections=$2, models=$3, backdrops=$4, price_min=$5, price_max=$6, "
        "profit_mode=$7, profit_rarity=$8, exclude_collections=$9, exclude_models=$10, "
        "exclude_backdrops=$11, min_discount=$12 WHERE user_id=$1",
        uid, f["collections"], f["models"], f["backdrops"], f["price_min"], f["price_max"],
        f["profit_mode"], f["profit_rarity"], f["exclude_collections"], f["exclude_models"],
        f["exclude_backdrops"], f["min_discount"],
    )


async def extend_sub(uid: int, days: int) -> dict:
    await pool.execute(
        """
        INSERT INTO users(user_id, sub_until) VALUES($1, now() + make_interval(days => $2))
        ON CONFLICT (user_id) DO UPDATE SET sub_until =
            GREATEST(COALESCE(users.sub_until, now()), now()) + make_interval(days => $2)
        """,
        uid, days,
    )
    return await get_user(uid)


def sub_active(u: dict | None) -> bool:
    return bool(u and u["sub_until"] and u["sub_until"] > datetime.now(timezone.utc))


def has_access(uid: int, u: dict | None) -> bool:
    """Админ, человек из белого списка или (в платном режиме) с активной подпиской."""
    if uid in config.ADMIN_IDS:
        return True
    if u and u.get("allowed"):
        return True
    return config.PAID_MODE and sub_active(u)


async def set_allowed(uid: int, allowed: bool) -> None:
    await pool.execute(
        "INSERT INTO users(user_id, allowed) VALUES($1, $2) "
        "ON CONFLICT (user_id) DO UPDATE SET allowed = EXCLUDED.allowed",
        uid, allowed,
    )


async def active_users() -> list[dict]:
    rows = await pool.fetch("SELECT * FROM users WHERE enabled")
    users = [dict(r) for r in rows]
    return [u for u in users if has_access(u["user_id"], u)]


# ---------- monitor ----------
async def get_cursors() -> dict[str, int]:
    rows = await pool.fetch("SELECT prefix, last_num FROM cursors")
    return {r["prefix"]: r["last_num"] for r in rows}


async def set_cursor(prefix: str, num: int) -> None:
    await pool.execute(
        "INSERT INTO cursors(prefix, last_num) VALUES($1, $2) "
        "ON CONFLICT (prefix) DO UPDATE SET last_num = EXCLUDED.last_num",
        prefix, num,
    )


async def save_gift(data: dict) -> bool:
    res = await pool.execute(
        "INSERT INTO gifts(slug, prefix, num, data) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING",
        data["slug"], data["prefix"], data["num"], json.dumps(data),
    )
    return res.endswith("1")


# ---------- favorites ----------
async def toggle_fav(uid: int, slug: str) -> bool:
    """True - добавлено, False - убрано."""
    res = await pool.execute("DELETE FROM favorites WHERE user_id=$1 AND slug=$2", uid, slug)
    if res.endswith("1"):
        return False
    await pool.execute(
        "INSERT INTO favorites(user_id, slug) VALUES($1,$2) ON CONFLICT DO NOTHING", uid, slug)
    return True


async def remove_fav(uid: int, slug: str) -> None:
    await pool.execute("DELETE FROM favorites WHERE user_id=$1 AND slug=$2", uid, slug)


async def list_favs(uid: int) -> list[dict]:
    rows = await pool.fetch(
        "SELECT g.data FROM favorites f JOIN gifts g ON g.slug = f.slug "
        "WHERE f.user_id=$1 ORDER BY f.created_at DESC LIMIT 100", uid)
    return [json.loads(r["data"]) if isinstance(r["data"], str) else r["data"] for r in rows]


# ---------- admin ----------
async def list_users() -> list[dict]:
    rows = await pool.fetch(
        "SELECT user_id, username, enabled, allowed, sub_until FROM users ORDER BY created_at")
    return [dict(r) for r in rows]


async def should_alert(kind: str, cooldown_s: float) -> bool:
    """True, если алерт такого типа не отправлялся последние cooldown_s секунд (и фиксирует отправку)."""
    v = await pool.fetchval(
        "INSERT INTO meta(key, ts) VALUES($1, now()) "
        "ON CONFLICT (key) DO UPDATE SET ts = now() "
        "WHERE meta.ts < now() - make_interval(secs => $2) RETURNING 1",
        f"alert:{kind}", float(cooldown_s))
    return v is not None
