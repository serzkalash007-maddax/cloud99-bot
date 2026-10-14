# -*- coding: utf-8 -*-
"""
CLOUD99 — Telegram-бот + WebApp магазин.
Один файл. Python 3.10+
Запуск: python bot.py
"""

import asyncio, os, sqlite3, logging
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

# ============================================================
#                        НАСТРОЙКИ
# ============================================================
BOT_TOKEN  = os.environ.get("BOT_TOKEN", "")
ADMIN_ID   = int(os.environ.get("ADMIN_ID", "1218585792"))
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")
PORT       = int(os.environ.get("PORT", 8080))
HOST       = "0.0.0.0"

DB_PATH    = "cloud99.db"
IMAGES_DIR = Path("images"); IMAGES_DIR.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("cloud99")

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN не задан в переменных окружения!")

# ============================================================
#                       БАЗА ДАННЫХ
# ============================================================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS categories(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT UNIQUE NOT NULL,
            emoji TEXT DEFAULT '',
            sort  INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS products(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            price REAL NOT NULL,
            image TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(category_id) REFERENCES categories(id)
        );
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            items TEXT,
            total REAL,
            status TEXT DEFAULT 'new',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS admins(
            user_id INTEGER PRIMARY KEY,
            username TEXT
        );
        """)
        for t, e, s in [("ЖИДКОСТИ","💧",1), ("РАСХОДНИКИ","🔧",2), ("ПОДЫ","💨",3)]:
            c.execute("INSERT OR IGNORE INTO categories(title,emoji,sort) VALUES(?,?,?)",
                      (t, e, s))
        c.commit()

def get_categories():
    with db() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM categories ORDER BY sort, id").fetchall()]

def get_products(cat_id=None):
    with db() as c:
        if cat_id:
            rows = c.execute("SELECT * FROM products WHERE category_id=? ORDER BY id DESC",
                             (cat_id,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

def get_product(pid):
    with db() as c:
        r = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        return dict(r) if r else None

def add_product(cat_id, title, desc, price, image):
    with db() as c:
        c.execute("INSERT INTO products(category_id,title,description,price,image) "
                  "VALUES(?,?,?,?,?)", (cat_id, title, desc, price, image))
        c.commit()

def update_product(pid, field, value):
    if field not in {"title","description","price","image","category_id"}:
        return
    with db() as c:
        c.execute(f"UPDATE products SET {field}=? WHERE id=?", (value, pid))
        c.commit()

def delete_product(pid):
    with db() as c:
        c.execute("DELETE FROM products WHERE id=?", (pid,)); c.commit()

def create_order(uid, uname, items, total):
    with db() as c:
        cur = c.execute("INSERT INTO orders(user_id,username,items,total) VALUES(?,?,?,?)",
                        (uid, uname, items, total))
        c.commit()
        return cur.lastrowid

def get_orders(uid=None):
    with db() as c:
        if uid:
            rows = c.execute("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC",
                             (uid,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

def add_admin(uid, uname):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO admins(user_id,username) VALUES(?,?)", (uid, uname))
        c.commit()

def is_admin(uid):
    if uid == ADMIN_ID:
        return True
    with db() as c:
        return c.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,)).fetchone() is not None

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
async def cmd_start(m: Message):
    u = m.from_user
    if u.id == ADMIN_ID:
        add_admin(u.id, u.username or "")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛒 Открыть магазин",
                             web_app=WebAppInfo(url=PUBLIC_URL or "https://example.com"))
    ]])
    await m.answer(
        f"<b>CLOUD99</b> — магазин\n\n"
        f"Привет, {u.first_name}! Открой магазин кнопкой ниже 👇",
        reply_markup=kb)

@dp.message(Command("admin"))
async def cmd_admin(m: Message):
    if not is_admin(m.from_user.id):
        return await m.answer("Нет доступа.")
    await m.answer("Админ-панель CLOUD99:", reply_markup=admin_menu())

@dp.message(F.text == "⬅️ Выйти")
async def admin_exit(m: Message):
    await m.answer("Ок.", reply_markup=None)

@dp.message(F.text == "➕ Добавить товар")
async def add_begin(m: Message, st: FSMContext):
    if not is_admin(m.from_user.id): return
    cats = get_categories()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{c['emoji']} {c['title']}",
                              callback_data=f"ac:{c['id']}")] for c in cats])
    await m.answer("Категория?", reply_markup=kb)
    await st.set_state(AddP.cat)

@dp.callback_query(AddP.cat, F.data.startswith("ac:"))
async def add_cat(c: CallbackQuery, st: FSMContext):
    await st.update_data(cat=int(c.data.split(":")[1]))
    await c.message.edit_text("Название товара:")
    await st.set_state(AddP.title)

@dp.message(AddP.title)
async def add_title(m: Message, st: FSMContext):
    await st.update_data(title=m.text.strip())
    await m.answer("Описание (или «-»):")
    await st.set_state(AddP.desc)

@dp.message(AddP.desc)
async def add_desc(m: Message, st: FSMContext):
    await st.update_data(desc="" if m.text.strip()=="-" else m.text.strip())
    await m.answer("Цена (число, BYN):")
    await st.set_state(AddP.price)

@dp.message(AddP.price)
async def add_price(m: Message, st: FSMContext):
    try:
        p = float(m.text.replace(",", ".").strip())
    except ValueError:
        return await m.answer("Не число. Ещё раз:")
    await st.update_data(price=p)
    await m.answer("Фото (или «-»):")
    await st.set_state(AddP.photo)

@dp.message(AddP.photo, F.photo | F.text)
async def add_photo(m: Message, st: FSMContext):
    image = ""
    if m.photo:
        f = await bot.get_file(m.photo[-1].file_id)
        image = f"images/{m.photo[-1].file_id}.jpg"
        await bot.download_file(f.file_path, image)
    d = await st.get_data()
    add_product(d["cat"], d["title"], d["desc"], d["price"], image)
    await m.answer(f"✅ «{d['title']}» добавлен.")
    await st.clear()

@dp.message(F.text == "✏️ Редактировать")
async def edit_list(m: Message):
    if not is_admin(m.from_user.id): return
    ps = get_products()
    if not ps: return await m.answer("Товаров нет.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"#{p['id']} {p['title']} — {p['price']}",
                              callback_data=f"e:{p['id']}")] for p in ps])
    await m.answer("Выбери товар:", reply_markup=kb)

@dp.callback_query(F.data.startswith("e:"))
async def edit_field(c: CallbackQuery):
    pid = c.data.split(":")[1]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Название", callback_data=f"ef:{pid}:title")],
        [InlineKeyboardButton(text="Описание", callback_data=f"ef:{pid}:description")],
        [InlineKeyboardButton(text="Цена",     callback_data=f"ef:{pid}:price")],
        [InlineKeyboardButton(text="Фото",     callback_data=f"ef:{pid}:image")],
    ])
    await c.message.edit_text("Что меняем?", reply_markup=kb)

@dp.callback_query(F.data.startswith("ef:"))
async def edit_prompt(c: CallbackQuery, st: FSMContext):
    _, pid, field = c.data.split(":")
    await st.update_data(pid=int(pid), field=field)
    await c.message.edit_text(f"Новое значение ({field}):")
    await st.set_state(EditP.val)

@dp.message(EditP.val, F.photo | F.text)
async def edit_save(m: Message, st: FSMContext):
    d = await st.get_data()
    field, pid = d["field"], d["pid"]
    if field == "image":
        if m.photo:
            f = await bot.get_file(m.photo[-1].file_id)
            val = f"images/{m.photo[-1].file_id}.jpg"
            await bot.download_file(f.file_path, val)
        else:
            val = ""
    elif field == "price":
        val = float(m.text.replace(",", "."))
    else:
        val = m.text.strip()
    update_product(pid, field, val)
    await m.answer("✅ Обновлено.")
    await st.clear()

@dp.message(F.text == "🗑 Удалить")
async def del_list(m: Message):
    if not is_admin(m.from_user.id): return
    ps = get_products()
    if not ps: return await m.answer("Товаров нет.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🗑 #{p['id']} {p['title']}",
                              callback_data=f"d:{p['id']}")] for p in ps])
    await m.answer("Что удалить?", reply_markup=kb)

@dp.callback_query(F.data.startswith("d:"))
async def del_do(c: CallbackQuery):
    delete_product(int(c.data.split(":")[1]))
    await c.message.edit_text("🗑 Удалено.")

@dp.message(F.text == "📦 Заказы")
async def orders(m: Message):
    if not is_admin(m.from_user.id): return
    os_ = get_orders()
    if not os_: return await m.answer("Заказов нет.")
    for o in os_[:15]:
        await m.answer(
            f"<b>Заказ #{o['id']}</b>\n"
            f"👤 {o['username']} (<code>{o['user_id']}</code>)\n"
            f"📦 {o['items']}\n"
            f"💰 <b>{o['total']:.2f} BYN</b>\n"
            f"🕒 {o['created_at']}")

# ============================================================
#                    WEBAPP (HTTP-СЕРВЕР)
# ============================================================
HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>CLOUD99</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{
  --bg:#0f1114; --card:#191c22; --card2:#20242c;
  --accent:#7c5cff; --accent2:#a48bff;
  --text:#eef1f6; --muted:#8b93a7;
  --sd:#0a0c0f; --sl:#252a33; --r:20px;
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--text);
  font-family:-apple-system,"SF Pro Display",Inter,system-ui,sans-serif;
  padding-bottom:90px;min-height:100vh}
.topbar{display:flex;justify-content:space-between;align-items:center;
  padding:16px;position:sticky;top:0;background:rgba(15,17,20,.9);
  backdrop-filter:blur(12px);z-index:20;border-bottom:1px solid #1c2026}
.logo{font-weight:900;letter-spacing:2px;font-size:20px;
  background:linear-gradient(135deg,var(--accent),var(--accent2));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent}
.cart-btn{background:var(--card);padding:10px 16px;border-radius:14px;
  box-shadow:5px 5px 10px var(--sd),-5px -5px 10px var(--sl);
  font-weight:700;position:relative;cursor:pointer}
.cart-btn span{color:var(--accent2)}
.badge{position:absolute;top:-6px;right:-6px;background:var(--accent);
  color:#fff;border-radius:50%;width:22px;height:22px;font-size:12px;
  display:flex;align-items:center;justify-content:center;font-weight:900}
.cats{display:flex;gap:10px;overflow-x:auto;padding:16px;scrollbar-width:none}
.cats::-webkit-scrollbar{display:none}
.cat{flex:0 0 auto;padding:12px 20px;border-radius:16px;background:var(--card);
  font-weight:800;font-size:13px;letter-spacing:.5px;cursor:pointer;
  box-shadow:5px 5px 12px var(--sd),-5px -5px 12px var(--sl);
  transition:.18s;border:none;color:var(--text)}
.cat.active{background:linear-gradient(135deg,var(--accent),var(--accent2));
  color:#fff;box-shadow:0 8px 22px rgba(124,92,255,.5)}
.cat:active{transform:scale(.95)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;padding:0 16px}
.card{background:var(--card);border-radius:var(--r);padding:12px;
  box-shadow:7px 7px 16px var(--sd),-7px -7px 16px var(--sl);
  display:flex;flex-direction:column;gap:8px;transition:.15s}
.card:active{transform:scale(.97)}
.card img{width:100%;aspect-ratio:1/1;object-fit:cover;border-radius:16px;
  background:var(--card2)}
.ph{width:100%;aspect-ratio:1/1;border-radius:16px;background:var(--card2);
  display:flex;align-items:center;justify-content:center;color:var(--muted);
  font-size:40px}
.card .t{font-size:13px;font-weight:700;line-height:1.3;min-height:34px}
.card .p{font-size:16px;font-weight:900;color:var(--accent2)}
.btn{border:none;border-radius:16px;padding:11px 14px;font-weight:800;
  font-size:13px;cursor:pointer;transition:.15s;letter-spacing:.3px;
  font-family:inherit}
.btn.primary{background:linear-gradient(135deg,var(--accent),var(--accent2));
  color:#fff;box-shadow:0 6px 16px rgba(124,92,255,.45),
  inset 0 1px 0 rgba(255,255,255,.3)}
.btn.primary:active{transform:translateY(2px);
  box-shadow:0 2px 8px rgba(124,92,255,.4)}
.btn.ghost{background:var(--card);color:var(--text);
  box-shadow:4px 4px 10px var(--sd),-4px -4px 10px var(--sl)}
.btn.ghost:active{transform:scale(.97)}
.add{margin-top:auto}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.7);display:flex;
  align-items:flex-end;justify-content:center;z-index:100;
  backdrop-filter:blur(8px);animation:fade .2s}
.modal.hidden{display:none}
@keyframes fade{from{opacity:0}to{opacity:1}}
.sheet{background:var(--card);border-radius:28px 28px 0 0;padding:22px;
  width:100%;max-height:85vh;overflow-y:auto;
  box-shadow:0 -12px 40px rgba(0,0,0,.7);animation:up .25s}
@keyframes up{from{transform:translateY(40px)}to{transform:translateY(0)}}
.sheet h2{margin:0 0 16px;font-size:20px}
.ci{display:flex;justify-content:space-between;align-items:center;
  padding:12px 0;border-bottom:1px solid #262b34;gap:10px}
.ci .info{flex:1;min-width:0}
.ci .info b{display:block;font-size:13px;line-height:1.3}
.ci .info span{font-size:12px;color:var(--muted)}
.qty{display:flex;gap:6px;align-items:center}
.qty button{width:30px;height:30px;border-radius:10px;border:none;
  background:var(--card2);color:#fff;font-size:16px;font-weight:900;
  cursor:pointer}
.qty button:active{transform:scale(.9)}
.qty span{min-width:20px;text-align:center;font-weight:800}
.total{font-size:18px;margin:18px 0;padding-top:16px;
  border-top:2px solid #262b34;display:flex;justify-content:space-between}
.total b{color:var(--accent2)}
.empty{text-align:center;color:var(--muted);padding:40px 0}
.toast{position:fixed;bottom:100px;left:50%;transform:translateX(-50%);
  background:var(--accent);color:#fff;padding:12px 22px;border-radius:14px;
  font-weight:700;z-index:200;animation:fade .2s;
  box-shadow:0 8px 24px rgba(124,92,255,.5)}
</style>
</head>
<body>
<div class="topbar">
  <div class="logo">CLOUD99</div>
  <div class="cart-btn" onclick="openCart()">
    🛒 <span id="cc">0</span>
    <div class="badge" id="cb" style="display:none">0</div>
  </div>
</div>

<div class="cats" id="cats"></div>
<div class="grid" id="grid"><div class="empty">Загрузка...</div></div>

<div class="modal hidden" id="cart" onclick="if(event.target===this)closeCart()">
  <div class="sheet">
    <h2>🛒 Корзина</h2>
    <div id="ci"></div>
    <div class="total">Итого: <b id="ct">0.00 BYN</b></div>
    <button class="btn primary" style="width:100%;padding:15px"
            onclick="checkout()">Оформить заказ</button>
    <button class="btn ghost" style="width:100%;padding:12px;margin-top:8px"
            onclick="closeCart()">Закрыть</button>
  </div>
</div>

<script>
const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }

const API = location.origin;
let CATS = [], PRODUCTS = [], CART = JSON.parse(localStorage.getItem('c99')||'{}');
let CURRENT_CAT = null;

function saveCart(){ localStorage.setItem('c99', JSON.stringify(CART)); }
function money(n){ return n.toFixed(2)+' BYN'; }

async function api(path, opts){
  const r = await fetch(API+path, opts);
  return r.json();
}

async function loadCats(){
  CATS = await api('/api/categories');
  const box = document.getElementById('cats');
  box.innerHTML = '';
  const all = document.createElement('button');
  all.className = 'cat' + (CURRENT_CAT===null?' active':'');
  all.textContent = 'ВСЁ';
  all.onclick = ()=>{ CURRENT_CAT=null; loadCats(); loadProducts(); };
  box.appendChild(all);
  CATS.forEach(c=>{
    const b = document.createElement('button');
    b.className = 'cat' + (CURRENT_CAT===c.id?' active':'');
    b.textContent = c.title;
    b.onclick = ()=>{ CURRENT_CAT=c.id; loadCats(); loadProducts(); };
    box.appendChild(b);
  });
}

async function loadProducts(){
  const url = CURRENT_CAT ? '/api/products?cat='+CURRENT_CAT : '/api/products';
  PRODUCTS = await api(url);
  const g = document.getElementById('grid');
  if (!PRODUCTS.length){
    g.innerHTML = '<div class="empty" style="grid-column:1/-1">Товаров нет</div>';
    return;
  }
  g.innerHTML = PRODUCTS.map(p=>`
    <div class="card">
      ${p.image ? `<img src="/${p.image}" alt="">` : `<div class="ph">🛍</div>`}
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
  saveCart(); updateBadge(); toast('Добавлено ✓');
}

function updateBadge(){
  const n = Object.values(CART).reduce((s,i)=>s+i.qty,0);
  document.getElementById('cc').textContent = n;
  const b = document.getElementById('cb');
  b.style.display = n ? 'flex':'none';
  b.textContent = n;
}

function openCart(){
  const box = document.getElementById('ci');
  const items = Object.values(CART);
  if (!items.length){
    box.innerHTML = '<div class="empty">Корзина пуста</div>';
  } else {
    box.innerHTML = items.map(i=>`
      <div class="ci">
        <div class="info">
          <b>${escape(i.title)}</b>
          <span>${money(i.price)} × ${i.qty}</span>
        </div>
        <div class="qty">
          <button onclick="chQty(${i.id},-1)">−</button>
          <span>${i.qty}</span>
          <button onclick="chQty(${i.id},1)">+</button>
        </div>
      </div>
    `).join('');
  }
  const total = items.reduce((s,i)=>s+i.price*i.qty,0);
  document.getElementById('ct').textContent = money(total);
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
  const items = Object.values(CART);
  if (!items.length) return toast('Корзина пуста');
  const u = tg?.initDataUnsafe?.user || {id:0, username:'guest'};
  const total = items.reduce((s,i)=>s+i.price*i.qty,0);
  const res = await api('/api/order', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      user_id: u.id,
      username: u.username || u.first_name || 'guest',
      items: items.map(i=>({title:i.title, qty:i.qty, price:i.price})),
      total
    })
  });
  if (res.ok){
    CART = {}; saveCart(); updateBadge(); closeCart();
    toast('Заказ #'+res.order_id+' оформлен ✓');
    if (tg) setTimeout(()=>tg.close(), 1200);
  } else {
    toast('Ошибка. Попробуй ещё.');
  }
}

function toast(t){
  const el = document.createElement('div');
  el.className = 'toast'; el.textContent = t;
  document.body.appendChild(el);
  setTimeout(()=>el.remove(), 1800);
}

loadCats(); loadProducts(); updateBadge();
</script>
</body>
</html>
"""

async def handle_index(req):
    return web.Response(text=HTML, content_type="text/html")

async def handle_categories(req):
    return web.json_response(get_categories())

async def handle_products(req):
    cat = req.query.get("cat")
    return web.json_response(get_products(int(cat) if cat else None))

async def handle_order(req):
    data = await req.json()
    items_str = "; ".join(f"{i['title']} x{i['qty']}" for i in data["items"])
    oid = create_order(data.get("user_id",0), data.get("username","guest"),
                       items_str, data.get("total",0))
    with db() as c:
        admins = [r["user_id"] for r in c.execute("SELECT user_id FROM admins").fetchall()]
    text = (f"🛒 <b>Новый заказ #{oid}</b>\n"
            f"👤 {data.get('username')} (<code>{data.get('user_id')}</code>)\n"
            f"📦 {items_str}\n"
            f"💰 <b>{data.get('total',0):.2f} BYN</b>")
    for a in admins:
        try:
            await bot.send_message(a, text)
        except Exception as e:
            log.warning(f"notify {a}: {e}")
    return web.json_response({"ok": True, "order_id": oid})

async def handle_image(req):
    name = req.match_info["name"]
    path = IMAGES_DIR / name
    if not path.exists():
        return web.Response(status=404)
    return web.FileResponse(path)

async def handle_health(req):
    return web.Response(text="ok")

def build_web():
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/categories", handle_categories)
    app.router.add_get("/api/products", handle_products)
    app.router.add_post("/api/order", handle_order)
    app.router.add_get("/images/{name}", handle_image)
    return app

# ============================================================
#                  KEEP-ALIVE (антисон)
# ============================================================
async def keep_alive():
    """Раз в 10 минут стучимся в /health, чтобы Render не усыплял сервис."""
    if not PUBLIC_URL:
        return
    import aiohttp
    url = PUBLIC_URL.rstrip("/") + "/health"
    while True:
        await asyncio.sleep(600)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=20) as r:
                    log.info(f"keep-alive: {r.status}")
        except Exception as e:
            log.warning(f"keep-alive error: {e}")

# ============================================================
#                       ЗАПУСК
# ============================================================
async def main():
    init_db()
    if not PUBLIC_URL:
        log.warning("PUBLIC_URL не задан — WebApp-кнопка не откроется.")

    runner = web.AppRunner(build_web())
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT)
    await site.start()
    log.info(f"WebApp: http://{HOST}:{PORT}")

    asyncio.create_task(keep_alive())

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановлено.")