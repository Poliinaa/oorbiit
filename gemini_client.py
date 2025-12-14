import logging
import os
import time
from typing import List, Optional

import requests

from config import GEMINI_API_KEY
from session_store import MAX_IMAGES_FLASH, MAX_IMAGES_PRO

GEMINIGEN_GENERATE_URL = "https://api.geminigen.ai/uapi/v1/generate_image"

# Маппинг ваших режимов на модели GeminiGen
MODEL_FLASH = "imagen-flash"  # Gemini 2.5 Flash
MODEL_PRO = "imagen-pro"      # Gemini 3.0 Image (Nano Banana Pro)

# Опционально: дефолтные стили (можно переопределять через env)
DEFAULT_STYLE_FLASH = os.getenv("GEMINIGEN_STYLE_FLASH", "Photorealistic")
DEFAULT_STYLE_PRO = os.getenv("GEMINIGEN_STYLE_PRO", "Photorealistic")

# Сетевые настройки
HTTP_TIMEOUT = int(os.getenv("GEMINIGEN_HTTP_TIMEOUT", "120"))
HTTP_MAX_RETRIES = int(os.getenv("GEMINIGEN_HTTP_MAX_RETRIES", "3"))
HTTP_BACKOFF_BASE = float(os.getenv("GEMINIGEN_HTTP_BACKOFF_BASE", "1.0"))


class GeminiGenAPIError(Exception):
    pass


class GeminiGenNoImageError(GeminiGenAPIError):
    pass


def _post_with_retry(url: str, headers: dict, data: dict, files: list) -> requests.Response:
    backoff = HTTP_BACKOFF_BASE
    last_exc = None
    for attempt in range(1, HTTP_MAX_RETRIES + 1):
        try:
            # ВАЖНО: Content-Type руками не ставим, requests сам сделает multipart boundary
            resp = requests.post(url, headers=headers, data=data, files=files, timeout=HTTP_TIMEOUT)
            if 500 <= resp.status_code < 600 and attempt < HTTP_MAX_RETRIES:
                logging.warning("GeminiGen HTTP %s, retry %s/%s", resp.status_code, attempt, HTTP_MAX_RETRIES)
                time.sleep(backoff)
                backoff *= 2
                continue
            return resp
        except (requests.Timeout, requests.RequestException) as e:
            last_exc = e
            logging.warning("GeminiGen request error, retry %s/%s: %s", attempt, HTTP_MAX_RETRIES, e)
            if attempt >= HTTP_MAX_RETRIES:
                raise GeminiGenAPIError(f"GeminiGen request failed: {e}") from e
            time.sleep(backoff)
            backoff *= 2
    raise GeminiGenAPIError(f"GeminiGen request failed: {last_exc}")


def _download_image_bytes(url: str) -> bytes:
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT)
        if r.status_code >= 400:
            raise GeminiGenNoImageError(f"Не удалось скачать изображение: HTTP {r.status_code}")
        return r.content
    except requests.RequestException as e:
        raise GeminiGenNoImageError(f"Не удалось скачать изображение: {e}") from e


def _call_geminigen(
    model: str,
    image_list: List[bytes],
    prompt: str,
    aspect_ratio: Optional[str] = None,
    style: Optional[str] = None,
) -> bytes:
    if not GEMINI_API_KEY:
        raise GeminiGenAPIError("GEMINI_API_KEY не задан в окружении (нужен ключ GeminiGen).")

    if not prompt and not image_list:
        raise GeminiGenAPIError("Пустой запрос: нет ни prompt, ни images.")

    headers = {"x-api-key": GEMINI_API_KEY}

    form = {
        "prompt": prompt or "",
        "model": model,
    }
    if aspect_ratio:
        form["aspect_ratio"] = aspect_ratio
    if style and style != "None":
        form["style"] = style

    # multipart files: повторяющееся поле "files"
    files_payload = []
    for i, img in enumerate(image_list or []):
        if not img:
            continue
        # Если у вас иногда PNG — можно заменить image/jpeg на application/octet-stream, но JPEG обычно ок
        files_payload.append(("files", (f"ref_{i}.jpg", img, "image/jpeg")))

    resp = _post_with_retry(GEMINIGEN_GENERATE_URL, headers=headers, data=form, files=files_payload)

    if resp.status_code >= 400:
        raise GeminiGenAPIError(f"GeminiGen HTTP {resp.status_code}: {resp.text}")

    try:
        data = resp.json()
    except Exception as e:
        raise GeminiGenAPIError(f"GeminiGen invalid JSON: {e} | raw={resp.text[:500]}") from e

    status = data.get("status")
    status_desc = (data.get("status_desc") or "").lower()
    img_url = data.get("generate_result")

    if status == 3 or status_desc == "failed":
        err = data.get("error_message") or "unknown error"
        raise GeminiGenAPIError(f"GeminiGen failed: {err}")

    # Если вернули processing — у GeminiGen есть webhooks/history, но без polling мы не ждём
    if status == 1 or status_desc == "processing":
        raise GeminiGenAPIError("GeminiGen вернул статус processing. Подключите History API polling или webhooks.")

    # completed, но ссылки нет
    if (status == 2 or status_desc == "completed") and not img_url:
        raise GeminiGenNoImageError(f"GeminiGen completed, но нет generate_result: {data}")

    if not img_url:
        raise GeminiGenNoImageError(f"GeminiGen не вернул ссылку на изображение: {data}")

    return _download_image_bytes(img_url)


def call_gemini_flash(
    image_list: List[bytes],
    user_prompt: str,
    aspect_ratio: Optional[str] = None,
) -> bytes:
    limited = (image_list or [])[:MAX_IMAGES_FLASH]
    return _call_geminigen(
        model=MODEL_FLASH,
        image_list=limited,
        prompt=user_prompt,
        aspect_ratio=aspect_ratio,
        style=DEFAULT_STYLE_FLASH,
    )


def call_gemini_pro(
    image_list: List[bytes],
    user_prompt: str,
    aspect_ratio: Optional[str] = None,
    resolution: Optional[str] = None,  # оставляем, чтобы не ломать существующие вызовы
) -> bytes:
    limited = (image_list or [])[:MAX_IMAGES_PRO]
    # resolution в GeminiGen endpoint не заявлен — параметр игнорируем
    return _call_geminigen(
        model=MODEL_PRO,
        image_list=limited,
        prompt=user_prompt,
        aspect_ratio=aspect_ratio,
        style=DEFAULT_STYLE_PRO,
    )
