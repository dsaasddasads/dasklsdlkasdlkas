"""Запусти ЛОКАЛЬНО один раз: python generate_session.py
Выведет SESSION_STRING для юзербота (отдельный аккаунт, не основной!)."""
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = int(input("API_ID: ").strip())
api_hash = input("API_HASH: ").strip()
with TelegramClient(StringSession(), api_id, api_hash) as c:
    print("\nSESSION_STRING=" + c.session.save())
