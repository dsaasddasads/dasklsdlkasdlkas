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

ALTER TABLE users ADD COLUMN IF NOT EXISTS auto_ok    BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS auto_state TEXT    NOT NULL DEFAULT 'off';
ALTER TABLE users ADD COLUMN IF NOT EXISTS auto_limit INT     NOT NULL DEFAULT 5;
ALTER TABLE users ADD COLUMN IF NOT EXISTS auto_gap   INT     NOT NULL DEFAULT 60;
ALTER TABLE users ADD COLUMN IF NOT EXISTS auto_tpl   INT     NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS auto_text  TEXT    NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS auto_sent  INT     NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS events (
    id      BIGSERIAL PRIMARY KEY,
    ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id BIGINT NOT NULL,
    kind    TEXT NOT NULL,
    slug    TEXT
);
CREATE INDEX IF NOT EXISTS events_ts_idx   ON events(ts);
CREATE INDEX IF NOT EXISTS events_user_idx ON events(user_id, kind);

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
CREATE TABLE IF NOT EXISTS threads (
    id         SERIAL PRIMARY KEY,
    buyer_id   BIGINT NOT NULL,
    owner_id   BIGINT NOT NULL,
    owner_hash BIGINT,
    slug       TEXT NOT NULL,
    collection TEXT,
    num        INT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_in    TIMESTAMPTZ
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
        "SELECT user_id, username, enabled, allowed, sub_until, auto_ok, auto_state FROM users ORDER BY created_at")
    return [dict(r) for r in rows]


async def should_alert(kind: str, cooldown_s: float) -> bool:
    """True, если алерт такого типа не отправлялся последние cooldown_s секунд (и фиксирует отправку)."""
    v = await pool.fetchval(
        "INSERT INTO meta(key, ts) VALUES($1, now()) "
        "ON CONFLICT (key) DO UPDATE SET ts = now() "
        "WHERE meta.ts < now() - make_interval(secs => $2) RETURNING 1",
        f"alert:{kind}", float(cooldown_s))
    return v is not None


# ---------- меню в чате ----------
async def set_profit(uid: int, on: bool) -> None:
    await pool.execute("UPDATE users SET profit_mode=$2 WHERE user_id=$1", uid, on)


async def get_gift(slug: str) -> dict | None:
    v = await pool.fetchval("SELECT data FROM gifts WHERE slug=$1", slug)
    if v is None:
        return None
    return json.loads(v) if isinstance(v, str) else v


async def add_exclude(uid: int, col: str, value: str) -> None:
    if col not in ("exclude_collections", "exclude_models", "exclude_backdrops"):
        raise ValueError(col)
    await pool.execute(
        f"UPDATE users SET {col} = array_append({col}, $2) "
        f"WHERE user_id=$1 AND NOT ($2 = ANY({col}))", uid, value)


# ---------- диалоги с владельцами ----------
async def create_thread(buyer_id: int, owner_id: int, owner_hash: int | None,
                        slug: str, collection: str | None, num: int | None) -> int:
    return await pool.fetchval(
        "INSERT INTO threads(buyer_id, owner_id, owner_hash, slug, collection, num) "
        "VALUES($1,$2,$3,$4,$5,$6) RETURNING id",
        buyer_id, owner_id, owner_hash, slug, collection, num)


async def get_thread(tid: int) -> dict | None:
    row = await pool.fetchrow("SELECT * FROM threads WHERE id=$1", tid)
    return dict(row) if row else None


async def thread_for(buyer_id: int, owner_id: int) -> dict | None:
    """Недавний диалог этого человека с этим владельцем (72 часа)."""
    row = await pool.fetchrow(
        "SELECT * FROM threads WHERE buyer_id=$1 AND owner_id=$2 "
        "AND created_at > now() - interval '72 hours' ORDER BY created_at DESC LIMIT 1",
        buyer_id, owner_id)
    return dict(row) if row else None


async def latest_thread_for_owner(owner_id: int) -> dict | None:
    row = await pool.fetchrow(
        "SELECT * FROM threads WHERE owner_id=$1 AND created_at > now() - interval '14 days' "
        "ORDER BY COALESCE(last_in, created_at) DESC, created_at DESC LIMIT 1", owner_id)
    return dict(row) if row else None


async def touch_thread_in(tid: int) -> None:
    await pool.execute("UPDATE threads SET last_in=now() WHERE id=$1", tid)


async def count_threads(hours: int, buyer_id: int | None) -> int:
    if buyer_id is None:
        return await pool.fetchval(
            "SELECT count(*) FROM threads WHERE created_at > now() - make_interval(hours => $1)", hours)
    return await pool.fetchval(
        "SELECT count(*) FROM threads WHERE buyer_id=$2 AND created_at > now() - make_interval(hours => $1)",
        hours, buyer_id)


# ---------- авто-режим ----------
def auto_allowed(u: dict | None) -> bool:
    """Авто-сообщения доступны админам и тем, кому админ их разрешил."""
    return bool(config.RELAY and u and (u["user_id"] in config.ADMIN_IDS or u.get("auto_ok")))


async def set_auto_ok(uid: int, ok: bool) -> None:
    await pool.execute(
        "INSERT INTO users(user_id, auto_ok) VALUES($1, $2) "
        "ON CONFLICT (user_id) DO UPDATE SET auto_ok = EXCLUDED.auto_ok, "
        "auto_state = CASE WHEN EXCLUDED.auto_ok THEN users.auto_state ELSE 'off' END",
        uid, ok)


async def set_auto_settings(uid: int, limit, gap, tpl, text: str) -> None:
    limit = max(1, min(int(limit), config.AUTO_MAX))
    gap = max(config.AUTO_MIN_GAP, min(int(gap), config.AUTO_MAX_GAP))
    tpl = max(0, int(tpl))
    await pool.execute(
        "UPDATE users SET auto_limit=$2, auto_gap=$3, auto_tpl=$4, auto_text=$5 WHERE user_id=$1",
        uid, limit, gap, tpl, (text or "").strip()[:500])


async def auto_control(uid: int, action: str) -> dict | None:
    sql = {
        "start": "UPDATE users SET auto_state='on', auto_sent=0 WHERE user_id=$1 RETURNING *",
        "pause": "UPDATE users SET auto_state='paused' WHERE user_id=$1 AND auto_state='on' RETURNING *",
        "resume": "UPDATE users SET auto_state='on' WHERE user_id=$1 AND auto_state='paused' RETURNING *",
        "stop": "UPDATE users SET auto_state='off' WHERE user_id=$1 RETURNING *",
    }[action]
    row = await pool.fetchrow(sql, uid)
    return dict(row) if row else await get_user(uid)


async def auto_incr(uid: int) -> dict:
    """+1 отправленное авто-сообщение; при достижении лимита режим сам выключается."""
    row = await pool.fetchrow(
        "UPDATE users SET auto_sent = auto_sent + 1, "
        "auto_state = CASE WHEN auto_sent + 1 >= LEAST(auto_limit, $2) THEN 'off' ELSE auto_state END "
        "WHERE user_id=$1 RETURNING auto_sent, auto_limit, auto_state", uid, config.AUTO_MAX)
    return dict(row)


# ---------- события и статистика ----------
async def log_event(uid: int, kind: str, slug: str | None = None) -> None:
    try:
        await pool.execute("INSERT INTO events(user_id, kind, slug) VALUES($1,$2,$3)", uid, kind, slug)
    except Exception:  # статистика не должна ломать работу бота
        pass


async def event_counts(hours: int, uid: int | None = None) -> list[dict]:
    if uid is None:
        rows = await pool.fetch(
            "SELECT user_id, kind, count(*)::int AS c FROM events "
            "WHERE ts > now() - make_interval(hours => $1) GROUP BY user_id, kind", hours)
    else:
        rows = await pool.fetch(
            "SELECT user_id, kind, count(*)::int AS c FROM events "
            "WHERE ts > now() - make_interval(hours => $1) AND user_id=$2 GROUP BY user_id, kind", hours, uid)
    return [dict(r) for r in rows]


async def last_seen() -> dict:
    rows = await pool.fetch("SELECT user_id, max(ts) AS ts FROM events GROUP BY user_id")
    return {r["user_id"]: r["ts"] for r in rows}


async def gifts_count(hours: int) -> int:
    return await pool.fetchval(
        "SELECT count(*)::int FROM gifts WHERE created_at > now() - make_interval(hours => $1)", hours)


async def thread_totals() -> tuple[int, int]:
    row = await pool.fetchrow("SELECT count(*)::int AS total, count(last_in)::int AS replied FROM threads")
    return row["total"], row["replied"]


async def user_threads(uid: int, n: int = 5) -> list[dict]:
    rows = await pool.fetch("SELECT * FROM threads WHERE buyer_id=$1 ORDER BY created_at DESC LIMIT $2", uid, n)
    return [dict(r) for r in rows]
