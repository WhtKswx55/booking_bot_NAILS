import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "8561266872:AAEBnYLtMwN9PryjqqT7ymr6rQVioRuPvU8")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split("7240769536,419029365") if x.strip()]
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://booking-bot-i204.onrender.com/webapp/index.html")  # например https://your-app.onrender.com/webapp/
BASE_URL = os.getenv("BASE_URL", "https://booking-bot-i204.onrender.com")      # тот же домен, нужен для self-ping
PORT = int(os.getenv("PORT", "8080"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в переменных окружения")
