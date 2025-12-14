# handlers/admin_panel.py

import io
import csv
from datetime import date, datetime, timedelta
from typing import Dict, Any

from aiogram import types, Dispatcher, Bot
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup

from services.generation import ADMIN_IDS, get_admin_period_info, get_all_admin_period_info
from database import (
    get_user,
    get_model_usage,
    add_extra_generations,
    get_username,
    get_daily_generation_log,
)

MAIN_ADMIN_ID = 420273925


# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------


def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def _build_main_menu_keyboard(chat_id: int) -> types.InlineKeyboardMarkup:
    """
    Главное меню, чтобы можно было вернуться из админки.
    Должно совпадать с basic.py.
    """
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("👤 Мой профиль", callback_data="menu_profile"))
    kb.add(types.InlineKeyboardButton("⚙️ Настройки", callback_data="menu_settings"))
    kb.add(types.InlineKeyboardButton("💳 Подписка", callback_data="menu_subscribe"))
    kb.add(types.InlineKeyboardButton("💬 Поддержка", url="https://t.me/poliifly"))

    if chat_id in ADMIN_IDS:
        kb.add(
            types.InlineKeyboardButton(
                "🛠 Админ-панель", callback_data="menu_admin"
            )
        )
    return kb


def _build_admin_keyboard() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton(
            "📊 Статус пользователя", callback_data="admin_user_status"
        )
    )
    kb.add(
        types.InlineKeyboardButton(
            "➕ Начислить ORB", callback_data="admin_add_generations"
        )
    )
    kb.add(
        types.InlineKeyboardButton(
            "📈 Лимиты админов", callback_data="admin_admin_limits"
        )
    )
    kb.add(
        types.InlineKeyboardButton(
            "📊 Ежедневный отчёт (за вчера)", callback_data="admin_daily_report"
        )
    )
    kb.add(
        types.InlineKeyboardButton(
            "⬅️ Назад в меню", callback_data="admin_close"
        )
    )
    return kb


def _build_back_to_admin_keyboard() -> types.InlineKeyboardMarkup:
    """
    Клавиатура для состояний "введите ID / количество" с кнопкой Назад.
    """
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton(
            "⬅️ Назад", callback_data="admin_back_root"
        )
    )
    return kb


# ---------- ЕЖЕДНЕВНЫЙ ОТЧЁТ В CSV ----------


async def send_daily_report_for_date(bot: Bot, admin_id: int, day: date) -> None:
    """
    Формирует CSV-отчёт по генерациям за указанный день и отправляет админу.
    """
    rows = get_daily_generation_log(day)  # [(user_id, model_code, created_at), ...]
    if not rows:
        await bot.send_message(
            admin_id,
            f"Отчёт за {day.strftime('%d.%m.%Y')}: генераций не было.",
        )
        return

    # user_id -> {"flash": n, "pro": m}
    stats: Dict[int, Dict[str, int]] = {}
    for user_id, model_code, created_at in rows:
        user_stats = stats.setdefault(user_id, {"flash": 0, "pro": 0})
        if model_code in user_stats:
            user_stats[model_code] += 1

    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')
    writer.writerow(["user_id", "username", "flash", "pro", "total"])

    for user_id, usage in stats.items():
        username = get_username(user_id) or ""
        flash_cnt = usage.get("flash", 0)
        pro_cnt = usage.get("pro", 0)
        total = flash_cnt + pro_cnt
        writer.writerow([str(user_id), username, flash_cnt, pro_cnt, total])

    data = output.getvalue().encode("utf-8-sig")
    buf = io.BytesIO(data)
    buf.name = f"orbit_report_{day.strftime('%Y-%m-%d')}.csv"
    buf.seek(0)

    caption = f"Ежедневный отчёт за {day.strftime('%d.%m.%Y')}"

    await bot.send_document(
        admin_id,
        document=buf,
        caption=caption,
    )


# ---------- FSM ДЛЯ АДМИН-ПАНЕЛИ ----------


class AdminStates(StatesGroup):
    WAIT_USER_ID_STATUS = State()
    WAIT_USER_ID_GENERATIONS = State()
    WAIT_GENERATIONS_AMOUNT = State()


# ---------- РЕГИСТРАЦИЯ ХЭНДЛЕРОВ ----------


def register_admin_panel_handlers(dp: Dispatcher) -> None:
    """
    Регистрирует все хэндлеры админ-панели.
    ИМЕННО это имя импортируется в handlers/__init__.py.
    """

    # Открытие админ-панели из главного меню
    @dp.callback_query_handler(lambda c: c.data == "menu_admin", state="*")
    async def open_admin_panel(callback: types.CallbackQuery, state: FSMContext):
        if not _is_admin(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return

        await state.finish()
        kb = _build_admin_keyboard()
        try:
            await callback.message.edit_text("🛠 Админ-панель Orbit", reply_markup=kb)
        except Exception:
            await callback.message.answer("🛠 Админ-панель Orbit", reply_markup=kb)
        await callback.answer()

    # Назад в админ-панель из состояний (кнопка "⬅️ Назад")
    @dp.callback_query_handler(lambda c: c.data == "admin_back_root", state="*")
    async def admin_back_root(callback: types.CallbackQuery, state: FSMContext):
        if not _is_admin(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return

        await state.finish()
        kb = _build_admin_keyboard()
        try:
            await callback.message.edit_text("🛠 Админ-панель Orbit", reply_markup=kb)
        except Exception:
            await callback.message.answer("🛠 Админ-панель Orbit", reply_markup=kb)
        await callback.answer()

    # Кнопка "⬅️ Назад в меню" из админ-панели
    @dp.callback_query_handler(lambda c: c.data == "admin_close", state="*")
    async def admin_close(callback: types.CallbackQuery, state: FSMContext):
        if not _is_admin(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return

        await state.finish()
        kb = _build_main_menu_keyboard(callback.message.chat.id)
        try:
            await callback.message.edit_text("Выберите раздел:", reply_markup=kb)
        except Exception:
            await callback.message.answer("Выберите раздел:", reply_markup=kb)
        await callback.answer()

    # ---------- Статус пользователя ----------

    @dp.callback_query_handler(lambda c: c.data == "admin_user_status", state="*")
    async def admin_user_status_start(callback: types.CallbackQuery, state: FSMContext):
        if not _is_admin(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return

        await AdminStates.WAIT_USER_ID_STATUS.set()
        kb = _build_back_to_admin_keyboard()
        await callback.message.edit_text(
            "Введите ID пользователя, чей статус нужно показать:",
            reply_markup=kb,
        )
        await callback.answer()

    @dp.message_handler(state=AdminStates.WAIT_USER_ID_STATUS)
    async def admin_user_status_process(message: types.Message, state: FSMContext):
        if not _is_admin(message.from_user.id):
            await state.finish()
            return

        text = (message.text or "").strip()
        if not text.isdigit():
            kb = _build_back_to_admin_keyboard()
            await message.answer(
                "ID должен быть числом. Введите корректный ID пользователя:",
                reply_markup=kb,
            )
            return

        target_id = int(text)
        user_row = get_user(target_id)
        if not user_row:
            kb = _build_back_to_admin_keyboard()
            await message.answer(
                f"Пользователь с ID {target_id} не найден.",
                reply_markup=kb,
            )
            return

        (
            _uid,
            plan,
            expires_at,
            daily_limit,
            used_today,
            extra_balance,
            last_reset,
        ) = user_row

        username = get_username(target_id) or "—"
        model_usage = get_model_usage(target_id)
        flash_used = model_usage.get("flash", 0)
        pro_used = model_usage.get("pro", 0)

        plan_str = plan or "free"
        exp_str = expires_at.strftime("%d.%m.%Y") if expires_at else "нет"
        last_reset_str = last_reset.strftime("%d.%m.%Y") if last_reset else "нет"

        text_lines = [
            f"👤 Статус пользователя <code>{target_id}</code>",
            f"Username: <b>{username}</b>",
            "",
            f"Тариф: <b>{plan_str}</b>",
            f"Подписка до: <b>{exp_str}</b>",
            "",
            f"Дневной лимит: <b>{daily_limit}</b>",
            f"Использовано сегодня: <b>{used_today}</b>",
            f"Баланс ORB: <b>{extra_balance}</b>",
            f"Последний сброс лимита: <b>{last_reset_str}</b>",
            "",
            f"Использование моделей (всего):",
            f"• Gemini 2.5 Flash: <b>{flash_used}</b>",
            f"• Gemini 3 Pro: <b>{pro_used}</b>",
        ]

        await state.finish()
        kb_admin = _build_admin_keyboard()
        await message.answer(
            "\n".join(text_lines),
            parse_mode="HTML",
            reply_markup=kb_admin,
        )

    # ---------- Выдать дополнительные генерации ----------

    @dp.callback_query_handler(lambda c: c.data == "admin_add_generations", state="*")
    async def admin_add_generations_start(callback: types.CallbackQuery, state: FSMContext):
        if not _is_admin(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return

        await AdminStates.WAIT_USER_ID_GENERATIONS.set()
        kb = _build_back_to_admin_keyboard()
        await callback.message.edit_text(
            "Введите ID пользователя, которому начислить ORB:",
            reply_markup=kb,
        )
        await callback.answer()

    @dp.message_handler(state=AdminStates.WAIT_USER_ID_GENERATIONS)
    async def admin_add_generations_user(message: types.Message, state: FSMContext):
        if not _is_admin(message.from_user.id):
            await state.finish()
            return

        text = (message.text or "").strip()
        if not text.isdigit():
            kb = _build_back_to_admin_keyboard()
            await message.answer(
                "ID должен быть числом. Введите корректный ID пользователя:",
                reply_markup=kb,
            )
            return

        target_id = int(text)
        user_row = get_user(target_id)
        if not user_row:
            kb = _build_back_to_admin_keyboard()
            await message.answer(
                f"Пользователь с ID {target_id} не найден.",
                reply_markup=kb,
            )
            return

        await state.update_data(target_user_id=target_id)
        await AdminStates.WAIT_GENERATIONS_AMOUNT.set()
        kb = _build_back_to_admin_keyboard()
        await message.answer(
            f"Пользователь <code>{target_id}</code> найден.\n"
            f"Введите количество ORB для начисления:",
            parse_mode="HTML",
            reply_markup=kb,
        )

    @dp.message_handler(state=AdminStates.WAIT_GENERATIONS_AMOUNT)
    async def admin_add_generations_amount(message: types.Message, state: FSMContext):
        if not _is_admin(message.from_user.id):
            await state.finish()
            return

        text = (message.text or "").strip()
        if not text.isdigit():
            kb = _build_back_to_admin_keyboard()
            await message.answer(
                "Количество должно быть положительным числом. Введите ещё раз:",
                reply_markup=kb,
            )
            return

        amount = int(text)
        if amount <= 0:
            kb = _build_back_to_admin_keyboard()
            await message.answer(
                "Количество должно быть больше нуля. Введите ещё раз:",
                reply_markup=kb,
            )
            return

        data: Dict[str, Any] = await state.get_data()
        target_id = data.get("target_user_id")
        if not target_id:
            await state.finish()
            kb_admin = _build_admin_keyboard()
            await message.answer(
                "Внутренняя ошибка: не запомнен ID пользователя. Начните заново.",
                reply_markup=kb_admin,
            )
            return

        add_extra_generations(target_id, amount)
        username = get_username(target_id) or "—"

        await state.finish()
        kb_admin = _build_admin_keyboard()
        await message.answer(
            f"✅ Пользователю <code>{target_id}</code> ({username}) "
            f"начислено <b>{amount}</b> ORB",
            parse_mode="HTML",
            reply_markup=kb_admin,
        )

    # ---------- Лимиты админов ----------

    @dp.callback_query_handler(lambda c: c.data == "admin_admin_limits", state="*")
    async def admin_limits(callback: types.CallbackQuery, state: FSMContext):
        if not _is_admin(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return

        info_all = get_all_admin_period_info()
        lines = []

        for uid, info in info_all.items():
            username = get_username(uid) or "—"
            flash = info.get("flash", {})
            pro = info.get("pro", {})
            lines.append(
                f"👤 <code>{uid}</code> ({username})\n"
                f"• Flash: {flash.get('used', 0)}/{flash.get('limit', 0)} "
                f"(осталось {flash.get('remaining', 0)})\n"
                f"• Pro: {pro.get('used', 0)}/{pro.get('limit', 0)} "
                f"(осталось {pro.get('remaining', 0)})"
            )

        kb = _build_admin_keyboard()
        text = "📈 Лимиты администраторов:\n\n" + "\n\n".join(lines) if lines else "Нет данных по лимитам."
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await callback.answer()

    # ---------- Ежедневный отчёт (за вчера) ----------

    @dp.callback_query_handler(lambda c: c.data == "admin_daily_report", state="*")
    async def admin_daily_report(callback: types.CallbackQuery, state: FSMContext):
        if not _is_admin(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return

        day = date.today() - timedelta(days=1)
        await callback.answer("Формирую отчёт...", show_alert=False)
        await send_daily_report_for_date(callback.message.bot, callback.from_user.id, day)



