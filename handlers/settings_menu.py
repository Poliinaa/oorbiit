# handlers/settings_menu.py

from aiogram import types, Dispatcher
from aiogram.utils.exceptions import MessageNotModified

from session_store import (
    get_session,
    set_model,
    set_aspect_ratio,
    set_resolution,
    set_images_per_prompt,
    ALLOWED_ASPECT_RATIOS,
    ALLOWED_RESOLUTIONS,
)
from services.generation import ADMIN_IDS  # если нужно дальше по логике, можно оставить


def register_settings_handlers(dp: Dispatcher) -> None:

    @dp.callback_query_handler(lambda c: c.data == "menu_settings")
    async def cb_menu_settings(callback: types.CallbackQuery):
        chat_id = callback.message.chat.id
        sess = get_session(chat_id)

        current_ratio = sess["aspect_ratio"]
        current_model = sess["model"]
        current_res = sess.get("resolution", "1K")
        current_count = int(sess.get("images_per_prompt", 1) or 1)

        keyboard = types.InlineKeyboardMarkup(row_width=2)

        flash_text = "Gemini 2.5 Flash"
        pro_text = "Gemini 3 Pro Image Preview"

        if current_model == "flash":
            flash_text = "✅ " + flash_text
        else:
            pro_text = "✅ " + pro_text

        keyboard.add(
            types.InlineKeyboardButton(flash_text, callback_data="set_model_flash"),
            types.InlineKeyboardButton(pro_text, callback_data="set_model_pro"),
        )

        # Популярные соотношения сторон
        popular_ratios = ["1:1", "3:2", "2:3", "4:5", "5:4", "9:16", "16:9"]
        buttons = []
        for r in popular_ratios:
            if r not in ALLOWED_ASPECT_RATIOS:
                continue
            text = f"✅ {r}" if r == current_ratio else r
            buttons.append(
                types.InlineKeyboardButton(text, callback_data=f"set_ratio_{r}")
            )
        if buttons:
            keyboard.add(*buttons)

        # Количество изображений за один запрос (1–4)
        count_buttons = []
        for n in (1, 2, 3, 4):
            label = f"{n} фото"
            if n == current_count:
                label = f"✅ {label}"
            count_buttons.append(
                types.InlineKeyboardButton(label, callback_data=f"set_count_{n}")
            )
        keyboard.add(*count_buttons)

        # Разрешения для Pro
        if current_model == "pro":
            res_buttons = []
            for res in ["1K", "2K"]:
                if res not in ALLOWED_RESOLUTIONS:
                    continue
                label = f"✅ {res}" if res == current_res else res
                res_buttons.append(
                    types.InlineKeyboardButton(label, callback_data=f"set_res_{res}")
                )
            if res_buttons:
                keyboard.add(*res_buttons)

            settings_text = (
                "<b>Настройки:</b>\n\n"
                "1) Выберите модель (Flash / Pro).\n"
                "2) Выберите соотношение сторон.\n"
                "3) Для Gemini Pro выберите качество (1K / 2K).\n"
                "4) Выберите, сколько изображений генерировать за один запрос (1–4).\n\n"
                "<b>Расход ORB:</b>\n"
                "• Gemini 2.5 Flash — 1 ORB за изображение\n"
                "• Gemini 3 Pro — 3 ORB за изображение"
            )
        else:
            settings_text = (
                "<b>Настройки:</b>\n\n"
                "1) Выберите модель (Flash / Pro).\n"
                "2) Выберите соотношение сторон.\n"
                "3) Качество для Gemini 2.5 Flash фиксировано.\n"
                "4) Выберите, сколько изображений генерировать за один запрос (1–4).\n\n"
                "<b>Расход ORB:</b>\n"
                "• Gemini 2.5 Flash — 1 ORB за изображение\n"
                "• Gemini 3 Pro — 3 ORB за изображение"
            )

        keyboard.add(
            types.InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu_back")
        )

        try:
            await callback.message.edit_text(
                settings_text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except MessageNotModified:
            pass

        await callback.answer()

    @dp.callback_query_handler(lambda c: c.data in ("set_model_flash", "set_model_pro"))
    async def cb_set_model(callback: types.CallbackQuery):
        chat_id = callback.message.chat.id

        if callback.data == "set_model_flash":
            set_model(chat_id, "flash")
            await cb_menu_settings(callback)
            return

        # В новой модели ORB Gemini 3 Pro доступна всем,
        # ограничение только по ORB-балансу при генерации.
        set_model(chat_id, "pro")
        await cb_menu_settings(callback)

    @dp.callback_query_handler(lambda c: c.data.startswith("set_ratio_"))
    async def cb_set_ratio(callback: types.CallbackQuery):
        chat_id = callback.message.chat.id
        ratio = callback.data.replace("set_ratio_", "", 1)

        if ratio not in ALLOWED_ASPECT_RATIOS:
            await callback.answer("Это соотношение не поддерживается.", show_alert=True)
            return

        set_aspect_ratio(chat_id, ratio)
        await cb_menu_settings(callback)

    @dp.callback_query_handler(lambda c: c.data.startswith("set_res_"))
    async def cb_set_resolution(callback: types.CallbackQuery):
        chat_id = callback.message.chat.id
        res = callback.data.replace("set_res_", "", 1)

        if res not in ALLOWED_RESOLUTIONS:
            await callback.answer("Это качество не поддерживается.", show_alert=True)
            return

        set_resolution(chat_id, res)
        await cb_menu_settings(callback)

    @dp.callback_query_handler(lambda c: c.data.startswith("set_count_"))
    async def cb_set_images_count(callback: types.CallbackQuery):
        chat_id = callback.message.chat.id
        data = callback.data.replace("set_count_", "", 1)
        try:
            value = int(data)
        except ValueError:
            await callback.answer("Некорректное значение.", show_alert=True)
            return

        if value < 1 or value > 4:
            await callback.answer("Можно выбрать от 1 до 4 изображений.", show_alert=True)
            return

        set_images_per_prompt(chat_id, value)
        await cb_menu_settings(callback)
