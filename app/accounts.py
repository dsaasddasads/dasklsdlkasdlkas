"""Личные аккаунты пользователей.

Человек привязывает СВОЙ аккаунт Telegram (вход по коду в приложении, как в обычном клиенте).
С этого аккаунта бот пишет владельцам подарков и принимает их ответы.

Безопасность:
  * сессия хранится в базе в зашифрованном виде (Fernet, ключ из SESSION_KEY);
  * бот пересылает только ответы тех владельцев, кому человек писал сам, остальные чаты не трогает;
  * «Отвязать» завершает сессию на стороне Telegram (аккаунт выходит из устройства «бот»);
  * лимит попыток входа: 5 в час на человека.
"""
import asyncio
import base64
import hashlib
import html
import logging
import os
import time

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from cryptography.fernet import Fernet
from telethon import TelegramClient, events
from telethon.errors import (
    ApiIdInvalidError, FloodWaitError, PasswordHashInvalidError, PhoneCodeEmptyError,
    PhoneCodeExpiredError, PhoneCodeInvalidError, PhoneNumberBannedError, PhoneNumberFloodError,
    PhoneNumberInvalidError, SessionPasswordNeededError,
)
from telethon.sessions import StringSession

from . import config, db

log = logging.getLogger("accounts")

_secret = os.getenv("SESSION_KEY") or ("gift-track|" + config.BOT_TOKEN)
_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(_secret.encode()).digest()))


def encrypt(s: str) -> str:
    return _fernet.encrypt(s.encode()).decode()


def decrypt(s: str) -> str:
    return _fernet.decrypt(s.encode()).decode()


class LinkError(Exception):
    def __init__(self, text: str):
        super().__init__(text)
        self.text = text


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
        return f"Слишком много попыток. Подожди {e.seconds} с."
    for cls, text in ERRORS.items():
        if isinstance(e, cls):
            return text
    return f"Не удалось: {e}"


class Accounts:
    def __init__(self) -> None:
        self.clients: dict[int, TelegramClient] = {}
        self.pending: dict[int, dict] = {}
        self.attempts: dict[int, list[float]] = {}
        self.bot: Bot | None = None

    def bind_bot(self, bot: Bot) -> None:
        self.bot = bot

    def get(self, uid: int) -> TelegramClient | None:
        return self.clients.get(uid)

    # ---------- запуск и отзыв ----------
    async def start(self) -> None:
        """Подключает все сохранённые аккаунты."""
        for row in await db.list_accounts():
            uid = row["user_id"]
            try:
                client = TelegramClient(StringSession(decrypt(row["session"])), config.API_ID, config.API_HASH)
                await client.connect()
                if not await client.is_user_authorized():
                    await client.disconnect()
                    await self.drop(uid)  # сессию отозвали в Telegram
                    continue
                self._attach(uid, client)
                self.clients[uid] = client
            except Exception as e:
                log.warning("аккаунт %s не подключён: %s", uid, e)
            await asyncio.sleep(0.5)
        log.info("привязанных аккаунтов подключено: %d", len(self.clients))

    async def drop(self, uid: int, notify: bool = True) -> None:
        """Сессия больше не работает: удаляем привязку и сообщаем человеку."""
        c = self.clients.pop(uid, None)
        if c:
            try:
                await c.disconnect()
            except Exception:
                pass
        await db.delete_account(uid)
        if notify and self.bot:
            try:
                await self.bot.send_message(
                    uid, "⚠️ Сессия привязанного аккаунта сброшена. Привяжи аккаунт заново: "
                         "приложение → вкладка «Аккаунт».")
            except Exception:
                pass

    async def unlink(self, uid: int) -> None:
        c = self.clients.pop(uid, None)
        if c:
            try:
                await c.log_out()  # завершает сессию на стороне Telegram
            except Exception:
                try:
                    await c.disconnect()
                except Exception:
                    pass
        await db.delete_account(uid)
        await db.log_event(uid, "account_unlink")

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
        if len(lst) >= 5:
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
        digits = "".join(ch for ch in phone if ch.isdigit())
        if len(digits) < 8:
            raise LinkError("Неверный номер телефона. Введи в формате +380...")
        self._rate(uid)
        await self.cancel(uid)
        client = TelegramClient(StringSession(), config.API_ID, config.API_HASH)
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
        name = " ".join(x for x in (me.first_name, me.last_name) if x)
        old = self.clients.pop(uid, None)
        if old is not None and old is not client:
            try:
                await old.log_out()  # прежнюю привязку отзываем
            except Exception:
                pass
        await db.save_account(uid, encrypt(client.session.save()), p["phone"], me.id, name, me.username)
        self.pending.pop(uid, None)
        self._attach(uid, client)
        self.clients[uid] = client
        await db.log_event(uid, "account_link")

    async def info(self, uid: int) -> dict:
        row = await db.get_account(uid)
        if not row:
            return {"linked": False}
        ph = row["phone"] or ""
        masked = ph[:4] + "•" * max(0, len(ph) - 6) + ph[-2:] if len(ph) > 6 else ph
        return {"linked": True, "name": row["name"], "username": row["username"],
                "phone": masked, "connected": uid in self.clients}


manager = Accounts()
