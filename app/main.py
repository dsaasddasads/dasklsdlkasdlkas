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


async def main() -> None:
    await db.init()

    client = TelegramClient(StringSession(config.SESSION_STRING), config.API_ID, config.API_HASH,
                            receive_updates=False)
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit(
            "SESSION_STRING пустой или недействительный. Сгенерируй заново: python generate_session.py, "
            "и вставь в Railway Variables только саму строку (без 'SESSION_STRING=' и без кавычек).")

    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    async def on_gift(d: dict) -> None:
        await botmod.broadcast(bot, client, d)

    monitor = Monitor(client, on_gift)

    dp = Dispatcher()
    dp.include_router(botmod.router)
    dp["monitor"] = monitor

    runner = web.AppRunner(make_app(bot))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", config.PORT).start()

    await asyncio.gather(monitor.run(), dp.start_polling(bot))


if __name__ == "__main__":
    asyncio.run(main())
