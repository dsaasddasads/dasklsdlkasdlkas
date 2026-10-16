import os


def _ids(v: str) -> set[int]:
    return {int(x) for x in v.replace(" ", "").split(",") if x}


BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"].strip().strip("\"'").removeprefix("SESSION_STRING=").strip()
DATABASE_URL = os.environ["DATABASE_URL"]
_wu = os.getenv("WEBAPP_URL", "").strip().rstrip("/")
if _wu and not _wu.startswith(("http://", "https://")):
    _wu = "https://" + _wu  # Telegram принимает для Mini App только HTTPS
WEBAPP_URL = _wu
ADMIN_IDS = _ids(os.getenv("ADMIN_IDS", ""))
PORT = int(os.getenv("PORT", "8080"))

COLLECTIONS = [c.strip() for c in os.getenv("COLLECTIONS", "").split(",") if c.strip()]
# Защита сессии от сброса: общий лимит запросов к Telegram и адаптивные интервалы опроса
RPS = float(os.getenv("RPS", "2"))                      # макс. запросов в секунду на всё приложение
MIN_INTERVAL = float(os.getenv("MIN_INTERVAL", "15"))   # как часто проверять "горячую" коллекцию, сек
MAX_INTERVAL = float(os.getenv("MAX_INTERVAL", "120"))  # как редко проверять "холодную" коллекцию, сек

# PAID_MODE=1 включает платный тариф; по умолчанию бот приватный (доступ по списку)
PAID_MODE = os.getenv("PAID_MODE", "0") == "1"
# дни -> цена в Telegram Stars (один тариф)
PLANS = {7: int(os.getenv("PRICE_7D", "2000"))}

# Оценка рынка и режим выгодных предложений
PREMIUM_X = float(os.getenv("PREMIUM_X", "1.5"))   # во сколько раз модель/фон должны быть дороже флора коллекции
MIN_LISTED = int(os.getenv("MIN_LISTED", "3"))     # мин. число листингов, чтобы флор считался надёжным

# Контроль состояния: уведомления админам
STALL_SECONDS = int(os.getenv("STALL_SECONDS", "300"))      # нет ответов Telegram дольше - тревога
NO_GIFTS_HOURS = float(os.getenv("NO_GIFTS_HOURS", "6"))    # нет новых подарков дольше - предупреждение

# Какие подарки показывать
ONLY_CONTACTABLE = os.getenv("ONLY_CONTACTABLE", "1") == "1"  # только если владельцу можно написать (обычный пользователь)
REQUIRE_OFFER = os.getenv("REQUIRE_OFFER", "1") == "1"        # только если на подарок можно отправить Offer to Buy

# Сообщения владельцам от имени юзербота (только по нажатию кнопки)
RELAY = os.getenv("RELAY", "1") == "1"
RELAY_DAILY_LIMIT = int(os.getenv("RELAY_DAILY_LIMIT", "15"))    # новых диалогов на человека за 24 ч
RELAY_GLOBAL_DAILY = int(os.getenv("RELAY_GLOBAL_DAILY", "100"))  # новых диалогов всего за 24 ч
RELAY_MIN_GAP = float(os.getenv("RELAY_MIN_GAP", "20"))          # пауза между отправками, сек
RELAY_TEMPLATES = [t.strip() for t in os.getenv("RELAY_TEMPLATES", "").split("||") if t.strip()]

# Авто-режим сообщений (включается админом для конкретных людей, жёсткие потолки)
AUTO_MAX = int(os.getenv("AUTO_MAX", "5"))              # макс. сообщений за один запуск
AUTO_MIN_GAP = int(os.getenv("AUTO_MIN_GAP", "30"))     # мин. пауза между авто-сообщениями, сек
AUTO_MAX_GAP = int(os.getenv("AUTO_MAX_GAP", "600"))
