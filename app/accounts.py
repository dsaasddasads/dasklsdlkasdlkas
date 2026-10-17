"""Личные аккаунты пользователей.

Человек привязывает СВОЙ аккаунт Telegram (вход по коду в приложении, как в обычном клиенте).
С этого аккаунта бот пишет владельцам подарков и принимает их ответы.

Безопасность:
  * сессия хранится в базе только в зашифрованном виде (Fernet, ключ SESSION_KEY из переменных Railway);
    без SESSION_KEY (не короче 16 символов) новые аккаунты привязать нельзя;
  * старые привязки, зашифрованные прежним способом (ключ из BOT_TOKEN), при запуске перешифровываются на SESSION_KEY;
  * номер телефона в базе хранится только в замаскированном виде, код и пароль 2FA нигде не сохраняются;
  * один Telegram-аккаунт можно привязать только к одному пользователю бота;
  * «Отвязать» завершает сессию на стороне Telegram; админ тоже может отвязать аккаунт;
  * потерянные соединения восстанавливаются сами, сброшенные сессии отвязываются с уведомлением человеку;
  * бот пересылает только ответы владельцев, которым человек писал сам, остальные чаты не трогает;
  * лимит попыток входа: LINK_ATTEMPTS в час на человека.
"""
import asyncio
import base64
import hashlib
import html
import logging
import time

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from cryptography.fernet import Fernet, InvalidToken
from telethon import TelegramClient, events
from telethon.errors import (
    ApiIdInvalidError, AuthKeyDuplicatedError, AuthKeyUnregisteredError, FloodWaitError,
    PasswordHashInvalidError, PhoneCodeEmptyError, PhoneCodeExpiredError, PhoneCodeInvalidError,
    PhoneNumberBannedError, PhoneNumberFloodError, PhoneNumberInvalidError, SessionExpiredError,
    SessionPasswordNeededError, SessionRevokedError, UserDeactivatedBanError, UserDeactivatedError,
)
from telethon.sessions import StringSession

from . import config, db, settings

log = logging.getLogger("accounts")

DEAD = (AuthKeyDuplicatedError, AuthKeyUnregisteredError, SessionRevokedError, SessionExpiredError,
        UserDeactivatedError, UserDeactivatedBanError)


def _fernet(secret: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


_PRIMARY = _fernet(config.SESSION_KEY) if config.SESSION_KEY else None
_LEGACY = _fernet("gift-track|" + config.BOT_TOKEN)  # так шифровали раньше, пока SESSION_KEY не был задан


def link_ok() -> bool:
    """Можно ли привязывать новые аккаунты: нужен нормальный ключ шифрования."""
    return bool(config.SESSION_KEY) and len(config.SESSION_KEY) >= 16


def problem() -> str | None:
    if not config.SESSION_KEY:
        return "не задана переменная SESSION_KEY"
    if len(config.SESSION_KEY) < 16:
        return "SESSION_KEY слишком короткий (нужно не менее 16 символов)"
    return None


class LinkError(Exception):
    def __init__(self, text: str):
        super().__init__(text)
        self.text = text


def encrypt(s: str) -> str:
    if _PRIMARY is None:
        raise LinkError("Привязка недоступна: админу нужно задать SESSION_KEY.")
    return _PRIMARY.encrypt(s.encode()).decode()


def decrypt(s: str) -> tuple[str, bool]:
    """(сессия, расшифровано старым ключом)."""
    if _PRIMARY is not None:
        try:
            return _PRIMARY.decrypt(s.encode()).decode(), False
        except InvalidToken:
            pass
    return _LEGACY.decrypt(s.encode()).decode(), True


def mask_phone(phone: str) -> str:
    return phone[:4] + "•" * max(0, len(phone) - 6) + phone[-2:] if len(phone) > 6 else phone


ERRORS = {
    PhoneNumberInvalidError: "Неверный номер телефона. Введи в формате +380...",
    PhoneNumberBannedError: "Этот номер заблокирован Telegram.",
    PhoneNumberFloodError: "Слишком много попыток для этого номера. Попробуй позже.",
    PhoneCodeInvalidError: "Неверный код.",
    PhoneCodeEmptyError: "Введи код из Telegram.",
    PhoneCodeExpiredError: "Код истёк. Запроси новый.",
    PasswordHashInvalidError: "Неверный пароль двухэтапной проверки.",
    ApiIdInvalidError: "Ошибка настроек API бота (API_ID / API_HASH).",
}


def explain(e: Exception) -> str:
    """Человеческое объяснение ошибки входа."""
    if isinstance(e, LinkError):
        return e.text
    if isinstance(e, FloodWaitError):
        return f"Слишком много попыток. Подожди {max(1, e.seconds // 60)} мин."
    for cls, text in ERRORS.items():
        if isinstance(e, cls):
            return text
    log.warning("link error: %s", type(e).__name__)
    return "Не удалось войти. Попробуй ещё раз."


def _new_client(session: str = "") -> TelegramClient:
    return TelegramClient(StringSession(session), config.API_ID, config.API_HASH,
                          device_model="Gift Track", system_version="Linux", app_version="1.0")


class Accounts:
    def __init__(self) -> None:
        self.clients: dict[int, TelegramClient] = {}
        self.linked: set[int] = set()
        self.pending: dict[int, dict] = {}
        self.attempts: dict[int, list[float]] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._retry: dict[int, float] = {}
        self.bot: Bot | None = None

    def bind_bot(self, bot: Bot) -> None:
        self.bot = bot

    def get(self, uid: int) -> TelegramClient | None:
        return self.clients.get(uid)

    def has(self, uid: int) -> bool:
        """Аккаунт привязан (даже если прямо сейчас переподключается)."""
        return uid in self.linked

    def _lock(self, uid: int) -> asyncio.Lock:
        return self._locks.setdefault(uid, asyncio.Lock())

    # ---------- запуск, восстановление и отзыв ----------
    async def start(self) -> None:
        """Подключает все сохранённые аккаунты, переводит их на новый ключ и маскирует телефоны."""
        rows = await db.list_accounts()
        self.linked = {r["user_id"] for r in rows}
        if rows and _PRIMARY is None:
            log.warning("Привязанные аккаунты зашифрованы ключом из BOT_TOKEN. Задай SESSION_KEY (%s), "
                        "после этого они перешифруются автоматически.", problem())
        for row in rows:
            await self._migrate(row)
            await self.ensure(row["user_id"])
            await asyncio.sleep(0.5)
        log.info("привязанных аккаунтов подключено: %d из %d", len(self.clients), len(rows))

    async def _migrate(self, row: dict) -> None:
        """Старый ключ -> SESSION_KEY, открытый телефон -> маска."""
        try:
            session, legacy = decrypt(row["session"])
        except (InvalidToken, ValueError):
            return  # ensure() разберётся и отвяжет
        phone = row.get("phone") or ""
        need_mask = bool(phone) and "•" not in phone
        if (legacy and _PRIMARY is not None) or need_mask:
            await db.update_account_session(
                row["user_id"], encrypt(session) if _PRIMARY is not None else row["session"],
                mask_phone(phone) if need_mask else phone)

    async def ensure(self, uid: int) -> TelegramClient | None:
        """Подключённый клиент человека. Сам переподключается; None - если аккаунта нет или он не отвечает."""
        async with self._lock(uid):
            c = self.clients.get(uid)
            if c is not None:
                if c.is_connected():
                    return c
                try:
                    await c.connect()
                    return c
                except Exception as e:
                    self._retry[uid] = time.time() + 120
                    log.warning("account %s reconnect failed: %s", uid, type(e).__name__)
                    return None
            row = await db.get_account(uid)
            if not row:
                self.linked.discard(uid)
                return None
            self.linked.add(uid)
            try:
                session, _ = decrypt(row["session"])
            except (InvalidToken, ValueError):
                await self.drop(uid, reason="не удалось расшифровать сессию (изменился SESSION_KEY)")
                return None
            c = _new_client(session)
            try:
                await c.connect()
                if not await c.is_user_authorized():
                    await c.disconnect()
                    await self.drop(uid, reason="Telegram сбросил сессию")
                    return None
            except DEAD as e:
                await self.drop(uid, reason=f"Telegram сбросил сессию ({type(e).__name__})")
                return None
            except Exception as e:  # сеть - keepalive попробует снова
                self._retry[uid] = time.time() + 120
                log.warning("account %s connect failed: %s", uid, type(e).__name__)
                try:
                    await c.disconnect()
                except Exception:
                    pass
                return None
            self._attach(uid, c)
            self.clients[uid] = c
            self._retry.pop(uid, None)
            return c

    async def keepalive(self) -> None:
        """Раз в минуту поднимает отвалившиеся аккаунты."""
        while True:
            await asyncio.sleep(60)
            try:
                for row in await db.list_accounts():
                    uid = row["user_id"]
                    c = self.clients.get(uid)
                    if c is not None and c.is_connected():
                        continue
                    if time.time() < self._retry.get(uid, 0):
                        continue
                    await self.ensure(uid)
            except Exception:
                log.exception("keepalive failed")

    async def drop(self, uid: int, notify: bool = True, reason: str = "Telegram сбросил сессию") -> None:
        """Сессия больше не работает: удаляем привязку и сообщаем человеку."""
        c = self.clients.pop(uid, None)
        if c:
            try:
                await c.disconnect()
            except Exception:
                pass
        await db.delete_account(uid)
        self.linked.discard(uid)
        log.warning("account %s dropped: %s", uid, reason)
        if notify and self.bot:
            await self._tell(uid, f"⚠️ Аккаунт отвязан: {reason}. Привяжи заново: приложение → «Аккаунт».")

    async def _tell(self, uid: int, text: str) -> None:
        try:
            await self.bot.send_message(uid, text)
        except Exception:
            pass

    async def unlink(self, uid: int, by_admin: bool = False) -> None:
        c = self.clients.pop(uid, None) or await self._temp_client(uid)
        if c:
            try:
                await c.log_out()  # завершает сессию на стороне Telegram
            except Exception:
                try:
                    await c.disconnect()
                except Exception:
                    pass
        await db.delete_account(uid)
        self.linked.discard(uid)
        await db.log_event(uid, "account_unlink")
        if by_admin and self.bot:
            await self._tell(uid, "Админ отвязал твой аккаунт. Сессия завершена.")

    async def _temp_client(self, uid: int) -> TelegramClient | None:
        """Клиент для отвязки аккаунта, который сейчас не подключён."""
        row = await db.get_account(uid)
        if not row:
            return None
        try:
            c = _new_client(decrypt(row["session"])[0])
            await c.connect()
            return c
        except Exception:
            return None

    # ---------- входящие ответы ----------
    def _attach(self, uid: int, client: TelegramClient) -> None:
        async def on_message(event) -> None:
            try:
                if not event.is_private or event.out or not self.bot:
                    return
                th = await db.thread_for_reply(uid, event.sender_id)
                if not th:
                    return  # не наш собеседник - остальные личные сообщения не пересылаем
                await db.touch_thread_in(th["id"])
                await db.log_event(uid, "reply_in", th["slug"])
                sender = await event.get_sender()
                name = " ".join(x for x in (getattr(sender, "first_name", None),
                                            getattr(sender, "last_name", None)) if x)
                uname = getattr(sender, "username", None)
                label = f"{name} (@{uname})" if name and uname else (f"@{uname}" if uname else name or "владелец")
                text = event.raw_text or ""
                if event.message.media:
                    text = (text + "\n" if text else "") + "📎 вложение (открой чат с владельцем, чтобы посмотреть)"
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="↩️ Ответить", callback_data=f"rep:{th['id']}")],
                    [InlineKeyboardButton(text="🎁 Открыть подарок", url=f"https://t.me/nft/{th['slug']}")],
                ])
                await self.bot.send_message(
                    uid,
                    f"💬 <b>Ответ по {html.escape(th['collection'] or th['slug'])} #{th['num']}</b>\n"
                    f"от {html.escape(label)}:\n\n{html.escape(text[:3500])}",
                    reply_markup=kb)
            except Exception:
                log.exception("incoming relay failed")

        client.add_event_handler(on_message, events.NewMessage(incoming=True))

    # ---------- привязка: телефон -> код -> (пароль) ----------
    def _rate(self, uid: int) -> None:
        now = time.time()
        lst = [t for t in self.attempts.get(uid, []) if now - t < 3600]
        if len(lst) >= config.LINK_ATTEMPTS:
            raise LinkError("Слишком много попыток входа. Попробуй через час.")
        lst.append(now)
        self.attempts[uid] = lst

    async def cancel(self, uid: int) -> None:
        p = self.pending.pop(uid, None)
        if p:
            try:
                await p["client"].disconnect()
            except Exception:
                pass

    def _pending(self, uid: int) -> dict:
        p = self.pending.get(uid)
        if not p or time.time() - p["ts"] > 600:
            raise LinkError("Время входа истекло. Начни заново.")
        return p

    async def link_phone(self, uid: int, phone: str) -> None:
        if settings.get_bool("link_closed"):
            raise LinkError("Привязка аккаунтов временно закрыта админом.")
        if not link_ok():
            raise LinkError("Привязка недоступна: админу нужно задать SESSION_KEY (не короче 16 символов).")
        digits = "".join(ch for ch in phone if ch.isdigit())
        if not 8 <= len(digits) <= 15:
            raise LinkError("Неверный номер телефона. Введи в формате +380...")
        self._rate(uid)
        await self.cancel(uid)
        client = _new_client()
        await client.connect()
        try:
            sent = await client.send_code_request("+" + digits)
        except Exception:
            await client.disconnect()
            raise
        self.pending[uid] = {"client": client, "phone": "+" + digits, "hash": sent.phone_code_hash,
                             "ts": time.time()}

    async def link_code(self, uid: int, code: str) -> str:
        """'done' - аккаунт привязан, 'password' - нужен пароль двухэтапной проверки."""
        p = self._pending(uid)
        digits = "".join(ch for ch in code if ch.isdigit())
        try:
            await p["client"].sign_in(phone=p["phone"], code=digits, phone_code_hash=p["hash"])
        except SessionPasswordNeededError:
            return "password"
        await self._finish(uid, p)
        return "done"

    async def link_password(self, uid: int, password: str) -> None:
        p = self._pending(uid)
        await p["client"].sign_in(password=password)
        await self._finish(uid, p)

    async def _finish(self, uid: int, p: dict) -> None:
        client = p["client"]
        me = await client.get_me()
        owner = await db.account_owner(me.id)
        if owner is not None and owner != uid:
            try:
                await client.log_out()  # чужой привязанный аккаунт не берём
            except Exception:
                pass
            self.pending.pop(uid, None)
            raise LinkError("Этот аккаунт уже привязан к другому пользователю.")
        name = " ".join(x for x in (me.first_name, me.last_name) if x)
        old = self.clients.pop(uid, None)
        if old is not None and old is not client:
            try:
                await old.log_out()  # прежнюю привязку отзываем
            except Exception:
                pass
        await db.save_account(uid, encrypt(client.session.save()), mask_phone(p["phone"]), me.id, name, me.username)
        self.pending.pop(uid, None)
        self._attach(uid, client)
        self.clients[uid] = client
        self.linked.add(uid)
        self._retry.pop(uid, None)
        await db.log_event(uid, "account_link")

    async def info(self, uid: int) -> dict:
        row = await db.get_account(uid)
        if not row:
            return {"linked": False}
        return {"linked": True, "name": row["name"], "username": row["username"],
                "phone": row["phone"] or "", "connected": uid in self.clients}

    async def health(self) -> list[dict]:
        """Состояние всех привязанных аккаунтов - для админ-панели."""
        out = []
        for r in await db.list_accounts():
            uid = r["user_id"]
            c = self.clients.get(uid)
            out.append({"id": uid, "name": r["username"] or r["name"] or "", "connected": bool(c and c.is_connected()),
                        "retry": max(0, int(self._retry.get(uid, 0) - time.time()))})
        return out


manager = Accounts()
