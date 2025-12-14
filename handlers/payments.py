# payments.py

from aiogram import types, Dispatcher
from aiogram.types import ContentTypes

from config import PAYMENT_PROVIDER_TOKEN
from database import (
    add_purchase,          # логирование покупок
    add_extra_generations, # начислить ORB
    get_referrer_id,       # получить реферера 1-го уровня
)

# В твоём проекте topup_generations живёт в services.subscriptions
# (если у тебя он в другом модуле – просто поправь импорт)
from services.subscriptions import (
    topup_generations,     # использовать как начисление ORB
)

# ===== ТАРИФЫ ORB-ПАКЕТОВ =====
# Эти значения должны совпадать с тем, что ты показываешь в меню и мини-аппе.

ORB_PACKS = {
    "mini": {
        "code": "mini",
        "title": "MINI — 100 ORB",
        "description": "Пробный пакет для теста Orbit AI",
        "orbs": 100,
        "amount": 590_00,   # в копейках, 590₽
    },
    "standard": {
        "code": "standard",
        "title": "STANDARD — 250 ORB",
        "description": "Оптимальный пакет для регулярного использования",
        "orbs": 250,
        "amount": 1_390_00,
    },
    "super": {
        "code": "super",
        "title": "SUPER — 500 ORB",
        "description": "Для активных пользователей и создания серий",
        "orbs": 500,
        "amount": 2_590_00,
    },
    "premium": {
        "code": "premium",
        "title": "PREMIUM — 1000 ORB",
        "description": "Профессиональный пакет для интенсивной работы",
        "orbs": 1000,
        "amount": 4_490_00,
    },
    "max": {
        "code": "max",
        "title": "MAX — 2000 ORB",
        "description": "Максимум возможностей Orbit AI",
        "orbs": 2000,
        "amount": 7_990_00,
    },
}

# ===== НАСТРОЙКИ МНОГОУРОВНЕВОЙ РЕФЕРАЛКИ =====
# lvl1 — тот, кто пригласил покупателя
# lvl2 — тот, кто пригласил lvl1

REFERRAL_BONUS_PACK = {
    "mini": {
        "lvl1": 10,
        "lvl2": 5,
    },
    "standard": {
        "lvl1": 25,
        "lvl2": 12,
    },
    "super": {
        "lvl1": 50,
        "lvl2": 25,
    },
    "premium": {
        "lvl1": 100,
        "lvl2": 50,
    },
    "max": {
        "lvl1": 200,
        "lvl2": 100,
    },
}


def _reward_referrer_for_pack(user_id: int, pack_code: str) -> None:
    """
    Начислить бонусы реферерам за покупку ORB-пакета.
    user_id — тот, кто оплатил.
    lvl1 — прямой реферер
    lvl2 — реферер реферера
    """
    # 1. Ищем прямого реферера (уровень 1)
    lvl1_id = get_referrer_id(user_id)
    if not lvl1_id:
        return

    cfg = REFERRAL_BONUS_PACK.get(pack_code)
    if not cfg:
        return

    lvl1_bonus = cfg.get("lvl1", 0) or 0
    lvl2_bonus = cfg.get("lvl2", 0) or 0

    # 2. Начисляем бонус 1-му уровню
    if lvl1_bonus > 0:
        try:
            add_extra_generations(lvl1_id, lvl1_bonus)
        except Exception:
            # Ошибка бонуса не должна ломать основную оплату
            pass

    # 3. Ищем реферера 2-го уровня (реферер реферера)
    if lvl2_bonus > 0:
        lvl2_id = get_referrer_id(lvl1_id)
        # На всякий случай защищаемся от циклических связей
        if lvl2_id and lvl2_id not in (user_id, lvl1_id):
            try:
                add_extra_generations(lvl2_id, lvl2_bonus)
            except Exception:
                pass


# ===== РЕГИСТРАЦИЯ ХЕНДЛЕРОВ ПЛАТЕЖЕЙ =====


def register_payment_handlers(dp: Dispatcher) -> None:
    """
    Регистрируем все хендлеры, связанные с оплатой:
    - выбор пакета по callback'ам из меню
    - pre_checkout
    - успешный платеж
    """

    # ---- Выбор пакета из меню подписок ----

    @dp.callback_query_handler(lambda c: c.data and c.data.startswith("pack_"))
    async def callback_choose_pack(callback: types.CallbackQuery):
        """
        Обработка нажатия на кнопку пакета:
        pack_mini, pack_standard, pack_super, pack_premium, pack_max
        """
        await callback.answer()

        data = callback.data  # например "pack_mini"
        pack_code = data.split("_", 1)[1]  # mini
        pack = ORB_PACKS.get(pack_code)
        if not pack:
            await callback.message.answer(
                "Неизвестный пакет ORB. Обновите приложение или напишите в поддержку."
            )
            return

        prices = [
            types.LabeledPrice(
                label=pack["title"],
                amount=pack["amount"],  # в копейках
            )
        ]

        await callback.bot.send_invoice(
            chat_id=callback.from_user.id,
            title=pack["title"],
            description=pack["description"],
            provider_token=PAYMENT_PROVIDER_TOKEN,
            currency="RUB",
            prices=prices,
            start_parameter=f"orb_{pack_code}",
            payload=f"pack:{pack_code}",
        )

    # ---- Дополнительно: команда /pay_orb (если захочешь вызывать из команды) ----

    @dp.message_handler(commands=["pay_orb"])
    async def cmd_pay_orb(message: types.Message):
        """
        Простая команда для теста оплаты: /pay_orb mini
        """
        parts = message.text.strip().split()
        if len(parts) < 2:
            await message.answer(
                "Укажи код пакета: /pay_orb mini|standard|super|premium|max"
            )
            return

        pack_code = parts[1].lower()
        pack = ORB_PACKS.get(pack_code)
        if not pack:
            await message.answer("Неизвестный пакет ORB.")
            return

        prices = [
            types.LabeledPrice(
                label=pack["title"],
                amount=pack["amount"],
            )
        ]

        await message.bot.send_invoice(
            chat_id=message.chat.id,
            title=pack["title"],
            description=pack["description"],
            provider_token=PAYMENT_PROVIDER_TOKEN,
            currency="RUB",
            prices=prices,
            start_parameter=f"orb_{pack_code}",
            payload=f"pack:{pack_code}",
        )

    # ---- Pre checkout: Telegram спрашивает, можно ли подтверждать платёж ----

    @dp.pre_checkout_query_handler(lambda q: True)
    async def pre_checkout(pre_checkout_query: types.PreCheckoutQuery):
        """
        Здесь можно делать доп. проверки (лимиты, доступность, и т.п.).
        Пока просто одобряем любой корректный запрос.
        """
        try:
            payload = pre_checkout_query.invoice_payload or ""
            # Простая валидация payload
            if payload.startswith("pack:"):
                await pre_checkout_query.answer(ok=True)
                return

            # неизвестный payload
            await pre_checkout_query.answer(
                ok=False,
                error_message="Не удалось распознать тип оплаты. Попробуйте ещё раз или напишите в поддержку.",
            )
        except Exception:
            await pre_checkout_query.answer(
                ok=False,
                error_message="Произошла ошибка при обработке платежа. Попробуйте ещё раз.",
            )

    # ---- Успешный платёж ----

    @dp.message_handler(content_types=ContentTypes.SUCCESSFUL_PAYMENT)
    async def successful_payment(message: types.Message):
        """
        Обработка успешной оплаты от Telegram.
        Здесь мы:
        - определяем пакет;
        - начисляем ORB;
        - логируем покупку;
        - начисляем реферальные бонусы (1-й и 2-й уровень).
        """
        sp: types.SuccessfulPayment = message.successful_payment
        payload = sp.invoice_payload or ""

        # Ожидаем payload вида "pack:mini"
        if payload.startswith("pack:"):
            pack_code = payload.split(":", 1)[1]
            pack = ORB_PACKS.get(pack_code)

            if not pack:
                await message.answer(
                    "Оплата прошла, но пакет не найден. Напишите, пожалуйста, в поддержку."
                )
                return

            user_id = message.from_user.id

            # 1. Начисляем ORB пользователю
            topup_generations(user_id, pack["orbs"])

            # 2. Логируем покупку
            add_purchase(
                user_id=user_id,
                p_type="topup",
                code=pack_code,
                amount=pack["orbs"],
            )

            # 3. Реферальные бонусы (многоуровневые)
            _reward_referrer_for_pack(user_id, pack_code)

            # 4. Сообщение пользователю
            await message.answer(
                f"✅ Оплата получена, начислено {pack['orbs']} ORB.\n"
                "Проверить баланс можно в /menu → 👤 Мой профиль."
            )
            return

        # ---- Неизвестный payload ----
        await message.answer(
            "Платёж прошёл, но тип не распознан.\n"
            "Если что-то не так — напишите в поддержку."
        )
