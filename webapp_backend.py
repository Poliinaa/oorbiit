# webapp_backend.py

import os
import json
import urllib.parse
import requests

from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from config import TELEGRAM_TOKEN, PAYMENT_PROVIDER_TOKEN
from database import (
    get_user,
    get_model_usage,
    get_user_settings,
    update_user_settings,
)

# ---- ОПИСАНИЕ ORB-ПАКЕТОВ (скопировано из payments.py, чтобы не импортировать модуль) ----
ORB_PACKS = {
    "mini": {
        "title": "MINI — 100 ORB",
        "amount": 59000,   # 590 ₽
        "orbs": 100,
    },
    "standard": {
        "title": "STANDARD — 250 ORB",
        "amount": 139000,  # 1390 ₽
        "orbs": 250,
    },
    "super": {
        "title": "SUPER — 500 ORB",
        "amount": 259000,  # 2590 ₽
        "orbs": 500,
    },
    "premium": {
        "title": "PREMIUM — 1000 ORB",
        "amount": 449000,  # 4490 ₽
        "orbs": 1000,
    },
    "max": {
        "title": "MAX — 2000 ORB",
        "amount": 799000,  # 7990 ₽
        "orbs": 2000,
    },
}

# ТВОЙ бот — по умолчанию берём Orbit_AIBot
REF_BASE_URL = os.getenv("REF_BASE_URL", "https://t.me/Orbit_AIBot")

PLAN_LABELS = {
    "free": "Free",
    "basic": "Basic",
    "pro": "Pro",
    "ultra": "Pro",  # старый ultra пользователю показываем как Pro
}

app = FastAPI()


def _parse_init_data(raw: str) -> dict:
    parsed = urllib.parse.parse_qs(raw, keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


def _get_user_id_from_init_data(init_data: str) -> int:
    try:
        data = _parse_init_data(init_data)
        user_raw = data.get("user")
        if not user_raw:
            raise ValueError("no user")

        user = json.loads(user_raw)
        return int(user["id"])
    except Exception:
        raise HTTPException(status_code=400, detail="init_data invalid")


@app.get("/api/profile")
async def api_profile(request: Request):
    """
    Профиль пользователя для мини-аппа:
    - тариф (внутренний + красивое название)
    - баланс ORB
    - использование моделей
    - реферальная ссылка
    """
    init_data = request.headers.get("X-Telegram-Init-Data") or request.query_params.get(
        "init_data"
    )
    if not init_data:
        raise HTTPException(status_code=400, detail="init_data required")

    user_id = _get_user_id_from_init_data(init_data)

    row = get_user(user_id)
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    (
        user_id_db,
        plan,
        expires_at,
        daily_limit,   # сейчас не показываем в UI
        used_today,    # тоже не показываем
        extra_balance,
        last_reset,    # можно использовать для внутренней логики
    ) = row

    usage = get_model_usage(user_id)

    raw_plan = plan or "free"
    plan_label = PLAN_LABELS.get(raw_plan, raw_plan)

    ref_link = f"{REF_BASE_URL}?start={user_id}"

    return {
        "user_id": user_id,
        "plan": raw_plan,
        "plan_label": plan_label,
        "orb_balance": extra_balance or 0,
        "usage": usage,
        "ref_link": ref_link,
    }


@app.post("/api/create_invoice")
async def api_create_invoice(request: Request):
    """
    Создать инвойс на оплату ORB-пакета из мини-аппа.
    Ожидаем:
      - заголовок X-Telegram-Init-Data
      - JSON: { "pack_code": "mini" | "standard" | ... }
    """
    # 1. Берём init_data из заголовка или query-параметра (на всякий случай)
    init_data = request.headers.get("X-Telegram-Init-Data") or request.query_params.get(
        "init_data"
    )
    if not init_data:
        raise HTTPException(status_code=400, detail="init_data required")

    # 2. Получаем user_id из init_data
    user_id = _get_user_id_from_init_data(init_data)

    # 3. Проверяем, что пользователь есть в БД
    row = get_user(user_id)
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    # 4. Читаем JSON из тела запроса
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")

    pack_code = (data.get("pack_code") or "").strip()
    if not pack_code:
        raise HTTPException(status_code=400, detail="pack_code required")

    pack = ORB_PACKS.get(pack_code)
    if not pack:
        raise HTTPException(status_code=400, detail="unknown pack_code")

    # 5. Готовим payload для sendInvoice
    payload = {
        "chat_id": user_id,
        "title": pack["title"],
        "description": f"Пакет {pack['orbs']} ORB для @Orbit_AIBot.",
        "provider_token": PAYMENT_PROVIDER_TOKEN,
        "currency": "RUB",
        "prices": [
            {
                "label": pack["title"],
                "amount": pack["amount"],
            }
        ],
        "start_parameter": f"pack_{pack_code}",
        "payload": f"pack:{pack_code}",
    }

    # 6. Вызываем Telegram Bot API
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendInvoice"
    try:
        resp = requests.post(url, json=payload, timeout=10)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"request error: {e}")

    if resp.status_code != 200:
        try:
            err_text = resp.text
        except Exception:
            err_text = "<no text>"
        raise HTTPException(
            status_code=502,
            detail=f"telegram error {resp.status_code}: {err_text}",
        )

    return {"ok": True}


# settings оставляем как есть, мини-апп их просто не использует

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")
