# settings.py
from typing import Dict, Any

from database import get_user_settings, update_user_settings

# Хранилище сессий в памяти:
# chat_id -> {
#   "photos": list[bytes],
#   "model": "flash" | "pro",
#   "aspect_ratio": "1:1" | "9:16" | ...,
#   "resolution": "1K" | "2K" | "4K",
#   "images_per_prompt": int (1–4),
#   "photo_status_message_ids": list[int],
#   "photo_message_ids": list[int],
#   "media_groups": dict,
#   "last_generate_ts": float | None,
# }
SESSIONS: Dict[int, Dict[str, Any]] = {}

# Допустимые соотношения сторон
ALLOWED_ASPECT_RATIOS = {
    "1:1", "3:2", "2:3", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"
}

# Разрешения ТОЛЬКО для Gemini Pro
ALLOWED_RESOLUTIONS = {"1K", "2K"}

# Значения по умолчанию (если вдруг БД вернёт пустое)
DEFAULT_MODEL = "flash"
DEFAULT_ASPECT_RATIO = "1:1"
DEFAULT_RESOLUTION = "1K"
DEFAULT_IMAGES_PER_PROMPT = 1

# Максимум референс-фото для моделей
MAX_IMAGES_FLASH = 4
MAX_IMAGES_PRO = 14


def _default_session() -> Dict[str, Any]:
    return {
        "photos": [],
        "model": DEFAULT_MODEL,
        "aspect_ratio": DEFAULT_ASPECT_RATIO,
        "resolution": DEFAULT_RESOLUTION,
        "images_per_prompt": DEFAULT_IMAGES_PER_PROMPT,
        "photo_status_message_ids": [],
        "photo_message_ids": [],
        "media_groups": {},
        "last_generate_ts": None,
    }


def get_session(chat_id: int) -> Dict[str, Any]:
    """
    Возвращает сессию пользователя, гарантируя наличие всех необходимых ключей.
    Для новых сессий подхватывает настройки (model, aspect_ratio, resolution, images_per_prompt) из БД.
    """
    sess = SESSIONS.get(chat_id)
    if not sess:
        # берём сохранённые настройки из БД
        db_settings = get_user_settings(chat_id)
        sess = {
            "photos": [],
            "model": db_settings.get("model", DEFAULT_MODEL),
            "aspect_ratio": db_settings.get("aspect_ratio", DEFAULT_ASPECT_RATIO),
            "resolution": db_settings.get("resolution", DEFAULT_RESOLUTION),
            "images_per_prompt": db_settings.get("images_per_prompt", DEFAULT_IMAGES_PER_PROMPT),
            "photo_status_message_ids": [],
            "photo_message_ids": [],
            "media_groups": {},
            "last_generate_ts": None,
        }
        SESSIONS[chat_id] = sess
    else:
        # гарантия, что все ключи есть
        base = _default_session()
        for k, v in base.items():
            sess.setdefault(k, v)
    return sess


def reset_session(chat_id: int) -> None:
    """
    Полный сброс сессии чата (оперативной).
    Настройки в БД (модель/аспект/качество/кол-во изображений) при этом НЕ сбрасываются.
    """
    SESSIONS.pop(chat_id, None)


# ====== MODEL & ASPECT RATIO & RESOLUTION & IMAGES PER PROMPT ======

def set_model(chat_id: int, model: str) -> None:
    sess = get_session(chat_id)
    sess["model"] = model
    update_user_settings(chat_id, model=model)


def set_aspect_ratio(chat_id: int, ratio: str) -> None:
    sess = get_session(chat_id)
    sess["aspect_ratio"] = ratio
    update_user_settings(chat_id, aspect_ratio=ratio)


def set_resolution(chat_id: int, value: str) -> None:
    sess = get_session(chat_id)
    sess["resolution"] = value
    update_user_settings(chat_id, resolution=value)


def set_images_per_prompt(chat_id: int, value: int) -> None:
    """
    Устанавливает количество изображений за один запрос (1–4)
    и сохраняет это в сессии и в БД.
    """
    if value < 1:
        value = 1
    if value > 4:
        value = 4
    sess = get_session(chat_id)
    sess["images_per_prompt"] = value
    update_user_settings(chat_id, images_per_prompt=value)
