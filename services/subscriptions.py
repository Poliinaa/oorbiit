from datetime import date, datetime, timedelta
from typing import Optional, Tuple

from database import get_user, update_user, PLAN_LIMITS, add_extra_generations


# Приоритеты тарифов: нельзя купить тариф ниже активного
PLAN_PRIORITY = {
    "free": 0,
    "basic": 1,
    "pro": 2,
    "ultra": 3,
}


def is_subscription_active_row(row) -> bool:
    """
    Проверка, активна ли подписка по строке из таблицы users.
    Ожидается формат:
    (user_id, plan, expires_at, daily_limit, used_today, extra_balance, last_reset)
    """
    if not row:
        return False

    _, plan, expires_at, *_ = row

    if plan == "free" or not expires_at:
        return False

    if isinstance(expires_at, str):
        try:
            exp_date = datetime.fromisoformat(expires_at).date()
        except ValueError:
            return False
    elif isinstance(expires_at, date):
        exp_date = expires_at
    else:
        return False

    return exp_date >= date.today()


def can_use_pro_model(user_id: int) -> bool:
    """
    В новой модели ORB Gemini 3 Pro доступна всем.
    Ограничение только по ORB-балансу, который проверяется при генерации.
    """
    return True



def activate_subscription(user_id: int, plan: str, days: int) -> None:
    """
    Активирует или продлевает подписку пользователю.
    Обновляет план, дату окончания, дневной лимит и сбрасывает used_today.
    """
    row = get_user(user_id)
    if not row:
        return

    _, _, expires_at, *_ = row
    today = date.today()

    if isinstance(expires_at, str):
        try:
            current_exp = datetime.fromisoformat(expires_at).date()
        except ValueError:
            current_exp = None
    elif isinstance(expires_at, date):
        current_exp = expires_at
    else:
        current_exp = None

    if current_exp and current_exp >= today:
        new_expires = current_exp + timedelta(days=days)
    else:
        new_expires = today + timedelta(days=days)

    daily_limit = PLAN_LIMITS.get(plan, 0)

    update_user(
        user_id,
        plan=plan,
        expires_at=new_expires,
        daily_limit=daily_limit,
        used_today=0,
    )


def can_upgrade_to_plan(user_id: int, new_plan: str) -> Tuple[bool, Optional[str]]:
    """
    Проверяет, можно ли купить указанный тариф.

    Нельзя купить тариф ниже уже активного:
    - если подписки нет → можно любой тариф;
    - если есть, но новый приоритет ниже → вернёт (False, текст_ошибки).
    """
    row = get_user(user_id)
    if not row or not is_subscription_active_row(row):
        return True, None

    _, current_plan, *_ = row
    current_priority = PLAN_PRIORITY.get(current_plan, 0)
    new_priority = PLAN_PRIORITY.get(new_plan, 0)

    if new_priority < current_priority:
        return False, (
            "У вас уже активен более высокий тариф.\n"
            "Нельзя купить более низкий, пока текущая подписка не закончится."
        )

    return True, None


def topup_generations(user_id: int, amount: int) -> None:
    """
    Начисляет дополнительные генерации пользователю.
    """
    add_extra_generations(user_id, amount)
