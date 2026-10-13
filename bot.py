import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qsl

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
OPERATOR_IDS = {int(x.strip()) for x in os.getenv("OPERATOR_IDS", "").split(",") if x.strip().isdigit()}
STATIC_INDEX = Path(__file__).parent / "static" / "index.html"
CATEGORIES = ("Kartridze", "Razki", "Liquidy")

if not BOT_TOKEN:
    raise RuntimeError("Brak BOT_TOKEN. Ustaw BOT_TOKEN w Render → Environment.")
if not MINI_APP_URL.startswith("https://"):
    raise RuntimeError("Brak poprawnego MINI_APP_URL. Ustaw pełny adres HTTPS usługi Render.")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
app = FastAPI(title="Sklep Telegram")


def connect_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with connect_db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            created_day TEXT NOT NULL,
            city TEXT NOT NULL,
            products TEXT NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            price REAL NOT NULL DEFAULT 0,
            photo TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        count = con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        if count == 0:
            initial = [
                ("🍋 🍇 ELFLIQ - Blue Razz Lemonade", "Liquidy", 0, "https://www.liqua-online.com/2470-default_md/elfliq-blue-razz-lemonade-elfbar.webp"),
                ("🍇 🌹 🌿 ELFLIQ - Blueberry Rose Mint", "Liquidy", 0, "https://www.liqua-online.com/2478-default_md/elfliq-blueberry-elfbar.webp"),
                ("🥤 ELFLIQ - Cola", "Liquidy", 0, "https://www.liqua-online.com/2512-default_md/elfliq-cola-elfbar.webp"),
                ("🌿 ❄️ ELFLIQ - Cool Mint", "Liquidy", 0, "https://www.liqua-online.com/2466-default_md/elfliq-spearmint-elfbar.webp"),
                ("🦬 ⚡ 🧊 ELFBULL Ice", "Liquidy", 0, "https://www.liqua-online.com/2489-default_md/elfliq-elfbull-ice-elfbar.webp"),
                ("🍋 🟢 ELFLIQ - Lemon Lime", "Liquidy", 0, "https://www.liqua-online.com/2500-default_md/elfliq-lemon-lime-elfbar.webp"),
                ("🍓 ❄️ ELFLIQ - Strawberry Snow", "Liquidy", 0, "https://www.liqua-online.com/2453-default_md/elfliq-strawberry-ice-elfbar.webp"),
            ]
            con.executemany("INSERT INTO products (name,category,price,photo) VALUES (?,?,?,?)", initial)


def validate_init_data(init_data: str) -> dict:
    if not init_data:
        raise HTTPException(status_code=401, detail="Otwórz sklep przez Telegram.")
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
        raise HTTPException(status_code=401, detail="Dane Telegrama wygasły. Otwórz sklep ponownie.")
    try:
        user = json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=401, detail="Nieprawidłowe dane użytkownika.")
    if not user.get("id"):
        raise HTTPException(status_code=401, detail="Nie znaleziono użytkownika Telegrama.")
    return user


def require_operator(init_data: str) -> dict:
    user = validate_init_data(init_data)
    if int(user["id"]) not in OPERATOR_IDS:
        raise HTTPException(status_code=403, detail="Brak uprawnień operatora.")
    return user


class OrderRequest(BaseModel):
    init_data: str
    city: str
    product_ids: list[int] = Field(min_length=1, max_length=50)
    age_confirmed: bool = False


class ProductCreate(BaseModel):
    init_data: str
    name: str = Field(min_length=1, max_length=160)
    category: str
    price: float = Field(ge=0, le=100000)
    photo: str = Field(default="", max_length=1000)


class ProductDelete(BaseModel):
    init_data: str


@dp.message(CommandStart())
async def start(message: Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛍️ Otwórz sklep", web_app=WebAppInfo(url=MINI_APP_URL))
    ]])
    await message.answer("Cześć! Witaj w sklepie. Kliknij przycisk poniżej, aby zobaczyć kategorie i produkty.", reply_markup=keyboard)


@dp.message(Command("myid"))
async def myid(message: Message):
    await message.answer(f"Twoje numeryczne Telegram ID: {message.from_user.id}")


@dp.message(Command("health"))
async def health(message: Message):
    await message.answer("Bot działa.")


@app.get("/")
async def home():
    if not STATIC_INDEX.exists():
        raise HTTPException(status_code=500, detail="Brak pliku static/index.html.")
    return FileResponse(str(STATIC_INDEX))


@app.get("/api/health")
async def api_health():
    return {"ok": True}


@app.get("/api/operator/check")
async def operator_check(x_telegram_init_data: str = ""):
    user = require_operator(x_telegram_init_data)
    return {"ok": True, "operator_id": int(user["id"])}


@app.get("/api/products")
async def get_products():
    with connect_db() as con:
        rows = con.execute("SELECT id,name,category,price,photo FROM products WHERE active=1 ORDER BY id").fetchall()
    return {"products": [dict(r) for r in rows], "categories": list(CATEGORIES)}


@app.post("/api/operator/products")
async def add_product(product: ProductCreate):
    require_operator(product.init_data)
    if product.category not in CATEGORIES:
        raise HTTPException(status_code=400, detail="Wybierz prawidłową kategorię.")
    photo = product.photo.strip()
    if photo and not photo.startswith(("https://", "http://")):
        raise HTTPException(status_code=400, detail="Zdjęcie musi być adresem http:// lub https://.")
    with connect_db() as con:
        cur = con.execute(
            "INSERT INTO products(name,category,price,photo,active) VALUES(?,?,?,?,1)",
            (product.name.strip(), product.category, product.price, photo)
        )
        new_id = cur.lastrowid
    return {"ok": True, "id": new_id}


@app.delete("/api/operator/products/{product_id}")
async def delete_product(product_id: int, init_data: str):
    require_operator(init_data)
    with connect_db() as con:
        cur = con.execute("UPDATE products SET active=0 WHERE id=? AND active=1", (product_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Nie znaleziono aktywnego produktu.")
    return {"ok": True}


@app.post("/api/order")
async def create_order(order: OrderRequest):
    user = validate_init_data(order.init_data)
    if not order.age_confirmed:
        raise HTTPException(status_code=400, detail="Potwierdź wymagany wiek.")
    if not order.city.strip():
        raise HTTPException(status_code=400, detail="Wybierz miasto.")
    ids = list(dict.fromkeys(order.product_ids))
    if not ids or len(ids) > 50:
        raise HTTPException(status_code=400, detail="Lista produktów jest nieprawidłowa.")
    placeholders = ",".join("?" for _ in ids)
    with connect_db() as con:
        rows = con.execute(
            f"SELECT id,name FROM products WHERE active=1 AND id IN ({placeholders})", ids
        ).fetchall()
        by_id = {int(r["id"]): r["name"] for r in rows}
        if len(by_id) != len(ids):
            raise HTTPException(status_code=400, detail="Jeden z produktów jest już niedostępny.")
        names = [by_id[i] for i in ids]
        day = time.strftime("%Y-%m-%d", time.gmtime())
        count = con.execute(
            "SELECT COUNT(*) FROM orders WHERE user_id=? AND created_day=?",
            (int(user["id"]), day)
        ).fetchone()[0]
        if count >= 2:
            raise HTTPException(status_code=429, detail="Dzienny limit zamówień został osiągnięty.")
        cur = con.execute(
            "INSERT INTO orders (user_id, created_day, city, products) VALUES (?, ?, ?, ?)",
            (int(user["id"]), day, order.city.strip()[:100], "\n".join(names))
        )
        order_id = cur.lastrowid

    if COURIER_CHAT_ID:
        first, last = user.get("first_name", ""), user.get("last_name", "")
        username = ("@" + user["username"]) if user.get("username") else "(brak username)"
        message_text = (
            f"🛒 NOWE ZAMÓWIENIE #{order_id}\n\nKlient: {first} {last}\n"
            f"Telegram: {username}\nID klienta: {user['id']}\nMiasto: {order.city.strip()}\n\nProdukty:\n"
            + "\n".join("• " + n for n in names)
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
