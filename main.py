import json
import time
import requests
import asyncio
import logging
import threading
import os
import sqlite3
import re
from datetime import datetime
from threading import Lock, Semaphore
from queue import Queue

# Импорты для телеграмма
import telebot
from telebot import types
from telebot.util import quick_markup
from telethon import TelegramClient, sync
from telethon.tl.types import Channel, Chat, User
from telethon.sessions import StringSession
import phonenumbers
from phonenumbers import carrier
from phonenumbers.phonenumberutil import number_type

# --- КОНФИГУРАЦИЯ ---
API_ID = 22778226
API_HASH = "9be02c55dfb4c834210599490dcd58a8"
TELEGRAM_BOT_TOKEN = "8203239986:AAF7fFMo5t6Io3sgll8NFaAlYlldfrP2zTM"
CRYPTOBOT_TOKEN = "507310:AAkc7QTMPlo6TFGIydedMhKP8WSofx35hna"
ADMIN_IDS = [8050595279]
SUPPORT_USER = "@Wawichh"
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
    cursor.execute('CREATE TABLE IF NOT EXISTS sessions (phone TEXT PRIMARY KEY, session_string TEXT, added_at REAL)')
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

def validate_phone_number(phone):
    """Валидация номера телефона"""
    try:
        parsed = phonenumbers.parse(phone, None)
        if not phonenumbers.is_valid_number(parsed):
            return False
        # Проверяем, что это мобильный номер
        if carrier._is_mobile(number_type(parsed)):
            return True
        return False
    except:
        return False

def normalize_phone(phone):
    """Нормализация номера телефона"""
    phone = re.sub(r'[^\d+]', '', phone)
    if not phone.startswith('+'):
        phone = '+' + phone
    return phone

# --- КРАСИВЫЕ КЛАВИАТУРЫ ---
def create_main_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        types.KeyboardButton("🍪 Выпечка"),
        types.KeyboardButton("🎫 Абонемент")
    )
    kb.add(
        types.KeyboardButton("📚 Рецепты"),
        types.KeyboardButton("🛠 Поддержка")
    )
    if is_admin(telebot.util.extract_arguments):
        kb.add(types.KeyboardButton("⚙️ Админ-панель"))
    return kb

def create_admin_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        types.KeyboardButton("👥 Пользователи"),
        types.KeyboardButton("💳 Платежи")
    )
    kb.add(
        types.KeyboardButton("📊 Статистика"),
        types.KeyboardButton("🛠 Сессии")
    )
    kb.add(
        types.KeyboardButton("➕ Добавить сессию"),
        types.KeyboardButton("🔙 Главное меню")
    )
    return kb

def create_days_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("1 день - 1.5$ / 100₽", callback_data="sel_1"),
        types.InlineKeyboardButton("3 дня - 4.0$ / 300₽", callback_data="sel_3")
    )
    kb.add(
        types.InlineKeyboardButton("7 дней - 7.0$ / 500₽", callback_data="sel_7"),
        types.InlineKeyboardButton("14 дней - 12.0$ / 1200₽", callback_data="sel_14")
    )
    kb.add(types.InlineKeyboardButton("30 дней - 28.0$ / 2800₽", callback_data="sel_30"))
    return kb

def create_pay_method_keyboard(days):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton(f"💎 CryptoBot ({PRICES_USD[days]}$)", callback_data=f"pay_crypto_{days}"))
    kb.add(types.InlineKeyboardButton(f"💳 Банковская карта ({PRICES_RUB[days]} руб)", callback_data=f"pay_card_{days}"))
    kb.add(types.InlineKeyboardButton("↩️ Назад", callback_data="back_to_days"))
    return kb

def create_back_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main"))
    return kb

# --- ОБРАБОТЧИКИ БОТА ---
@bot.message_handler(func=lambda m: is_banned(m.from_user.id))
def handle_banned(message):
    bot.send_message(message.chat.id, "🚫 Вы заблокированы в этой пекарне.")

@bot.message_handler(commands=['start'])
def cmd_start(message):
    welcome_text = """
🍰 *Добро пожаловать в Пекарню!* 🥖

Здесь вы можете:
• 🍪 Использовать *Выпечку* для работы
• 🎫 Приобрести *Абонемент* на доступ
• 📚 Изучить *Рецепты* работы
• 🛠 Получить *Поддержку*

Выберите действие из меню ниже:
    """
    bot.send_message(message.chat.id, welcome_text, 
                     reply_markup=create_main_keyboard(),
                     parse_mode='Markdown')

@bot.message_handler(func=lambda m: m.text == "🍪 Выпечка")
def bakery_handler(message):
    uid = message.from_user.id
    
    if is_banned(uid):
        bot.send_message(message.chat.id, "🚫 Вы заблокированы.")
        return
    
    # Админы могут использовать бесплатно и без кулдауна
    if is_admin(uid):
        msg = bot.send_message(message.chat.id, 
                              "👨‍🍳 *Вы админ!*\nВведите адрес доставки (@username):",
                              parse_mode='Markdown')
        bot.register_next_step_handler(msg, process_bakery)
        return
    
    sub = db_query("SELECT end_time, last_use FROM subscriptions WHERE user_id = ?", (str(uid),), fetchone=True)
    if not sub or sub[0] < time.time():
        bot.send_message(message.chat.id, 
                        "🎫 *Требуется абонемент!*\n\nПриобретите абонемент для использования Выпечки.",
                        parse_mode='Markdown',
                        reply_markup=create_days_keyboard())
        return
    
    last_use = sub[1] if sub[1] else 0
    if time.time() - last_use < COOLDOWN_SECONDS:
        remaining = COOLDOWN_SECONDS - (time.time() - last_use)
        minutes = int(remaining // 60)
        seconds = int(remaining % 60)
        bot.send_message(message.chat.id, 
                        f"⏳ *Печи остывают...*\n\nПодождите {minutes} мин {seconds} сек перед следующим использованием.",
                        parse_mode='Markdown')
        return
    
    msg = bot.send_message(message.chat.id, 
                          "📍 *Введите адрес доставки:*\n(формат: @username)",
                          parse_mode='Markdown')
    bot.register_next_step_handler(msg, process_bakery)

def process_bakery(message):
    username = message.text.strip()
    if not username.startswith('@'):
        bot.send_message(message.chat.id, 
                        "❌ *Неверный формат!*\n\nУкажите username, начинающийся с @",
                        parse_mode='Markdown')
        return
    
    # Обновляем время последнего использования
    db_query("UPDATE subscriptions SET last_use = ? WHERE user_id = ?", 
             (time.time(), str(message.from_user.id)), commit=True)
    
    status_msg = bot.send_message(message.chat.id, 
                                 f"👨‍🍳 *Готовим пирожки для {username}...*\n\n🔄 Замешиваем тесто...",
                                 parse_mode='Markdown')

    def run_attack():
        success, total, info = start_multi_session_attack(username)
        if success:
            report = f"✅ *Пирожки доставлены!*\n\n📍 Адрес: {username}\n📦 Отправлено: {total} шт.\n\n🎉 Заказ успешно выполнен!"
        else:
            report = f"❌ *Ошибка доставки*\n\nПричина: {total}"
        
        bot.edit_message_text(report, message.chat.id, status_msg.message_id, parse_mode='Markdown')
        logger.info(f"Боевой вылет: {username} результат {total}")

    threading.Thread(target=run_attack).start()

@bot.message_handler(func=lambda m: m.text == "🎫 Абонемент")
def sub_menu(message):
    uid = message.from_user.id
    
    if is_banned(uid):
        bot.send_message(message.chat.id, "🚫 Вы заблокированы.")
        return
    
    sub = db_query("SELECT end_time FROM subscriptions WHERE user_id = ?", (str(uid),), fetchone=True)
    
    if sub and sub[0] > time.time():
        status_text = f"✅ *Активен до:* {format_msk_datetime(sub[0])}"
    else:
        status_text = "❌ *Не активен*"
    
    menu_text = f"""
🎫 *Ваш абонемент*

{status_text}

Выберите срок продления:
"""
    bot.send_message(message.chat.id, menu_text, 
                     parse_mode='Markdown',
                     reply_markup=create_days_keyboard())

@bot.message_handler(func=lambda m: m.text == "📚 Рецепты")
def recipe_handler(message):
    recipe_text = """
📚 *Рецепты работы Пекарни* 🍪

*Основные ингредиенты:*
• DC1, DC3, DC5 - рабочие дата-центры
• Печи 2022-2025 модельного года
• Качественная мука (сессии)

*Процесс приготовления:*
1. Выбираем адрес доставки (@username)
2. Загружаем печи (сессии)
3. Замешиваем тесто (подготовка)
4. Отправляем пирожки (выполнение)
5. Получаем результат

*Важно:* Соблюдайте кулдаун между выпечками!
"""
    bot.send_message(message.chat.id, recipe_text, parse_mode='Markdown')

@bot.message_handler(func=lambda m: m.text == "🛠 Поддержка")
def support_handler(message):
    support_text = f"""
🛠 *Техническая поддержка*

По всем вопросам обращайтесь:
👤 {SUPPORT_USER}

*Часы работы:* круглосуточно
*Среднее время ответа:* 1-2 часа

*Если у вас:*
• Проблемы с оплатой
• Вопросы по работе бота
• Технические неполадки
• Предложения по улучшению
"""
    bot.send_message(message.chat.id, support_text, parse_mode='Markdown')

@bot.message_handler(func=lambda m: m.text == "⚙️ Админ-панель")
def admin_panel_handler(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "🚫 Доступ запрещен.")
        return
    
    admin_text = """
⚙️ *Админ-панель Пекарни*

*Доступные разделы:*
• 👥 Управление пользователями
• 💳 Просмотр платежей
• 📊 Статистика системы
• 🛠 Управление сессиями
• ➕ Добавление новых сессий

Выберите раздел:
"""
    bot.send_message(message.chat.id, admin_text, 
                     parse_mode='Markdown',
                     reply_markup=create_admin_keyboard())

@bot.message_handler(func=lambda m: m.text == "🔙 Главное меню")
def back_to_main(message):
    bot.send_message(message.chat.id, "Возвращаемся в главное меню...",
                     reply_markup=create_main_keyboard())

@bot.message_handler(func=lambda m: m.text == "📊 Статистика" and is_admin(m.from_user.id))
def admin_stats_gui(message):
    if not is_admin(message.from_user.id):
        return
    
    subs_count = db_query("SELECT COUNT(*) FROM subscriptions WHERE end_time > ?", 
                         (time.time(),), fetchone=True)[0]
    total_payments = db_query("SELECT COUNT(*) FROM payments WHERE status = 'paid'", 
                             fetchone=True)[0]
    total_amount = db_query("SELECT SUM(amount) FROM payments WHERE status = 'paid'", 
                           fetchone=True)[0] or 0
    sessions = len(get_session_files())
    bans = db_query("SELECT COUNT(*) FROM bans", fetchone=True)[0]
    
    stats_text = f"""
📊 *Статистика системы*

👥 *Пользователи:*
• Активных подписок: {subs_count}
• Заблокированных: {bans}

💰 *Финансы:*
• Всего оплат: {total_payments}
• Общая сумма: ${total_amount:.2f}

🛠 *Ресурсы:*
• Активных сессий: {sessions}
• Свободных печей: {BAN_SEMAPHORE._value}

📈 *Состояние:* ✅ Работает стабильно
"""
    bot.send_message(message.chat.id, stats_text, parse_mode='Markdown')

@bot.message_handler(func=lambda m: m.text == "➕ Добавить сессию" and is_admin(m.from_user.id))
def add_session_start(message):
    if not is_admin(message.from_user.id):
        return
    
    session_text = """
📱 *Добавление новой сессии*

Для добавления сессии отправьте номер телефона в формате:
• +79991234567
• 79991234567
• 89991234567

*Примечание:* Номер должен быть действительным и привязан к Telegram.
"""
    msg = bot.send_message(message.chat.id, session_text, parse_mode='Markdown')
    bot.register_next_step_handler(msg, process_phone_number)

def process_phone_number(message):
    phone = normalize_phone(message.text.strip())
    
    if not validate_phone_number(phone):
        bot.send_message(message.chat.id, 
                        "❌ *Неверный номер телефона!*\n\nПроверьте формат и попробуйте снова.",
                        parse_mode='Markdown')
        return
    
    # Проверяем, есть ли уже такая сессия
    existing = db_query("SELECT phone FROM sessions WHERE phone = ?", (phone,), fetchone=True)
    if existing:
        bot.send_message(message.chat.id, 
                        "⚠️ *Сессия уже существует!*\n\nЭтот номер телефона уже добавлен в систему.",
                        parse_mode='Markdown')
        return
    
    # Сохраняем номер и запрашиваем код
    db_query("INSERT INTO sessions (phone, session_string, added_at) VALUES (?, ?, ?)",
             (phone, 'pending', time.time()), commit=True)
    
    bot.send_message(message.chat.id,
                    f"✅ *Номер принят:* {phone}\n\nТеперь отправьте код подтверждения, который придет в Telegram:",
                    parse_mode='Markdown')
    
    # Запускаем процесс авторизации в отдельном потоке
    threading.Thread(target=authorize_session, args=(phone, message.chat.id)).start()

def authorize_session(phone, chat_id):
    """Авторизация сессии через Telethon"""
    try:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        
        # Отправляем запрос на код
        client.connect()
        sent_code = client.send_code_request(phone)
        
        # Ждем код от пользователя
        bot.send_message(chat_id, 
                        f"📱 *Код отправлен на {phone}*\n\nОтправьте код подтверждения в формате: `12345`",
                        parse_mode='Markdown')
        
        # Здесь нужно реализовать ожидание кода от пользователя
        # В реальной реализации нужно использовать состояние бота
        
    except Exception as e:
        bot.send_message(chat_id, f"❌ *Ошибка авторизации:* {str(e)}", parse_mode='Markdown')
        logger.error(f"Session auth error: {e}")

@bot.message_handler(func=lambda m: m.text == "🛠 Сессии" and is_admin(m.from_user.id))
def manage_sessions(message):
    if not is_admin(message.from_user.id):
        return
    
    sessions = get_session_files()
    db_sessions = db_query("SELECT phone, added_at FROM sessions", fetchall=True)
    
    sessions_text = """
🛠 *Управление сессиями*

*Файлы сессий (.session):*
"""
    
    if sessions:
        for i, session in enumerate(sessions, 1):
            sessions_text += f"{i}. `{session}`\n"
    else:
        sessions_text += "❌ Нет файлов сессий\n"
    
    sessions_text += "\n*Сессии в базе данных:*\n"
    
    if db_sessions:
        for phone, added_at in db_sessions:
            date_str = datetime.fromtimestamp(added_at).strftime('%d.%m.%Y')
            sessions_text += f"• {phone} (добавлена: {date_str})\n"
    else:
        sessions_text += "❌ Нет сессий в БД\n"
    
    sessions_text += f"\n📊 Всего: {len(sessions)} файлов, {len(db_sessions)} записей в БД"
    
    bot.send_message(message.chat.id, sessions_text, parse_mode='Markdown')

@bot.message_handler(func=lambda m: m.text == "👥 Пользователи" and is_admin(m.from_user.id))
def manage_users(message):
    if not is_admin(message.from_user.id):
        return
    
    # Получаем статистику пользователей
    active_subs = db_query("SELECT COUNT(*) FROM subscriptions WHERE end_time > ?", 
                          (time.time(),), fetchone=True)[0]
    total_bans = db_query("SELECT COUNT(*) FROM bans", fetchone=True)[0]
    
    users_text = f"""
👥 *Управление пользователями*

📈 *Статистика:*
• Активных подписок: {active_subs}
• Заблокированных: {total_bans}

⚡ *Быстрые команды:*
`/ban <user_id>` - заблокировать
`/unban <user_id>` - разблокировать
`/addsub <user_id> <days>` - выдать подписку
`/rmsub <user_id>` - удалить подписку

📋 *Пример:*
`/ban 123456789`
`/addsub 123456789 30`
"""
    bot.send_message(message.chat.id, users_text, parse_mode='Markdown')

@bot.message_handler(func=lambda m: m.text == "💳 Платежи" and is_admin(m.from_user.id))
def manage_payments(message):
    if not is_admin(message.from_user.id):
        return
    
    # Получаем последние 10 платежей
    payments = db_query("SELECT invoice_id, user_id, amount, days, status, created_at FROM payments ORDER BY created_at DESC LIMIT 10", 
                       fetchall=True)
    
    payments_text = """
💳 *Последние платежи*

"""
    
    if payments:
        for inv_id, user_id, amount, days, status, created_at in payments:
            date_str = datetime.fromtimestamp(created_at).strftime('%d.%m %H:%M')
            status_icon = "✅" if status == 'paid' else "⏳" if status == 'pending' else "❌"
            payments_text += f"{status_icon} *{user_id}* - ${amount} ({days} дн.)\n`{inv_id[:8]}...` - {date_str}\n\n"
    else:
        payments_text += "📭 Нет платежей\n"
    
    total_paid = db_query("SELECT SUM(amount) FROM payments WHERE status = 'paid'", fetchone=True)[0] or 0
    payments_text += f"\n💰 *Всего получено:* ${total_paid:.2f}"
    
    bot.send_message(message.chat.id, payments_text, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda c: True)
def handle_callbacks(call):
    if is_banned(call.from_user.id):
        return
    
    data = call.data
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    if data == "back_to_days":
        bot.edit_message_text("🎫 *Выберите срок абонемента:*", 
                             chat_id, msg_id, 
                             reply_markup=create_days_keyboard(),
                             parse_mode='Markdown')
    
    elif data.startswith("sel_"):
        days = int(data.split("_")[1])
        price_usd = PRICES_USD[days]
        price_rub = PRICES_RUB[days]
        text = f"""
🎫 *Абонемент на {days} дней*

*Стоимость:*
• {price_usd}$ через CryptoBot
• {price_rub}₽ на карту

Выберите способ оплаты:
"""
        bot.edit_message_text(text, chat_id, msg_id,
                             reply_markup=create_pay_method_keyboard(days),
                             parse_mode='Markdown')
    
    elif data.startswith("pay_crypto_"):
        days = int(data.split("_")[2])
        price = PRICES_USD[days]
        ok, inv = cryptobot.create_invoice(price, f"Bakery Subscription {days} days")
        
        if ok:
            db_query("INSERT INTO payments VALUES (?, ?, ?, ?, ?, ?)",
                     (str(inv['invoice_id']), call.from_user.id, price, days, "pending", time.time()), commit=True)
            
            kb = types.InlineKeyboardMarkup(row_width=1)
            kb.add(types.InlineKeyboardButton("💎 Оплатить", url=inv['pay_url']))
            kb.add(types.InlineKeyboardButton("🔄 Проверить оплату", callback_data=f"chk_{inv['invoice_id']}"))
            kb.add(types.InlineKeyboardButton("↩️ Назад", callback_data=f"sel_{days}"))
            
            text = f"""
💎 *Оплата через CryptoBot*

🔗 Ссылка для оплаты готова!
Сумма: *{price}$*
Дней: *{days}*

*После оплаты нажмите "Проверить оплату":*
"""
            bot.edit_message_text(text, chat_id, msg_id,
                                 reply_markup=kb,
                                 parse_mode='Markdown')
    
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
                
                text = f"""
✅ *Абонемент активирован!*

🎫 Срок: *{p[1]} дней*
⏳ Действует до: *{format_msk_datetime(end)}*

Теперь вы можете использовать *Выпечку* 🍪
"""
                bot.edit_message_text(text, chat_id, msg_id, parse_mode='Markdown')
    
    elif data.startswith("pay_card_"):
        days = int(data.split("_")[2])
        price_rub = PRICES_RUB[days]
        
        text = f"""
💳 *Оплата банковской картой*

*Реквизиты:*
СберБанк: `2202208359860005`

*Сумма:* {price_rub} руб.
*Назначение:* Пекарня {days} дней

*После оплаты:*
1. Сохраните чек (PDF)
2. Отправьте его в этот чат
3. Ожидайте проверки (1-12 часов)

*Примечание:* Платежи проверяются вручную администратором.
"""
        bot.edit_message_text(text, chat_id, msg_id, parse_mode='Markdown')

# --- АДМИН КОМАНДЫ (остаются как были) ---
@bot.message_handler(commands=['adminhelp'])
def admin_help(message):
    if not is_admin(message.from_user.id):
        return
    text = """
📋 *Команды администратора*

👥 *Пользователи:*
`/ban <user_id>` - заблокировать
`/unban <user_id>` - разбанить
`/addsub <user_id> <days>` - выдать подписку
`/rmsub <user_id>` - удалить подписку

⚡ *Действия:*
`/attack <@username>` - выполнить Выпечку
`/sessions` - показать сессии

📊 *Информация:*
`/stats` - статистика
`/adminhelp` - эта справка

*Также доступна графическая панель:* ⚙️ Админ-панель
"""
    bot.send_message(message.chat.id, text, parse_mode='Markdown')

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
    bot.send_message(message.chat.id, f"✅ Пользователь {uid} заблокирован.")

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
    bot.send_message(message.chat.id, f"✅ Пользователь {uid} разбанен.")

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
    bot.send_message(message.chat.id, f"✅ Подписка для {uid} выдана на {days} дн.")

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
    bot.send_message(message.chat.id, f"✅ Подписка пользователя {uid} удалена.")

@bot.message_handler(commands=['sessions'])
def cmd_sessions(message):
    if not is_admin(message.from_user.id):
        return
    sessions = get_session_files()
    if not sessions:
        bot.send_message(message.chat.id, "Сессий не найдено.")
        return
    bot.send_message(message.chat.id, "📱 *Сессии:*\n" + "\n".join(f"• `{s}`" for s in sessions), 
                     parse_mode='Markdown')

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

    status_msg = bot.send_message(message.chat.id, f"👨‍🍳 *Запускаю выпечку для {username}...*", 
                                 parse_mode='Markdown')

    def run_attack_cmd():
        success, total, info = start_multi_session_attack(username)
        report = f"✅ *Пирожки доставлены!*\n📍 Адрес: {username}\n📦 Отправлено: {total} шт." if success else f"❌ *Ошибка:* {total}"
        bot.edit_message_text(report, message.chat.id, status_msg.message_id, parse_mode='Markdown')

    threading.Thread(target=run_attack_cmd).start()

@bot.message_handler(commands=['stats'])
def admin_stats(message):
    if message.from_user.id not in ADMIN_IDS:
        return
    subs_count = db_query("SELECT COUNT(*) FROM subscriptions WHERE end_time > ?", (time.time(),), fetchone=True)[0]
    total_payments = db_query("SELECT COUNT(*) FROM payments WHERE status = 'paid'", fetchone=True)[0]
    sessions = len(get_session_files())
    text = f"""
📊 *Статистика:*
• 🎫 Активных подписок: {subs_count}
• 💰 Успешных оплат: {total_payments}
• 📱 Активных сессий: {sessions}
• ⚡ Печей свободно: {BAN_SEMAPHORE._value}
"""
    bot.send_message(message.chat.id, text, parse_mode='Markdown')

# Функция атаки (остается без изменений)
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
           