import asyncio
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
)

API_ID = 34910343
API_HASH = "d20860d195501ba0993b794f952b1ef1"

SESSIONS_DIR = Path("sessions")
SESSIONS_DIR.mkdir(exist_ok=True)


def get_sessions():
    return sorted(SESSIONS_DIR.glob("*.session"))


async def get_account_info(session):
    client = TelegramClient(
        str(session.with_suffix("")),
        API_ID,
        API_HASH
    )

    try:
        await client.connect()

        if not await client.is_user_authorized():
            return "НЕ АВТОРИЗОВАНА", None

        me = await client.get_me()

        return "OK", me

    except Exception as e:
        return f"ОШИБКА: {e}", None

    finally:
        await client.disconnect()


async def show_sessions():
    sessions = get_sessions()

    if not sessions:
        print("\nПапка sessions пустая.")
        return

    print("\n========== СЕССИИ ==========")

    for i, session in enumerate(sessions, 1):
        status, me = await get_account_info(session)

        print(f"\n{i}. {session.name}")
        print(f"   Статус: {status}")

        if me:
            print(f"   ID: {me.id}")
            print(f"   Телефон: {me.phone or '-'}")
            print(f"   Username: @{me.username}" if me.username else
                  "   Username: -")


async def request_code():
    sessions = get_sessions()

    if not sessions:
        print("\nНет .session файлов.")
        return

    print("\n========== ВЫБЕРИТЕ СЕССИЮ ==========")

    for i, session in enumerate(sessions, 1):
        print(f"{i}. {session.name}")

    choice = input("\nНомер сессии: ").strip()

    try:
        session = sessions[int(choice) - 1]
    except (ValueError, IndexError):
        print("Неверный выбор.")
        return

    client = TelegramClient(
        str(session.with_suffix("")),
        API_ID,
        API_HASH
    )

    try:
        await client.connect()

        me = await client.get_me()

        if not me:
            print("\nЭта сессия не авторизована.")
            print("Для новой авторизации потребуется номер телефона.")
            return

        print(f"\nАккаунт: @{me.username or '-'}")
        print(f"Телефон: {me.phone or '-'}")

        phone = input(
            "\nВведите номер телефона этого аккаунта: "
        ).strip()

        if not phone:
            return

        print("\nЗапрашиваю код у Telegram...")

        sent = await client.send_code_request(phone)

        print("\nКод запрошен.")
        print("Telegram покажет/отправит код доступным способом.")
        print("Введите код вручную, когда он у вас появится.")

        code = input("\nКод: ").strip()

        try:
            await client.sign_in(
                phone=phone,
                code=code
            )

        except SessionPasswordNeededError:
            password = input("Пароль 2FA: ")
            await client.sign_in(password=password)

        me = await client.get_me()

        print("\nУСПЕШНО!")
        print(f"ID: {me.id}")
        print(f"Username: @{me.username or '-'}")

    except PhoneCodeInvalidError:
        print("\nНеверный код.")

    except PhoneNumberInvalidError:
        print("\nНеверный номер.")

    except Exception as e:
        print(f"\nОшибка: {e}")

    finally:
        await client.disconnect()


async def main():
    while True:
        print("""
========================================
       TELEGRAM SESSION MANAGER
========================================

1. Показать все сессии и аккаунты
2. Получить код
0. Выход

========================================
""")

        choice = input("Выберите действие: ").strip()

        if choice == "1":
            await show_sessions()

        elif choice == "2":
            await request_code()

        elif choice == "0":
            break

        else:
            print("Неизвестная команда.")


if __name__ == "__main__":
    asyncio.run(main())
