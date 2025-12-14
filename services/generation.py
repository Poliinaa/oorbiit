import io
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Sequence, Dict

from aiogram import Bot, types

from session_store import get_session
from gemini_client import call_gemini_flash, call_gemini_pro
from database import (
    can_generate,
    register_generation,
    increment_model_usage,
    log_generation_event,
    get_model_usage_for_period,
    get_admin_period_usage,
)

# Администраторы с полным доступом (не расходуют подписку и extra_balance)
ADMIN_IDS = {
    420273925,  # ITS ME
    801938649,  # OKS
    1429506195,  # NATASHA
    639960483,  # KRIS
    1169321143,  # ALLA
    744363768,  # KSU
}

# Сдвиг часового пояса для расчёта админских периодов (МСК = UTC+3)
ADMIN_TZ_OFFSET_HOURS = 3
ADMIN_RESET_HOUR = 11  # 11:00 МСК

# Лимиты для администраторов по моделям на админский день (24 часа от 11:00 до 11:00)
ADMIN_PERIOD_LIMITS = {
    "flash": 330,  # Gemini 2.5 Flash
    "pro": 41,     # Gemini 3 Pro
}


def _now_admin_time() -> datetime:
    """
    Текущее время с учётом сдвига ADMIN_TZ_OFFSET_HOURS.
    Все админские лимиты считаются относительно этого времени.
    """
    return datetime.utcnow() + timedelta(hours=ADMIN_TZ_OFFSET_HOURS)


def _current_admin_period_start() -> datetime:
    """
    Начало текущего дневного периода для админа.

    Логика:
    - считаем админский день по МСК с ADMIN_RESET_HOUR (11:00) до 11:00 следующего дня;
    - расчёт границы ведём во времени МСК;
    - в БД ходим в UTC, поэтому возвращаем начало периода в UTC.
    """
    now_utc = datetime.utcnow()
    now_msk = now_utc + timedelta(hours=ADMIN_TZ_OFFSET_HOURS)

    if now_msk.hour < ADMIN_RESET_HOUR:
        # До 11:00 по МСК — ещё идёт вчерашний админский день
        ref_msk = now_msk - timedelta(days=1)
    else:
        # После/в 11:00 — уже сегодняшний админский день
        ref_msk = now_msk

    start_msk = ref_msk.replace(
        hour=ADMIN_RESET_HOUR,
        minute=0,
        second=0,
        microsecond=0,
    )

    start_utc = start_msk - timedelta(hours=ADMIN_TZ_OFFSET_HOURS)
    return start_utc


def _period_label_from_start(period_start: datetime) -> str:
    """
    Человекочитаемый текст периода для профиля/сообщений.
    Сейчас период всегда 24 часа от ADMIN_RESET_HOUR до ADMIN_RESET_HOUR следующего дня.
    """
    period_end = period_start + timedelta(days=1)
    start_str = period_start.strftime("%H:%M")
    end_str = period_end.strftime("%H:%M")
    return f"{start_str}–{end_str} (МСК)"


def _check_admin_limit_db(user_id: int, model: str) -> Dict[str, int]:
    """
    Проверяет лимит администратора по БД.
    Основано на таблице generation_log, никакого in-memory состояния.

    Возвращает словарь:
      {
        "can": 0/1,
        "used": N,
        "limit": L,
        "remaining": R,
        "period_label": "...",
      }
    """
    period_start = _current_admin_period_start()
    period_end = period_start + timedelta(hours=24)
    label = _period_label_from_start(period_start)

    if model not in ADMIN_PERIOD_LIMITS:
        # Для других моделей лимит не считаем
        return {
            "can": 1,
            "used": 0,
            "limit": 10**9,
            "remaining": 10**9,
            "period_label": label,
        }

    used = get_model_usage_for_period(user_id, model, period_start, period_end)
    limit = ADMIN_PERIOD_LIMITS[model]
    remaining = max(limit - used, 0)

    return {
        "can": 1 if used < limit else 0,
        "used": used,
        "limit": limit,
        "remaining": remaining,
        "period_label": label,
    }


def get_admin_period_info(user_id: int) -> Dict[str, Dict]:
    """
    Информация по админским лимитам на текущий период (для профиля).
    Всё считается по БД.
    """
    period_start = _current_admin_period_start()
    period_end = period_start + timedelta(hours=24)
    label = _period_label_from_start(period_start)

    usage = get_admin_period_usage(user_id, period_start, period_end)
    flash_used = usage.get("flash", 0)
    pro_used = usage.get("pro", 0)

    flash_limit = ADMIN_PERIOD_LIMITS["flash"]
    pro_limit = ADMIN_PERIOD_LIMITS["pro"]

    return {
        "period_label": label,
        "flash": {
            "limit": flash_limit,
            "used": flash_used,
            "remaining": max(flash_limit - flash_used, 0),
        },
        "pro": {
            "limit": pro_limit,
            "used": pro_used,
            "remaining": max(pro_limit - pro_used, 0),
        },
    }


def get_all_admin_period_info() -> Dict[int, Dict]:
    """
    Срез по всем админам на текущий период.
    Возвращает словарь: { user_id: get_admin_period_info(...) }
    """
    info: Dict[int, Dict] = {}
    for uid in ADMIN_IDS:
        info[uid] = get_admin_period_info(uid)
    return info


async def generate_and_send(
    bot: Bot,
    chat_id: int,
    prompt: Optional[str],
    photos: Sequence[bytes],
) -> None:
    """
    Центральная точка генерации:
    - читает настройки модели из сессии,
    - читает количество изображений за запрос из сессии,
    - проверяет лимиты (админские по БД, обычные — по users / ORB),
    - вызывает Gemini N раз,
    - отправляет результат (N превью + N оригиналов),
    - фиксирует списание и статистику по моделям и журналу.
    """
    sess = get_session(chat_id)

    prompt = (prompt or "").strip()
    photos = [p for p in (photos or []) if p]

    if not prompt and not photos:
        await bot.send_message(
            chat_id,
            "⚠️ Не задан ни текстовый запрос, ни изображения.\n"
            "Отправьте текстовый промт и/или загрузите одно или несколько фото.",
        )
        return

    model = sess.get("model", "flash")
    if model not in ("flash", "pro"):
        model = "flash"

    aspect_ratio = sess.get("aspect_ratio", "1:1")
    resolution = sess.get("resolution", "1K")

    # Количество изображений за один промт (1–4)
    images_per_prompt = int(sess.get("images_per_prompt", 1) or 1)
    if images_per_prompt < 1:
        images_per_prompt = 1
    if images_per_prompt > 4:
        images_per_prompt = 4

    # Стоимость в ORB за одно изображение:
    #   - flash → 1 ORB
    #   - pro   → 3 ORB
    cost_units = 1 if model == "flash" else 3
    total_cost_units = cost_units * images_per_prompt

    is_admin = chat_id in ADMIN_IDS
    source: Optional[str] = None

    # ===== Лимиты для администраторов по моделям (день, через БД) =====
    if is_admin:
        limit_info = _check_admin_limit_db(chat_id, model)
        remaining = limit_info.get("remaining", 0)

        if remaining < images_per_prompt:
            # Не хватает лимита даже на запрошенное количество изображений
            if model == "pro":
                text = (
                    "🚫 Достигнут лимит генераций для Gemini 3 Pro "
                    f"в текущем периоде {limit_info['period_label']}.\n"
                    f"Лимит: {limit_info['limit']} генераций, "
                    f"осталось: {remaining}."
                )
            else:
                text = (
                    "🚫 Достигнут лимит генераций для Gemini 2.5 Flash "
                    f"в текущем периоде {limit_info['period_label']}.\n"
                    f"Лимит: {limit_info['limit']} генераций, "
                    f"осталось: {remaining}."
                )
            await bot.send_message(chat_id, text)
            return
    else:
        try:
            allowed, source, reason, _ = can_generate(chat_id, cost=total_cost_units)
        except Exception as e:
            logging.exception("Ошибка при проверке лимитов can_generate: %s", e)
            await bot.send_message(
                chat_id,
                "❗ Не удалось проверить ORB-баланс.\n"
                "Попробуйте позже или напишите в поддержку.",
            )
            return

        if not allowed:
            await bot.send_message(
                chat_id,
                reason or "Генерация сейчас недоступна (ORB-баланс).",
            )
            return

    # Статус «генерация началась»
    if images_per_prompt == 1:
        status_text = "🌀 Генерация изображения запущена..."
    else:
        status_text = f"🌀 Генерация {images_per_prompt} изображений запущена..."
    status_msg = await bot.send_message(chat_id, status_text)

    try:
        success_count = 0

        # Генерируем N изображений по одному и тому же промту
        for idx in range(images_per_prompt):
            if model == "flash":
                result_bytes = await asyncio.to_thread(
                    call_gemini_flash,
                    photos,
                    prompt,
                    aspect_ratio,
                )
            elif model == "pro":
                result_bytes = await asyncio.to_thread(
                    call_gemini_pro,
                    photos,
                    prompt,
                    aspect_ratio,
                    resolution,
                )
            else:
                result_bytes = await asyncio.to_thread(
                    call_gemini_flash,
                    photos,
                    prompt,
                    aspect_ratio,
                )

            if not result_bytes:
                # Если одна из генераций не вернула изображение — просто пропускаем
                continue

            success_count += 1

            # Статистика по моделям (для всех, включая админов)
            try:
                increment_model_usage(chat_id, model)
            except Exception as e:
                logging.warning("Не удалось обновить статистику по моделям: %s", e)

            # Журнал генераций (для лимитов, отчётов и т.п.)
            try:
                log_generation_event(chat_id, model)
            except Exception as e:
                logging.warning("Не удалось записать событие генерации в журнал: %s", e)

            # Отправляем результат: превью + файл в исходном качестве
            img_buf_photo = io.BytesIO(result_bytes)
            img_buf_photo.seek(0)
            img_buf_doc = io.BytesIO(result_bytes)
            img_buf_doc.seek(0)

            await bot.send_photo(
                chat_id,
                photo=img_buf_photo,
                caption=f"✅ Сгенерировано в @Orbit_AIBot ({success_count}/{images_per_prompt})",
            )

            try:
                await bot.send_document(
                    chat_id,
                    document=types.InputFile(
                        img_buf_doc,
                        filename=f"orbit_result_{success_count}.png",
                    ),
                    caption="Файл в исходном качестве",
                )
            except Exception as e:
                logging.warning("Не удалось отправить документ с исходным файлом: %s", e)

        if success_count == 0:
            await bot.send_message(
                chat_id,
                "⚠️ Gemini не вернул изображения.\n"
                "Попробуйте повторить запрос или немного изменить промт.",
            )
            return

        # Списываем ORB (если не админ и есть источник), только за успешно полученные изображения
        if not is_admin and source and success_count > 0:
            try:
                register_generation(chat_id, source, amount=success_count * cost_units)
            except Exception as e:
                logging.warning("Не удалось списать ORB из базы: %s", e)

    except Exception as e:
        logging.exception("Generation error: %s", e)
        msg = str(e)
        msg_lower = msg.lower()

        if "no_image" in msg_lower:
            text = (
                "⚠️ Gemini не смог вернуть изображение по этому запросу.\n"
                "Попробуйте немного изменить промт или упростить описание."
            )
        elif "503" in msg or "overloaded" in msg_lower or "unavailable" in msg_lower:
            text = (
                "⚠️ Сервис Gemini временно перегружен.\n"
                "Ваш промт и фото в порядке — попробуйте повторить запрос чуть позже."
            )
        elif "timeout" in msg_lower or "timed out" in msg_lower:
            text = (
                "⏱ Сервис Gemini слишком долго не отвечал.\n"
                "Попробуйте ещё раз через 10–20 секунд или упростите запрос."
            )
        elif (
            "blocked by safety filters" in msg_lower
            or "blockreason" in msg_lower
            or "safety" in msg_lower
        ):
            text = (
                "🚫 Запрос был заблокирован системой безопасности Gemini.\n"
                "Попробуйте переформулировать запрос более нейтрально."
            )
        elif "gemini http 500" in msg_lower or '"code": 500' in msg_lower:
            text = (
                "⚠️ На стороне сервиса Gemini внутренняя ошибка (500).\n"
                "Ваш промт и фото в порядке — повторите попытку позже."
            )
        elif "ошибка при обращении к gemini" in msg_lower:
            text = (
                "⚠️ Не удалось связаться с сервисом Gemini.\n"
                "Проверьте подключение к интернету и попробуйте ещё раз."
            )
        else:
            text = (
                "❗ Не удалось сгенерировать изображение.\n"
                f"Техническая информация: {msg}"
            )

        await bot.send_message(chat_id, text)

    finally:
        try:
            await bot.delete_message(chat_id, status_msg.message_id)
        except Exception as e:
            logging.warning("Не удалось удалить статусное сообщение: %s", e)
