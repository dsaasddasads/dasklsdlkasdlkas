# Gift Track Bot

Бот уведомляет о только что улучшенных Telegram-подарках (NFT) по фильтрам пользователя,
даёт ссылку на владельца и создаёт закрытый чат для сделки. Подписка - за Telegram Stars.
Один процесс: aiogram (бот) + Telethon (мониторинг) + aiohttp (Mini App и API).

## Запуск
1. **my.telegram.org** -> API development tools -> получи `API_ID`, `API_HASH`.
2. **@BotFather** -> `/newbot` -> `BOT_TOKEN`.
3. Локально: `pip install telethon && python generate_session.py` -> `SESSION_STRING`
   (используй ОТДЕЛЬНЫЙ аккаунт, не основной).
4. Залей папку в GitHub-репозиторий.
5. **Railway**: New Project -> Deploy from GitHub repo -> выбери репозиторий.
6. В проекте: New -> Database -> PostgreSQL.
7. В сервисе бота -> Variables: добавь переменные из `.env.example`
   (`DATABASE_URL` = `${{Postgres.DATABASE_URL}}`).
8. Settings -> Networking -> Generate Domain, вставь домен в `WEBAPP_URL`, Redeploy.
9. @BotFather -> `/mybots` -> твой бот -> Bot Settings -> Menu Button -> укажи `WEBAPP_URL`.
10. Напиши боту `/start` (админы из `ADMIN_IDS` имеют доступ сразу). Друзьям: они жмут `/start`, ты пишешь `/allow <их_id>`.

## Команды
`/start` (список функций), `/status`, админ: `/allow <user_id>`, `/deny <user_id>`, `/grant <user_id> <дней>` (только при `PAID_MODE=1`)

## Важно
- Схема TL по подаркам меняется: если после обновления Telethon что-то упало,
  проверь `core.telegram.org/schema` (методы `payments.getUniqueStarGift`,
  `payments.getStarGifts`, `payments.getResaleStarGifts`).
- Чем больше коллекций и чем меньше `POLL_INTERVAL`, тем выше риск FloodWait. Подбирай
  `POLL_INTERVAL` и `CONCURRENCY` по логам.
- Цена в уведомлении - ориентир (флор коллекции на маркете), не оценка конкретной редкости.
- Бот не пишет владельцам сам: покупатель нажимает кнопку и пишет лично.
