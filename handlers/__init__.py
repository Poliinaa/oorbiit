# handlers/__init__.py

from aiogram import Dispatcher

from .basic import register_basic_handlers, setup_bot_commands
from .media import register_media_handlers
from .text import register_text_handlers
from .profile import register_profile_handlers
from .settings_menu import register_settings_handlers
from .subscriptions_menu import register_subscription_menu_handlers
from .payments import register_payment_handlers
from .admin_panel import register_admin_panel_handlers


def register_all_handlers(dp: Dispatcher) -> None:
    # Базовые команды и меню
    register_basic_handlers(dp)

    # Разделы меню
    register_profile_handlers(dp)
    register_settings_handlers(dp)
    register_subscription_menu_handlers(dp)

    # Админ-панель (FSM)
    register_admin_panel_handlers(dp)

    # Платежи
    register_payment_handlers(dp)

    # Работа с медиа и текстом
    register_media_handlers(dp)
    register_text_handlers(dp)


__all__ = ["register_all_handlers", "setup_bot_commands"]
