import time
from typing import Dict

from aiogram import Bot


DEFAULT_COOLDOWN_SECONDS = 1  # стандартный интервал между генерациями (секунд)


async def ensure_cooldown_and_mark(
    bot: Bot,
    chat_id: int,
    sess: Dict,
    cooldown: int = DEFAULT_COOLDOWN_SECONDS,
) -> bool:
    """
    Общая проверка cooldown для генераций.

    - Если запрос отправлен слишком рано, отправляет пользователю сообщение
      с оставшимся временем и возвращает False.
    - Если всё в порядке, проставляет sess["last_generate_ts"] и возвращает True.
    """
    now = time.time()
    last_ts = sess.get("last_generate_ts")

    if last_ts is not None and now - last_ts < cooldown:
        remain = int(cooldown - (now - last_ts))
        if remain < 1:
            remain = 1
        await bot.send_message(
            chat_id,
            f"⚠️ Пожалуйста, отправьте новый запрос повторно через {remain} с.",
        )
        return False

    sess["last_generate_ts"] = now
    return True
