import json
import time
import requests
import asyncio
import logging
import threading
import os
import sqlite3
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
TELEGRAM_BOT_TOKEN = "8554501259:AAF-3ffy4EB3EI0IRc_p0_PoJ3GS3M3nyeM"
CRYPTOBOT_TOKEN = "494713:AAKJ8TnI1GqyQScObDXeuFHFXyOM6IrGATc"
ADMIN_IDS = [8061841695]
SUPPORT_USER = "@ftsmaneg"
SESSIONS_DIR = "sessions"
DB_PATH = "bakery_data.db"
COOLDOWN_SECONDS = 20 * 60

PRICES_USD = {1: 1.5, 3: 4.0, 7: 7.0, 14: 12.0, 30: 28.0}
PRICES_RUB = {1: 100, 3: 300, 7: 500, 14: 1200, 30: 2800}

# --- ИНИЦИАЛИЗАЦИЯ БД ---
def init_db():
    if not os.path.exists(SESSIONS_DIR):
        os.makedirs(SESSIONS_DIR)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        'CREATE TABLE IF NOT EXISTS subscriptions (user_id TEXT PRIMARY KEY, end_time REAL, start_time REAL, last_use REAL DEFAULT 0)')
    cursor.execute(
        'CREATE TABLE IF NOT EXISTS payments (invoice_id TEXT PRIMARY KEY, user_id INTEGER, amount REAL, days INTEGER, status TEXT, created_at REAL)')
    cursor.execute('CREATE TABLE IF NOT EXISTS bans (user_id TEXT PRIMARY KEY)')
    conn.commit()
    conn.close()

init_db()

def db_query(query, params=(), fetchone=False, fetchall=False, commit=False):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute(query, params)
        if commit:
            conn.commit()
        if fetchone:
            return cursor.fetchone()
        if fetchall:
            return cursor.fetchall()
    except Exception as e:
        logger.error(f"DB Error: {e}")
    finally:
        conn.close()

# --- ЛОГИРОВАНИЕ ---
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
            if res.get("ok"):
                return True, res["result"]
            return False, res.get("error", {}).get("name", "Unknown Error")
        except Exception as e:
            return False, str(e)

    def get_invoices(self, invoice_id):
        headers = {"Crypto-Pay-API-Token": self.token}
        params = {"invoice_ids": str(invoice_id)}
        try:
            r = requests.get(f"{self.base_url}/getInvoices", headers=headers, params=params, timeout=10)
            res = r.json()
            if res.get("ok") and res["result"]["items"]:
                return True, res["result"]["items"][0]
            return False, "not_found"
        except Exception as e:
            return False, str(e)

cryptobot = CryptoBot(CRYPTOBOT_TOKEN)
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, threaded=True, num_threads=15)
BAN_SEMAPHORE = Semaphore(1)

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def is_banned(user_id):
    res = db_query("SELECT user_id FROM bans WHERE user_id = ?", (str(user_id),), fetchone=True)
    return res is not None


def is_admin(user_id):
    try:
        return int(user_id) in ADMIN_IDS
    except Exception:
        return False

def format_msk_datetime(timestamp):
    return datetime.fromtimestamp(timestamp).strftime('%d.%m.%Y %H:%M MSK')

def get_session_files():
    if not os.path.exists(SESSIONS_DIR):
        return []
    return [f[:-8] for f in os.listdir(SESSIONS_DIR) if f.endswith('.session')]

# --- КЛАВИАТУРЫ ---
def create_main_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("Выпечка", "Абонемент")
    kb.add("Рецепты", "Поддержка")
    return kb

def create_days_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("1 день", callback_data="sel_1"),
           types.InlineKeyboardButton("3 дня", callback_data="sel_3"))
    kb.add(types.InlineKeyboardButton("7 дней", callback_data="sel_7"),
           types.InlineKeyboardButton("14 дней", callback_data="sel_14"))
    kb.add(types.InlineKeyboardButton("30 дней", callback_data="sel_30"))
    return kb

def create_pay_method_keyboard(days):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(f"CryptoBot ({PRICES_USD[days]}$)", callback_data=f"pay_crypto_{days}"))
    kb.add(types.InlineKeyboardButton(f"Карта ({PRICES_RUB[days]} руб)", callback_data=f"pay_card_{days}"))
    kb.add(types.InlineKeyboardButton("Назад", callback_data="back_to_days"))
    return kb

# --- ОБРАБОТЧИКИ БОТА ---
@bot.message_handler(func=lambda m: is_banned(m.from_user.id))
def handle_banned(message):
    bot.send_message(message.chat.id, "Вы заблокированы в этой пекарне.")

@bot.message_handler(commands=['start'])
def cmd_start(message):
    bot.send_message(message.chat.id, "Добро пожаловать в Пекарню", reply_markup=create_main_keyboard())

@bot.message_handler(commands=['stats'])
def admin_stats(message):
    if message.from_user.id not in ADMIN_IDS:
        return
    subs_count = db_query("SELECT COUNT(*) FROM subscriptions WHERE end_time > ?", (time.time(),), fetchone=True)[0]
    total_payments = db_query("SELECT COUNT(*) FROM payments WHERE status = 'paid'", fetchone=True)[0]
    sessions = len(get_session_files())
    text = f"📊 Статистика:\nАктивных подписок: {subs_count}\nОплат: {total_payments}\nСессий: {sessions}"
    bot.send_message(message.chat.id, text)

@bot.callback_query_handler(func=lambda c: True)
def handle_callbacks(call):
    if is_banned(call.from_user.id):
        return
    data = call.data
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    if data == "back_to_days":
        bot.edit_message_text("Выберите срок абонемента:", chat_id, msg_id, reply_markup=create_days_keyboard())
    elif data.startswith("sel_"):
        days = int(data.split("_")[1])
        bot.edit_message_text(f"Срок: {days} дн. Выберите способ оплаты:", chat_id, msg_id,
                              reply_markup=create_pay_method_keyboard(days))
    elif data.startswith("pay_crypto_"):
        days = int(data.split("_")[2])
        price = PRICES_USD[days]
        ok, inv = cryptobot.create_invoice(price, f"Bakery {days}d")
        if ok:
            db_query("INSERT INTO payments VALUES (?, ?, ?, ?, ?, ?)",
                     (str(inv['invoice_id']), call.from_user.id, price, days, "pending", time.time()), commit=True)
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
                db_query("INSERT OR REPLACE INTO subscriptions (user_id, end_time, start_time) VALUES (?, ?, ?)",
                         (str(p[0]), end, time.time()), commit=True)
                db_query("UPDATE payments SET status = 'paid' WHERE invoice_id = ?", (inv_id,), commit=True)
                bot.edit_message_text("✅ Абонемент активирован!", chat_id, msg_id)
        else:
            bot.answer_callback_query(call.id, "Оплата не найдена.", show_alert=True)
    elif data.startswith("pay_card_"):
        bot.edit_message_text("Реквизиты: СберБанк 2202208359860005\nПришлите PDF-чек.", chat_id, msg_id)

@bot.message_handler(func=lambda m: m.text == "Выпечка")
def bakery_handler(message):
    uid = message.from_user.id
    # Админы могут использовать бесплатно и без кулдауна
    if is_admin(uid):
        msg = bot.send_message(message.chat.id, "Введите адрес доставки (@username) — вы админ, подписка и кулдаун не требуются:")
        bot.register_next_step_handler(msg, process_bakery)
        return
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
    if not BAN_SEMAPHORE.acquire(blocking=False):
        return False, "Все печи заняты", None
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
                                except:
                                    continue
                except:
                    continue
            return (True, total, username) if total > 0 else (False, "Нет прав/цель не найдена", None)
        return loop.run_until_complete(attack())
    finally:
        BAN_SEMAPHORE.release()

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


# -------------------- ADMIN COMMANDS --------------------
@bot.message_handler(commands=['adminhelp'])
def admin_help(message):
    if not is_admin(message.from_user.id):
        return
    text = (
        "📋 Команды администратора:\n"
        "/adminhelp - показать это меню\n"
        "/ban <user_id> - заблокировать пользователя\n"
        "/unban <user_id> - разбанить пользователя\n"
        "/addsub <user_id> <days> - выдать подписку\n"
        "/rmsub <user_id> - удалить подписку\n"
        "/attack <@username> - выполнить " + "Выпечку" + " от имени админа\n"
        "/sessions - показать активные сессии"
    )
    bot.send_message(message.chat.id, text)


@bot.message_handler(commands=['ban'])
def cmd_ban(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(message.chat.id, "Использование: /ban <user_id>")
        return
    uid = parts[1]
    db_query("INSERT OR REPLACE INTO bans (user_id) VALUES (?)", (str(uid),), commit=True)
    bot.send_message(message.chat.id, f"Пользователь {uid} заблокирован.")


@bot.message_handler(commands=['unban'])
def cmd_unban(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(message.chat.id, "Использование: /unban <user_id>")
        return
    uid = parts[1]
    db_query("DELETE FROM bans WHERE user_id = ?", (str(uid),), commit=True)
    bot.send_message(message.chat.id, f"Пользователь {uid} разбанен.")


@bot.message_handler(commands=['addsub'])
def cmd_addsub(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 3:
        bot.send_message(message.chat.id, "Использование: /addsub <user_id> <days>")
        return
    uid = parts[1]
    try:
        days = int(parts[2])
    except ValueError:
        bot.send_message(message.chat.id, "Дни должны быть числом.")
        return
    end = time.time() + days * 86400
    db_query("INSERT OR REPLACE INTO subscriptions (user_id, end_time, start_time) VALUES (?, ?, ?)",
             (str(uid), end, time.time()), commit=True)
    bot.send_message(message.chat.id, f"Подписка для {uid} выдана на {days} дн.")


@bot.message_handler(commands=['rmsub'])
def cmd_rmsub(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(message.chat.id, "Использование: /rmsub <user_id>")
        return
    uid = parts[1]
    db_query("DELETE FROM subscriptions WHERE user_id = ?", (str(uid),), commit=True)
    bot.send_message(message.chat.id, f"Подписка пользователя {uid} удалена.")


@bot.message_handler(commands=['sessions'])
def cmd_sessions(message):
    if not is_admin(message.from_user.id):
        return
    sessions = get_session_files()
    if not sessions:
        bot.send_message(message.chat.id, "Сессий не найдено.")
        return
    bot.send_message(message.chat.id, "Сессии:\n" + "\n".join(sessions))


@bot.message_handler(commands=['attack'])
def cmd_attack(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(message.chat.id, "Использование: /attack <@username>")
        return
    username = parts[1].strip()
    if not username.startswith('@'):
        bot.send_message(message.chat.id, "Укажите username, начинающийся с @")
        return

    status_msg = bot.send_message(message.chat.id, f"Запускаю выпечку для {username}...")

    def run_attack_cmd():
        success, total, info = start_multi_session_attack(username)
        report = f"Пирожки выехали: {username}\nОтправлено: {total} шт." if success else f"Ошибка: {total}"
        bot.edit_message_text(report, message.chat.id, status_msg.message_id)

    threading.Thread(target=run_attack_cmd).start()

# --- Запуск бота в фоне ---
if __name__ == "__main__":
    threading.Thread(target=lambda: bot.polling(none_stop=True), daemon=True).start()
    logger.info("Telegram bot started without GUI. Working in background...")
    while True:
        time.sleep(1)
