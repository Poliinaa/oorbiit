import asyncio
import logging
from datetime import datetime, date, timedelta
from database import set_username

from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram import Bot, Dispatcher, executor

from config import TELEGRAM_TOKEN

MAIN_ADMIN_ID = 420273925  # главный админ

from handlers import register_all_handlers, setup_bot_commands
from handlers.admin_panel import send_daily_report_for_date, MAIN_ADMIN_ID

from database import init_db

ADMIN_TZ_OFFSET_HOURS = 3

bot = Bot(token=TELEGRAM_TOKEN)
storage = MemoryStorage() 
dp = Dispatcher(bot, storage=storage) 

async def ensure_username(message):
    user = message.from_user
    username = user.username

    # Если у пользователя есть username – обновим в БД
    if username:
        set_username(user.id, username)
        
async def daily_report_scheduler(bot: Bot):
    """
    Каждые сутки в 11:00 по МСК отправляет главному админу отчёт
    за предыдущий "админский день" (11:00–11:00).
    """
    while True:
        # Текущее время в UTC и в МСК
        now_utc = datetime.utcnow()
        now_msk = now_utc + timedelta(hours=ADMIN_TZ_OFFSET_HOURS)

        # Считаем ближайшее 11:00 по МСК
        target_msk = now_msk.replace(hour=11, minute=0, second=0, microsecond=0)
        if now_msk >= target_msk:
            # 11:00 уже прошло сегодня → берём завтра
            target_msk = (now_msk + timedelta(days=1)).replace(
                hour=11, minute=0, second=0, microsecond=0
            )

        # Сколько ждать до ближайшего 11:00 по МСК
        delay_seconds = (target_msk - now_msk).total_seconds()
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)

        # В момент наступления 11:00 по МСК считаем, что только что
        # закончился "вчерашний" админский день: от (day 11:00) до (сейчас 11:00).
        report_day_msk = (target_msk - timedelta(days=1)).date()

        try:
            await send_daily_report_for_date(bot, MAIN_ADMIN_ID, report_day_msk)
        except Exception as e:
            logging.exception("Ошибка при отправке ежедневного отчёта: %s", e)


async def on_startup(dispatcher: Dispatcher):
    await setup_bot_commands(bot)
    asyncio.create_task(daily_report_scheduler(bot))


def main():
    init_db()   # важно вызывать до старта polling
    register_all_handlers(dp)
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)


if __name__ == "__main__":
    main()







