import logging
import os
import time
from typing import List, Optional, Dict, Any, Tuple

import requests

from config import GEMINI_API_KEY
from session_store import MAX_IMAGES_FLASH, MAX_IMAGES_PRO

GEMINIGEN_GENERATE_URL = "https://api.geminigen.ai/uapi/v1/generate_image"

# GeminiGen models
MODEL_FLASH = "imagen-flash"  # Gemini 2.5 Flash
MODEL_PRO = "imagen-pro"      # Gemini 3.0 Image (Nano Banana Pro)

DEFAULT_STYLE_FLASH = os.getenv("GEMINIGEN_STYLE_FLASH", "Photorealistic")
DEFAULT_STYLE_PRO = os.getenv("GEMINIGEN_STYLE_PRO", "Photorealistic")

HTTP_TIMEOUT = int(os.getenv("GEMINIGEN_HTTP_TIMEOUT", "120"))
HTTP_MAX_RETRIES = int(os.getenv("GEMINIGEN_HTTP_MAX_RETRIES", "3"))
HTTP_BACKOFF_BASE = float(os.getenv("GEMINIGEN_HTTP_BACKOFF_BASE", "1.0"))

# Polling settings
POLL_MAX_SECONDS = int(os.getenv("GEMINIGEN_POLL_MAX_SECONDS", "120"))
POLL_INTERVAL = float(os.getenv("GEMINIGEN_POLL_INTERVAL", "2.0"))

# If you know the exact History endpoint, set:
# GEMINIGEN_HISTORY_URL_TEMPLATE="https://api.geminigen.ai/uapi/v1/history/{uuid}"
GEMINIGEN_HISTORY_URL_TEMPLATE = os.getenv("GEMINIGEN_HISTORY_URL_TEMPLATE", "").strip()


class GeminiGenAPIError(Exception):
    pass


class GeminiGenNoImageError(GeminiGenAPIError):
    pass


def _headers() -> Dict[str, str]:
    if not GEMINI_API_KEY:
        raise GeminiGenAPIError("GEMINI_API_KEY is not set (GeminiGen key required).")
    return {"x-api-key": GEMINI_API_KEY}


def _post_with_retry(url: str, headers: dict, data: dict, files: list) -> requests.Response:
    backoff = HTTP_BACKOFF_BASE
    last_exc = None
    for attempt in range(1, HTTP_MAX_RETRIES + 1):
        try:
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
            raise GeminiGenNoImageError(f"Failed to download image: HTTP {r.status_code}")
        return r.content
    except requests.RequestException as e:
        raise GeminiGenNoImageError(f"Failed to download image: {e}") from e


def _extract_ids(payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[int]]:
    uuid = payload.get("uuid")
    req_id = payload.get("id")
    try:
        req_id = int(req_id) if req_id is not None else None
    except Exception:
        req_id = None
    return uuid, req_id


def _history_candidates(uuid: Optional[str], req_id: Optional[int]) -> List[Tuple[str, Optional[dict]]]:
    candidates: List[Tuple[str, Optional[dict]]] = []

    if GEMINIGEN_HISTORY_URL_TEMPLATE and uuid:
        candidates.append((GEMINIGEN_HISTORY_URL_TEMPLATE.format(uuid=uuid), None))

    if uuid:
        candidates += [
            (f"https://api.geminigen.ai/uapi/v1/history/{uuid}", None),
            ("https://api.geminigen.ai/uapi/v1/history", {"uuid": uuid}),
            (f"https://api.geminigen.ai/uapi/v1/get_history/{uuid}", None),
            ("https://api.geminigen.ai/uapi/v1/get_history", {"uuid": uuid}),
        ]

    if req_id is not None:
        candidates += [
            (f"https://api.geminigen.ai/uapi/v1/history/{req_id}", None),
            ("https://api.geminigen.ai/uapi/v1/history", {"id": req_id}),
            ("https://api.geminigen.ai/uapi/v1/get_history", {"id": req_id}),
        ]

    return candidates


def _get_json(url: str, params: Optional[dict] = None) -> Tuple[int, Any]:
    try:
        r = requests.get(url, headers=_headers(), params=params, timeout=HTTP_TIMEOUT)
        if r.status_code >= 400:
            return r.status_code, r.text
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text
    except requests.RequestException as e:
        return 0, str(e)


def _poll_until_done(initial_payload: Dict[str, Any]) -> Dict[str, Any]:
    uuid, req_id = _extract_ids(initial_payload)
    if not uuid and req_id is None:
        raise GeminiGenAPIError(f"Processing without uuid/id: {initial_payload}")

    candidates = _history_candidates(uuid, req_id)
    if not candidates:
        raise GeminiGenAPIError("No History API candidates. Set GEMINIGEN_HISTORY_URL_TEMPLATE.")

    deadline = time.time() + POLL_MAX_SECONDS
    last_seen = None

    while time.time() < deadline:
        for url, params in candidates:
            code, data = _get_json(url, params=params)

            # Ignore endpoints that don't exist / not allowed
            if code in (0, 404, 405):
                last_seen = (url, code, data)
                continue

            if isinstance(data, dict):
                status = data.get("status")
                status_desc = (data.get("status_desc") or "").lower()

                if status == 2 or status_desc == "completed":
                    return data

                if status == 3 or status_desc == "failed":
                    raise GeminiGenAPIError(f"GeminiGen failed (history): {data.get('error_message') or data}")

                last_seen = (url, code, data)
                continue

            last_seen = (url, code, data)

        time.sleep(POLL_INTERVAL)

    raise GeminiGenAPIError(
        f"Still processing after {POLL_MAX_SECONDS}s. Last history response: {last_seen}"
    )


def _call_geminigen(
    model: str,
    image_list: List[bytes],
    prompt: str,
    aspect_ratio: Optional[str] = None,
    style: Optional[str] = None,
) -> bytes:
    if not prompt and not image_list:
        raise GeminiGenAPIError("Empty request: no prompt and no images.")

    form = {"prompt": prompt or "", "model": model}
    if aspect_ratio:
        form["aspect_ratio"] = aspect_ratio
    if style and style != "None":
        form["style"] = style

    files_payload = []
    for i, img in enumerate(image_list or []):
        if img:
            files_payload.append(("files", (f"ref_{i}.jpg", img, "image/jpeg")))

    resp = _post_with_retry(GEMINIGEN_GENERATE_URL, headers=_headers(), data=form, files=files_payload)

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
        raise GeminiGenAPIError(f"GeminiGen failed: {data.get('error_message') or 'unknown error'}")

    if status == 1 or status_desc == "processing":
        data = _poll_until_done(data)
        img_url = data.get("generate_result")

    if not img_url:
        raise GeminiGenNoImageError(f"No generate_result in response: {data}")

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
    resolution: Optional[str] = None,  # kept for compatibility
) -> bytes:
    limited = (image_list or [])[:MAX_IMAGES_PRO]
    return _call_geminigen(
        model=MODEL_PRO,
        image_list=limited,
        prompt=user_prompt,
        aspect_ratio=aspect_ratio,
        style=DEFAULT_STYLE_PRO,
    )
