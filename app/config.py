import os


def _ids(v: str) -> set[int]:
    return {int(x) for x in v.replace(" ", "").split(",") if x}


BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"].strip().strip("\"'").removeprefix("SESSION_STRING=").strip()
DATABASE_URL = os.environ["DATABASE_URL"]
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")
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

# Оффер: уведомления только по подаркам, на которые Telegram разрешает отправить оффер (поле offer_min_stars)
ONLY_OFFERABLE = os.getenv("ONLY_OFFERABLE", "1") == "1"
# Кому можно написать: уведомления только по подаркам, у владельца которых есть username (по умолчанию выкл,
# оффер не требует личных сообщений)
ONLY_REACHABLE = os.getenv("ONLY_REACHABLE", "0") == "1"
# 1 = не отсекать владельцев с настройкой "писать могут только Premium" / платные сообщения
ALLOW_RESTRICTED = os.getenv("ALLOW_RESTRICTED", "0") == "1"
# Текст, который подставляется в чат по кнопке "Написать владельцу". Поля: {collection} {num} {link}
MSG_TEMPLATE = os.getenv("MSG_TEMPLATE", "Привет! Продаёшь {collection} #{num}? {link}")

# Цены с Portals (публичный API, без авторизации). PORTALS=0 - выключить
PORTALS = os.getenv("PORTALS", "1") == "1"
# Если в листинге цена ниже цены Portals на столько % - считаем выгодным (режим выгодных)
PORTALS_DISCOUNT = float(os.getenv("PORTALS_DISCOUNT", "15"))
