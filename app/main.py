import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiohttp import web
from telethon import TelegramClient
from telethon.sessions import StringSession

from . import bot as botmod, config, db
from .monitor import Monitor
from .web import make_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")


async def main() -> None:
    await db.init()
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    async def alert(text: str) -> None:
        """Сообщение всем админам."""
        for admin in config.ADMIN_IDS:
            try:
                await bot.send_message(admin, text)
            except Exception as e:
                log.debug("alert to %s failed: %s", admin, e)

    client = TelegramClient(StringSession(config.SESSION_STRING), config.API_ID, config.API_HASH,
                            receive_updates=False)
    await client.connect()
    if not await client.is_user_authorized():
        if await db.should_alert("session", 1800):
            await alert("🛑 SESSION_STRING пустой или недействительный. Сгенерируй новый "
                        "(generate_session.py) и обнови переменную на Railway.")
        raise SystemExit(
            "SESSION_STRING пустой или недействительный. Сгенерируй заново: python generate_session.py, "
            "и вставь в Railway Variables только саму строку (без 'SESSION_STRING=' и без кавычек).")

    async def on_gift(d: dict) -> None:
        await botmod.broadcast(bot, client, monitor, d)

    monitor = Monitor(client, on_gift, alert)

    dp = Dispatcher()
    dp.include_router(botmod.router)
    dp["monitor"] = monitor

    runner = web.AppRunner(make_app(bot))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", config.PORT).start()

    try:
        await asyncio.gather(monitor.run(), monitor.watchdog(), dp.start_polling(bot))
    except SystemExit:
        raise
    except Exception as e:
        if await db.should_alert("crash", 600):
            await alert(f"💥 Бот упал: {type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
