import asyncio

from aiogram import Dispatcher, types

from session_store import get_session
from services.generation import generate_and_send
from services.cooldown import ensure_cooldown_and_mark


def register_text_handlers(dp: Dispatcher) -> None:

    @dp.message_handler(content_types=["text"])
    async def handle_text_or_prompt(message: types.Message):
        chat_id = message.chat.id
        text = (message.text or "").strip()

        # Команды обрабатываются в handlers/commands.py
        if not text or text.startswith("/"):
            return

        sess = get_session(chat_id)

        # Проверяем cooldown
        if not await ensure_cooldown_and_mark(message.bot, chat_id, sess):
            return

        # Берём snapshot всех загруженных фото (режим Remix)
        photos = list(sess.get("photos", []))

        # Удаляем статусные сообщения о загруженных изображениях
        status_ids = sess.get("photo_status_message_ids", [])
        for mid in status_ids:
            try:
                await message.bot.delete_message(chat_id, mid)
            except Exception:
                pass
        sess["photo_status_message_ids"] = []

        # Очищаем staging для следующего набора
        sess["photos"] = []
        sess["photo_message_ids"] = []
        sess["prompt"] = ""

        # Запускаем генерацию в отдельной задаче
        asyncio.create_task(
            generate_and_send(message.bot, chat_id, text, photos)
        )
