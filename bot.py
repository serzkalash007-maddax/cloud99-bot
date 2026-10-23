# -*- coding: utf-8 -*-
"""
CLOUD99 — Telegram-бот + WebApp магазин.
База данных: Supabase (PostgreSQL) через psycopg.
"""

import asyncio, os, logging, traceback
from pathlib import Path
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo, ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.client.default import DefaultBotProperties
import psycopg
from psycopg_pool import AsyncConnectionPool

BOT_TOKEN  = os.environ.get("BOT_TOKEN", "")
ADMIN_ID   = int(os.environ.get("ADMIN_ID", "1218585792"))
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
PORT       = int(os.environ.get("PORT", 8080))
HOST       = "0.0.0.0"

DISCOUNT_PERCENT = 15
DISCOUNT_MIN_SUM = 35.0

IMAGES_DIR = Path("images"); IMAGES_DIR.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("cloud99")

if not BOT_TOKEN: raise SystemExit("BOT_TOKEN не задан!")
if not DATABASE_URL: raise SystemExit("DATABASE_URL не задан!")

# ============================================================
#                       БАЗА ДАННЫХ (Postgres)
# ============================================================
pool = None

async def get_pool():
    global pool
    if pool is None:
        # Заменяем префикс для psycopg
        dsn = DATABASE_URL.replace("postgresql+psycopg://", "postgresql://")
        pool = AsyncConnectionPool(dsn, min_size=1, max_size=5, open=False)
        await pool.open()
    return pool

async def init_db():
    p = await get_pool()
    async with p.connection() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS categories(
            id SERIAL PRIMARY KEY,
            title TEXT UNIQUE NOT NULL,
            emoji TEXT DEFAULT '',
            sort  INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS products(
            id SERIAL PRIMARY KEY,
            category_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            price REAL NOT NULL,
            image TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS orders(
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            username TEXT,
            items TEXT,
            total REAL,
            status TEXT DEFAULT 'new',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS admins(
            user_id BIGINT PRIMARY KEY,
            username TEXT
        );
        CREATE TABLE IF NOT EXISTS stickers(
            user_id BIGINT PRIMARY KEY,
            count INTEGER DEFAULT 0,
            discount_active INTEGER DEFAULT 0
        );
        """)
        for t, e, s in [("ЖИДКОСТИ","💧",1), ("РАСХОДНИКИ","🔧",2), ("ПОДЫ","💨",3)]:
            await conn.execute(
                "INSERT INTO categories(title,emoji,sort) VALUES(%s,%s,%s) ON CONFLICT (title) DO NOTHING",
                (t, e, s)
            )
        await conn.commit()
    log.info("DB initialized (Supabase via psycopg)")

async def get_categories():
    p = await get_pool()
    async with p.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            await cur.execute("SELECT * FROM categories ORDER BY sort, id")
            return await cur.fetchall()

async def get_products(cat_id=None):
    p = await get_pool()
    async with p.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            if cat_id:
                await cur.execute("SELECT * FROM products WHERE category_id=%s ORDER BY id DESC", (cat_id,))
            else:
                await cur.execute("SELECT * FROM products ORDER BY id DESC")
            return await cur.fetchall()

async def add_product(cat_id, title, desc, price, image):
    p = await get_pool()
    async with p.connection() as conn:
        await conn.execute(
            "INSERT INTO products(category_id,title,description,price,image) VALUES(%s,%s,%s,%s,%s)",
            (cat_id, title, desc, price, image)
        )
        await conn.commit()

async def update_product(pid, field, value):
    if field not in {"title","description","price","image","category_id"}: return
    p = await get_pool()
    async with p.connection() as conn:
        await conn.execute(f"UPDATE products SET {field}=%s WHERE id=%s", (value, pid))
        await conn.commit()

async def delete_product(pid):
    p = await get_pool()
    async with p.connection() as conn:
        await conn.execute("DELETE FROM products WHERE id=%s", (pid,))
        await conn.commit()

async def create_order(uid, uname, items, total):
    p = await get_pool()
    async with p.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO orders(user_id,username,items,total) VALUES(%s,%s,%s,%s) RETURNING id",
                (uid, uname, items, total)
            )
            row = await cur.fetchone()
            await conn.commit()
            return row[0]

async def update_order_status(oid, status):
    p = await get_pool()
    async with p.connection() as conn:
        await conn.execute("UPDATE orders SET status=%s WHERE id=%s", (status, oid))
        await conn.commit()

async def get_orders(uid=None):
    p = await get_pool()
    async with p.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            if uid:
                await cur.execute("SELECT * FROM orders WHERE user_id=%s ORDER BY id DESC", (uid,))
            else:
                await cur.execute("SELECT * FROM orders ORDER BY id DESC")
            return await cur.fetchall()

async def is_admin(uid):
    if uid == ADMIN_ID: return True
    p = await get_pool()
    async with p.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1 FROM admins WHERE user_id=%s", (uid,))
            return await cur.fetchone() is not None

async def add_admin(uid, uname):
    p = await get_pool()
    async with p.connection() as conn:
        await conn.execute(
            "INSERT INTO admins(user_id,username) VALUES(%s,%s) ON CONFLICT (user_id) DO NOTHING",
            (uid, uname or "")
        )
        await conn.commit()

async def get_user_stats(uid):
    p = await get_pool()
    async with p.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT COUNT(*) FROM orders WHERE user_id=%s AND status IN ('confirmed','shipping','ready')", (uid,)
            )
            orders_count = (await cur.fetchone())[0]
            
            await cur.execute("SELECT count, discount_active FROM stickers WHERE user_id=%s", (uid,))
            st = await cur.fetchone()
            if st:
                stickers, discount = st[0], st[1]
            else:
                stickers, discount = 0, 0
            return {"orders": orders_count or 0, "stickers": stickers, "discount_active": bool(discount)}

async def add_sticker(uid):
    p = await get_pool()
    async with p.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT count FROM stickers WHERE user_id=%s", (uid,))
            st = await cur.fetchone()
            if not st:
                await cur.execute("INSERT INTO stickers(user_id,count) VALUES(%s,1)", (uid,))
                await conn.commit()
                return 1, False
            count = st[0] + 1
            await cur.execute("UPDATE stickers SET count=%s WHERE user_id=%s", (count, uid))
            await conn.commit()
            return count, count >= 10

async def reset_stickers(uid):
    p = await get_pool()
    async with p.connection() as conn:
        await conn.execute("""
            INSERT INTO stickers(user_id, count, discount_active) VALUES(%s, 0, 1)
            ON CONFLICT(user_id) DO UPDATE SET count=0, discount_active=1
        """, (uid,))
        await conn.commit()

async def use_discount(uid):
    p = await get_pool()
    async with p.connection() as conn:
        await conn.execute("UPDATE stickers SET discount_active=0 WHERE user_id=%s", (uid,))
        await conn.commit()

# ============================================================
#                       БОТ
# ============================================================
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp  = Dispatcher()

class AddP(StatesGroup):
    cat=State(); title=State(); desc=State(); price=State(); photo=State()
class EditP(StatesGroup):
    val=State()

def admin_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="➕ Добавить товар")],
        [KeyboardButton(text="✏️ Редактировать")],
        [KeyboardButton(text="🗑 Удалить")],
        [KeyboardButton(text="📦 Заказы")],
        [KeyboardButton(text="⬅️ Выйти")]
    ], resize_keyboard=True)

@dp.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    u = m.from_user
    if u.id == ADMIN_ID: await add_admin(u.id, u.username)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛒 Открыть магазин", web_app=WebAppInfo(url=PUBLIC_URL or "https://example.com"))
    ]])
    await m.answer(f"<b>CLOUD99</b>\n\nПривет, {u.first_name}! Открой магазин 👇", reply_markup=kb)

@dp.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Отменено.", reply_markup=admin_menu() if await is_admin(m.from_user.id) else None)

@dp.message(Command("admin"))
async def cmd_admin(m: Message, state: FSMContext):
    await state.clear()
    if not await is_admin(m.from_user.id): return await m.answer("Нет доступа.")
    await m.answer("Админ-панель CLOUD99:", reply_markup=admin_menu())

@dp.message(Command("reset_stickers"))
async def cmd_reset_stickers(m: Message):
    if not await is_admin(m.from_user.id): return
    parts = m.text.split()
    if len(parts) < 2: return await m.answer("Использование: <code>/reset_stickers USER_ID</code>")
    try: uid = int(parts[1])
    except ValueError: return await m.answer("ID должен быть числом.")
    await reset_stickers(uid)
    await m.answer(f"✅ Стикеры клиента <code>{uid}</code> обнулены.")
    try:
        await bot.send_message(uid, f"🎉 <b>Вы собрали 10 отметок!</b>\n\nСкидка <b>{DISCOUNT_PERCENT}%</b> на заказ от <b>{DISCOUNT_MIN_SUM:.0f} BYN</b> активирована.")
    except: pass

@dp.message(F.text == "⬅️ Выйти")
async def admin_exit(m: Message, state: FSMContext):
    await state.clear(); await m.answer("Ок.", reply_markup=None)

@dp.message(F.text == "➕ Добавить товар")
async def add_begin(m: Message):
    if not await is_admin(m.from_user.id): return
    cats = await get_categories()
    if not cats: return await m.answer("Нет категорий.")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{c['emoji']} {c['title']}", callback_data=f"ac:{c['id']}")] for c in cats])
    await m.answer("Категория?", reply_markup=kb)

@dp.callback_query(F.data.startswith("ac:"))
async def add_cat(c: CallbackQuery, state: FSMContext):
    await state.update_data(cat=int(c.data.split(":")[1]))
    await state.set_state(AddP.title)
    await c.message.edit_text("Название товара:")

@dp.message(AddP.title)
async def add_title(m: Message, state: FSMContext):
    await state.update_data(title=m.text.strip()); await state.set_state(AddP.desc); await m.answer("Описание (или «-»):")

@dp.message(AddP.desc)
async def add_desc(m: Message, state: FSMContext):
    await state.update_data(desc="" if m.text.strip()=="-" else m.text.strip()); await state.set_state(AddP.price); await m.answer("Цена (число):")

@dp.message(AddP.price)
async def add_price(m: Message, state: FSMContext):
    try: p = float(m.text.replace(",", ".").strip())
    except: return await m.answer("Не число.")
    await state.update_data(price=p); await state.set_state(AddP.photo); await m.answer("Фото (или «-»):")

@dp.message(AddP.photo, F.photo | F.text)
async def add_photo(m: Message, state: FSMContext):
    image = ""
    if m.photo:
        f = await bot.get_file(m.photo[-1].file_id)
        image = f"images/{m.photo[-1].file_id}.jpg"
        await bot.download_file(f.file_path, image)
    d = await state.get_data()
    await add_product(d["cat"], d["title"], d["desc"], d["price"], image)
    await m.answer(f"✅ «{d['title']}» добавлен."); await state.clear()

@dp.message(F.text == "✏️ Редактировать")
async def edit_list(m: Message):
    if not await is_admin(m.from_user.id): return
    ps = await get_products()
    if not ps: return await m.answer("Товаров нет.")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"#{p['id']} {p['title']}", callback_data=f"e:{p['id']}")] for p in ps])
    await m.answer("Выбери:", reply_markup=kb)

@dp.callback_query(F.data.startswith("e:"))
async def edit_field(c: CallbackQuery):
    pid = c.data.split(":")[1]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Название", callback_data=f"ef:{pid}:title")],
        [InlineKeyboardButton(text="Описание", callback_data=f"ef:{pid}:description")],
        [InlineKeyboardButton(text="Цена", callback_data=f"ef:{pid}:price")],
        [InlineKeyboardButton(text="Фото", callback_data=f"ef:{pid}:image")],
    ])
    await c.message.edit_text("Что меняем?", reply_markup=kb)

@dp.callback_query(F.data.startswith("ef:"))
async def edit_prompt(c: CallbackQuery, state: FSMContext):
    _, pid, field = c.data.split(":"); await state.update_data(pid=int(pid), field=field); await state.set_state(EditP.val)
    await c.message.edit_text(f"Новое ({field}):")

@dp.message(EditP.val, F.photo | F.text)
async def edit_save(m: Message, state: FSMContext):
    d = await state.get_data(); field, pid = d["field"], d["pid"]
    if field == "image":
        if m.photo:
            f = await bot.get_file(m.photo[-1].file_id); val = f"images/{m.photo[-1].file_id}.jpg"; await bot.download_file(f.file_path, val)
        else: val = ""
    elif field == "price": val = float(m.text.replace(",", "."))
    else: val = m.text.strip()
    await update_product(pid, field, val)
    await m.answer("✅ Обновлено."); await state.clear()

@dp.message(F.text == "🗑 Удалить")
async def del_list(m: Message):
    if not await is_admin(m.from_user.id): return
    ps = await get_products()
    if not ps: return await m.answer("Товаров нет.")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"🗑 #{p['id']} {p['title']}", callback_data=f"d:{p['id']}")] for p in ps])
    await m.answer("Что удалить?", reply_markup=kb)

@dp.callback_query(F.data.startswith("d:"))
async def del_do(c: CallbackQuery):
    await delete_product(int(c.data.split(":")[1])); await c.message.edit_text("🗑 Удалено.")

@dp.message(F.text == "📦 Заказы")
async def orders(m: Message):
    if not await is_admin(m.from_user.id): return
    os_ = await get_orders()
    if not os_: return await m.answer("Заказов нет.")
    for o in os_[:15]:
        status = o.get('status','new')
        if status == 'new':
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm:{o['id']}:{o['user_id']}"),
                InlineKeyboardButton(text="🚚 В пути", callback_data=f"shipping:{o['id']}:{o['user_id']}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{o['id']}:{o['user_id']}"),
            ]])
        else: kb = None
        await m.answer(f"<b>Заказ #{o['id']}</b> [{status}]\n👤 {o['username']}\n📦 {o['items']}\n💰 {o['total']:.2f} BYN", reply_markup=kb)

# ---------- СТАТУСЫ ----------
@dp.callback_query(F.data.startswith("confirm:"))
async def order_confirm(c: CallbackQuery):
    _, oid, uid = c.data.split(":"); oid = int(oid); uid = int(uid)
    await update_order_status(oid, "confirmed")
    new_count, reached_10 = await add_sticker(uid)
    try: await bot.send_message(uid, f"✅ <b>Заказ #{oid} подтверждён!</b>")
    except: pass
    await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer(f"✅ Заказ #{oid} подтверждён. Стикеров: <b>{new_count}/10</b>.")
    if reached_10:
        try: await bot.send_message(ADMIN_ID, f"🎯 Клиент набрал 10 стикеров!\n/reset_stickers {uid}")
        except: pass

@dp.callback_query(F.data.startswith("shipping:"))
async def order_shipping(c: CallbackQuery):
    _, oid, uid = c.data.split(":"); oid = int(oid); uid = int(uid)
    await update_order_status(oid, "shipping")
    try: await bot.send_message(uid, f"🚚 <b>Заказ #{oid} в пути!</b>")
    except: pass
    await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer(f"🚚 Заказ #{oid} в пути.")

@dp.callback_query(F.data.startswith("reject:"))
async def order_reject(c: CallbackQuery):
    _, oid, uid = c.data.split(":"); oid = int(oid); uid = int(uid)
    await update_order_status(oid, "rejected")
    try: await bot.send_message(uid, f"❌ <b>Заказ #{oid} отклонён.</b>")
    except: pass
    await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer(f"❌ Заказ #{oid} отклонён.")

@dp.error()
async def global_error(event, exception):
    log.error(f"ERROR: {exception}\n{traceback.format_exc()}")

HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>CLOUD99</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{
  --bg:#1a1d16; --card:#242820; --card2:#2c3026;
  --olive:#4a5535; --olive2:#5d6b42; --sand:#c9b487;
  --accent:#8b9556; --accent2:#a8b56a;
  --text:#e8e6d8; --muted:#8a8a72;
  --sd:#0d0f0a; --sl:#363a2e; --r:14px;
  --danger:#a04a3a;
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--text);
  font-family:"Courier New",Courier,monospace;padding-bottom:90px;min-height:100vh;
  background-image:
    linear-gradient(rgba(139,149,86,.04) 1px, transparent 1px),
    linear-gradient(90deg, rgba(139,149,86,.04) 1px, transparent 1px);
  background-size:20px 20px;}
.topbar{display:flex;justify-content:space-between;align-items:center;
  padding:14px 16px;position:sticky;top:0;background:rgba(26,29,22,.95);
  backdrop-filter:blur(10px);z-index:20;border-bottom:2px solid var(--olive)}
.logo{font-weight:900;letter-spacing:3px;font-size:18px;color:var(--sand);
  text-transform:uppercase;text-shadow:1px 1px 0 var(--sd)}
.logo:before{content:"◤ ";color:var(--accent2)}
.topbar-right{display:flex;gap:8px;align-items:center}
.profile-btn,.cart-btn{background:var(--card);padding:10px 14px;border-radius:8px;
  box-shadow:3px 3px 0 var(--sd);cursor:pointer;border:1px solid var(--olive);
  color:var(--text);font-family:inherit;font-weight:700}
.cart-btn span{color:var(--accent2)}
.badge{position:absolute;top:-6px;right:-6px;background:var(--danger);color:#fff;
  border-radius:50%;width:22px;height:22px;font-size:12px;display:flex;
  align-items:center;justify-content:center;font-weight:900}
.cart-btn{position:relative}
.cats{display:flex;gap:8px;overflow-x:auto;padding:14px 16px;scrollbar-width:none}
.cats::-webkit-scrollbar{display:none}
.cat{flex:0 0 auto;padding:10px 18px;border-radius:6px;background:var(--card);
  font-weight:800;font-size:12px;letter-spacing:1px;cursor:pointer;
  box-shadow:3px 3px 0 var(--sd);border:1px solid var(--olive);
  color:var(--text);text-transform:uppercase;font-family:inherit}
.cat.active{background:var(--olive);color:var(--sand);border-color:var(--sand)}
.cat:active{transform:translate(2px,2px);box-shadow:1px 1px 0 var(--sd)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:0 16px}
.card{background:var(--card);border-radius:var(--r);padding:12px;
  box-shadow:4px 4px 0 var(--sd);border:1px solid var(--olive);
  display:flex;flex-direction:column;gap:8px;position:relative}
.card:before{content:"";position:absolute;top:6px;right:8px;width:6px;height:6px;
  background:var(--accent);border-radius:50%}
.card img{width:100%;aspect-ratio:1/1;object-fit:cover;border-radius:8px;
  background:var(--card2);border:1px solid var(--olive)}
.ph{width:100%;aspect-ratio:1/1;border-radius:8px;background:var(--card2);
  display:flex;align-items:center;justify-content:center;color:var(--muted);
  font-size:40px;border:1px solid var(--olive)}
.card .t{font-size:13px;font-weight:700;line-height:1.3;min-height:34px}
.card .p{font-size:15px;font-weight:900;color:var(--sand)}
.btn{border:none;border-radius:8px;padding:11px 14px;font-weight:800;
  font-size:12px;cursor:pointer;letter-spacing:.5px;text-transform:uppercase;
  font-family:inherit;transition:.1s}
.btn.primary{background:var(--olive);color:var(--sand);border:1px solid var(--sand);
  box-shadow:3px 3px 0 var(--sd)}
.btn.primary:active{transform:translate(2px,2px);box-shadow:1px 1px 0 var(--sd)}
.btn.ghost{background:var(--card);color:var(--text);border:1px solid var(--olive);
  box-shadow:3px 3px 0 var(--sd)}
.btn.ghost:active{transform:translate(2px,2px);box-shadow:1px 1px 0 var(--sd)}
.add{margin-top:auto}
.modal{position:fixed;inset:0;background:rgba(10,12,8,.85);display:flex;
  align-items:flex-end;justify-content:center;z-index:100;backdrop-filter:blur(6px)}
.modal.hidden{display:none}
.sheet{background:var(--card);border-radius:14px 14px 0 0;padding:22px;
  width:100%;max-height:85vh;overflow-y:auto;border-top:3px solid var(--olive);
  animation:up .2s}
@keyframes up{from{transform:translateY(40px)}to{transform:translateY(0)}}
.sheet h2{margin:0 0 16px;font-size:18px;color:var(--sand);
  text-transform:uppercase;letter-spacing:2px;
  border-bottom:1px dashed var(--olive);padding-bottom:10px}
.ci{display:flex;justify-content:space-between;align-items:center;
  padding:12px 0;border-bottom:1px dashed var(--olive);gap:10px}
.ci .info{flex:1}
.ci .info b{display:block;font-size:13px}
.ci .info span{font-size:12px;color:var(--muted)}
.qty{display:flex;gap:6px;align-items:center}
.qty button{width:30px;height:30px;border-radius:6px;
  border:1px solid var(--olive);background:var(--card2);color:var(--sand);
  font-size:16px;font-weight:900;cursor:pointer;font-family:inherit}
.qty span{min-width:20px;text-align:center;font-weight:800}
.total{font-size:16px;margin:18px 0;padding-top:16px;
  border-top:2px solid var(--olive);display:flex;justify-content:space-between;
  text-transform:uppercase;letter-spacing:1px}
.total b{color:var(--sand)}
.discount-line{color:var(--sand);font-size:13px;margin-top:8px;
  padding:8px;background:var(--olive);border-radius:6px;
  text-transform:uppercase;letter-spacing:1px;font-weight:800}
.empty{text-align:center;color:var(--muted);padding:40px 0;font-style:italic}
.toast{position:fixed;bottom:100px;left:50%;transform:translateX(-50%);
  background:var(--olive);color:var(--sand);padding:12px 22px;border-radius:8px;
  font-weight:700;z-index:200;border:1px solid var(--sand);
  box-shadow:3px 3px 0 var(--sd);text-transform:uppercase;font-size:12px;
  letter-spacing:1px;font-family:inherit}
.profile-card{background:var(--card2);border-radius:10px;padding:16px;
  box-shadow:3px 3px 0 var(--sd);margin-bottom:14px;border:1px solid var(--olive)}
.avatar{width:60px;height:60px;border-radius:8px;background:var(--olive);
  display:flex;align-items:center;justify-content:center;font-size:26px;
  font-weight:900;color:var(--sand);margin-bottom:12px;
  border:2px solid var(--sand);box-shadow:2px 2px 0 var(--sd)}
.profile-name{font-size:17px;font-weight:900;margin-bottom:4px;color:var(--sand);
  text-transform:uppercase;letter-spacing:1px}
.profile-sub{color:var(--muted);font-size:12px;margin-bottom:2px;
  font-family:"Courier New",monospace}
.stickers{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.sticker{width:38px;height:38px;border-radius:6px;background:var(--card);
  display:flex;align-items:center;justify-content:center;font-size:18px;
  border:1px solid var(--olive);color:var(--muted)}
.sticker.filled{background:var(--olive);color:var(--sand);border-color:var(--sand);
  box-shadow:0 0 8px rgba(139,149,86,.5)}
.section-title{font-size:14px;font-weight:900;margin:20px 0 10px;
  color:var(--sand);text-transform:uppercase;letter-spacing:2px;
  border-left:3px solid var(--olive);padding-left:10px}
.order{background:var(--card2);border-radius:8px;padding:14px;margin-bottom:10px;
  border:1px solid var(--olive);box-shadow:3px 3px 0 var(--sd)}
.order b{color:var(--sand)}
.order .meta{color:var(--muted);font-size:11px;font-family:monospace}
.order .status{display:inline-block;padding:2px 8px;border-radius:4px;
  font-size:10px;text-transform:uppercase;letter-spacing:1px;margin-top:4px;
  background:var(--olive);color:var(--sand)}
</style>
</head>
<body>
<div class="topbar">
  <div class="logo">Cloud99</div>
  <div class="topbar-right">
    <div class="profile-btn" onclick="openProfile()">👤</div>
    <div class="cart-btn" onclick="openCart()">
      🛒 <span id="cc">0</span>
      <div class="badge" id="cb" style="display:none">0</div>
    </div>
  </div>
</div>

<div class="cats" id="cats"></div>
<div class="grid" id="grid"><div class="empty">Загрузка...</div></div>

<div class="modal hidden" id="cart" onclick="if(event.target===this)closeCart()">
  <div class="sheet">
    <h2>🛒 Корзина</h2>
    <div id="ci"></div>
    <div class="total">Итого: <b id="ct">0.00 BYN</b></div>
    <div id="discountInfo"></div>
    <button class="btn primary" style="width:100%;padding:15px" onclick="checkout()">Оформить заказ</button>
    <button class="btn ghost" style="width:100%;padding:12px;margin-top:8px" onclick="closeCart()">Закрыть</button>
  </div>
</div>

<div class="modal hidden" id="profile" onclick="if(event.target===this)closeProfile()">
  <div class="sheet">
    <h2>👤 Личный кабинет</h2>
    <div id="profileContent">Загрузка...</div>
    <button class="btn ghost" style="width:100%;padding:12px;margin-top:12px" onclick="closeProfile()">Закрыть</button>
  </div>
</div>

<script>
const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }
const API = location.origin;
let CATS = [], PRODUCTS = [], CART = JSON.parse(localStorage.getItem('c99')||'{}');
let CURRENT_CAT = null, USER_DISCOUNT = false;
const DISCOUNT_PERCENT = 15, DISCOUNT_MIN_SUM = 35;

function saveCart(){ localStorage.setItem('c99', JSON.stringify(CART)); }
function money(n){ return n.toFixed(2)+' BYN'; }
async function api(path, opts){ const r = await fetch(API+path, opts); return r.json(); }

async function loadCats(){
  CATS = await api('/api/categories');
  const box = document.getElementById('cats'); box.innerHTML = '';
  const all = document.createElement('button'); all.className='cat'+(CURRENT_CAT===null?' active':''); all.textContent='ВСЁ';
  all.onclick = ()=>{ CURRENT_CAT=null; loadCats(); loadProducts(); }; box.appendChild(all);
  CATS.forEach(c=>{ const b=document.createElement('button'); b.className='cat'+(CURRENT_CAT===c.id?' active':''); b.textContent=c.title; b.onclick=()=>{ CURRENT_CAT=c.id; loadCats(); loadProducts(); }; box.appendChild(b); });
}

async function loadProducts(){
  const url = CURRENT_CAT ? '/api/products?cat='+CURRENT_CAT : '/api/products';
  PRODUCTS = await api(url);
  const g = document.getElementById('grid');
  if (!PRODUCTS.length){
    g.innerHTML = '<div class="empty" style="grid-column:1/-1">Товаров нет</div>'; return;
  }
  g.innerHTML = PRODUCTS.map(p=>`
    <div class="card">
      ${p.image ? `<img src="/${p.image}" alt="">` : `<div class="ph">📦</div>`}
      <div class="t">${escape(p.title)}</div>
      <div class="p">${money(p.price)}</div>
      <button class="btn primary add" onclick="addToCart(${p.id})">В корзину</button>
    </div>
  `).join('');
}

function escape(s){ return (s||'').replace(/[<>&"]/g, ch=>({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[ch])); }
function addToCart(id){
  const p = PRODUCTS.find(x=>x.id===id);
  if (!p) return;
  if (CART[id]) CART[id].qty++; else CART[id] = {id, title:p.title, price:p.price, qty:1};
  saveCart(); updateBadge(); toast('В корзине ✓');
}

function updateBadge(){
  const n = Object.values(CART).reduce((s,i)=>s+i.qty,0);
  document.getElementById('cc').textContent = n;
  const b = document.getElementById('cb');
  b.style.display = n ? 'flex':'none'; b.textContent = n;
}

async function checkDiscount(){
  const u = tg?.initDataUnsafe?.user; if (!u) return;
  const res = await api('/api/profile/'+u.id);
  USER_DISCOUNT = res.stats?.discount_active || false;
}

function calcTotals(){
  const items = Object.values(CART);
  const subtotal = items.reduce((s,i)=>s+i.price*i.qty,0);
  let discount = 0;
  const eligible = USER_DISCOUNT && subtotal >= DISCOUNT_MIN_SUM;
  if (eligible) discount = subtotal * DISCOUNT_PERCENT / 100;
  return {subtotal, discount, total: subtotal - discount, eligible};
}

async function openCart(){
  await checkDiscount();
  const box = document.getElementById('ci');
  const items = Object.values(CART);
  if (!items.length){ box.innerHTML = '<div class="empty">Корзина пуста</div>'; }
  else {
    box.innerHTML = items.map(i=>`
      <div class="ci">
        <div class="info"><b>${escape(i.title)}</b><span>${money(i.price)} × ${i.qty}</span></div>
        <div class="qty">
          <button onclick="chQty(${i.id},-1)">−</button>
          <span>${i.qty}</span>
          <button onclick="chQty(${i.id},1)">+</button>
        </div>
      </div>
    `).join('');
  }
  const {subtotal, discount, total, eligible} = calcTotals();
  document.getElementById('ct').textContent = money(total);
  const info = document.getElementById('discountInfo');
  if (USER_DISCOUNT && !eligible && items.length){
    info.innerHTML = `<div class="discount-line">Скидка 15% активируется при заказе от ${DISCOUNT_MIN_SUM} BYN</div>`;
  } else if (eligible){
    info.innerHTML = `<div class="discount-line">🎉 Скидка ${DISCOUNT_PERCENT}%: −${money(discount)}</div>`;
  } else { info.innerHTML = ''; }
  document.getElementById('cart').classList.remove('hidden');
}
function closeCart(){ document.getElementById('cart').classList.add('hidden'); }
function chQty(id,d){
  if (!CART[id]) return;
  CART[id].qty += d;
  if (CART[id].qty<=0) delete CART[id];
  saveCart(); updateBadge(); openCart();
}

async function checkout(){
  const items = Object.values(CART); if (!items.length) return toast('Пусто');
  const u = tg?.initDataUnsafe?.user || {id:0, username:'guest'};
  const {subtotal, discount, total, eligible} = calcTotals();
  const res = await api('/api/order', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      user_id: u.id, username: u.username || u.first_name || 'guest',
      items: items.map(i=>({title:i.title, qty:i.qty, price:i.price})),
      subtotal, discount, total, used_discount: eligible
    })
  });
  if (res.ok){
    CART = {}; saveCart(); updateBadge(); closeCart();
    toast('Заказ #'+res.order_id+' принят ✓');
  } else { toast('Ошибка'); }
}

function toast(t){
  const el = document.createElement('div'); el.className = 'toast'; el.textContent = t;
  document.body.appendChild(el); setTimeout(()=>el.remove(), 1800);
}

function statusLabel(s){ return {'new':'Новый','confirmed':'Подтверждён','shipping':'В пути','rejected':'Отклонён','ready':'Готов'}[s] || s; }

async function openProfile(){
  const modal = document.getElementById('profile');
  const box = document.getElementById('profileContent');
  modal.classList.remove('hidden'); box.innerHTML = 'Загрузка...';
  const u = tg?.initDataUnsafe?.user || {id:0, username:'guest', first_name:'Гость'};
  const res = await api('/api/profile/'+u.id);
  const orders = res.orders || [];
  const st = res.stats || {orders:0, stickers:0, discount_active:false};
  const initial = (u.first_name || u.username || 'C')[0].toUpperCase();
  let stickersHtml = '';
  for (let i = 0; i < 10; i++){ stickersHtml += `<div class="sticker${i < st.stickers ? ' filled' : ''}">${i < st.stickers ? '★' : ''}</div>`; }
  let ordersHtml = '';
  if (!orders.length){
    ordersHtml = '<div class="empty">Заказов пока нет</div><button class="btn primary" style="width:100%;margin-top:12px" onclick="closeProfile()">Начать покупку</button>';
  } else {
    ordersHtml = orders.map(o=>`
      <div class="order">
        <b>ЗАКАЗ #${o.id}</b><br>
        <span class="meta">${o.created_at}</span><br>
        📦 ${escape(o.items)}<br>
        💰 <b>${o.total.toFixed(2)} BYN</b><br>
        <span class="status">${statusLabel(o.status)}</span>
      </div>
    `).join('');
  }
  box.innerHTML = `
    <div class="profile-card">
      <div class="avatar">${initial}</div>
      <div class="profile-name">${escape(u.first_name || u.username || 'Гость')}</div>
      <div class="profile-sub">@${escape(u.username || '—')}</div>
      <div class="profile-sub">ID: ${u.id}</div>
      <div class="profile-sub">Подтверждённых: ${st.orders}</div>
    </div>
    <div class="profile-card">
      <div style="font-weight:900;color:var(--sand);text-transform:uppercase;letter-spacing:1px;font-size:13px">10 отметок = скидка 15%</div>
      ${st.discount_active ? '<div style="color:var(--sand);font-weight:800;margin-top:6px">★ СКИДКА АКТИВНА! (от 35 BYN)</div>' : ''}
      <div class="stickers">${stickersHtml}</div>
    </div>
    <div class="section-title">История заказов</div>
    ${ordersHtml}
  `;
}
function closeProfile(){ document.getElementById('profile').classList.add('hidden'); }

loadCats(); loadProducts(); updateBadge();
</script>
</body>
</html>
"""

# ============================================================
#                    HTTP СЕРВЕР И ЗАПУСК
# ============================================================
async def handle_index(req): return web.Response(text=HTML, content_type="text/html")
async def handle_health(req): return web.Response(text="ok")
async def handle_categories(req): return web.json_response(await get_categories())
async def handle_products(req):
    cat = req.query.get("cat")
    return web.json_response(await get_products(int(cat) if cat else None))

async def handle_order(req):
    data = await req.json()
    items_str = "; ".join(f"{i['title']} x{i['qty']}" for i in data["items"])
    uid = data.get("user_id", 0); uname = data.get("username", "guest")
    total = data.get("total", 0); subtotal = data.get("subtotal", total)
    discount = data.get("discount", 0); used_discount = data.get("used_discount", False)
    
    oid = await create_order(uid, uname, items_str, total)
    if used_discount and uid: await use_discount(uid)

    if uid:
        try:
            disc_line = f"🎁 Скидка: −{discount:.2f} BYN\n" if discount > 0 else ""
            await bot.send_message(uid, f"🕒 <b>Заказ #{oid} оформлен!</b>\n\n📦 {items_str}\n💵 Сумма: {subtotal:.2f} BYN\n{disc_line}💰 Итого: <b>{total:.2f} BYN</b>\n\nОжидайте подтверждения.")
        except Exception as e: log.warning(f"notify client {uid}: {e}")

    p = await get_pool()
    async with p.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT user_id FROM admins")
            admins = [r[0] for r in await cur.fetchall()]
    
    disc_line = f"🎁 Скидка: −{discount:.2f} BYN\n" if discount > 0 else ""
    text = (f"🛒 <b>Новый заказ #{oid}</b>\n👤 {uname} (<code>{uid}</code>)\n📦 {items_str}\n{disc_line}💰 <b>{total:.2f} BYN</b>\n\nВыбери действие:")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm:{oid}:{uid}")],
        [InlineKeyboardButton(text="🚚 В пути", callback_data=f"shipping:{oid}:{uid}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{oid}:{uid}")],
        [InlineKeyboardButton(text="💬 Написать покупателю", url=f"tg://user?id={uid}")],
    ])
    for a in admins:
        try: await bot.send_message(a, text, reply_markup=kb)
        except Exception as e: log.warning(f"notify {a}: {e}")
    return web.json_response({"ok": True, "order_id": oid})

async def handle_profile(req):
    uid = int(req.match_info["uid"])
    return web.json_response({"orders": await get_orders(uid), "stats": await get_user_stats(uid)})

async def handle_image(req):
    name = req.match_info["name"]; path = IMAGES_DIR / name
    if not path.exists(): return web.Response(status=404)
    return web.FileResponse(path)

def build_web():
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/categories", handle_categories)
    app.router.add_get("/api/products", handle_products)
    app.router.add_post("/api/order", handle_order)
    app.router.add_get("/api/profile/{uid}", handle_profile)
    app.router.add_get("/images/{name}", handle_image)
    return app

async def keep_alive():
    if not PUBLIC_URL: return
    import aiohttp
    url = PUBLIC_URL.rstrip("/") + "/health"
    while True:
        await asyncio.sleep(600)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=20) as r: log.info(f"keep-alive: {r.status}")
        except Exception as e: log.warning(f"keep-alive error: {e}")

async def main():
    await init_db()
    if not PUBLIC_URL: log.warning("PUBLIC_URL не задан — WebApp-кнопка не откроется.")
    runner = web.AppRunner(build_web()); await runner.setup()
    site = web.TCPSite(runner, HOST, PORT); await site.start()
    log.info(f"WebApp: http://{HOST}:{PORT}")
    asyncio.create_task(keep_alive())
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Bot started, polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): log.info("Остановлено.")
