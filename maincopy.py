import json
import time
import requests
import asyncio
import logging
import threading
import os
import sqlite3
import customtkinter as ctk
from datetime import datetime
from threading import Lock, Semaphore
from queue import Queue

# Импорты для телеграмма
import telebot
from telebot import types
from telethon import TelegramClient
from telethon.tl.types import Channel, Chat, User

# --- КОНФИГУРАЦИЯ ---
API_ID = 21826549
API_HASH = "c1a19f792cfd9e397200d16c7e448160"
TELEGRAM_BOT_TOKEN = "8076459403:AAH2N5D_wyKcz5f39FUSrAtuZCKHEcqMRE8"
CRYPTOBOT_TOKEN = "506416:AAt2RDz7WyZPV2uXmL64uFoRdR1naVXQFX8"
ADMIN_IDS = [984714880]
SUPPORT_USER = "@ftsmaneg"
SESSIONS_DIR = "sessions"   
DB_PATH = "bakery_data.db" 
COOLDOWN_SECONDS = 20 * 60

PRICES_USD = {1: 1.5, 3: 4.0, 7: 7.0, 14: 12.0, 30: 28.0}
PRICES_RUB = {1: 100, 3: 300, 7: 500, 14: 1200, 30: 2800}

# --- ИНИЦИАЛИЗАЦИЯ БД ---
def init_db():
    if not os.path.exists(SESSIONS_DIR): os.makedirs(SESSIONS_DIR)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS subscriptions (user_id TEXT PRIMARY KEY, end_time REAL, start_time REAL, last_use REAL DEFAULT 0)')
    cursor.execute('CREATE TABLE IF NOT EXISTS payments (invoice_id TEXT PRIMARY KEY, user_id INTEGER, amount REAL, days INTEGER, status TEXT, created_at REAL)')
    cursor.execute('CREATE TABLE IF NOT EXISTS bans (user_id TEXT PRIMARY KEY)')
    conn.commit()
    conn.close()

init_db()

def db_query(query, params=(), fetchone=False, fetchall=False, commit=False):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute(query, params)
        if commit: conn.commit()
        if fetchone: return cursor.fetchone()
        if fetchall: return cursor.fetchall()
    except Exception as e: 
        if logger: logger.error(f"DB Error: {e}")
    finally: conn.close()

# --- ЛОГИРОВАНИЕ В GUI ---
class TextHandler(logging.Handler):
    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget

    def emit(self, record):
        msg = self.format(record)
        def append():
            self.text_widget.configure(state="normal")
            self.text_widget.insert("end", msg + "\n")
            self.text_widget.see("end")
            self.text_widget.configure(state="disabled")
        self.text_widget.after(0, append)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- КЛАСС КРИПТОБОТА ---
class CryptoBot:
    def __init__(self, token):
        self.token = token
        self.base_url = "https://pay.crypt.bot/api"
        
    def create_invoice(self, amount, description):
        headers = {"Crypto-Pay-API-Token": self.token, "Content-Type": "application/json"}
        data = {"asset": "USDT", "amount": str(amount), "description": description}
        try:
            r = requests.post(f"{self.base_url}/createInvoice", headers=headers, json=data, timeout=10)
            res = r.json()
            if res.get("ok"): return True, res["result"]
            return False, res.get("error", {}).get("name", "Unknown Error")
        except Exception as e: return False, str(e)
    
    def get_invoices(self, invoice_id):
        headers = {"Crypto-Pay-API-Token": self.token}
        params = {"invoice_ids": str(invoice_id)}
        try:
            r = requests.get(f"{self.base_url}/getInvoices", headers=headers, params=params, timeout=10)
            res = r.json()
            if res.get("ok") and res["result"]["items"]: return True, res["result"]["items"][0]
            return False, "not_found"
        except Exception as e: return False, str(e)

cryptobot = CryptoBot(CRYPTOBOT_TOKEN)
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, threaded=True, num_threads=15)
user_states = {}
BAN_SEMAPHORE = Semaphore(1)

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def is_banned(user_id):
    res = db_query("SELECT user_id FROM bans WHERE user_id = ?", (str(user_id),), fetchone=True)
    return res is not None

def format_msk_datetime(timestamp):
    return datetime.fromtimestamp(timestamp).strftime('%d.%m.%Y %H:%M MSK')

def get_session_files():
    if not os.path.exists(SESSIONS_DIR): return []
    return [f[:-8] for f in os.listdir(SESSIONS_DIR) if f.endswith('.session')]

# --- КЛАВИАТУРЫ ---
def create_main_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("Выпечка", "Абонемент")
    kb.add("Рецепты", "Поддержка")
    return kb

def create_days_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("1 день", callback_data="sel_1"), types.InlineKeyboardButton("3 дня", callback_data="sel_3"))
    kb.add(types.InlineKeyboardButton("7 дней", callback_data="sel_7"), types.InlineKeyboardButton("14 дней", callback_data="sel_14"))
    kb.add(types.InlineKeyboardButton("30 дней", callback_data="sel_30"))
    return kb

def create_pay_method_keyboard(days):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(f"CryptoBot ({PRICES_USD[days]}$)", callback_data=f"pay_crypto_{days}"))
    kb.add(types.InlineKeyboardButton(f"Карта ({PRICES_RUB[days]} руб)", callback_data=f"pay_card_{days}"))
    kb.add(types.InlineKeyboardButton("Назад", callback_data="back_to_days"))
    return kb

# --- ОБРАБОТЧИКИ ТЕЛЕГРАМ БОТА ---

@bot.message_handler(func=lambda m: is_banned(m.from_user.id))
def handle_banned(message):
    bot.send_message(message.chat.id, "Вы заблокированы в этой пекарне.")

@bot.message_handler(commands=['start'])
def cmd_start(message):
    bot.send_message(message.chat.id, "Добро пожаловать в Пекарню", reply_markup=create_main_keyboard())

@bot.message_handler(commands=['stats'])
def admin_stats(message):
    if message.from_user.id not in ADMIN_IDS: return
    subs_count = db_query("SELECT COUNT(*) FROM subscriptions WHERE end_time > ?", (time.time(),), fetchone=True)[0]
    total_payments = db_query("SELECT COUNT(*) FROM payments WHERE status = 'paid'", fetchone=True)[0]
    sessions = len(get_session_files())
    text = f"📊 Статистика:\nАктивных подписок: {subs_count}\nОплат: {total_payments}\nСессий: {sessions}"
    bot.send_message(message.chat.id, text)

@bot.callback_query_handler(func=lambda c: True)
def handle_callbacks(call):
    if is_banned(call.from_user.id): return
    data = call.data
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    if data == "back_to_days":
        bot.edit_message_text("Выберите срок абонемента:", chat_id, msg_id, reply_markup=create_days_keyboard())
    elif data.startswith("sel_"):
        days = int(data.split("_")[1])
        bot.edit_message_text(f"Срок: {days} дн. Выберите способ оплаты:", chat_id, msg_id, reply_markup=create_pay_method_keyboard(days))
    elif data.startswith("pay_crypto_"):
        days = int(data.split("_")[2])
        price = PRICES_USD[days]
        ok, inv = cryptobot.create_invoice(price, f"Bakery {days}d")
        if ok:
            db_query("INSERT INTO payments VALUES (?, ?, ?, ?, ?, ?)", (str(inv['invoice_id']), call.from_user.id, price, days, "pending", time.time()), commit=True)
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("Оплатить", url=inv['pay_url']))
            kb.add(types.InlineKeyboardButton("Проверить", callback_data=f"chk_{inv['invoice_id']}"))
            bot.edit_message_text(f"Счет на {price}$ создан:", chat_id, msg_id, reply_markup=kb)
    elif data.startswith("chk_"):
        inv_id = data.split("_")[1]
        ok, res = cryptobot.get_invoices(inv_id)
        if ok and res.get('status') == 'paid':
            p = db_query("SELECT user_id, days FROM payments WHERE invoice_id = ?", (inv_id,), fetchone=True)
            if p:
                end = time.time() + (p[1] * 86400)
                db_query("INSERT OR REPLACE INTO subscriptions (user_id, end_time, start_time) VALUES (?, ?, ?)", (str(p[0]), end, time.time()), commit=True)
                db_query("UPDATE payments SET status = 'paid' WHERE invoice_id = ?", (inv_id,), commit=True)
                bot.edit_message_text("✅ Абонемент активирован!", chat_id, msg_id)
        else:
            bot.answer_callback_query(call.id, "Оплата не найдена.", show_alert=True)
    elif data.startswith("pay_card_"):
        user_states[call.from_user.id] = "waiting_pdf"
        bot.edit_message_text("Реквизиты: СберБанк 2202208359860005\nПришлите PDF-чек.", chat_id, msg_id)

@bot.message_handler(func=lambda m: m.text == "Выпечка")
def bakery_handler(message):
    uid = message.from_user.id
    sub = db_query("SELECT end_time, last_use FROM subscriptions WHERE user_id = ?", (str(uid),), fetchone=True)
    if not sub or sub[0] < time.time():
        bot.send_message(message.chat.id, "У вас нет активного абонемента.")
        return
    last_use = sub[1] if sub[1] else 0
    if time.time() - last_use < COOLDOWN_SECONDS:
        bot.send_message(message.chat.id, "⌛️ Подождите, печи остывают.")
        return
    msg = bot.send_message(message.chat.id, "Введите адрес доставки (@username):")
    bot.register_next_step_handler(msg, process_bakery)

def process_bakery(message):
    username = message.text.strip()
    if not username.startswith('@'):
        bot.send_message(message.chat.id, "Неверный формат.")
        return
    db_query("UPDATE subscriptions SET last_use = ? WHERE user_id = ?", (time.time(), str(message.from_user.id)), commit=True)
    status_msg = bot.send_message(message.chat.id, "Замешиваем тесто...")
    
    def run_attack():
        success, total, info = start_multi_session_attack(username)
        report = f"Пирожки выехали: {username}\nОтправлено: {total} шт." if success else f"Ошибка: {total}"
        bot.edit_message_text(report, message.chat.id, status_msg.message_id)
        logger.info(f"Боевой вылет: {username} результат {total}")
    
    threading.Thread(target=run_attack).start()

def start_multi_session_attack(username):
    if not BAN_SEMAPHORE.acquire(blocking=False): return False, "Все печи заняты", None
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        async def attack():
            sessions = get_session_files()
            total = 0
            for s in sessions:
                try:
                    async with TelegramClient(os.path.join(SESSIONS_DIR, s), API_ID, API_HASH) as client:
                        target = await client.get_entity(username)
                        async for d in client.iter_dialogs():
                            if isinstance(d.entity, (Chat, Channel)):
                                try:
                                    await client.edit_permissions(d.entity.id, target, view_messages=False)
                                    total += 1
                                except: continue
                except: continue
            return (True, total, username) if total > 0 else (False, "Нет прав/цель не найдена", None)
        return loop.run_until_complete(attack())
    finally: BAN_SEMAPHORE.release()

@bot.message_handler(func=lambda m: m.text == "Абонемент")
def sub_menu(message):
    uid = message.from_user.id
    sub = db_query("SELECT end_time FROM subscriptions WHERE user_id = ?", (str(uid),), fetchone=True)
    status = f"Активен до: {format_msk_datetime(sub[0])}" if sub and sub[0] > time.time() else "Не активен"
    bot.send_message(message.chat.id, f"Ваш статус: {status}", reply_markup=create_days_keyboard())

@bot.message_handler(func=lambda m: m.text == "Поддержка")
def support_handler(message):
    bot.send_message(message.chat.id, f"Поддержка: {SUPPORT_USER}")

@bot.message_handler(func=lambda m: m.text == "Рецепты")
def recipe_handler(message):
    bot.send_message(message.chat.id, "Инструкция: Работа по DC1, DC3, DC5. Печи 2022-2025.")

@bot.message_handler(content_types=['document'])
def handle_docs(message):
    if message.document.mime_type == 'application/pdf':
        for aid in ADMIN_IDS:
            bot.send_document(aid, message.document.file_id, caption=f"ЧЕК ОТ {message.from_user.id}")
        bot.send_message(message.chat.id, "Чек получен и отправлен на проверку.")

# --- GUI ПРИЛОЖЕНИЕ ---
class BakeryAdminApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Bakery Admin Panel v2.0")
        self.geometry("900x650")
        ctk.set_appearance_mode("dark")

        # Сетка
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # Боковая панель
        self.sidebar = ctk.CTkFrame(self, width=200, corner_radius=0)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        
        self.logo_label = ctk.CTkLabel(self.sidebar, text="BAKERY CMS", font=ctk.CTkFont(size=20, weight="bold"))
        self.logo_label.pack(pady=20)

        self.btn_stats = ctk.CTkButton(self.sidebar, text="Статистика", command=self.show_stats)
        self.btn_stats.pack(pady=10, padx=20)

        self.btn_sub = ctk.CTkButton(self.sidebar, text="Выдать сабку", command=self.show_add_sub)
        self.btn_sub.pack(pady=10, padx=20)

        self.btn_ban = ctk.CTkButton(self.sidebar, text="Бан / Разбан", command=self.show_ban_tool)
        self.btn_ban.pack(pady=10, padx=20)

        # Основной контент
        self.main_frame = ctk.CTkFrame(self, corner_radius=15, fg_color="transparent")
        self.main_frame.grid(row=0, column=1, sticky="nsew", padx=20, pady=20)

        # Поле логов
        self.log_view = ctk.CTkTextbox(self, height=180, font=("Consolas", 12))
        self.log_view.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=20, pady=10)
        self.log_view.configure(state="disabled")

        # Настройка логгера для GUI
        handler = TextHandler(self.log_view)
        logger.addHandler(handler)
        logging.getLogger('telebot').addHandler(handler)

        self.show_stats()
        self.start_bot_thread()

    def clear_main(self):
        for widget in self.main_frame.winfo_children():
            widget.destroy()

    def show_stats(self):
        self.clear_main()
        label = ctk.CTkLabel(self.main_frame, text="Текущая статистика", font=ctk.CTkFont(size=18))
        label.pack(pady=10)

        subs = db_query("SELECT COUNT(*) FROM subscriptions WHERE end_time > ?", (time.time(),), fetchone=True)[0]
        pays = db_query("SELECT COUNT(*) FROM payments WHERE status = 'paid'", fetchone=True)[0]
        sessions = len(get_session_files())

        stats_text = f"Активных подписок: {subs}\nУспешных оплат: {pays}\nЗагружено сессий: {sessions}"
        ctk.CTkLabel(self.main_frame, text=stats_text, justify="left", font=("Segoe UI", 14)).pack(pady=20)
        ctk.CTkButton(self.main_frame, text="Обновить", command=self.show_stats).pack()

    def show_add_sub(self):
        self.clear_main()
        ctk.CTkLabel(self.main_frame, text="Выдача подписки", font=ctk.CTkFont(size=18)).pack(pady=10)
        
        uid_entry = ctk.CTkEntry(self.main_frame, placeholder_text="User ID", width=300)
        uid_entry.pack(pady=5)
        
        days_entry = ctk.CTkEntry(self.main_frame, placeholder_text="Количество", width=300)
        days_entry.pack(pady=5)

        unit_var = ctk.StringVar(value="Дни")
        ctk.CTkSegmentedButton(self.main_frame, values=["Дни", "Часы", "Минуты"], variable=unit_var).pack(pady=10)

        def submit():
            uid = uid_entry.get()
            try:
                amt = int(days_entry.get())
                unit = unit_var.get()
                mult = 86400 if unit == "Дни" else 3600 if unit == "Часы" else 60
                
                current_sub = db_query("SELECT end_time FROM subscriptions WHERE user_id = ?", (uid,), fetchone=True)
                base = max(time.time(), current_sub[0]) if current_sub else time.time()
                new_end = base + (amt * mult)
                
                db_query("INSERT OR REPLACE INTO subscriptions (user_id, end_time, start_time) VALUES (?, ?, ?)", 
                         (uid, new_end, time.time()), commit=True)
                logger.info(f"GUI: Выдана сабка {uid} до {format_msk_datetime(new_end)}")
            except Exception as e: logger.error(f"GUI Error: {e}")

        ctk.CTkButton(self.main_frame, text="Подтвердить", fg_color="green", command=submit).pack(pady=20)

    def show_ban_tool(self):
        self.clear_main()
        ctk.CTkLabel(self.main_frame, text="Управление блокировками", font=ctk.CTkFont(size=18)).pack(pady=10)
        
        uid_entry = ctk.CTkEntry(self.main_frame, placeholder_text="User ID", width=300)
        uid_entry.pack(pady=10)

        def do_ban():
            db_query("INSERT OR IGNORE INTO bans VALUES (?)", (uid_entry.get(),), commit=True)
            logger.info(f"GUI: Забанен {uid_entry.get()}")

        def do_unban():
            db_query("DELETE FROM bans WHERE user_id = ?", (uid_entry.get(),), commit=True)
            logger.info(f"GUI: Разбанен {uid_entry.get()}")

        ctk.CTkButton(self.main_frame, text="Забанить", fg_color="red", command=do_ban).pack(pady=5)
        ctk.CTkButton(self.main_frame, text="Разбанить", command=do_unban).pack(pady=5)

    def start_bot_thread(self):
        thread = threading.Thread(target=lambda: bot.polling(none_stop=True), daemon=True)
        thread.start()
        logger.info("Бот запущен в фоне")

if __name__ == "__main__":
    app = BakeryAdminApp()
    app.mainloop()