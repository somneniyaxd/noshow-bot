import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton

# ============================================================
# ТОКЕН
# ============================================================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# ============================================================
# База данных
# ============================================================
DB = "noshow.db"


def init_db():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            groomer_id INTEGER,
            name TEXT,
            chat_id INTEGER,
            visit_time TEXT,
            confirmed INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            role TEXT
        )
    """)
    conn.commit()
    conn.close()


def set_role(user_id, role):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO users (user_id, role) VALUES (?, ?)", (user_id, role))
    conn.commit()
    conn.close()


def get_role(user_id):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT role FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def add_client(groomer_id, name, chat_id, visit_time):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO clients (groomer_id, name, chat_id, visit_time) VALUES (?, ?, ?, ?)",
        (groomer_id, name, chat_id, visit_time),
    )
    conn.commit()
    conn.close()


def get_clients(groomer_id):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, visit_time, confirmed FROM clients WHERE groomer_id = ? ORDER BY visit_time",
        (groomer_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def cleanup_old_records():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    now = datetime.now()
    cutoff = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
    cur.execute("DELETE FROM clients WHERE visit_time < ?", (cutoff,))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    return deleted


# ============================================================
# Бот
# ============================================================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

role_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Я грумер")],
        [KeyboardButton(text="Я клиент")],
    ],
    resize_keyboard=True,
)

main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📋 Мои записи")],
        [KeyboardButton(text="➕ Добавить запись")],
    ],
    resize_keyboard=True,
)


@dp.message(Command("start"))
async def cmd_start(message: Message):
    role = get_role(message.from_user.id)

    if role == "groomer":
        await message.answer(
            f"С возвращением, {message.from_user.first_name}!\n\n"
            "Ты вошёл как грумер. Используй кнопки ниже.",
            reply_markup=main_kb,
        )
    elif role == "client":
        await message.answer(
            "Ты в режиме клиента. Всё в порядке — жди напоминания о записи."
        )
    else:
        await message.answer(
            f"Привет, {message.from_user.first_name}!\n\n"
            "Кто ты? Это нужно выбрать один раз.",
            reply_markup=role_kb,
        )


@dp.message(F.text == "Я грумер")
async def choose_groomer(message: Message):
    set_role(message.from_user.id, "groomer")
    await message.answer(
        "Отлично! Теперь ты можешь добавлять клиентов.\n\n"
        "Используй кнопки ниже или /add.",
        reply_markup=main_kb,
    )


@dp.message(F.text == "Я клиент")
async def choose_client(message: Message):
    set_role(message.from_user.id, "client")
    await message.answer(
        "Понял! Ты клиент. Когда приблизится время записи — я напомню.",
        reply_markup=ReplyKeyboardMarkup(keyboard=[], resize_keyboard=True),
    )


@dp.message(Command("add"))
async def cmd_add(message: Message):
    if get_role(message.from_user.id) != "groomer":
        await message.answer("Эта команда только для грумеров.")
        return
    await message.answer(
        "Отправь данные клиента в формате:\n\n"
        "Имя, chat_id, ГГГГ-ММ-ДД ЧЧ:ММ\n\n"
        "📌 Пример:\n"
        "Барсик, 473980999, 2026-09-18 15:30\n\n"
        "Где:\n"
        "• Барсик — имя клиента\n"
        "• 473980999 — его Telegram ID (узнать через @userinfobot)\n"
        "• 2026-09-18 15:30 — дата и время визита\n\n"
        "Дата обязательно в будущем!"
    )


@dp.message(F.text.regexp(r"^.+,.+,.+$"))
async def handle_add(message: Message):
    if get_role(message.from_user.id) != "groomer":
        return
    try:
        parts = [p.strip() for p in message.text.split(",")]
        name, chat_id, visit_time = parts[0], int(parts[1]), parts[2]
        datetime.strptime(visit_time, "%Y-%m-%d %H:%M")

        add_client(message.from_user.id, name, chat_id, visit_time)
        await message.answer(f"✅ Запись для {name} на {visit_time} сохранена.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}\n\nФормат: Имя, chat_id, 2026-09-18 15:30")


@dp.message(F.text.lower().in_({"да", "yes", "+", "подтверждаю"}))
async def handle_confirm(message: Message):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute(
        "UPDATE clients SET confirmed = 1 WHERE chat_id = ? AND confirmed = 0",
        (message.from_user.id,),
    )
    changed = cur.rowcount
    conn.commit()
    conn.close()

    if changed:
        await message.answer("✅ Спасибо! Ваша запись подтверждена.")
    else:
        await message.answer("У вас нет активных записей для подтверждения.")


@dp.message(Command("list"))
async def cmd_list(message: Message):
    if get_role(message.from_user.id) != "groomer":
        await message.answer("Эта команда только для грумеров.")
        return
    rows = get_clients(message.from_user.id)
    if not rows:
        await message.answer("Пока нет записей.")
        return
    text = "📋 Твои записи:\n\n"
    for rid, name, visit_time, confirmed in rows:
        status = "✅" if confirmed else "⏳"
        text += f"{status} {name} — {visit_time}\n"
    await message.answer(text)


@dp.message(F.text == "📋 Мои записи")
async def btn_list(message: Message):
    await cmd_list(message)


@dp.message(F.text == "➕ Добавить запись")
async def btn_add(message: Message):
    await cmd_add(message)


# ============================================================
# Автонапоминания: два захода — за 3 часа и за 30 минут
# ============================================================
async def reminder_loop():
    while True:
        try:
            conn = sqlite3.connect(DB)
            cur = conn.cursor()
            now = datetime.now()

            # --- Напоминание 1: за 3 часа ---
            target_3h = now + timedelta(hours=3)
            cur.execute(
                "SELECT id, name, chat_id, visit_time FROM clients "
                "WHERE confirmed = 0 AND visit_time BETWEEN ? AND ?",
                (target_3h.strftime("%Y-%m-%d %H:%M"),
                 (target_3h + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")),
            )
            rows_3h = cur.fetchall()

            # --- Напоминание 2: за 30 минут ---
            target_30m = now + timedelta(minutes=30)
            cur.execute(
                "SELECT id, name, chat_id, visit_time FROM clients "
                "WHERE confirmed = 0 AND visit_time BETWEEN ? AND ?",
                (target_30m.strftime("%Y-%m-%d %H:%M"),
                 (target_30m + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")),
            )
            rows_30m = cur.fetchall()
            conn.close()

            print(f"[{now.strftime('%H:%M')}] За 3ч: {len(rows_3h)} | За 30мин: {len(rows_30m)}")

            # --- Отправка: за 3 часа ---
            for cid, name, chat_id, visit_time in rows_3h:
                try:
                    visit_dt = datetime.strptime(visit_time, "%Y-%m-%d %H:%M")
                    day_word = "сегодня" if visit_dt.date() == now.date() else "завтра"
                    time_str = visit_dt.strftime("%H:%M")
                    await bot.send_message(
                        chat_id,
                        f"Здравствуйте! Напоминаем, что {day_word} у вас запись на груминг в {time_str}.\n\n"
                        "Если не сможете прийти — предупредите, пожалуйста."
                    )
                    print(f"[3ч OK] {name} ({chat_id})")
                except Exception as e:
                    print(f"[3ч ОШИБКА] {name}: {e}")

            # --- Отправка: за 30 минут ---
            for cid, name, chat_id, visit_time in rows_30m:
                try:
                    time_str = datetime.strptime(visit_time, "%Y-%m-%d %H:%M").strftime("%H:%M")
                    await bot.send_message(
                        chat_id,
                        f"Здравствуйте! Ваша запись на груминг через полчаса — в {time_str}.\n\n"
                        "Ответьте «Да», чтобы подтвердить."
                    )
                    print(f"[30мин OK] {name} ({chat_id})")
                except Exception as e:
                    print(f"[30мин ОШИБКА] {name}: {e}")

        except Exception as e:
            logging.error(f"Ошибка в reminder_loop: {e}")

        await asyncio.sleep(60)


# ============================================================
# Очистка старых записей: раз в час удаляем то, что старше суток
# ============================================================
async def cleanup_loop():
    while True:
        try:
            deleted = cleanup_old_records()
            if deleted:
                print(f"[ОЧИСТКА] Удалено старых записей: {deleted}")
        except Exception as e:
            logging.error(f"Ошибка в cleanup_loop: {e}")
        await asyncio.sleep(3600)


# ============================================================
# Запуск
# ============================================================
async def main():
    logging.basicConfig(level=logging.INFO)
    init_db()
    asyncio.create_task(reminder_loop())
    asyncio.create_task(cleanup_loop())
    print("Бот запущен. Нажми Ctrl+C для остановки.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())