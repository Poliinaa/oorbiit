import os
import logging
from dotenv import load_dotenv

# Загружаем переменные из .env
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not TELEGRAM_TOKEN:
    raise SystemExit("TELEGRAM_TOKEN не найден. Проверь .env файл.")
if not GEMINI_API_KEY:
    raise SystemExit("GEMINI_API_KEY не найден. Проверь .env файл.")

# ТЕСТОВЫЙ ПРОВАЙДЕР ЮKassa ДЛЯ TELEGRAM PAYMENTS
PAYMENT_PROVIDER_TOKEN = os.getenv(
    "PAYMENT_PROVIDER_TOKEN",
    "381764678:TEST:153064",  # твой тестовый токен от BotFather
)

# Логирование
logging.basicConfig(level=logging.INFO)
