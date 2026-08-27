import os
import asyncio
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.errors import PhoneNumberInvalidError, PhoneNumberOccupiedError

# Конфиги
API_ID = "34910343"  # Получи на https://my.telegram.org/apps
API_HASH = "d20860d195501ba0993b794f952b1ef1"

# Директория для сессий
SESSIONS_DIR = "sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)

# Хранилище активных клиентов и процессов входа
active_clients = {}
auth_processes = {}  # {user_id: {"client": client, "phone": phone, "hash": hash}}

# Создаем Telegram бота
bot = TelegramClient("bot_session", API_ID, API_HASH)

@bot.on(events.NewMessage(pattern="/start"))
async def start_handler(event):
    """Команда /start с кнопками"""
    from telethon.tl.types import ReplyKeyboardMarkup, KeyboardButton
    
    buttons = [
        [KeyboardButton("📋 Список сессий"), KeyboardButton("➕ Загрузить сессию")],
        [KeyboardButton("🔑 Получить код входа"), KeyboardButton("✅ Проверить сессию")]
    ]
    
    await event.reply(
        "👋 Привет! Я работаю с Telethon сессиями.\n\n"
        "Выбери действие:",
        buttons=ReplyKeyboardMarkup(buttons, resize=True)
    )

@bot.on(events.NewMessage(pattern="📋 Список сессий"))
async def list_sessions_handler(event):
    """Список доступных файлов сессий"""
    try:
        if not os.path.exists(SESSIONS_DIR):
            await event.reply("📭 Папка сессий не существует.")
            return
        
        session_files = [f[:-7] for f in os.listdir(SESSIONS_DIR) if f.endswith(".session")]
        
        if not session_files:
            await event.reply("📭 Нет доступных сессий в папке.")
            return
        
        message = "📋 Доступные сессии:\n\n"
        for i, session in enumerate(session_files, 1):
            session_path = os.path.join(SESSIONS_DIR, session)
            size = os.path.getsize(f"{session_path}.session")
            message += f"{i}. `{session}` ({size} bytes)\n"
        
        await event.reply(message)
    
    except Exception as e:
        await event.reply(f"❌ Ошибка: {str(e)}")

@bot.on(events.NewMessage(pattern="🔑 Получить код входа"))
async def get_login_code_handler(event):
    """Запрос номера телефона для получения кода входа"""
    await event.reply(
        "📱 Отправь номер телефона в формате:\n"
        "`+79991234567`\n\n"
        "Или отправь сообщение `/login_with_phone +79991234567`"
    )

@bot.on(events.NewMessage(pattern=r"^\+\d{10,15}$"))
async def phone_number_handler(event):
    """Обработка номера телефона"""
    try:
        phone = event.text.strip()
        user_id = event.sender_id
        
        # Создаем временного клиента для получения кода
        client = TelegramClient(f"temp_{user_id}", API_ID, API_HASH)
        await client.connect()
        
        # Запрашиваем код
        result = await client.send_code_request(phone)
        
        # Сохраняем информацию для следующего шага
        auth_processes[user_id] = {
            "client": client,
            "phone": phone,
            "phone_code_hash": result.phone_code_hash
        }
        
        await event.reply(
            f"✅ Код входа отправлен на номер {phone}\n\n"
            f"📲 Проверь свой Telegram и отправь сюда код\n"
            f"Используй: `/enter_code КОД`\n\n"
            f"Или просто отправь код в чат (например: `123456`)"
        )
    
    except PhoneNumberInvalidError:
        await event.reply("❌ Неверный формат номера телефона. Используй: +79991234567")
    except PhoneNumberOccupiedError:
        await event.reply("❌ Этот номер уже зарегистрирован в Telegram")
    except Exception as e:
        await event.reply(f"❌ Ошибка: {str(e)}")

@bot.on(events.NewMessage(pattern="/login_with_phone"))
async def login_with_phone_handler(event):
    """Команда для входа с номером телефона"""
    try:
        args = event.text.split(maxsplit=1)
        if len(args) < 2:
            await event.reply("Использование: `/login_with_phone +79991234567`")
            return
        
        phone = args[1].strip()
        user_id = event.sender_id
        
        client = TelegramClient(f"temp_{user_id}", API_ID, API_HASH)
        await client.connect()
        
        result = await client.send_code_request(phone)
        
        auth_processes[user_id] = {
            "client": client,
            "phone": phone,
            "phone_code_hash": result.phone_code_hash
        }
        
        await event.reply(
            f"✅ Код отправлен на {phone}\n\n"
            f"Отправь код: `/enter_code КОД`"
        )
    
    except Exception as e:
        await event.reply(f"❌ Ошибка: {str(e)}")

@bot.on(events.NewMessage(pattern="/enter_code"))
async def enter_code_handler(event):
    """Ввод кода входа"""
    try:
        args = event.text.split(maxsplit=1)
        if len(args) < 2:
            await event.reply("Использование: `/enter_code 123456`")
            return
        
        code = args[1].strip()
        user_id = event.sender_id
        
        if user_id not in auth_processes:
            await event.reply("❌ Сначала запроси код через кнопку '🔑 Получить код входа'")
            return
        
        auth_data = auth_processes[user_id]
        client = auth_data["client"]
        phone = auth_data["phone"]
        phone_code_hash = auth_data["phone_code_hash"]
        
        try:
            # Пытаемся войти с кодом
            await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
            
            # Получаем информацию о пользователе
            me = await client.get_me()
            
            # Сохраняем сессию с нормальным именем
            session_name = f"{me.first_name}_{me.id}"
            session_path = os.path.join(SESSIONS_DIR, session_name)
            
            # Экспортируем сессию в новый файл
            await client.log_out()
            
            # Создаем новую сессию с сохранённой
            new_client = TelegramClient(session_path, API_ID, API_HASH)
            await new_client.connect()
            
            await new_client.sign_in(phone, code, phone_code_hash=phone_code_hash)
            me = await new_client.get_me()
            
            active_clients[session_name] = new_client
            
            # Удаляем временный процесс
            del auth_processes[user_id]
            
            await event.reply(
                f"✅ Успешный вход!\n\n"
                f"👤 Пользователь: {me.first_name}\n"
                f"🆔 ID: {me.id}\n"
                f"📱 Username: @{me.username if me.username else 'N/A'}\n\n"
                f"💾 Сессия сохранена как: `{session_name}`"
            )
        
        except SessionPasswordNeededError:
            await event.reply(
                "🔐 Требуется пароль двухфакторной аутентификации\n"
                "Отправь пароль: `/enter_password ваш_пароль`"
            )
        
    except Exception as e:
        await event.reply(f"❌ Ошибка при вводе кода: {str(e)}")

@bot.on(events.NewMessage(pattern="/enter_password"))
async def enter_password_handler(event):
    """Ввод пароля 2FA"""
    try:
        args = event.text.split(maxsplit=1)
        if len(args) < 2:
            await event.reply("Исп��льзование: `/enter_password ваш_пароль`")
            return
        
        password = args[1].strip()
        user_id = event.sender_id
        
        if user_id not in auth_processes:
            await event.reply("❌ Процесс входа не активен")
            return
        
        client = auth_processes[user_id]["client"]
        
        await client.sign_in(password=password)
        me = await client.get_me()
        
        session_name = f"{me.first_name}_{me.id}"
        session_path = os.path.join(SESSIONS_DIR, session_name)
        
        active_clients[session_name] = client
        del auth_processes[user_id]
        
        await event.reply(
            f"✅ 2FA пройдена!\n\n"
            f"👤 Пользователь: {me.first_name}\n"
            f"🆔 ID: {me.id}\n"
            f"💾 Сессия сохранена как: `{session_name}`"
        )
    
    except Exception as e:
        await event.reply(f"❌ Ошибка: {str(e)}")

@bot.on(events.NewMessage(pattern="✅ Проверить сессию"))
async def check_session_menu_handler(event):
    """Меню для проверки сессии"""
    await event.reply(
        "Введи имя сессии для проверки:\n"
        "`/check_session имя_сессии`"
    )

@bot.on(events.NewMessage(pattern="/check_session"))
async def check_session_handler(event):
    """Проверить статус сессии"""
    try:
        args = event.text.split(maxsplit=1)
        if len(args) < 2:
            await event.reply("Использование: `/check_session имя_сессии`")
            return
        
        session_name = args[1].strip()
        session_path = os.path.join(SESSIONS_DIR, session_name)
        
        if not os.path.exists(f"{session_path}.session"):
            await event.reply(f"❌ Сессия '{session_name}' не найдена!")
            return
        
        # Загружаем сессию
        client = TelegramClient(session_path, API_ID, API_HASH)
        await client.connect()
        
        if await client.is_user_authorized():
            me = await client.get_me()
            await event.reply(
                f"✅ Сессия активна!\n\n"
                f"👤 Пользователь: {me.first_name} {me.last_name or ''}\n"
                f"🆔 ID: {me.id}\n"
                f"📱 Username: @{me.username if me.username else 'N/A'}\n"
                f"📱 Телефон: {me.phone}"
            )
        else:
            await event.reply(f"⚠️ Сессия '{session_name}' требует авторизации")
        
        await client.disconnect()
    
    except Exception as e:
        await event.reply(f"❌ Ошибка: {str(e)}")

@bot.on(events.NewMessage(pattern="➕ Загрузить сессию"))
async def load_session_menu_handler(event):
    """Меню для загрузки сессии"""
    try:
        if not os.path.exists(SESSIONS_DIR):
            await event.reply("📭 Папка сессий не существует.")
            return
        
        session_files = [f[:-7] for f in os.listdir(SESSIONS_DIR) if f.endswith(".session")]
        
        if not session_files:
            await event.reply("📭 Нет доступных сессий в папке.")
            return
        
        from telethon.tl.types import ReplyKeyboardMarkup, KeyboardButton
        
        buttons = [[KeyboardButton(session)] for session in session_files]
        buttons.append([KeyboardButton("« Назад")])
        
        await event.reply(
            "Выбери сессию для загрузки:",
            buttons=ReplyKeyboardMarkup(buttons, resize=True)
        )
    
    except Exception as e:
        await event.reply(f"❌ Ошибка: {str(e)}")

@bot.on(events.NewMessage())
async def session_select_handler(event):
    """Обработка выбора сессии из кнопок"""
    try:
        text = event.text.strip()
        session_name = text
        
        if text == "« Назад":
            from telethon.tl.types import ReplyKeyboardMarkup, KeyboardButton
            buttons = [
                [KeyboardButton("📋 Список сессий"), KeyboardButton("➕ Загрузить сессию")],
                [KeyboardButton("🔑 Получить код входа"), KeyboardButton("✅ Проверить сессию")]
            ]
            await event.reply("Выбери действие:", buttons=ReplyKeyboardMarkup(buttons, resize=True))
            return
        
        session_path = os.path.join(SESSIONS_DIR, session_name)
        
        if os.path.exists(f"{session_path}.session"):
            # Загружаем сессию
            client = TelegramClient(session_path, API_ID, API_HASH)
            await client.connect()
            
            if await client.is_user_authorized():
                me = await client.get_me()
                active_clients[session_name] = client
                
                await event.reply(
                    f"✅ Сессия '{session_name}' загружена!\n\n"
                    f"👤 Пользователь: {me.first_name}\n"
                    f"🆔 ID: {me.id}\n"
                    f"📱 Username: @{me.username if me.username else 'N/A'}"
                )
            else:
                await event.reply(f"⚠️ Сессия '{session_name}' требует авторизации")
                await client.disconnect()
    
    except Exception as e:
        pass

async def main():
    """Запуск бота"""
    print("🤖 Запуск бота...")
    async with bot:
        await 8842192137:AAEjfWCKek-riiQ_jzyQFqS9EW13Tj8CcRs  # Получи токен у @BotFather
        print("✅ Бот запущен!")
        await bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
