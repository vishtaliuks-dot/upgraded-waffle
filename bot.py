import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
import time
from urllib.parse import parse_qsl
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
import uvicorn

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MINI_APP_URL = os.getenv("MINI_APP_URL", "").strip().rstrip("/")
COURIER_USERNAME = os.getenv("COURIER_USERNAME", "florida_dostawa").strip().lstrip("@")
COURIER_CHAT_ID = os.getenv("COURIER_CHAT_ID", "").strip()
DB_PATH = os.getenv("DB_PATH", "sklep.db")
STATIC_INDEX = Path(__file__).parent / "static" / "index.html"

if not BOT_TOKEN:
    raise RuntimeError("Brak BOT_TOKEN. Ustaw BOT_TOKEN w Render → Environment.")
if not MINI_APP_URL.startswith("https://"):
    raise RuntimeError("Brak poprawnego MINI_APP_URL. Ustaw pełny adres HTTPS usługi Render.")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
app = FastAPI(title="Sklep Telegram")

PRODUCTS = {
    "1": "🍋 🍇 ELFLIQ - Blue Razz Lemonade",
    "2": "🍇 🌹 🌿 ELFLIQ - Blueberry Rose Mint",
    "3": "🥤 ELFLIQ - Cola",
    "4": "🌿 ❄️ ELFLIQ - Cool Mint",
    "5": "🦬 ⚡ 🧊 ELFBULL Ice",
    "6": "🍋 🟢 ELFLIQ - Lemon Lime",
    "7": "🍓 ❄️ ELFLIQ - Strawberry Snow",
}
VALID_PRODUCT_IDS = set(PRODUCTS) | {f"p{i}" for i in range(1, 8)}

def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            created_day TEXT NOT NULL,
            city TEXT NOT NULL,
            products TEXT NOT NULL
        )""")

def validate_init_data(init_data: str) -> dict:
    if not init_data:
        raise HTTPException(status_code=401, detail="Brak danych Telegrama. Otwórz sklep przez Telegram.")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Nieprawidłowe dane Telegrama.")
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise HTTPException(status_code=401, detail="Nie udało się potwierdzić danych Telegrama.")
    try:
        auth_date = int(pairs.get("auth_date", "0") or "0")
    except ValueError:
        auth_date = 0
    if not auth_date or abs(time.time() - auth_date) > 86400:
        raise HTTPException(status_code=401, detail="Dane Telegrama wygasły. Zamknij i otwórz sklep ponownie.")
    try:
        user = json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=401, detail="Nieprawidłowe dane użytkownika.")
    if not user.get("id"):
        raise HTTPException(status_code=401, detail="Nie znaleziono użytkownika Telegrama.")
    return user

class OrderRequest(BaseModel):
    init_data: str
    city: str
    product_ids: list[str] = Field(min_length=1, max_length=7)
    age_confirmed: bool = False

@dp.message(CommandStart())
async def start(message: Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛍️ Otwórz sklep", web_app=WebAppInfo(url=MINI_APP_URL))
    ]])
    await message.answer("Cześć! Witamy w sklepie. Kliknij przycisk poniżej, aby zobaczyć produkty.", reply_markup=keyboard)

@dp.message(Command("myid"))
async def myid(message: Message):
    await message.answer(f"Twoje numeryczne Telegram ID: {message.from_user.id}")

@dp.message(Command("health"))
async def health(message: Message):
    await message.answer("Bot działa.")

@app.get("/")
async def home():
    if not STATIC_INDEX.exists():
        raise HTTPException(status_code=500, detail="Brak pliku static/index.html w repozytorium.")
    return FileResponse(str(STATIC_INDEX))

@app.get("/api/health")
async def api_health():
    return {"ok": True}

@app.post("/api/order")
async def create_order(order: OrderRequest):
    user = validate_init_data(order.init_data)
    if not order.age_confirmed:
        raise HTTPException(status_code=400, detail="Potwierdź wymagany wiek.")
    if not order.city.strip():
        raise HTTPException(status_code=400, detail="Wybierz miasto.")
    # Mini App używa ID p1-p7, a starsza wersja mogła wysyłać 1-7.
    normalized_ids = []
    for raw_id in order.product_ids:
        pid = str(raw_id).strip()
        if pid.startswith("p") and pid[1:].isdigit():
            pid = pid[1:]
        normalized_ids.append(pid)
    if (not normalized_ids or len(normalized_ids) > 7
            or len(set(normalized_ids)) != len(normalized_ids)
            or any(pid not in PRODUCTS for pid in normalized_ids)):
        raise HTTPException(status_code=400, detail="Lista produktów jest nieprawidłowa.")
    city = order.city.strip()[:100]
    day = time.strftime("%Y-%m-%d", time.gmtime())
    with sqlite3.connect(DB_PATH) as con:
        count = con.execute(
            "SELECT COUNT(*) FROM orders WHERE user_id=? AND created_day=?",
            (int(user["id"]), day)
        ).fetchone()[0]
        if count >= 2:
            raise HTTPException(status_code=429, detail="Dzienny limit zamówień został osiągnięty.")
        names = [PRODUCTS[pid] for pid in normalized_ids]
        cur = con.execute(
            "INSERT INTO orders (user_id, created_day, city, products) VALUES (?, ?, ?, ?)",
            (int(user["id"]), day, city, "\n".join(names))
        )
        order_id = cur.lastrowid

    if COURIER_CHAT_ID:
        first = user.get("first_name", "")
        last = user.get("last_name", "")
        username = ("@" + user["username"]) if user.get("username") else "(brak username)"
        message_text = (
            f"🛒 NOWE ZAMÓWIENIE #{order_id}\n\n"
            f"Klient: {first} {last}\nTelegram: {username}\nID klienta: {user['id']}\n"
            f"Miasto: {city}\n\nProdukty:\n" + "\n".join("• " + n for n in names)
        )
        try:
            await bot.send_message(int(COURIER_CHAT_ID), message_text)
        except Exception as exc:
            print(f"Nie udało się powiadomić kuriera: {exc!r}")
    else:
        print("UWAGA: COURIER_CHAT_ID nie ustawiono; zamówienie zapisano, ale kurier nie dostał wiadomości.")

    return {"ok": True, "order_id": order_id, "courier_url": f"https://t.me/{COURIER_USERNAME}"}

async def run_web():
    config = uvicorn.Config(app, host="0.0.0.0", port=int(os.getenv("PORT", "10000")), log_level="info")
    await uvicorn.Server(config).serve()

async def main():
    init_db()
    try:
        await bot.set_chat_menu_button(
            menu_button={"type": "web_app", "text": "Sklep", "web_app": {"url": MINI_APP_URL}}
        )
    except Exception as exc:
        print(f"Nie udało się ustawić przycisku menu: {exc!r}")
    await asyncio.gather(dp.start_polling(bot), run_web())

if __name__ == "__main__":
    asyncio.run(main())
