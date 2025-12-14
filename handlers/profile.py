from aiogram import types, Dispatcher
from aiogram.utils.exceptions import MessageNotModified

from datetime import datetime

from session_store import get_session
from database import get_user, get_model_usage
from services.generation import ADMIN_IDS, get_admin_period_info


def _get_model_name(code: str) -> str:
    return {
        "flash": "Gemini 2.5 Flash Image",
        "pro": "Gemini 3 Pro Image Preview",
    }.get(code, "Gemini 2.5 Flash Image")


# ====== Склонение слов: фотку / фотки / фоток ======

def plural_ru(n: int, form1: str, form2: str, form5: str) -> str:
    """
    Русское склонение:
    1 фотку
    2–4 фотки
    5+ фоток
    исключения: 11–14 → фоток
    """
    n_abs = abs(n)
    if n_abs % 10 == 1 and n_abs % 100 != 11:
        return form1      # 1 фотку
    if 2 <= n_abs % 10 <= 4 and not (12 <= n_abs % 100 <= 14):
        return form2      # 2–4 фотки
    return form5          # 5+ фоток


# ====== ОБРАБОТЧИК ПРОФИЛЯ ======

def register_profile_handlers(dp: Dispatcher):

    @dp.callback_query_handler(lambda c: c.data == "menu_profile")
    async def cb_menu_profile(callback: types.CallbackQuery):
        chat_id = callback.from_user.id
        tg_user = callback.from_user

        # ===== получение данных пользователя =====
        row = get_user(chat_id)
        if row is None:
            await callback.answer("Пользователь не найден в БД.", show_alert=True)
            return

        (
            user_id,
            plan,
            expires_at,
            daily_limit,
            used_today,
            extra_balance,
            last_reset,
        ) = row

        orb_balance = extra_balance or 0

        # ===== общая статистика за месяц =====
        usage = get_model_usage(chat_id)
        flash_used_total = usage.get("flash", 0)
        pro_used_total = usage.get("pro", 0)

        # ===== месяц =====
        now = datetime.now()
        MONTHS_RU = {
            1: "Январь",
            2: "Февраль",
            3: "Март",
            4: "Апрель",
            5: "Май",
            6: "Июнь",
            7: "Июль",
            8: "Август",
            9: "Сентябрь",
            10: "Октябрь",
            11: "Ноябрь",
            12: "Декабрь",
        }
        month_label = MONTHS_RU[now.month]
        year_label = now.year

        # ===== БЛОК АДМИНА (если админ) =====
        admin_block = ""
        if chat_id in ADMIN_IDS:
            admin_info = get_admin_period_info(chat_id)

            flash_info = admin_info["flash"]
            pro_info = admin_info["pro"]

            flash_used = flash_info["used"]
            flash_limit = flash_info["limit"]
            flash_left = max(flash_limit - flash_used, 0)

            pro_used = pro_info["used"]
            pro_limit = pro_info["limit"]
            pro_left = max(pro_limit - pro_used, 0)

            flash_word = plural_ru(flash_used, "фотку", "фотки", "фоток")
            pro_word = plural_ru(pro_used, "фотку", "фотки", "фоток")

            # красивое имя
            if tg_user.username:
                admin_name = f"@{tg_user.username}"
            elif tg_user.first_name:
                admin_name = tg_user.first_name
            else:
                admin_name = "солнышко"

            admin_block = (
                f"✨ Заюш, {admin_name}!\n\n"
                f"Сегодня ты уже забабахала:\n"
                f"🍌 {flash_used} {flash_word} в Банане — осталось ещё {flash_left}\n"
                f"💎 {pro_used} {pro_word} в Прошке — можешь ещё потратить {pro_left}\n\n"
            )

        # ===== РЕФЕРАЛКА =====
        ref_link = f"https://t.me/Orbit_AIBot?start={tg_user.id}"

        # ===== ФИНАЛЬНЫЙ ТЕКСТ =====
        text = (
            "👤 <b>Профиль</b>\n\n"
            f"ID: <code>{tg_user.id}</code>\n\n"
            f"Баланс ORB: <b>{orb_balance}</b>\n\n"
            f"{admin_block}"
            f"📆 Период: {month_label} {year_label}\n"
            f"📊 Flash генераций за месяц: <b>{flash_used_total}</b>\n"
            f"📊 Pro генераций за месяц: <b>{pro_used_total}</b>\n\n"
            "🔗 Ваша реферальная ссылка:\n"
            f"<code>{ref_link}</code>"
        )

        # ===== КНОПКИ =====
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        keyboard.add(
            types.InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu_back")
        )

        # ===== отправка =====
        try:
            await callback.message.edit_text(
                text, parse_mode="HTML", reply_markup=keyboard
            )
        except MessageNotModified:
            pass

        await callback.answer()
