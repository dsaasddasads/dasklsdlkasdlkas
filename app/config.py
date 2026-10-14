import os


def _ids(v: str) -> set[int]:
    return {int(x) for x in v.replace(" ", "").split(",") if x}


BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]
DATABASE_URL = os.environ["DATABASE_URL"]
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")
ADMIN_IDS = _ids(os.getenv("ADMIN_IDS", ""))
PORT = int(os.getenv("PORT", "8080"))

COLLECTIONS = [c.strip() for c in os.getenv("COLLECTIONS", "").split(",") if c.strip()]
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "3"))
CONCURRENCY = int(os.getenv("CONCURRENCY", "8"))

# PAID_MODE=1 включает платный тариф; по умолчанию бот приватный (доступ по списку)
PAID_MODE = os.getenv("PAID_MODE", "0") == "1"
# дни -> цена в Telegram Stars (один тариф)
PLANS = {7: int(os.getenv("PRICE_7D", "2000"))}
