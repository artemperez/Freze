import os
import asyncio
import shutil
from telethon import TelegramClient
from telethon.errors import (
    AuthKeyDuplicatedError, 
    UserDeactivatedError, 
    SessionPasswordNeededError,
    PeerFloodError,
    UserRestrictedError
)

# --- Настройки (взяты из твоего основного бота) ---
API_ID = 21826549
API_HASH = "c1a19f792cfd9e397200d16c7e448160"
SESSIONS_DIR = "sessions"
INVALID_DIR = "invalid"

# Создаем нужные папки
for folder in [SESSIONS_DIR, INVALID_DIR]:
    if not os.path.exists(folder):
        os.makedirs(folder)

def move_to_invalid(session_path):
    """Безопасно перемещает файлы .session и .session-journal в папку invalid"""
    try:
        if not os.path.exists(session_path):
            return
        
        file_name = os.path.basename(session_path)
        dest_path = os.path.join(INVALID_DIR, file_name)
        
        # Перемещаем основной файл
        shutil.move(session_path, dest_path)
        
        # Перемещаем файл журнала, если он есть (бывает при активном использовании)
        journal = session_path + "-journal"
        if os.path.exists(journal):
            shutil.move(journal, dest_path + "-journal")
            
    except Exception as e:
        print(f"\n[!] Ошибка при перемещении файла {session_path}: {e}")

async def check_session(session_path):
    """Проверяет сессию на валидность и наличие ограничений"""
    # Убираем расширение для инициализации клиента Telethon
    session_name = session_path[:-8] if session_path.endswith('.session') else session_path
    
    # connection_retries=0 чтобы не тратить время на мертвые прокси/соединения
    client = TelegramClient(session_name, API_ID, API_HASH, connection_retries=0, timeout=10)
    
    try:
        # 1. Попытка подключения (таймаут 15 сек на всё про всё)
        await asyncio.wait_for(client.connect(), timeout=15)
        
        # 2. Проверка авторизации
        if not await client.is_user_authorized():
            return False, "❌ Не авторизован"

        # 3. Получение данных и проверка на деактивацию
        try:
            me = await client.get_me()
            if not me:
                return False, "❌ Аккаунт пустой/удален"
            
            # 4. Проверка на ограничения (Spamblock / Заморозка)
            # Пытаемся отправить сообщение самому себе в "Избранное"
            try:
                await client.send_message("me", "System Check: Validating account status...")
                return True, f"✅ Полностью валиден | @{me.username or me.id}"
            
            except (PeerFloodError, UserRestrictedError):
                return False, "🚫 Ограничен (Spamblock/Заморозка)"
            except Exception as e:
                return False, f"⚠️ Ошибка при проверке ограничений: {e}"

        except UserDeactivatedError:
            return False, "❌ Аккаунт деактивирован (Бан)"

    except asyncio.TimeoutError:
        return False, "⏳ Таймаут (сессия не отвечает)"
    except AuthKeyDuplicatedError:
        return False, "❌ Ключ дублирован (Session dead)"
    except Exception as e:
        return False, f"❌ Критическая ошибка: {e}"
    finally:
        try:
            await client.disconnect()
        except:
            pass

async def main():
    print("="*60)
    print("🚀 Запуск глубокой проверки сессий...")
    print(f"Папка сессий: {SESSIONS_DIR}")
    print(f"Папка невалида: {INVALID_DIR}")
    print("="*60)

    files = [f for f in os.listdir(SESSIONS_DIR) if f.endswith('.session')]
    
    if not files:
        print("В папке /sessions нет файлов .session для проверки.")
        return

    print(f"Найдено файлов: {len(files)}")
    
    valid_count = 0
    invalid_count = 0

    for file in files:
        full_path = os.path.join(SESSIONS_DIR, file)
        
        # Если файл пропал (например, перемещен вручную во время работы)
        if not os.path.exists(full_path):
            continue
            
        print(f"🔍 Проверяю: {file.ljust(20)}", end=" | ", flush=True)
        
        try:
            is_ok, message = await check_session(full_path)
            print(message)

            if is_ok:
                valid_count += 1
            else:
                invalid_count += 1
                move_to_invalid(full_path)
                print(f"   ┗━──> Перемещен в /{INVALID_DIR}")
        
        except Exception as e:
            print(f"Критический сбой на файле {file}: {e}")
            move_to_invalid(full_path)
            invalid_count += 1

    print("="*60)
    print(f"📊 ИТОГИ ПРОВЕРКИ:")
    print(f"✅ Чистые сессии: {valid_count}")
    print(f"❌ Невалид/Заморозка: {invalid_count}")
    print("="*60)
    print("Все плохие сессии были отсеяны в папку /invalid.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[!] Проверка прервана пользователем.")