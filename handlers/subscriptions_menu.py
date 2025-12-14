from aiogram import types, Dispatcher
from aiogram.utils.exceptions import MessageNotModified


def register_subscription_menu_handlers(dp: Dispatcher) -> None:

    @dp.callback_query_handler(lambda c: c.data == "menu_subscribe")
    async def cb_menu_subscribe(callback: types.CallbackQuery):
        keyboard = types.InlineKeyboardMarkup(row_width=1)

        keyboard.add(
            types.InlineKeyboardButton("MINI — 100 ORB — 590₽", callback_data="pack_mini"),
        )
        keyboard.add(
            types.InlineKeyboardButton("STANDARD — 250 ORB — 1390₽", callback_data="pack_standard"),
        )
        keyboard.add(
            types.InlineKeyboardButton("SUPER — 500 ORB — 2590₽", callback_data="pack_super"),
        )
        keyboard.add(
            types.InlineKeyboardButton("PREMIUM — 1000 ORB — 4490₽", callback_data="pack_premium"),
        )
        keyboard.add(
            types.InlineKeyboardButton("MAX — 2000 ORB — 7990₽", callback_data="pack_max"),
        )

        keyboard.add(
            types.InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu_back"),
        )

        text = (
            "💳 <b>ORB-пакеты</b>\n\n"
            "Купите ORB и используйте их для генерации изображений:\n"
            "• Gemini 2.5 Flash — 1 ORB за изображение\n"
            "• Gemini 3 Pro — 3 ORB за изображение\n\n"
            "Выберите пакет:"
        )

        try:
            await callback.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except MessageNotModified:
            pass

        await callback.answer()
