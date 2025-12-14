# handlers/basic.py

from aiogram import types, Dispatcher
from aiogram.types import WebAppInfo

from database import set_username
from services.generation import ADMIN_IDS
from session_store import get_session, reset_session


def _get_model_name(code: str) -> str:
    return {
        "flash": "Gemini 2.5 Flash Image",
        "pro": "Gemini 3 Pro Image Preview",
    }.get(code, "Gemini 2.5 Flash Image")


def _build_main_menu_keyboard(chat_id: int) -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup(row_width=1)

    # 🔹 Кнопка мини-приложения (WebApp)
    keyboard.add(
        types.InlineKeyboardButton(
            "🌐 Открыть мини-апп",
            web_app=WebAppInfo(url="https://orbit-production-4de1.up.railway.app"),
        )
    )

    # Остальные пункты меню
    keyboard.add(
        types.InlineKeyboardButton("👤 Мой профиль", callback_data="menu_profile"),
    )
    keyboard.add(
        types.InlineKeyboardButton("⚙️ Настройки", callback_data="menu_settings"),
    )
    keyboard.add(
        types.InlineKeyboardButton("💳 Подписка", callback_data="menu_subscribe"),
    )
    keyboard.add(
        types.InlineKeyboardButton("💬 Поддержка", url="https://t.me/poliifly"),
    )

    if chat_id in ADMIN_IDS:
        keyboard.add(
            types.InlineKeyboardButton(
                "🛠 Админ-панель", callback_data="menu_admin"
            )
        )

    return keyboard


def register_basic_handlers(dp: Dispatcher) -> None:

    @dp.message_handler(commands=["start"])
    async def cmd_start(message: types.Message):
        chat_id = message.chat.id

        user = message.from_user
        if user.username:
            set_username(user.id, user.username)

        # Обработка реферального параметра /start <ref_id>
        args = message.get_args()
        if args:
            from database import set_referrer  # локальный импорт, чтобы избежать циклов
            try:
                ref_id = int(args)
                if ref_id != chat_id:
                    try:
                        set_referrer(chat_id, ref_id)
                    except Exception:
                        pass
            except ValueError:
                pass

        sess = get_session(chat_id)
        model_name = _get_model_name(sess["model"])

        text = (
            "<b>Добро пожаловать в Orbit AI!</b>\n\n"
            "Этот бот генерирует и стилизует изображения с помощью Gemini.\n"
            "Основное управление — через команду <code>/menu</code>.\n\n"
            "<b>Текущие настройки:</b>\n"
            f"• Модель: <b>{model_name}</b>\n"
            f"• Соотношение сторон: <b>{sess['aspect_ratio']}</b>\n"
            f"• Качество: <b>{sess.get('resolution', '1K')}</b>\n\n"
            "<b>Команды:</b>\n"
            "/menu – главное меню\n"
            "/reset – полный сброс сессии"
        )

        await message.answer(
            text,
            parse_mode="HTML",
            reply_markup=types.ReplyKeyboardRemove(),
        )

    @dp.message_handler(commands=["reset"])
    async def cmd_reset(message: types.Message):
        chat_id = message.chat.id
        reset_session(chat_id)

        text = (
            "<b>Полный сброс.</b>\n"
            "Настройки и фото очищены.\n"
            "Используйте <code>/start</code> или <code>/menu</code>, чтобы начать заново."
        )

        await message.answer(
            text,
            parse_mode="HTML",
            reply_markup=types.ReplyKeyboardRemove(),
        )

    @dp.message_handler(commands=["menu"])
    async def cmd_menu(message: types.Message):
        chat_id = message.chat.id
        keyboard = _build_main_menu_keyboard(chat_id)
        await message.answer("Выберите раздел:", reply_markup=keyboard)

    @dp.callback_query_handler(lambda c: c.data == "menu_back")
    async def cb_menu_back(callback: types.CallbackQuery):
        chat_id = callback.message.chat.id
        keyboard = _build_main_menu_keyboard(chat_id)
        try:
            await callback.message.edit_text("Выберите раздел:", reply_markup=keyboard)
        except Exception:
            await callback.message.answer("Выберите раздел:", reply_markup=keyboard)
        await callback.answer()


async def setup_bot_commands(bot):
    await bot.set_my_commands(
        [
            types.BotCommand("start", "Запустить бота"),
            types.BotCommand("menu", "Главное меню"),
            types.BotCommand("reset", "Полный сброс сессии"),
        ]
    )
