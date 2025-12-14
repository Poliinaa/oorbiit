from typing import List

from .settings import get_session


def get_photos(chat_id: int) -> List[bytes]:
    """
    Возвращает список фото в Remix-стейдже для данного чата.
    """
    sess = get_session(chat_id)
    return sess["photos"]


def add_photo(chat_id: int, photo_bytes: bytes) -> None:
    """
    Добавляет фото в Remix-стейдж для данного чата.
    """
    sess = get_session(chat_id)
    sess["photos"].append(photo_bytes)


def clear_photos(chat_id: int) -> None:
    """
    Полностью очищает Remix-стейдж:
    - список фото,
    - статусные сообщения,
    - id сообщений пользователя с фото,
    - media_groups.
    """
    sess = get_session(chat_id)
    sess["photos"] = []
    sess["photo_status_message_ids"] = []
    sess["photo_message_ids"] = []
    sess["media_groups"] = {}
