import os
import logging
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

# Часовой пояс для расчёта "сегодня" (Москва, UTC+3)
USER_TZ_OFFSET_HOURS = 3
RESET_CUTOFF_HOUR = 11
# URL подключения к Postgres (Railway → Variable: DATABASE_URL)
DB_URL = os.getenv("DATABASE_URL")
if not DB_URL:
    raise RuntimeError("DATABASE_URL environment variable is not set")

# Лимиты по тарифам (генераций в день)
PLAN_LIMITS: Dict[str, int] = {
    "free": 0,
    "basic": 30,
    "pro": 120,
    "ultra": 300,
}


@contextmanager
def get_conn():
    """
    Унифицированный менеджер контекста для подключения к БД Postgres.
    Авто commit/rollback.
    """
    conn = psycopg2.connect(DB_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("Ошибка при работе с БД (Postgres), выполнен rollback")
        raise
    finally:
        conn.close()


def _today() -> date:
    """
    Логический «текущий день» в пользовательском часовом поясе (Москва, UTC+3).

    Новый день для лимитов начинается в RESET_CUTOFF_HOUR (11:00 по МСК).
    До 11:00 считаем, что ещё идёт «вчерашний» день, чтобы сброс used_today
    и прочих суточных лимитов происходил в 11:00 по Москве.
    """
    now = datetime.utcnow() + timedelta(hours=USER_TZ_OFFSET_HOURS)

    # До 11:00 по МСК считаем, что это ещё вчерашний день для лимитов
    if now.hour < RESET_CUTOFF_HOUR:
        return (now - timedelta(days=1)).date()

    # После 11:00 по МСК — уже новый день
    return now.date()


def _to_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            return None
    return None


def init_db() -> None:
    """
    Создаёт все необходимые таблицы в Postgres (если их ещё нет).
    """
    with get_conn() as conn:
        cur = conn.cursor()
        # users
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                plan TEXT DEFAULT 'free',
                expires_at DATE,
                daily_limit INTEGER DEFAULT 0,
                used_today INTEGER DEFAULT 0,
                extra_balance INTEGER DEFAULT 0,
                last_reset DATE,
                referrer_id BIGINT,
                username TEXT
            )
            """
        )
        # На старых базах гарантированно добавляем отсутствующие колонки
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referrer_id BIGINT;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT;")

        # purchases
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS purchases (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                type TEXT NOT NULL,      -- 'subscription' / 'topup'
                code TEXT,
                amount INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # model_usage
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS model_usage (
                user_id BIGINT NOT NULL,
                model_code TEXT NOT NULL,        -- 'flash' или 'pro'
                total_used INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, model_code)
            )
            """
        )

        # generation_log
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS generation_log (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                model_code TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # user_settings
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id BIGINT PRIMARY KEY,
                model TEXT DEFAULT 'flash',
                aspect_ratio TEXT DEFAULT '1:1',
                resolution TEXT DEFAULT '1K',
                images_per_prompt INTEGER DEFAULT 1
            )
            """
        )
        # новые колонки на старых базах
        cur.execute(
            "ALTER TABLE user_settings "
            "ADD COLUMN IF NOT EXISTS images_per_prompt INTEGER DEFAULT 1;"
        )


# ================== ВСПОМОГАТЕЛЬНОЕ ==================


def _ensure_user(cur, user_id: int) -> None:
    """
    Гарантирует наличие записи в users и user_settings для user_id.
    Используется с RealDictCursor или обычным курсором.
    """
    cur.execute("SELECT 1 FROM users WHERE user_id = %s", (user_id,))
    if cur.fetchone() is None:
        today = _today()
        cur.execute(
            """
            INSERT INTO users (
                user_id, plan, expires_at,
                daily_limit, used_today,
                extra_balance, last_reset,
                referrer_id, username
            )
            VALUES (%s, 'free', NULL, 0, 0, 0, %s, NULL, NULL)
            """,
            (user_id, today),
        )

    cur.execute("SELECT 1 FROM user_settings WHERE user_id = %s", (user_id,))
    if cur.fetchone() is None:
        cur.execute(
            """
            INSERT INTO user_settings (user_id, model, aspect_ratio, resolution, images_per_prompt)
            VALUES (%s, 'flash', '1:1', '1K', 1)
            """,
            (user_id,),
        )


def _reset_daily_if_needed_row(cur, row: Dict[str, Any]) -> Tuple[Any, ...]:
    """
    Сбрасывает used_today, если наступил новый день.
    Ожидает row как dict (RealDictRow).
    """
    user_id = row["user_id"]
    plan = row["plan"]
    expires_at = row["expires_at"]
    daily_limit = row["daily_limit"]
    used_today = row["used_today"]
    extra_balance = row["extra_balance"]
    last_reset = row["last_reset"]

    today = _today()
    last_reset_date = _to_date(last_reset)

    if last_reset_date is None or last_reset_date < today:
        cur.execute(
            """
            UPDATE users
            SET used_today = 0,
                last_reset = %s
            WHERE user_id = %s
            """,
            (today, user_id),
        )
        used_today = 0
        last_reset = today

    return user_id, plan, expires_at, daily_limit, used_today, extra_balance, last_reset


# ================== ПОЛЬЗОВАТЕЛЬ / ЛИМИТЫ ==================


def get_user(user_id: int) -> Optional[Tuple]:
    """
    Возвращает кортеж:
      (user_id, plan, expires_at, daily_limit, used_today, extra_balance, last_reset)
    Гарантирует наличие пользователя (создаёт при необходимости).
    """
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        _ensure_user(cur, user_id)

        cur.execute(
            """
            SELECT user_id, plan, expires_at,
                   daily_limit, used_today,
                   extra_balance, last_reset
            FROM users
            WHERE user_id = %s
            """,
            (user_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None

        return _reset_daily_if_needed_row(cur, row)


def update_user(
    user_id: int,
    plan: Optional[str] = None,
    expires_at: Optional[date] = None,
    daily_limit: Optional[int] = None,
    used_today: Optional[int] = None,
    extra_balance: Optional[int] = None,
    last_reset: Optional[date] = None,
) -> None:
    """
    Частичное обновление записи пользователя.
    """
    allowed_fields = {
        "plan",
        "expires_at",
        "daily_limit",
        "used_today",
        "extra_balance",
        "last_reset",
    }

    raw_fields: Dict[str, Any] = {
        "plan": plan,
        "expires_at": expires_at,
        "daily_limit": daily_limit,
        "used_today": used_today,
        "extra_balance": extra_balance,
        "last_reset": last_reset,
    }

    clean_fields: Dict[str, Any] = {}
    for key, value in raw_fields.items():
        if value is None or key not in allowed_fields:
            continue
        clean_fields[key] = value

    if not clean_fields:
        return

    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        _ensure_user(cur, user_id)

        set_parts: List[str] = []
        params: List[Any] = []
        for key, value in clean_fields.items():
            set_parts.append(f"{key} = %s")
            params.append(value)

        params.append(user_id)
        sql = "UPDATE users SET {set_clause} WHERE user_id = %s".format(
            set_clause=", ".join(set_parts)
        )
        cur.execute(sql, params)


def can_generate(user_id: int, cost: int = 1):
    """
    Проверка возможности генерации по системе ORB.

    cost — сколько ORB нужно списать за одну генерацию:
      - 1 для Gemini 2.5 Flash
      - 3 для Gemini 3 Pro

    Возвращает:
      (can: bool, source: Optional[str], reason: Optional[str], user_row)

    source: всегда 'extra' — списываем с ORB-баланса (extra_balance).
    """
    if cost <= 0:
        cost = 1

    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        _ensure_user(cur, user_id)

        cur.execute(
            """
            SELECT user_id, plan, expires_at,
                   daily_limit, used_today,
                   extra_balance, last_reset
            FROM users
            WHERE user_id = %s
            """,
            (user_id,),
        )
        row = cur.fetchone()
        if row is None:
            return False, None, "Пользователь не найден.", None

        (
            user_id_,
            plan,
            expires_at,
            daily_limit,
            used_today,
            extra_balance,
            last_reset,
        ) = _reset_daily_if_needed_row(cur, row)

        extra_balance = extra_balance or 0

        user_row = (
            user_id_,
            plan,
            expires_at,
            daily_limit,
            used_today,
            extra_balance,
            last_reset,
        )

        if extra_balance >= cost:
            return True, "extra", None, user_row

        return (
            False,
            None,
            "Недостаточно ORB для генерации. Пополните баланс в разделе «Подписка».",
            user_row,
        )


def register_generation(user_id: int, source: str, amount: int = 1) -> None:
    """
    Списывает ORB:

      - 'extra' → extra_balance -= amount

    amount — сколько ORB списать (1 для Flash, 3 для Pro).
    """
    if amount <= 0:
        amount = 1

    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        _ensure_user(cur, user_id)

        cur.execute(
            """
            SELECT user_id, plan, expires_at,
                   daily_limit, used_today,
                   extra_balance, last_reset
            FROM users
            WHERE user_id = %s
            """,
            (user_id,),
        )
        row = cur.fetchone()
        if row is None:
            return

        (
            user_id_,
            plan,
            expires_at,
            daily_limit,
            used_today,
            extra_balance,
            last_reset,
        ) = _reset_daily_if_needed_row(cur, row)

        if source == "extra":
            extra_balance = max(0, (extra_balance or 0) - amount)
            cur.execute(
                "UPDATE users SET extra_balance = %s WHERE user_id = %s",
                (extra_balance, user_id_),
            )


def add_extra_generations(user_id: int, amount: int) -> None:
    """
    Начисляет пользователю дополнительные генерации (extra_balance += amount).
    """
    if amount <= 0:
        return

    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        _ensure_user(cur, user_id)

        cur.execute(
            "SELECT extra_balance FROM users WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
        current = row["extra_balance"] if row and row["extra_balance"] is not None else 0
        new_val = current + amount

        cur.execute(
            "UPDATE users SET extra_balance = %s WHERE user_id = %s",
            (new_val, user_id),
        )


# ================== ПОДПИСКИ / ПОКУПКИ ==================


def add_purchase(user_id: int, p_type: str, code: Optional[str], amount: Optional[int]) -> None:
    """
    Логирует покупку в таблицу purchases.
    """
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO purchases (user_id, type, code, amount) VALUES (%s, %s, %s, %s)",
            (user_id, p_type, code, amount),
        )


def set_plan(user_id: int, plan: str, expires_at: Optional[date]) -> None:
    """
    Устанавливает пользователю тариф и дату окончания подписки.
    """
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        _ensure_user(cur, user_id)

        cur.execute(
            """
            UPDATE users
            SET plan = %s,
                expires_at = %s
            WHERE user_id = %s
            """,
            (plan, expires_at, user_id),
        )


def get_plan(user_id: int) -> Tuple[str, Optional[date]]:
    """
    Возвращает (plan, expires_at) для пользователя.
    """
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        _ensure_user(cur, user_id)

        cur.execute(
            "SELECT plan, expires_at FROM users WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            return "free", None

        plan = row["plan"] or "free"
        expires_at = _to_date(row["expires_at"])
        return plan, expires_at


# ================== РЕФЕРЕР ==================


def set_referrer(user_id: int, referrer_id: int) -> None:
    """
    Сохраняет, кто пригласил пользователя.
    Не перезаписывает уже установленного реферера.
    Не даёт пользователю указать самого себя.
    """
    if user_id == referrer_id:
        return

    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        _ensure_user(cur, user_id)

        cur.execute("SELECT referrer_id FROM users WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        if row and row["referrer_id"] is not None:
            return

        cur.execute(
            "UPDATE users SET referrer_id = %s WHERE user_id = %s",
            (referrer_id, user_id),
        )


def get_referrer_id(user_id: int) -> Optional[int]:
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT referrer_id FROM users WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        return row["referrer_id"] if row else None


# ================== СТАТИСТИКА ПО МОДЕЛЯМ ==================


def get_model_usage(user_id: int) -> Dict[str, int]:
    """
    Возвращает словарь:
      {"flash": <int>, "pro": <int>}
    """
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT model_code, total_used FROM model_usage WHERE user_id = %s",
            (user_id,),
        )
        rows = cur.fetchall()

    usage: Dict[str, int] = {"flash": 0, "pro": 0}
    for row in rows:
        model_code = row["model_code"]
        total_used = row["total_used"]
        if model_code in usage:
            usage[model_code] = total_used or 0
    return usage


def increment_model_usage(user_id: int, model_code: str) -> None:
    """
    Увеличивает счётчик использования модели у пользователя.
    model_code: 'flash' или 'pro'
    """
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO model_usage (user_id, model_code, total_used)
            VALUES (%s, %s, 1)
            ON CONFLICT (user_id, model_code)
            DO UPDATE SET total_used = model_usage.total_used + 1
            """,
            (user_id, model_code),
        )


# ================== USERNAME / ЖУРНАЛ ГЕНЕРАЦИЙ ==================


def set_username(user_id: int, username: Optional[str]) -> None:
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        _ensure_user(cur, user_id)
        cur.execute(
            "UPDATE users SET username = %s WHERE user_id = %s",
            (username, user_id),
        )


def get_username(user_id: int) -> Optional[str]:
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT username FROM users WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        return row["username"] if row else None


def log_generation_event(
    user_id: int,
    model_code: str,
    created_at: Optional[datetime] = None,
) -> None:
    """
    Логирует факт генерации в таблицу generation_log.
    """
    with get_conn() as conn:
        cur = conn.cursor()
        if created_at is None:
            cur.execute(
                "INSERT INTO generation_log (user_id, model_code) VALUES (%s, %s)",
                (user_id, model_code),
            )
        else:
            cur.execute(
                "INSERT INTO generation_log (user_id, model_code, created_at) VALUES (%s, %s, %s)",
                (user_id, model_code, created_at),
            )


def get_daily_generation_log(day: date) -> List[Tuple[int, str, datetime]]:
    """
    Возвращает список (user_id, model_code, created_at) за "админский день",
    который считается с RESET_CUTOFF_HOUR (11:00) по МСК до 11:00 следующего дня.
    """
    # 1) Начало периода в МСК: day 11:00
    start_msk = datetime.combine(day, datetime.min.time()).replace(
        hour=RESET_CUTOFF_HOUR,
        minute=0,
        second=0,
        microsecond=0,
    )

    # 2) Переводим в UTC (БД хранит created_at в UTC)
    start_utc = start_msk - timedelta(hours=USER_TZ_OFFSET_HOURS)
    end_utc = start_utc + timedelta(days=1)

    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT user_id, model_code, created_at
            FROM generation_log
            WHERE created_at >= %s AND created_at < %s
            """,
            (start_utc, end_utc),
        )
        rows = cur.fetchall()

    result: List[Tuple[int, str, datetime]] = []
    for row in rows:
        ts_raw = row["created_at"]
        if isinstance(ts_raw, datetime):
            ts = ts_raw
        else:
            try:
                ts = datetime.fromisoformat(str(ts_raw))
            except Exception:
                ts = start_utc
        result.append((row["user_id"], row["model_code"], ts))

    return result


def get_model_usage_for_period(
    user_id: int,
    model_code: str,
    start: datetime,
    end: datetime,
) -> int:
    """
    Считает, сколько раз пользователь user_id вызывал модель model_code
    в интервале [start; end).
    """
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM generation_log
            WHERE user_id = %s
              AND model_code = %s
              AND created_at >= %s
              AND created_at < %s
            """,
            (user_id, model_code, start, end),
        )
        row = cur.fetchone()
        return row["cnt"] if row and row["cnt"] is not None else 0


def get_admin_period_usage(
    user_id: int,
    period_start: datetime,
    period_end: datetime,
) -> Dict[str, int]:
    """
    Возвращает использование моделей админом за период:
      {"flash": N, "pro": M}
    """
    result: Dict[str, int] = {}
    for model_code in ("flash", "pro"):
        result[model_code] = get_model_usage_for_period(
            user_id, model_code, period_start, period_end
        )
    return result


# ================== НАСТРОЙКИ ПОЛЬЗОВАТЕЛЯ ==================


def get_user_settings(user_id: int) -> Dict[str, Any]:
    """
    Возвращает настройки пользователя (model, aspect_ratio, resolution, images_per_prompt).
    """
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        _ensure_user(cur, user_id)

        cur.execute(
            "SELECT model, aspect_ratio, resolution, images_per_prompt "
            "FROM user_settings WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            return {
                "model": "flash",
                "aspect_ratio": "1:1",
                "resolution": "1K",
                "images_per_prompt": 1,
            }

        return {
            "model": row["model"],
            "aspect_ratio": row["aspect_ratio"],
            "resolution": row["resolution"],
            "images_per_prompt": row.get("images_per_prompt") or 1,
        }


def update_user_settings(
    user_id: int,
    model: Optional[str] = None,
    aspect_ratio: Optional[str] = None,
    resolution: Optional[str] = None,
    images_per_prompt: Optional[int] = None,
) -> None:
    """
    Частично обновляет настройки пользователя.
    """
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        _ensure_user(cur, user_id)

        fields: List[str] = []
        params: List[Any] = []

        if model is not None:
            fields.append("model = %s")
            params.append(model)
        if aspect_ratio is not None:
            fields.append("aspect_ratio = %s")
            params.append(aspect_ratio)
        if resolution is not None:
            fields.append("resolution = %s")
            params.append(resolution)
        if images_per_prompt is not None:
            # защита от некорректных значений на уровне БД-слоя
            if images_per_prompt < 1:
                images_per_prompt = 1
            if images_per_prompt > 4:
                images_per_prompt = 4
            fields.append("images_per_prompt = %s")
            params.append(images_per_prompt)

        if not fields:
            return

        params.append(user_id)
        sql = "UPDATE user_settings SET {set_clause} WHERE user_id = %s".format(
            set_clause=", ".join(fields)
        )
        cur.execute(sql, params)
