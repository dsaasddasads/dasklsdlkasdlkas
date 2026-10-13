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
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    collections TEXT[] NOT NULL DEFAULT '{}',
    models      TEXT[] NOT NULL DEFAULT '{}',
    backdrops   TEXT[] NOT NULL DEFAULT '{}',
    price_min   BIGINT,
    price_max   BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
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
CREATE TABLE IF NOT EXISTS deals (
    id          SERIAL PRIMARY KEY,
    slug        TEXT NOT NULL,
    buyer_id    BIGINT NOT NULL,
    invite_link TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
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


async def set_filters(uid: int, f: dict) -> None:
    await pool.execute(
        "UPDATE users SET enabled=$2, collections=$3, models=$4, backdrops=$5, "
        "price_min=$6, price_max=$7 WHERE user_id=$1",
        uid, f["enabled"], f["collections"], f["models"], f["backdrops"],
        f["price_min"], f["price_max"],
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


async def active_users() -> list[dict]:
    rows = await pool.fetch("SELECT * FROM users WHERE enabled AND sub_until > now()")
    return [dict(r) for r in rows]


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


# ---------- deals ----------
async def create_deal(slug: str, buyer_id: int) -> int:
    return await pool.fetchval(
        "INSERT INTO deals(slug, buyer_id) VALUES($1,$2) RETURNING id", slug, buyer_id
    )


async def set_deal_link(deal_id: int, link: str) -> None:
    await pool.execute("UPDATE deals SET invite_link=$2 WHERE id=$1", deal_id, link)
