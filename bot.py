# -*- coding: utf-8 -*-
import asyncio, os, logging, traceback, csv, io
from pathlib import Path
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo, ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile
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
#                       БАЗА ДАННЫХ
# ============================================================
pool = None

async def get_pool():
    global pool
    if pool is None:
        dsn = DATABASE_URL.replace("postgresql+psycopg://", "postgresql://")
        pool = AsyncConnectionPool(dsn, min_size=1, max_size=5, open=False, check=AsyncConnectionPool.check_connection)
        await pool.open()
    return pool

async def init_db():
    p = await get_pool()
    async with p.connection() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS categories(id SERIAL PRIMARY KEY, title TEXT UNIQUE NOT NULL, emoji TEXT DEFAULT '', sort INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS products(
            id SERIAL PRIMARY KEY, category_id INTEGER NOT NULL, title TEXT NOT NULL, 
            description TEXT DEFAULT '', price REAL NOT NULL, image TEXT DEFAULT '', 
            stock INTEGER DEFAULT 10, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS orders(id SERIAL PRIMARY KEY, user_id BIGINT, username TEXT, items TEXT, total REAL, status TEXT DEFAULT 'new', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS admins(user_id BIGINT PRIMARY KEY, username TEXT);
        CREATE TABLE IF NOT EXISTS stickers(user_id BIGINT PRIMARY KEY, count INTEGER DEFAULT 0, discount_active INTEGER DEFAULT 0);
        """)
        await conn.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS stock INTEGER DEFAULT 10;")
        
        for t, e, s in [("ЖИДКОСТИ","💧",1), ("РАСХОДНИКИ","🔧",2), ("ПОДЫ","💨",3)]:
            await conn.execute("INSERT INTO categories(title,emoji,sort) VALUES(%s,%s,%s) ON CONFLICT (title) DO NOTHING", (t, e, s))
        await conn.commit()
    log.info("DB initialized")

# --- Функции базы ---
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

async def add_product(cat_id, title, desc, price, image, stock):
    p = await get_pool()
    async with p.connection() as conn:
        await conn.execute("INSERT INTO products(category_id,title,description,price,image,stock) VALUES(%s,%s,%s,%s,%s,%s)", (cat_id, title, desc, price, image, stock))
        await conn.commit()

async def update_product(pid, field, value):
    if field not in {"title","description","price","image","category_id","stock"}: return
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
            await cur.execute("INSERT INTO orders(user_id,username,items,total) VALUES(%s,%s,%s,%s) RETURNING id", (uid, uname, items, total))
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
        await conn.execute("INSERT INTO admins(user_id,username) VALUES(%s,%s) ON CONFLICT (user_id) DO NOTHING", (uid, uname or ""))
        await conn.commit()

async def get_user_stats(uid):
    p = await get_pool()
    async with p.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT COUNT(*) FROM orders WHERE user_id=%s AND status IN ('confirmed','shipping','ready')", (uid,))
            orders_count = (await cur.fetchone())[0]
            await cur.execute("SELECT count, discount_active FROM stickers WHERE user_id=%s", (uid,))
            st = await cur.fetchone()
            return {"orders": orders_count or 0, "stickers": st[0] if st else 0, "discount_active": bool(st[1]) if st else False}

async def add_sticker(uid):
    p = await get_pool()
    async with p.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT count FROM stickers WHERE user_id=%s", (uid,))
            st = await cur.fetchone()
            if not st:
                await cur.execute("INSERT INTO stickers(user_id,count) VALUES(%s,1)", (uid,))
                await conn.commit(); return 1, False
            count = st[0] + 1
            await cur.execute("UPDATE stickers SET count=%s WHERE user_id=%s", (count, uid))
            await conn.commit(); return count, count >= 10

async def reset_stickers(uid):
    p = await get_pool()
    async with p.connection() as conn:
        await conn.execute("INSERT INTO stickers(user_id, count, discount_active) VALUES(%s, 0, 1) ON CONFLICT(user_id) DO UPDATE SET count=0, discount_active=1", (uid,))
        await conn.commit()

async def use_discount(uid):
    p = await get_pool()
    async with p.connection() as conn:
        await conn.execute("UPDATE stickers SET discount_active=0 WHERE user_id=%s", (uid,))
        await conn.commit()

# --- Статистика ---
async def get_admin_stats():
    p = await get_pool()
    async with p.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            # За сегодня
            await cur.execute("SELECT COUNT(*), COALESCE(SUM(total),0) FROM orders WHERE status IN ('confirmed','shipping','ready') AND created_at >= CURRENT_DATE")
            today = await cur.fetchone()
            # За неделю
            await cur.execute("SELECT COUNT(*), COALESCE(SUM(total),0) FROM orders WHERE status IN ('confirmed','shipping','ready') AND created_at >= NOW() - INTERVAL '7 days'")
            week = await cur.fetchone()
            # За месяц
            await cur.execute("SELECT COUNT(*), COALESCE(SUM(total),0) FROM orders WHERE status IN ('confirmed','shipping','ready') AND created_at >= NOW() - INTERVAL '30 days'")
            month = await cur.fetchone()
            # Топ товаров (простой парсинг items)
            await cur.execute("SELECT items FROM orders WHERE status IN ('confirmed','shipping','ready') AND created_at >= NOW() - INTERVAL '30 days'")
            all_items = await cur.fetchall()
            
            top = {}
            for row in all_items:
                # Ожидаем строку вида "Товар A x1; Товар B x2"
                for part in row['items'].split(';'):
                    part = part.strip()
                    if ' x' in part:
                        name, qty = part.rsplit(' x', 1)
                        try: qty = int(qty)
                        except: continue
                        top[name] = top.get(name, 0) + qty
            
            top_sorted = sorted(top.items(), key=lambda x: x[1], reverse=True)[:5]
            return {"today": today, "week": week, "month": month, "top": top_sorted}

# ============================================================
#                       БОТ
# ============================================================
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp  = Dispatcher()

class AddP(StatesGroup):
    cat=State(); title=State(); desc=State(); price=State(); stock=State(); photo=State()
class EditP(StatesGroup):
    val=State()

def admin_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="➕ Добавить товар"), KeyboardButton(text="✏️ Редактировать")],
        [KeyboardButton(text="🗑 Удалить"), KeyboardButton(text="📦 Заказы")],
        [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="📁 Экспорт CSV")],
        [KeyboardButton(text="⬅️ Выйти")]
    ], resize_keyboard=True)

@dp.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    if m.from_user.id == ADMIN_ID: await add_admin(m.from_user.id, m.from_user.username)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Открыть магазин", web_app=WebAppInfo(url=PUBLIC_URL or "https://example.com"))],
        [InlineKeyboardButton(text="💬 Написать нам", url="https://t.me/ВАШ_ЮЗЕРНЕЙМ")] 
    ])
    await m.answer(f"<b>CLOUD99</b>\n\nПривет, {m.from_user.first_name}! Открой магазин 👇\nЕсли есть вопросы — жми «Написать нам».", reply_markup=kb)

@dp.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(
        "<b>📖 Помощь CLOUD99</b>\n\n"
        "• <b>Как заказать?</b> — Открой магазин, выбери товар и нажми «В корзину», затем «Оформить заказ».\n"
        "• <b>Как получить скидку?</b> — Сделай 10 заказов, и мы активируем тебе скидку 15% на заказ от 35 BYN.\n"
        "• <b>Колесо Фортуны?</b> — Крути раз в неделю в профиле или за заказ от 40 BYN.\n"
        "• <b>Как связаться?</b> — Нажми «Написать нам» в главном меню."
    )

@dp.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear(); await m.answer("Отменено.", reply_markup=admin_menu() if await is_admin(m.from_user.id) else None)

@dp.message(Command("admin"))
async def cmd_admin(m: Message, state: FSMContext):
    await state.clear()
    if not await is_admin(m.from_user.id): return await m.answer("Нет доступа.")
    await m.answer("Админ-панель CLOUD99:", reply_markup=admin_menu())

@dp.message(F.text == "📊 Статистика")
async def admin_stats(m: Message):
    if not await is_admin(m.from_user.id): return
    stats = await get_admin_stats()
    
    text = f"<b>📊 Статистика CLOUD99</b>\n\n"
    text += f"<b>Сегодня:</b>\nЗаказов: {stats['today'][0]}\nВыручка: {stats['today'][1]:.2f} BYN\n\n"
    text += f"<b>За 7 дней:</b>\nЗаказов: {stats['week'][0]}\nВыручка: {stats['week'][1]:.2f} BYN\n\n"
    text += f"<b>За 30 дней:</b>\nЗаказов: {stats['month'][0]}\nВыручка: {stats['month'][1]:.2f} BYN\n\n"
    
    if stats['top']:
        text += "<b>🔥 Топ товаров (30 дней):</b>\n"
        for name, qty in stats['top']:
            text += f"• {name} — {qty} шт.\n"
    
    await m.answer(text)

@dp.message(F.text == "📁 Экспорт CSV")
async def admin_export(m: Message):
    if not await is_admin(m.from_user.id): return
    orders = await get_orders()
    if not orders: return await m.answer("Заказов нет.")
    
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')
    writer.writerow(["ID", "Дата", "Пользователь", "User ID", "Товары", "Сумма", "Статус"])
    
    for o in orders:
        writer.writerow([
            o['id'], 
            o['created_at'].strftime("%Y-%m-%d %H:%M"),
            o['username'],
            o['user_id'],
            o['items'],
            f"{o['total']:.2f}",
            o['status']
        ])
    
    # Переводим в байты для отправки
    csv_bytes = output.getvalue().encode('utf-8-sig') # utf-8-sig для Excel
    file = BufferedInputFile(csv_bytes, filename=f"orders_{m.date.strftime('%Y%m%d')}.csv")
    
    await m.answer_document(file, caption="📁 Экспорт всех заказов в CSV (открывается в Excel).")

@dp.message(Command("reset_stickers"))
async def cmd_reset_stickers(m: Message):
    if not await is_admin(m.from_user.id): return
    parts = m.text.split()
    if len(parts) < 2: return await m.answer("Использование: <code>/reset_stickers USER_ID</code>")
    try: uid = int(parts[1])
    except ValueError: return await m.answer("ID должен быть числом.")
    await reset_stickers(uid); await m.answer(f"✅ Стикеры <code>{uid}</code> обнулены.")
    try: await bot.send_message(uid, f"🎉 <b>Вы собрали 10 отметок!</b>\n\nСкидка <b>{DISCOUNT_PERCENT}%</b> на заказ от <b>{DISCOUNT_MIN_SUM:.0f} BYN</b> активирована.")
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
    await state.update_data(cat=int(c.data.split(":")[1])); await state.set_state(AddP.title); await c.message.edit_text("Название товара:")

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
    await state.update_data(price=p); await state.set_state(AddP.stock); await m.answer("Сколько штук в наличии?")

@dp.message(AddP.stock)
async def add_stock(m: Message, state: FSMContext):
    try: s = int(m.text.strip())
    except: return await m.answer("Введи целое число.")
    await state.update_data(stock=s); await state.set_state(AddP.photo); await m.answer("Фото (или «-»):")

@dp.message(AddP.photo, F.photo | F.text)
async def add_photo(m: Message, state: FSMContext):
    image = m.photo[-1].file_id if m.photo else ""
    d = await state.get_data(); await add_product(d["cat"], d["title"], d["desc"], d["price"], image, d["stock"]); await m.answer(f"✅ «{d['title']}» добавлен."); await state.clear()

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
        [InlineKeyboardButton(text="Название", callback_data=f"ef:{pid}:title")], [InlineKeyboardButton(text="Описание", callback_data=f"ef:{pid}:description")],
        [InlineKeyboardButton(text="Цена", callback_data=f"ef:{pid}:price")], [InlineKeyboardButton(text="Фото", callback_data=f"ef:{pid}:image")],
        [InlineKeyboardButton(text="Кол-во на складе", callback_data=f"ef:{pid}:stock")]
    ])
    await c.message.edit_text("Что меняем?", reply_markup=kb)

@dp.callback_query(F.data.startswith("ef:"))
async def edit_prompt(c: CallbackQuery, state: FSMContext):
    _, pid, field = c.data.split(":"); await state.update_data(pid=int(pid), field=field); await state.set_state(EditP.val); await c.message.edit_text(f"Новое ({field}):")

@dp.message(EditP.val, F.photo | F.text)
async def edit_save(m: Message, state: FSMContext):
    d = await state.get_data(); field, pid = d["field"], d["pid"]
    if field == "image": val = m.photo[-1].file_id if m.photo else ""
    elif field == "price": val = float(m.text.replace(",", "."))
    elif field == "stock": val = int(m.text.strip())
    else: val = m.text.strip()
    await update_product(pid, field, val); await m.answer("✅ Обновлено."); await state.clear()

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
    new_orders = [o for o in os_ if o.get('status') == 'new']
    if not new_orders: return await m.answer("Нет новых заказов. Все обработаны.")
    for o in new_orders[:15]:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm:{o['id']}:{o['user_id']}"),
            InlineKeyboardButton(text="🚚 В пути", callback_data=f"shipping:{o['id']}:{o['user_id']}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{o['id']}:{o['user_id']}"),
        ]])
        await m.answer(f"<b>Заказ #{o['id']}</b> [new]\n👤 {o['username']}\n📦 {o['items']}\n💰 {o['total']:.2f} BYN\n🆔 <code>{o['user_id']}</code>", reply_markup=kb)

@dp.callback_query(F.data.startswith("confirm:"))
async def order_confirm(c: CallbackQuery):
    _, oid, uid = c.data.split(":"); oid = int(oid); uid = int(uid)
    await update_order_status(oid, "confirmed"); new_count, reached_10 = await add_sticker(uid)
    try: await bot.send_message(uid, f"✅ <b>Заказ #{oid} подтверждён!</b> Мы начали собирать его.")
    except: pass
    await c.message.edit_reply_markup(reply_markup=None); await c.message.answer(f"✅ Заказ #{oid} подтверждён. Стикеров: <b>{new_count}/10</b>.")
    if reached_10:
        try: await bot.send_message(ADMIN_ID, f"🎯 Клиент набрал 10 стикеров!\n/reset_stickers {uid}")
        except: pass

@dp.callback_query(F.data.startswith("shipping:"))
async def order_shipping(c: CallbackQuery):
    _, oid, uid = c.data.split(":"); oid = int(oid); uid = int(uid)
    await update_order_status(oid, "shipping")
    try: await bot.send_message(uid, f"🚚 <b>Заказ #{oid} в пути!</b>")
    except: pass
    await c.message.edit_reply_markup(reply_markup=None); await c.message.answer(f"🚚 Заказ #{oid} в пути.")

@dp.callback_query(F.data.startswith("reject:"))
async def order_reject(c: CallbackQuery):
    _, oid, uid = c.data.split(":"); oid = int(oid); uid = int(uid)
    await update_order_status(oid, "rejected")
    try: await bot.send_message(uid, f"❌ <b>Заказ #{oid} отклонён.</b>")
    except: pass
    await c.message.edit_reply_markup(reply_markup=None); await c.message.answer(f"❌ Заказ #{oid} отклонён.")

@dp.error()
async def global_error(event, exception): log.error(f"ERROR: {exception}\n{traceback.format_exc()}")

HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>CLOUD99</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{
  --bg-color: #0a1622;
  --text-main: #ffffff;
  --text-sub: #8ab4c4;
  --accent: #00f0ff;
  --accent-glow: rgba(0, 240, 255, 0.3);
  --accent-grad: linear-gradient(135deg, #00f0ff, #0077ff);
  --glass-bg: rgba(255, 255, 255, 0.07);
  --glass-border: rgba(255, 255, 255, 0.15);
  --font: 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent;font-family:var(--font)}
html,body{
  margin:0;
  background-color: var(--bg-color);
  background-image: radial-gradient(circle at 10% 20%, rgba(0, 240, 255, 0.15) 0%, transparent 40%),
                    radial-gradient(circle at 90% 80%, rgba(0, 119, 255, 0.15) 0%, transparent 40%);
  background-attachment: fixed;
  background-repeat: no-repeat;
  color:var(--text-main);
  padding-bottom:90px;
  min-height:100vh;
  overflow-x:hidden;
}
::-webkit-scrollbar { width: 0px; }

.topbar{display:flex;justify-content:space-between;align-items:center;padding:16px 20px;position:sticky;top:0;background:rgba(10, 22, 34, 0.8);backdrop-filter:blur(20px);z-index:20;border-bottom:1px solid var(--glass-border)}
.logo{font-weight:900;letter-spacing:2px;font-size:22px;color:var(--accent);text-shadow:0 0 15px var(--accent-glow)}
.topbar-right{display:flex;gap:12px;align-items:center}
.profile-btn,.cart-btn{background:var(--glass-bg);padding:10px 16px;border-radius:12px;cursor:pointer;border:1px solid var(--glass-border);color:var(--text-main);font-weight:700;transition:.2s;backdrop-filter:blur(10px);display:flex;align-items:center;gap:6px}
.profile-btn:active,.cart-btn:active{transform:scale(.95);background:rgba(255,255,255,0.15)}
.cart-btn span{color:var(--accent)}
.badge{position:absolute;top:-4px;right:-4px;background:var(--accent);color:#000;border-radius:50%;width:20px;height:20px;font-size:11px;display:flex;align-items:center;justify-content:center;font-weight:900;box-shadow:0 0 10px var(--accent-glow)}
.cart-btn{position:relative}

.search-bar{padding:0 20px 16px}
.search-input{width:100%;padding:12px 16px;border-radius:12px;background:var(--glass-bg);border:1px solid var(--glass-border);color:var(--text-main);font-size:14px;outline:none;backdrop-filter:blur(10px);transition:.2s}
.search-input:focus{border-color:var(--accent);box-shadow:0 0 15px var(--accent-glow)}

.cats{display:flex;gap:10px;overflow-x:auto;padding:0 20px 16px;scrollbar-width:none}
.cats::-webkit-scrollbar{display:none}
.cat{flex:0 0 auto;padding:10px 20px;border-radius:30px;background:var(--glass-bg);font-weight:600;font-size:14px;cursor:pointer;border:1px solid var(--glass-border);color:var(--text-main);transition:.3s;backdrop-filter:blur(10px)}
.cat.active{background:var(--accent-grad);color:#000;border-color:transparent;box-shadow:0 4px 15px var(--accent-glow);font-weight:800}
.cat:active{transform:scale(.95)}

.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:0 20px}
.card{background:var(--glass-bg);border-radius:20px;padding:14px;border:1px solid var(--glass-border);display:flex;flex-direction:column;gap:10px;position:relative;backdrop-filter:blur(10px);transition:.3s;opacity:0;transform:translateY(15px);animation:fadeIn .4s forwards;box-shadow:0 8px 32px rgba(0,0,0,.15)}
@keyframes fadeIn{to{opacity:1;transform:translateY(0)}}
.card:active{transform:scale(.97);border-color:var(--accent);box-shadow:0 0 20px var(--accent-glow)}
.card img{width:100%;aspect-ratio:1/1;object-fit:cover;border-radius:14px;background:rgba(255,255,255,0.05)}
.ph{width:100%;aspect-ratio:1/1;border-radius:14px;background:rgba(255,255,255,0.05);display:flex;align-items:center;justify-content:center;color:var(--text-sub);font-size:40px}
.card .t{font-size:15px;font-weight:700;line-height:1.2;min-height:36px}
.card .p{font-size:18px;font-weight:900;color:var(--accent);text-shadow:0 0 10px var(--accent-glow)}
.stock{font-size:12px;color:var(--text-sub);margin-top:-4px}

.btn{border:none;border-radius:12px;padding:12px 16px;font-weight:700;font-size:13px;cursor:pointer;transition:.2s;width:100%}
.btn.primary{background:var(--accent-grad);color:#000;box-shadow:0 4px 15px var(--accent-glow);transition: transform 0.2s cubic-bezier(0.34, 1.56, 0.64, 1);}
.btn.primary:active{transform:scale(.92);}
.btn.ghost{background:var(--glass-bg);color:var(--text-main);border:1px solid var(--glass-border);backdrop-filter:blur(10px)}
.btn.disabled{background:rgba(255,255,255,0.05);color:var(--text-sub);cursor:not-allowed;box-shadow:none;border:1px solid transparent}
.add{margin-top:auto}

.modal{position:fixed;inset:0;background:rgba(10, 22, 34, 0.7);display:flex;align-items:flex-end;justify-content:center;z-index:100;backdrop-filter:blur(10px)}
.modal.hidden{display:none}
.sheet{background:#0f1d2b;border-radius:24px 24px 0 0;padding:24px;width:100%;max-height:85vh;overflow-y:auto;border-top:2px solid var(--accent);animation:up .3s;box-shadow:0 -10px 50px rgba(0,0,0,.5)}
@keyframes up{from{transform:translateY(50px)}to{transform:translateY(0)}}
.sheet h2{margin:0 0 20px;font-size:20px;color:var(--text-main);font-weight:800}
.ci{display:flex;justify-content:space-between;align-items:center;padding:14px 0;border-bottom:1px solid var(--glass-border);gap:12px}
.ci .info{flex:1}
.ci .info b{display:block;font-size:14px;color:var(--text-main)}
.ci .info span{font-size:12px;color:var(--text-sub)}
.qty{display:flex;gap:8px;align-items:center}
.qty button{width:32px;height:32px;border-radius:10px;border:none;background:var(--glass-bg);color:var(--accent);font-size:18px;font-weight:800;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:.2s}
.qty button:active{background:var(--accent);color:#000;transform:scale(.9)}
.qty span{min-width:20px;text-align:center;font-weight:800;font-size:16px;color:var(--text-main)}
.total{font-size:18px;margin:20px 0;padding-top:20px;border-top:1px solid var(--glass-border);display:flex;justify-content:space-between;align-items:center;font-weight:800}
.total b{font-size:24px;color:var(--accent);text-shadow:0 0 10px var(--accent-glow)}
.discount-line{color:#000;font-size:14px;margin-top:10px;padding:10px;background:var(--accent);border-radius:10px;font-weight:800;text-align:center;box-shadow:0 0 20px var(--accent-glow)}
.empty{text-align:center;color:var(--text-sub);padding:50px 0;font-size:16px;display:flex;flex-direction:column;align-items:center;gap:16px}
.empty-icon{font-size:64px;filter:drop-shadow(0 0 10px rgba(255,255,255,0.1))}
.toast{position:fixed;bottom:100px;left:50%;transform:translateX(-50%);background:var(--accent-grad);color:#000;padding:14px 24px;border-radius:14px;font-weight:800;z-index:200;box-shadow:0 8px 25px var(--accent-glow);animation:fadeIn .2s;font-size:14px}

.profile-card{background:var(--glass-bg);border-radius:16px;padding:20px;margin-bottom:16px;border:1px solid var(--glass-border);backdrop-filter:blur(10px)}
.avatar{width:70px;height:70px;border-radius:50%;background:var(--accent-grad);display:flex;align-items:center;justify-content:center;font-size:28px;font-weight:900;color:#000;margin-bottom:16px;overflow:hidden;border:2px solid var(--accent);box-shadow:0 0 15px var(--accent-glow)}
.avatar img{width:100%;height:100%;object-fit:cover}
.profile-name{font-size:20px;font-weight:900;margin-bottom:6px;color:var(--text-main)}
.profile-sub{color:var(--text-sub);font-size:13px;margin-bottom:4px}
.stickers{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
.sticker{width:42px;height:42px;border-radius:12px;background:rgba(255,255,255,0.05);display:flex;align-items:center;justify-content:center;font-size:20px;color:var(--text-sub);transition:.3s;border:1px solid var(--glass-border)}
.sticker.filled{background:var(--accent-grad);color:#000;box-shadow:0 0 15px var(--accent-glow);transform:scale(1.05);border:none}
.section-title{font-size:18px;font-weight:900;margin:24px 0 14px;color:var(--text-main);display:flex;align-items:center;gap:8px}
.order{background:var(--glass-bg);border-radius:14px;padding:16px;margin-bottom:12px;border:1px solid var(--glass-border);transition:.2s}
.order b{color:var(--accent);font-size:15px}
.order .meta{color:var(--text-sub);font-size:12px;margin:4px 0;display:block}
.order .status{display:inline-block;padding:4px 10px;border-radius:8px;font-size:11px;font-weight:800;margin-top:8px;background:rgba(255,255,255,0.05);text-transform:uppercase;color:var(--text-main)}
.status.new{color:var(--accent);border:1px solid var(--accent)}
.status.confirmed{color:#00ff88;border:1px solid #00ff88}
.copy-btn{margin-top:10px;font-size:12px;padding:6px 12px;background:var(--glass-bg);border:1px solid var(--glass-border);color:var(--accent);border-radius:8px;cursor:pointer;font-weight:700;transition:.2s}
.copy-btn:active{background:var(--accent);color:#000}

.product-modal{position:fixed;inset:0;background:rgba(10, 22, 34, 0.9);display:flex;align-items:center;justify-content:center;z-index:300;padding:20px;backdrop-filter:blur(10px)}
.pm-box{background:#0f1d2b;border-radius:20px;padding:20px;max-width:400px;width:100%;border:1px solid var(--accent);box-shadow:0 0 40px var(--accent-glow);position:relative;max-height:90vh;overflow-y:auto;animation:fadeIn .3s}
.pm-close{position:absolute;top:12px;right:16px;font-size:24px;cursor:pointer;color:var(--text-sub);font-weight:bold;transition:.2s}
.pm-close:active{color:var(--accent);transform:scale(1.1)}
.pm-box img{width:100%;border-radius:12px;margin-bottom:16px;border:1px solid var(--glass-border)}
.pm-title{font-size:20px;font-weight:900;color:var(--text-main);margin-bottom:8px}
.pm-desc{font-size:14px;color:var(--text-sub);margin-bottom:16px;line-height:1.5}
.pm-price{font-size:22px;font-weight:900;color:var(--accent);margin-bottom:16px;text-shadow:0 0 15px var(--accent-glow)}
.scroll-top{position:fixed;bottom:24px;right:16px;width:44px;height:44px;border-radius:50%;background:var(--glass-bg);color:var(--accent);display:flex;align-items:center;justify-content:center;font-size:20px;box-shadow:0 4px 15px rgba(0,0,0,0.3);cursor:pointer;opacity:0;pointer-events:none;transition:.3s;z-index:50;border:1px solid var(--accent);backdrop-filter:blur(10px)}
.scroll-top.visible{opacity:1;pointer-events:auto}

/* --- КОЛЕСО ФОРТУНЫ --- */
.wheel-overlay {
    position: fixed; inset: 0; background: rgba(5, 10, 16, 0.95); 
    display: none; align-items: center; justify-content: center; z-index: 1000;
    backdrop-filter: blur(10px);
}
.wheel-modal {
    background: #0f1d2b; border-radius: 20px; padding: 30px 20px;
    border: 2px solid var(--accent); box-shadow: 0 0 50px var(--accent-glow);
    display: flex; flex-direction: column; align-items: center;
    position: relative; width: 90%; max-width: 350px;
}
.wheel-pointer {
    width: 0; height: 0; border-left: 15px solid transparent; 
    border-right: 15px solid transparent; border-top: 25px solid var(--accent);
    filter: drop-shadow(0 0 10px var(--accent-glow)); margin-bottom: -10px; z-index: 2;
}
.wheel-svg-container { position: relative; width: 280px; height: 280px; margin: 10px 0; }
.wheel-svg { width: 100%; height: 100%; transition: transform 4s cubic-bezier(0.17, 0.67, 0.12, 0.99); transform-origin: center; }
.wheel-btn-close {
    position: absolute; top: 10px; right: 15px; font-size: 24px;
    color: var(--text-sub); cursor: pointer; transition: .2s;
}
.wheel-btn-close:active { color: var(--accent); transform: scale(1.2); }
.wheel-result-text {
    font-size: 20px; font-weight: 900; color: var(--accent);
    text-shadow: 0 0 10px var(--accent-glow); margin-top: 15px; text-align: center;
    min-height: 28px;
}
</style>
</head>
<body>
<div class="topbar">
  <div class="logo">CLOUD99</div>
  <div class="topbar-right">
    <div class="profile-btn" onclick="openProfile()">👤</div>
    <div class="cart-btn" id="cartBtn" onclick="openCart()">🛒 <span id="cc">0</span><div class="badge" id="cb" style="display:none">0</div></div>
  </div>
</div>

<div class="search-bar">
  <input type="text" id="searchInput" class="search-input" placeholder="🔍 Поиск товара..." oninput="filterProducts()">
</div>

<div class="cats" id="cats"></div>
<div class="grid" id="grid">
    <div class="skeleton"><div class="sk-img"></div><div class="sk-title"></div><div class="sk-price"></div><div class="sk-btn"></div></div>
    <div class="skeleton"><div class="sk-img"></div><div class="sk-title"></div><div class="sk-price"></div><div class="sk-btn"></div></div>
    <div class="skeleton"><div class="sk-img"></div><div class="sk-title"></div><div class="sk-price"></div><div class="sk-btn"></div></div>
    <div class="skeleton"><div class="sk-img"></div><div class="sk-title"></div><div class="sk-price"></div><div class="sk-btn"></div></div>
</div>

<div class="modal hidden" id="cart" onclick="if(event.target===this)closeCart()">
  <div class="sheet">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <h2 style="margin:0;border:none;padding:0">🛒 Корзина</h2>
      <button class="btn ghost" style="padding:6px 10px;font-size:10px;width:auto" onclick="clearCart()">Очистить</button>
    </div>
    <div id="ci"></div>
    <div class="total">Итого: <b id="ct">0.00 BYN</b></div>
    <div id="discountInfo"></div>
    <button class="btn primary" style="width:100%;padding:16px;margin-top:12px" onclick="checkout()">Оформить заказ</button>
    <button class="btn ghost" style="width:100%;padding:12px;margin-top:10px" onclick="closeCart()">Закрыть</button>
  </div>
</div>

<div class="modal hidden" id="profile" onclick="if(event.target===this)closeProfile()">
  <div class="sheet">
    <h2>👤 Личный кабинет</h2>
    <div id="profileContent">Загрузка...</div>
    
    <div class="profile-card" style="margin-top:16px; text-align:center;">
        <div style="font-weight:900;color:var(--accent);font-size:18px;margin-bottom:8px;">🎰 Колесо Фортуны</div>
        <div style="font-size:12px;color:var(--text-sub);margin-bottom:12px;">Раз в неделю или за заказ от 40 BYN</div>
        <button class="btn primary" id="wheelBtn" onclick="openWheel()" style="padding:15px;font-size:16px;">КРУТИТЬ КОЛЕСО</button>
        <div id="wheelStatus" style="font-size:12px;color:var(--accent);margin-top:10px;font-weight:700;"></div>
    </div>

    <button class="btn ghost" style="width:100%;padding:12px;margin-top:12px" onclick="closeProfile()">Закрыть</button>
    <button class="btn primary" style="width:100%;padding:12px;margin-top:10px" onclick="window.open('https://t.me/ВАШ_ЮЗЕРНЕЙМ', '_blank')">💬 Написать нам</button>
  </div>
</div>
<div id="productModal" style="display:none" class="product-modal" onclick="if(event.target===this)closeProductModal()"></div>
<div class="scroll-top" id="scrollTop" onclick="scrollToTop()">▲</div>

<!-- МОДАЛКА КОЛЕСА -->
<div class="wheel-overlay" id="wheelOverlay">
    <div class="wheel-modal">
        <div class="wheel-btn-close" onclick="closeWheel()">×</div>
        <h2 style="margin:0 0 10px;color:var(--accent);text-transform:uppercase;letter-spacing:2px;">Колесо Фортуны</h2>
        <div class="wheel-pointer"></div>
        <div class="wheel-svg-container">
            <svg class="wheel-svg" id="wheelSvg" viewBox="0 0 100 100">
                <!-- Сектора -->
                <circle cx="50" cy="50" r="48" fill="#0a1622" stroke="var(--accent)" stroke-width="1"/>
                <path d="M50,50 L50,2 A48,48 0 0,1 98,50 Z" fill="#ff007f" opacity="0.8"/> <!-- 15% -->
                <path d="M50,50 L98,50 A48,48 0 0,1 50,98 Z" fill="#00f0ff" opacity="0.8"/> <!-- 10% -->
                <path d="M50,50 L50,98 A48,48 0 0,1 2,50 Z" fill="#ff007f" opacity="0.8"/> <!-- 5% -->
                <path d="M50,50 L2,50 A48,48 0 0,1 50,2 Z" fill="#333" opacity="0.8"/> <!-- Пусто -->
                
                <!-- Текст -->
                <text x="75" y="30" fill="#fff" font-size="6" font-weight="bold" transform="rotate(45 75 30)">15%</text>
                <text x="75" y="70" fill="#000" font-size="6" font-weight="bold" transform="rotate(-45 75 70)">10%</text>
                <text x="25" y="70" fill="#fff" font-size="6" font-weight="bold" transform="rotate(45 25 70)">5%</text>
                <text x="25" y="30" fill="#fff" font-size="6" font-weight="bold" transform="rotate(-45 25 30)">Пусто</text>
                
                <circle cx="50" cy="50" r="8" fill="#0f1d2b" stroke="var(--accent)" stroke-width="2"/>
            </svg>
        </div>
        <div class="wheel-result-text" id="wheelResultText">Нажми, чтобы крутить!</div>
        <button class="btn primary" id="spinBtn" onclick="doSpin()" style="margin-top:15px;">КРУТИТЬ</button>
    </div>
</div>

<script>
const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }
const API = location.origin;
let CATS = [], PRODUCTS = [], FILTERED = [], CART = JSON.parse(localStorage.getItem('c99')||'{}');
let CURRENT_CAT = null, USER_DISCOUNT = false, WHEEL_DISCOUNT = 0;
const DISCOUNT_PERCENT = 15, DISCOUNT_MIN_SUM = 35;

function saveCart(){ localStorage.setItem('c99', JSON.stringify(CART)); }
function money(n){ return n.toFixed(2)+' BYN'; }
async function api(path, opts){ const r = await fetch(API+path, opts); return r.json(); }

// --- Колесо логика ---
function getWheelStatus() {
    let ws = JSON.parse(localStorage.getItem('wheel_status') || '{"last_spin":0,"discount":0,"extra":0}');
    const now = Date.now();
    const week = 7 * 24 * 60 * 60 * 1000;
    
    let canSpin = false;
    let daysLeft = 0;
    
    if (ws.extra > 0) canSpin = true;
    else if (!ws.last_spin || (now - ws.last_spin) >= week) canSpin = true;
    else {
        const diff = week - (now - ws.last_spin);
        daysLeft = Math.ceil(diff / (24 * 60 * 60 * 1000));
    }
    return { canSpin, daysLeft, discount: ws.discount, extra: ws.extra };
}

function openWheel() {
    const status = getWheelStatus();
    if (!status.canSpin) {
        toast(`Доступно через ${status.daysLeft} дн.`);
        return;
    }
    document.getElementById('wheelOverlay').style.display = 'flex';
    document.getElementById('wheelResultText').textContent = 'Нажми, чтобы крутить!';
    
    const svg = document.getElementById('wheelSvg');
    svg.style.transition = 'none';
    svg.style.transform = 'rotate(0deg)';
    
    setTimeout(() => { svg.style.transition = 'transform 4s cubic-bezier(0.17, 0.67, 0.12, 0.99)'; }, 50);
}

function closeWheel() {
    document.getElementById('wheelOverlay').style.display = 'none';
}

function doSpin() {
    const btn = document.getElementById('spinBtn');
    if (btn.disabled) return;
    btn.disabled = true;
    
    const status = getWheelStatus();
    if (!status.canSpin) {
        closeWheel();
        return;
    }
    
    let ws = JSON.parse(localStorage.getItem('wheel_status') || '{"last_spin":0,"discount":0,"extra":0}');
    
    // Шансы: 5% (4%), 10% (3%), 15% (3%), Пусто (90%)
    const chance = Math.random() * 100;
    let discount = 0;
    let targetAngle = 0;
    
    // Определяем сектор и УГОЛ ЦЕНТРА сектора (в градусах)
    // 0° — сверху (Пусто), 90° — справа (15%), 180° — снизу (10%), 270° — слева (5%)
    if (chance < 4) { 
        discount = 5; 
        targetAngle = 270; // 5% (слева)
    } else if (chance < 7) { 
        discount = 10; 
        targetAngle = 180; // 10% (снизу)
    } else if (chance < 10) { 
        discount = 15; 
        targetAngle = 90; // 15% (справа)
    } else { 
        discount = 0; 
        targetAngle = 0; // Пусто (сверху)
    }
    
    // --- МАГИЯ ОСТАНОВКИ ПО ЦЕНТРУ ---
    const offset = 45; // Смещение, чтобы попасть в центр сектора
    const randomSpins = Math.floor(Math.random() * 5) + 4; // Случайное кол-во оборотов (4-8)
    const baseRotation = randomSpins * 360;
    const finalAngle = baseRotation + (360 - targetAngle) + offset;
    
    const svg = document.getElementById('wheelSvg');
    svg.style.transform = `rotate(${finalAngle}deg)`;
    
    setTimeout(() => {
        if (discount > 0) {
            document.getElementById('wheelResultText').textContent = `🎉 ВЫПАЛО ${discount}%!`;
            ws.discount = discount;
            toast(`Скидка ${discount}% активирована!`);
        } else {
            document.getElementById('wheelResultText').textContent = `😢 Пусто. +1 стикер ⭐`;
            toast(`Пусто. +1 стикер!`);
        }
        
        if (ws.extra > 0) ws.extra--;
        else ws.last_spin = Date.now();
        
        localStorage.setItem('wheel_status', JSON.stringify(ws));
        btn.disabled = false;
        
        updateWheelStatusUI();
    }, 4200);
}
// -------------------

async function loadCats(){
  CATS = await api('/api/categories');
  const box = document.getElementById('cats'); box.innerHTML = '';
  const all = document.createElement('button'); all.className='cat'+(CURRENT_CAT===null?' active':''); all.textContent='ВСЁ';
  all.onclick = ()=>{ CURRENT_CAT=null; loadCats(); loadProducts(); }; box.appendChild(all);
  CATS.forEach(c=>{ const b=document.createElement('button'); b.className='cat'+(CURRENT_CAT===c.id?' active':''); b.textContent=c.title; b.onclick=()=>{ CURRENT_CAT=c.id; loadCats(); loadProducts(); }; box.appendChild(b); });
}

async function loadProducts(){
  const g = document.getElementById('grid');
  g.innerHTML = `
    <div class="skeleton"><div class="sk-img"></div><div class="sk-title"></div><div class="sk-price"></div><div class="sk-btn"></div></div>
    <div class="skeleton"><div class="sk-img"></div><div class="sk-title"></div><div class="sk-price"></div><div class="sk-btn"></div></div>
  `;
  await new Promise(r => setTimeout(r, 800));
  
  const url = CURRENT_CAT ? '/api/products?cat='+CURRENT_CAT : '/api/products';
  PRODUCTS = await api(url);
  FILTERED = [...PRODUCTS];
  filterProducts();
}

function filterProducts() {
  const query = document.getElementById('searchInput').value.toLowerCase();
  const g = document.getElementById('grid');
  FILTERED = PRODUCTS.filter(p => p.title.toLowerCase().includes(query));
  
  if (!FILTERED.length){
    g.innerHTML = '<div class="empty" style="grid-column:1/-1"><div class="empty-icon">🔍</div>Ничего не найдено</div>'; 
    return;
  }
  
  g.innerHTML = FILTERED.map((p,i)=>`
    <div class="card" style="animation-delay:${i*0.05}s" onclick="openProduct(${p.id})">
      ${p.image ? `<img src="/tgimg/${p.image}" alt="">` : `<div class="ph">📦</div>`}
      <div class="t">${escape(p.title)}</div>
      <div class="stock">${p.stock > 0 ? `В наличии: ${p.stock} шт` : 'Нет в наличии'}</div>
      <div class="p">${money(p.price)}</div>
      ${p.stock > 0 
        ? `<button class="btn primary add" onclick="event.stopPropagation(); addToCart(${p.id})">В корзину</button>`
        : `<button class="btn disabled add">Нет в наличии</button>`}
    </div>
  `).join('');
}

function openProduct(id){
    const p = PRODUCTS.find(x=>x.id===id);
    if (!p) return;
    const modal = document.getElementById('productModal');
    modal.style.display = 'flex';
    modal.innerHTML = `
      <div class="pm-box">
        <div class="pm-close" onclick="closeProductModal()">×</div>
        ${p.image ? `<img src="/tgimg/${p.image}" alt="">` : `<div class="ph" style="margin-bottom:16px">📦</div>`}
        <div class="pm-title">${escape(p.title)}</div>
        <div class="pm-desc">${escape(p.description || 'Описание отсутствует')}</div>
        <div class="pm-price">${money(p.price)}</div>
        <div class="stock" style="margin-bottom:16px;font-size:14px">${p.stock > 0 ? `В наличии: ${p.stock} шт` : 'Нет в наличии'}</div>
        ${p.stock > 0 
          ? `<button class="btn primary" style="width:100%" onclick="addToCart(${p.id}); closeProductModal()">В корзину</button>`
          : `<button class="btn disabled" style="width:100%">Нет в наличии</button>`}
        <button class="btn ghost" style="width:100%;margin-top:10px" onclick="shareProduct(${p.id})">Поделиться</button>
      </div>
    `;
}
function closeProductModal(){ document.getElementById('productModal').style.display = 'none'; }

function shareProduct(id){
    const p = PRODUCTS.find(x=>x.id===id);
    if(!p) return;
    const text = `Зацени товар в CLOUD99: ${p.title} за ${money(p.price)}!`;
    const url = `https://t.me/share/url?url=${encodeURIComponent('https://cloud99.onrender.com')}&text=${encodeURIComponent(text)}`;
    if (tg) tg.openTelegramLink(url); else window.open(url, '_blank');
}

function escape(s){ return (s||'').replace(/[<>&"]/g, ch=>({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[ch])); }
function addToCart(id){
  const p = PRODUCTS.find(x=>x.id===id);
  if (!p || p.stock <= 0) return;
  if (CART[id]) CART[id].qty++; else CART[id] = {id, title:p.title, price:p.price, qty:1};
  saveCart(); updateBadge(); 
  
  const cb = document.getElementById('cartBtn');
  cb.classList.remove('bump');
  void cb.offsetWidth;
  cb.classList.add('bump');
  
  toast('В корзине ✓');
}

function updateBadge(){
  const n = Object.values(CART).reduce((s,i)=>s+i.qty,0);
  document.getElementById('cc').textContent = n;
  const b = document.getElementById('cb');
  b.style.display = n ? 'flex':'none'; b.textContent = n;
}

function updateWheelStatusUI() {
    const ws = getWheelStatus();
    const wheelBtn = document.getElementById('wheelBtn');
    const wheelStatus = document.getElementById('wheelStatus');
    
    if (ws.extra > 0) {
        wheelBtn.disabled = false;
        wheelBtn.classList.remove('disabled');
        wheelStatus.innerHTML = `🎁 У вас есть ${ws.extra} доп. попытка!`;
    } else if (ws.canSpin) {
        wheelBtn.disabled = false;
        wheelBtn.classList.remove('disabled');
        wheelStatus.innerHTML = `🎰 Доступно 1 вращение!`;
    } else {
        wheelBtn.disabled = true;
        wheelBtn.classList.add('disabled');
        wheelStatus.innerHTML = `⏳ Доступно через ${ws.daysLeft} дн.`;
    }
}

async function checkDiscount(){
  const u = tg?.initDataUnsafe?.user; if (!u) return;
  const res = await api('/api/profile/'+u.id);
  USER_DISCOUNT = res.stats?.discount_active || false;
  if (USER_DISCOUNT) {
      document.querySelector('.profile-btn').classList.add('pulse');
  }
  updateWheelStatusUI();
}

function calcTotals(){
  const items = Object.values(CART);
  const subtotal = items.reduce((s,i)=>s+i.price*i.qty,0);
  
  let discount = 0;
  const ws = getWheelStatus();
  if (ws.discount > 0) discount += subtotal * ws.discount / 100;
  
  const eligible = USER_DISCOUNT && subtotal >= DISCOUNT_MIN_SUM;
  if (eligible) discount += subtotal * DISCOUNT_PERCENT / 100;
  
  return {subtotal, discount, total: subtotal - discount, eligible};
}

async function openCart(){
  await checkDiscount();
  const box = document.getElementById('ci');
  const items = Object.values(CART);
  if (!items.length){ 
    box.innerHTML = '<div class="empty"><div class="empty-icon">🛒</div>Корзина пуста</div>'; 
  }
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
  
  let infoHtml = '';
  const ws = getWheelStatus();
  if (ws.discount > 0) {
      infoHtml += `<div class="discount-line" style="background:var(--accent);color:#000;margin-bottom:8px;">🎰 Скидка с колеса: ${ws.discount}%</div>`;
  }
  if (USER_DISCOUNT && !eligible && items.length){
    infoHtml += `<div class="discount-line">Скидка 15% активируется при заказе от ${DISCOUNT_MIN_SUM} BYN</div>`;
  } else if (eligible){
    infoHtml += `<div class="discount-line">🎉 Скидка 15%: −${money(subtotal * DISCOUNT_PERCENT / 100)}</div>`;
  }
  info.innerHTML = infoHtml;
  
  document.getElementById('cart').classList.remove('hidden');
}
function closeCart(){ document.getElementById('cart').classList.add('hidden'); }
function clearCart(){ CART = {}; saveCart(); updateBadge(); openCart(); toast('Корзина очищена'); }
function chQty(id,d){
  if (!CART[id]) return;
  const p = PRODUCTS.find(x=>x.id===id);
  if (d > 0 && p && CART[id].qty >= p.stock) return toast('Больше нет на складе');
  CART[id].qty += d;
  if (CART[id].qty<=0) delete CART[id];
  saveCart(); updateBadge(); openCart();
}

async function checkout(){
  const items = Object.values(CART); if (!items.length) return toast('Пусто');
  const u = tg?.initDataUnsafe?.user || {id:0, username:'guest'};
  const {subtotal, discount, total, eligible} = calcTotals();
  
  const ws = getWheelStatus();
  if (ws.discount > 0) {
     ws.discount = 0;
     localStorage.setItem('wheel_status', JSON.stringify(ws));
  }
  
  if (total >= 40) {
     ws.extra = (ws.extra || 0) + 1;
     localStorage.setItem('wheel_status', JSON.stringify(ws));
  }

  const res = await api('/api/order', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      user_id: u.id, username: u.username || u.first_name || 'guest',
      items: items.map(i=>({id:i.id, title:i.title, qty:i.qty, price:i.price})),
      subtotal, discount, total, used_discount: eligible || ws.discount > 0
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
  
  let avatarHtml = '';
  if (u.photo_url) { avatarHtml = `<img src="${u.photo_url}" alt="">`; }
  else { avatarHtml = (u.first_name || u.username || 'C')[0].toUpperCase(); }

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
        <span class="status ${o.status}">${statusLabel(o.status)}</span>
      </div>
    `).join('');
  }
  box.innerHTML = `
    <div class="profile-card">
      <div class="avatar">${avatarHtml}</div>
      <div class="profile-name">${escape(u.first_name || u.username || 'Гость')}</div>
      <div class="profile-sub">@${escape(u.username || '—')}</div>
      <div class="profile-sub">ID: ${u.id}</div>
      <button class="copy-btn" onclick="copyId(${u.id})">Скопировать ID</button>
      <div class="profile-sub" style="margin-top:10px">Подтверждённых: ${st.orders}</div>
    </div>
    <div class="profile-card">
      <div style="font-weight:900;color:var(--accent);text-transform:uppercase;letter-spacing:1px;font-size:13px">10 отметок = скидка 15%</div>
      ${st.discount_active ? '<div style="color:var(--accent);font-weight:800;margin-top:6px">★ СКИДКА АКТИВНА! (от 35 BYN)</div>' : ''}
      <div class="stickers">${stickersHtml}</div>
    </div>
    <div class="section-title">История заказов</div>
    ${ordersHtml}
  `;
  
  checkDiscount();
}
function closeProfile(){ document.getElementById('profile').classList.add('hidden'); }
function copyId(id){
    navigator.clipboard.writeText(id.toString()).then(() => toast('ID скопирован'));
}

window.addEventListener('scroll', () => {
    const btn = document.getElementById('scrollTop');
    if (window.scrollY > 300) btn.classList.add('visible');
    else btn.classList.remove('visible');
});
function scrollToTop(){ window.scrollTo({top: 0, behavior: 'smooth'}); }

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
    products = await get_products(int(cat) if cat else None)
    for p in products:
        if p.get("created_at"): p["created_at"] = str(p["created_at"])
    return web.json_response(products)

async def handle_order(req):
    data = await req.json()
    items = data.get("items", [])
    items_str = "; ".join(f"{i['title']} x{i['qty']}" for i in items)
    uid = data.get("user_id", 0); uname = data.get("username", "guest")
    total = data.get("total", 0); subtotal = data.get("subtotal", total)
    discount = data.get("discount", 0); used_discount = data.get("used_discount", False)
    
    p = await get_pool()
    async with p.connection() as conn:
        for item in items:
            async with conn.cursor() as cur:
                await cur.execute("SELECT stock FROM products WHERE id=%s", (item['id'],))
                stock_row = await cur.fetchone()
                if not stock_row or stock_row[0] < item['qty']:
                    return web.json_response({"ok": False, "error": f"Товара {item['title']} не хватает"}, status=400)
        
        for item in items:
            await conn.execute("UPDATE products SET stock = stock - %s WHERE id=%s", (item['qty'], item['id']))
            # Проверка остатков (уведомление если осталось 1 или 0)
            async with conn.cursor() as cur:
                await cur.execute("SELECT title, stock FROM products WHERE id=%s", (item['id'],))
                prod = await cur.fetchone()
                if prod and prod[1] <= 1:
                    try:
                        await bot.send_message(ADMIN_ID, f"⚠️ <b>Последний товар!</b>\n«{prod[0]}» осталось: <b>{prod[1]} шт.</b>\nПора заказать еще!")
                    except: pass
        await conn.commit()

    oid = await create_order(uid, uname, items_str, total)
    if used_discount and uid: await use_discount(uid)

    if uid:
        try:
            disc_line = f"🎁 Скидка: −{discount:.2f} BYN\n" if discount > 0 else ""
            await bot.send_message(uid, f"🕒 <b>Заказ #{oid} оформлен!</b>\n\n📦 {items_str}\n💵 Сумма: {subtotal:.2f} BYN\n{disc_line}💰 Итого: <b>{total:.2f} BYN</b>\n\nОжидайте подтверждения.")
        except Exception as e: log.warning(f"notify client {uid}: {e}")

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
    orders = await get_orders(uid)
    for o in orders:
        if o.get("created_at"): o["created_at"] = str(o["created_at"])
    return web.json_response({"orders": orders, "stats": await get_user_stats(uid)})

async def handle_tgimg(req):
    file_id = req.match_info["file_id"]
    try:
        file_info = await bot.get_file(file_id)
        return web.HTTPFound(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}")
    except Exception as e:
        log.warning(f"img redirect error: {e}")
        return web.Response(status=404)

def build_web():
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/categories", handle_categories)
    app.router.add_get("/api/products", handle_products)
    app.router.add_post("/api/order", handle_order)
    app.router.add_get("/api/profile/{uid}", handle_profile)
    app.router.add_get("/tgimg/{file_id}", handle_tgimg)
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
    if not PUBLIC_URL: log.warning("PUBLIC_URL не задан.")
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
