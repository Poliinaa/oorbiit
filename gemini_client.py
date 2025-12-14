import base64
import logging
import os
import time
from typing import Any, Dict, List, Optional, Sequence

import requests

from config import GEMINI_API_KEY
from session_store import MAX_IMAGES_FLASH, MAX_IMAGES_PRO

# ===== МОДЕЛИ И БАЗОВЫЕ КОНСТАНТЫ =====

GEMINI_MODEL_FLASH = "gemini-2.5-flash-image"
GEMINI_MODEL_PRO = "gemini-3-pro-image-preview"

GEMINI_URL_FLASH = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL_FLASH}:generateContent"
)
GEMINI_URL_PRO = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL_PRO}:generateContent"
)

# HTTP-параметры — настраиваются через переменные окружения
GEMINI_HTTP_TIMEOUT = int(os.getenv("GEMINI_HTTP_TIMEOUT", "120"))
GEMINI_HTTP_MAX_RETRIES = int(os.getenv("GEMINI_HTTP_MAX_RETRIES", "3"))
GEMINI_HTTP_BACKOFF_BASE = float(os.getenv("GEMINI_HTTP_BACKOFF_BASE", "1.0"))

# Допустимые значения для aspect_ratio / resolution
ALLOWED_ASPECT_RATIOS = {
    "1:1",
    "3:4",
    "4:3",
    "9:16",
    "16:9",
    "2:3",
    "3:2",
}
ALLOWED_RESOLUTIONS = {"1K", "2K"}


# ===== СОБСТВЕННЫЕ ИСКЛЮЧЕНИЯ =====


class GeminiAPIError(Exception):
    """Базовое исключение для ошибок работы с Gemini API."""


class GeminiNoImageError(GeminiAPIError):
    """Ответ успешен, но изображение получить не удалось (NO_IMAGE / safety)."""


class GeminiInvalidConfigError(GeminiAPIError):
    """Некорректные параметры запроса (aspect_ratio, resolution и т.п.)."""


# ===== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====


def _normalize_aspect_ratio(aspect_ratio: Optional[str]) -> Optional[str]:
    if not aspect_ratio:
        return None
    aspect_ratio = aspect_ratio.strip()
    if aspect_ratio in ALLOWED_ASPECT_RATIOS:
        return aspect_ratio
    logging.warning("Недопустимое aspect_ratio '%s', параметр будет проигнорирован.", aspect_ratio)
    return None


def _normalize_resolution(resolution: Optional[str]) -> Optional[str]:
    if not resolution:
        return None
    resolution = resolution.strip().upper()
    if resolution in ALLOWED_RESOLUTIONS:
        return resolution
    logging.warning("Недопустимое resolution '%s', параметр будет проигнорирован.", resolution)
    return None


def _build_request_body(
    image_list: Sequence[bytes],
    user_prompt: str,
    aspect_ratio: Optional[str] = None,
    resolution: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Формирует тело запроса к Gemini image-модели.
    Работает и для text-to-image (без фото), и для image-to-image (с фото).
    """
    normalized_ar = _normalize_aspect_ratio(aspect_ratio)
    normalized_res = _normalize_resolution(resolution)

    parts: List[Dict[str, Any]] = []
    if user_prompt:
        parts.append({"text": user_prompt})

    for img_bytes in image_list:
        if not img_bytes:
            continue
        img_b64 = base64.b64encode(img_bytes).decode("utf-8")
        parts.append(
            {
                "inlineData": {
                    "mimeType": "image/jpeg",
                    "data": img_b64,
                }
            }
        )

    # Если нет ни текста, ни картинок — это логическая ошибка на более верхнем уровне
    if not parts:
        raise GeminiInvalidConfigError("Пустой запрос к Gemini: нет ни текста, ни изображений.")

    body: Dict[str, Any] = {
        "contents": [
            {
                "role": "user",
                "parts": parts,
            }
        ],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
        },
    }

    image_config: Dict[str, Any] = {}

    if normalized_ar:
        image_config["aspectRatio"] = normalized_ar

    if normalized_res:
        # Для Gemini 3 Pro: 1K / 2K / 4K
        image_config["imageSize"] = normalized_res

    if image_config:
        body["generationConfig"]["imageConfig"] = image_config

    # Логируем на DEBUG без base64-данных
    logging.debug(
        "Gemini request body (truncated): prompt_len=%s, images=%s, aspect_ratio=%s, resolution=%s",
        len(user_prompt or ""),
        len(list(image_list)),
        normalized_ar,
        normalized_res,
    )

    return body


def _extract_image_from_response(data: Dict[str, Any]) -> bytes:
    """
    Извлекает картинку из ответа Gemini.
    Выбрасывает GeminiNoImageError, если картинки нет (NO_IMAGE, safety и т.п.),
    либо GeminiAPIError при некорректном формате ответа.
    """
    if "error" in data:
        err = data["error"] or {}
        msg = err.get("message", "Unknown error")
        logging.error("Gemini API JSON error: %s", err)
        raise GeminiAPIError(f"Gemini HTTP error: {msg} | raw={err}")

    candidates = data.get("candidates") or []
    if not candidates:
        logging.error("Gemini returned no candidates: %s", data)
        raise GeminiNoImageError("NO_IMAGE: Gemini вернул пустой список candidates.")

    first = candidates[0] or {}
    content = first.get("content")
    if not content:
        finish_reason = first.get("finishReason")
        safety = first.get("safetyRatings")
        logging.error(
            "Gemini candidate has no content. finishReason=%s, safety=%s",
            finish_reason,
            safety,
        )
        raise GeminiNoImageError("NO_IMAGE: Gemini candidate has no content (finishReason/safety).")

    parts_out = content.get("parts") or []
    for part in parts_out:
        inline = part.get("inlineData")
        if inline and "data" in inline:
            try:
                return base64.b64decode(inline["data"])
            except Exception as e:
                logging.error("Failed to decode inlineData from Gemini response: %s", e)
                raise GeminiAPIError("Не удалось декодировать изображение из ответа Gemini.") from e

    logging.error("No inlineData with image found in Gemini response.")
    raise GeminiNoImageError("NO_IMAGE: в ответе Gemini нет inlineData с изображением.")


def _post_with_retry(
    url: str,
    headers: Dict[str, str],
    body: Dict[str, Any],
    max_retries: int = GEMINI_HTTP_MAX_RETRIES,
    timeout: int = GEMINI_HTTP_TIMEOUT,
) -> requests.Response:
    """
    Универсальный вызов requests.post с ретраями и экспоненциальным backoff.
    - Ретрай для 5xx и сетевых таймаутов / ошибок соединения.
    - Без ретрая для 4xx (квота, неверный запрос и т.п.).
    """
    backoff = GEMINI_HTTP_BACKOFF_BASE

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=timeout)
        except requests.Timeout:
            logging.warning(
                "Gemini request timeout (attempt %s/%s, url=%s)",
                attempt,
                max_retries,
                url,
            )
            if attempt == max_retries:
                raise GeminiAPIError("Время ожидания ответа Gemini истекло (timeout).")
            time.sleep(backoff)
            backoff *= 2
            continue
        except requests.RequestException as e:
            logging.exception(
                "Gemini request error (attempt %s/%s): %s",
                attempt,
                max_retries,
                e,
            )
            if attempt == max_retries:
                raise GeminiAPIError(f"Ошибка при обращении к Gemini: {e}") from e
            time.sleep(backoff)
            backoff *= 2
            continue

        # HTTP-код 2xx/3xx/4xx/5xx
        if 500 <= resp.status_code < 600:
            logging.warning(
                "Gemini HTTP %s (attempt %s/%s), will retry.",
                resp.status_code,
                attempt,
                max_retries,
            )
            if attempt == max_retries:
                # Дадим подробный текст наверх
                raise GeminiAPIError(f"Gemini HTTP {resp.status_code}: {resp.text}")
            time.sleep(backoff)
            backoff *= 2
            continue

        # Для 4xx и успешных кодов прекращаем ретраи
        return resp

    # Теоретически недостижимо
    raise GeminiAPIError("Не удалось получить ответ от Gemini после ретраев.")


def _call_gemini_image_api(
    url: str,
    image_list: Sequence[bytes],
    user_prompt: str,
    aspect_ratio: Optional[str] = None,
    resolution: Optional[str] = None,
) -> bytes:
    """
    Базовый вызов image-модели (Flash, Pro) через REST API.
    Работает и с image-to-image (при image_list), и с text-to-image (если image_list пустой).
    """
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY,
    }

    body = _build_request_body(image_list, user_prompt, aspect_ratio, resolution)
    resp = _post_with_retry(url, headers, body)

    # Обработка HTTP-кодов
    if resp.status_code >= 400:
        # 4xx — ошибки запроса, квоты и т.п. (без ретраев)
        logging.error("Gemini HTTP error %s: %s", resp.status_code, resp.text)
        raise GeminiAPIError(f"Gemini HTTP {resp.status_code}: {resp.text}")

    try:
        data = resp.json()
    except Exception as e:
        logging.exception("Failed to parse Gemini JSON response: %s", e)
        raise GeminiAPIError("Не удалось разобрать JSON-ответ Gemini.") from e

    # Извлекаем байты изображения
    return _extract_image_from_response(data)


# ===== ПУБЛИЧНЫЕ ФУНКЦИИ ДЛЯ ИСПОЛЬЗОВАНИЯ В БОТЕ =====


def call_gemini_flash(
    image_list: List[bytes],
    user_prompt: str,
    aspect_ratio: Optional[str] = None,
) -> bytes:
    """
    Вызов Gemini 2.5 Flash Image.
    Ограничение на количество референсов — MAX_IMAGES_FLASH.
    """
    limited = image_list[:MAX_IMAGES_FLASH]
    return _call_gemini_image_api(
        GEMINI_URL_FLASH,
        limited,
        user_prompt,
        aspect_ratio,
        resolution=None,
    )


def call_gemini_pro(
    image_list: List[bytes],
    user_prompt: str,
    aspect_ratio: Optional[str] = None,
    resolution: Optional[str] = None,
) -> bytes:
    """
    Вызов Gemini 3 Pro Image Preview.
    Ограничение на количество референсов — MAX_IMAGES_PRO.
    resolution: "1K" | "2K" | "4K" (опционально).
    """
    limited = image_list[:MAX_IMAGES_PRO]
    return _call_gemini_image_api(
        GEMINI_URL_PRO,
        limited,
        user_prompt,
        aspect_ratio,
        resolution,
    )

