import io
import time
import asyncio

from aiogram import Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from session_store import get_session, clear_photos
from services.generation import generate_and_send

# Должен совпадать с COOLDOWN_SECONDS в handlers/text.py
COOLDOWN_SECONDS = 1

# Сколько ждём, пока Telegram пришлёт все части альбома
ALBUM_COLLECT_DELAY = 1.0


def register_media_handlers(dp: Dispatcher) -> None:
    """
    Обработка изображений:

    1) Альбомы (media_group):
       - альбом с промтом (подпись в одной из фотографий) → генерация по ВСЕМ фото без Remix-статусов;
       - альбом без промта → каждая фотка добавляется в Remix как отдельное изображение.

    2) Одиночные фото:
       - фото + промт → моментальная генерация по этому фото;
       - фото без промта → добавление в Remix с подсказками и кнопкой «🗑 Удалить».

    3) Callback «🗑 Удалить»:
       - удаляет конкретное фото и его статус,
       - пересчитывает статусы оставшихся фото.
    """

    # ========= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =========

    def _max_photos_for_session(sess: dict) -> int:
        """
        Максимальное количество фото в зависимости от модели:
        - flash (Gemini 2.5) → до 4
        - pro (Gemini 3 Pro) → до 14
        """
        model = sess.get("model", "flash")
        return 14 if model == "pro" else 4

    def _build_delete_keyboard() -> InlineKeyboardMarkup:
        kb = InlineKeyboardMarkup()
        kb.add(
            InlineKeyboardButton(
                "🗑 Удалить",
                callback_data="delete_photo",
            )
        )
        return kb

    def _short_status_text(index: int) -> str:
        # index — 1-based
        return f"✅ {index} изображение добавлено."

    def _full_status_text(count: int, remaining: int) -> str:
        if count == 1:
            return (
                "✅ 1 изображение добавлено.\n"
                f"Вы можете ввести свой запрос и генерация начнётся, "
                f"или загрузить ещё до {remaining} изображений для использования режима Remix 👇"
            )
        else:
            return (
                f"✅ {count} изображение добавлено.\n"
                f"Теперь нейросеть будет использовать {count} изображений в режиме Remix. "
                f"Вы можете ввести свой запрос и генерация начнётся, "
                f"или загрузить ещё до {remaining} изображений 👇"
            )

    async def _ensure_cooldown_and_mark(sess: dict, bot, chat_id: int) -> bool:
        """
        Проверка cooldown. Если ок — ставит last_generate_ts.
        Возвращает True, если можно генерировать, False если нужно подождать.
        """
        now = time.time()
        last_ts = sess.get("last_generate_ts")

        if last_ts is not None and now - last_ts < COOLDOWN_SECONDS:
            remain = int(COOLDOWN_SECONDS - (now - last_ts))
            if remain < 1:
                remain = 1
            await bot.send_message(
                chat_id,
                f"⚠️ Пожалуйста, отправьте новый запрос повторно через {remain} с.",
            )
            return False

        sess["last_generate_ts"] = now
        return True

    async def _update_remix_statuses(bot, chat_id: int, sess: dict) -> None:
        """
        Единый пересчёт всех статусных сообщений для Remix:

        - количество статусных сообщений ВСЕГДА совпадает с количеством фото;
        - все, кроме последнего — короткие;
        - последнее — с длинным текстом и подсказкой.

        Важно: функция защищена асинхронным локом, чтобы не было гонок и
        лавины дублей при одновременном приходе нескольких фото.
        """
        # Пер-чатовый лок храним прямо в сессии
        lock = sess.get("_remix_lock")
        if lock is None:
            lock = asyncio.Lock()
            sess["_remix_lock"] = lock

        async with lock:
            photos = sess.get("photos", [])
            status_ids = sess.get("photo_status_message_ids", [])
            max_photos = _max_photos_for_session(sess)

            count = len(photos)

            # Если фото нет — удаляем все статусы и выходим.
            if count == 0:
                for mid in status_ids:
                    try:
                        await bot.delete_message(chat_id, mid)
                    except Exception:
                        pass
                sess["photo_status_message_ids"] = []
                return

            # 1) Если статусных сообщений больше, чем фото → лишние удаляем
            if len(status_ids) > count:
                extra_ids = status_ids[count:]
                for mid in extra_ids:
                    try:
                        await bot.delete_message(chat_id, mid)
                    except Exception:
                        pass
                status_ids = status_ids[:count]
                sess["photo_status_message_ids"] = status_ids

            # 2) Если статусных меньше, чем фото → создаём недостающие,
            # но только столько, сколько реально нужно
            remaining = max_photos - count
            while len(status_ids) < count:
                msg = await bot.send_message(
                    chat_id,
                    _full_status_text(count, remaining),
                    reply_markup=_build_delete_keyboard(),
                )
                status_ids.append(msg.message_id)
                sess["photo_status_message_ids"] = status_ids

            # 3) Теперь статусные и фото одной длины — переустанавливаем тексты
            remaining = max_photos - count
            for i, mid in enumerate(status_ids):
                try:
                    if i < count - 1:
                        text = _short_status_text(i + 1)
                    else:
                        text = _full_status_text(count, remaining)

                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=mid,
                        text=text,
                        reply_markup=_build_delete_keyboard(),
                    )
                except Exception:
                    # Если сообщение уже удалено/недоступно — просто игнорируем
                    continue

    async def _clear_remix_completely(bot, chat_id: int, sess: dict) -> None:
        """
        Полностью очищает Remix:
        - удаляет все статусные сообщения,
        - чистит фото/статусы/ids через clear_photos().
        """
        status_ids = sess.get("photo_status_message_ids", [])
        for mid in status_ids:
            try:
                await bot.delete_message(chat_id, mid)
            except Exception:
                pass

        clear_photos(chat_id)
        # пересоздаём сессию, чтобы получить чистые поля
        new_sess = get_session(chat_id)
        # переносим лок, если он был
        if "_remix_lock" in sess:
            new_sess["_remix_lock"] = sess["_remix_lock"]

    async def _process_media_group(bot, chat_id: int, media_group_id: str) -> None:
        """
        Обработка целого альбома:
        - ждём, пока доедут все части,
        - берём все фото и промт из группы,
        - проверяем cooldown,
        - запускаем generate_and_send с полным набором фото.
        """
        await asyncio.sleep(ALBUM_COLLECT_DELAY)

        sess = get_session(chat_id)
        media_groups = sess.get("media_groups", {})
        group = media_groups.pop(media_group_id, None)
        sess["media_groups"] = media_groups

        if not group:
            return

        photos = group.get("photos") or []
        prompt = (group.get("prompt") or "").strip()

        if not photos or not prompt:
            return

        max_photos = _max_photos_for_session(sess)
        if len(photos) > max_photos:
            photos = photos[:max_photos]

        # Проверяем cooldown
        if not await _ensure_cooldown_and_mark(sess, bot, chat_id):
            return

        # Полностью чистим Remix (если был), чтобы альбом не пересекался с ручным Remix
        await _clear_remix_completely(bot, chat_id, sess)
        sess = get_session(chat_id)

        asyncio.create_task(
            generate_and_send(
                bot,
                chat_id,
                prompt,
                photos,
            )
        )

    # ========= ОБРАБОТКА ФОТО =========

    @dp.message_handler(content_types=["photo"])
    async def handle_photo(message: types.Message):
        chat_id = message.chat.id
        bot = message.bot
        sess = get_session(chat_id)

        photos = sess["photos"]
        photo_msg_ids = sess["photo_message_ids"]
        media_groups = sess["media_groups"]

        max_photos = _max_photos_for_session(sess)

        # Получаем байты текущего фото
        photo_size = message.photo[-1]
        buf = io.BytesIO()
        try:
            await photo_size.download(destination_file=buf)
        except asyncio.TimeoutError:
            await message.answer(
                "⚠️ Не удалось загрузить изображение из Telegram (таймаут).\n"
                "Пожалуйста, отправьте фотографию ещё раз."
            )
            return
        except Exception:
            await message.answer(
                "⚠️ Произошла ошибка при загрузке изображения из Telegram.\n"
                "Пожалуйста, отправьте фотографию ещё раз."
            )
            return

        image_bytes = buf.getvalue()

        caption_prompt = (message.caption or "").strip()
        media_group_id = message.media_group_id

        # ===== КЕЙС 1: альбом (media_group_id есть) =====
        if media_group_id is not None:
            group = media_groups.get(media_group_id)

            album_has_prompt = (
                (group is not None and group.get("prompt"))
                or bool(caption_prompt)
            )

            if album_has_prompt:
                # Альбом С промтом → собираем группу и запускаем генерацию один раз
                if group is None:
                    group = {
                        "photos": [],
                        "prompt": None,
                        "scheduled": False,
                    }

                group["photos"].append(image_bytes)

                if caption_prompt and not group.get("prompt"):
                    group["prompt"] = caption_prompt

                media_groups[media_group_id] = group
                sess["media_groups"] = media_groups

                if group.get("prompt") and not group.get("scheduled"):
                    group["scheduled"] = True
                    media_groups[media_group_id] = group
                    sess["media_groups"] = media_groups

                    asyncio.create_task(
                        _process_media_group(bot, chat_id, media_group_id)
                    )

                # Для альбомов с промтом НЕ создаём Remix-статусы и не добавляем в sess["photos"]
                return

            # Если альбом БЕЗ промта → падаем дальше в обычную ветку (Remix)

        # ===== КЕЙС 2: одиночное фото + промт (без альбома) =====
        if caption_prompt and media_group_id is None:
            if not await _ensure_cooldown_and_mark(sess, bot, chat_id):
                return

            # Полностью чистим Remix (если был)
            await _clear_remix_completely(bot, chat_id, sess)
            sess = get_session(chat_id)

            asyncio.create_task(
                generate_and_send(
                    bot,
                    chat_id,
                    caption_prompt,
                    [image_bytes],
                )
            )
            return

        # ===== КЕЙС 3: фото без промта (одиночное или часть альбома без промта) → Remix =====
        photos_count = len(photos)
        if photos_count >= max_photos:
            await message.answer(
                f"⚠️ Для выбранной модели уже загружено максимум изображений ({max_photos}).\n"
                "Отправьте текстовый запрос для генерации или удалите лишние изображения перед загрузкой новых."
            )
            return

        # Добавляем фото в staging для Remix
        photos.append(image_bytes)
        photo_msg_ids.append(message.message_id)

        # Пересчитываем/создаём статусы так, чтобы:
        # - 1-е изображение → длинный текст,
        # - при добавлении 2-го и далее → предыдущие короткие, последнее длинное.
        await _update_remix_statuses(bot, chat_id, sess)

    # ========= УДАЛЕНИЕ КОНКРЕТНОГО ФОТО (REMIX) =========

    @dp.callback_query_handler(lambda c: c.data == "delete_photo")
    async def handle_delete_photo(callback_query: types.CallbackQuery):
        chat_id = callback_query.message.chat.id
        bot = callback_query.message.bot
        status_message_id = callback_query.message.message_id

        sess = get_session(chat_id)
        photos = sess["photos"]
        status_ids = sess["photo_status_message_ids"]
        photo_msg_ids = sess["photo_message_ids"]

        # Пытаемся найти индекс статуса.
        try:
            idx = status_ids.index(status_message_id)
        except ValueError:
            # Что-то рассинхронизировалось — аккуратно сбрасываем Remix.
            await callback_query.answer(
                "Состояние изображений сбилось, я очистила список. Загрузите их заново.",
                show_alert=True,
            )
            await _clear_remix_completely(bot, chat_id, sess)
            return

        # Удаляем фото и соответствующие записи
        if 0 <= idx < len(photos):
            photos.pop(idx)

        user_photo_msg_id = None
        if 0 <= idx < len(photo_msg_ids):
            user_photo_msg_id = photo_msg_ids.pop(idx)

        # Удаляем статусное сообщение
        mid = status_ids.pop(idx)
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass

        # Удаляем сообщение пользователя с фото
        if user_photo_msg_id is not None:
            try:
                await bot.delete_message(chat_id, user_photo_msg_id)
            except Exception:
                pass

        await callback_query.answer("Изображение удалено.")

        # Если фото не осталось — полностью очищаем Remix
        if not photos:
            await _clear_remix_completely(bot, chat_id, sess)
            return

        # Обновляем статусы оставшихся фото
        await _update_remix_statuses(bot, chat_id, sess)

