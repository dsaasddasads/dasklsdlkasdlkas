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
