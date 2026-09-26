import asyncio
import logging
import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg2

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    ReplyKeyboardRemove,
)

# ============================================================
# КОНФИГ
# ============================================================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
BOT_USERNAME = "noshow_killer_bot"  # без @
DATABASE_URL = os.getenv("DATABASE_URL", "noshow.db")

TIMEZONE = os.getenv("TIMEZONE", "Europe/Minsk")
TZ = ZoneInfo(TIMEZONE)

ADMIN_ID = 473980999

TRIAL_DAYS = 3
PAID_DAYS = 30

DEFAULT_REMINDER_MINUTES = 30
SILENT_TIMEOUT_MINUTES = 10

REMINDER_PRESETS = [
    (30, "За 30 минут"),
    (60, "За 1 час"),
    (120, "За 2 часа"),
    (180, "За 3 часа"),
    (360, "За 6 часов"),
    (720, "За 12 часов"),
    (1440, "За 24 часа"),
]

PAYMENT_LINK = "https://example.com/pay"  # ЗАГЛУШКА

USE_POSTGRES = DATABASE_URL.startswith("postgres")


def now():
    return datetime.now(TZ)


def format_minutes(minutes):
    if minutes < 60:
        return f"{minutes} мин"
    hours = minutes // 60
    mins = minutes % 60
    if mins == 0:
        if hours % 10 == 1 and hours % 100 != 11:
            word = "час"
        elif hours % 10 in (2, 3, 4) and hours % 100 not in (12, 13, 14):
            word = "часа"
        else:
            word = "часов"
        return f"{hours} {word}"
    return f"{hours} ч {mins} мин"


def gen_token():
    return secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:10]


# ============================================================
# БАЗА
# ============================================================
def get_conn():
    if USE_POSTGRES:
        return psycopg2.connect(DATABASE_URL)
    return sqlite3.connect(DATABASE_URL)


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    if USE_POSTGRES:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                role TEXT,
                reminder_minutes INTEGER DEFAULT 30
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                id SERIAL PRIMARY KEY,
                groomer_id BIGINT,
                name TEXT,
                chat_id BIGINT,
                invite_token TEXT UNIQUE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                id SERIAL PRIMARY KEY,
                groomer_id BIGINT,
                customer_id INTEGER,
                name TEXT,
                chat_id BIGINT,
                visit_time TEXT,
                confirmed INTEGER DEFAULT 0,
                cancelled INTEGER DEFAULT 0,
                reschedule INTEGER DEFAULT 0,
                notified INTEGER DEFAULT 0,
                notified_silent INTEGER DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id BIGINT PRIMARY KEY,
                trial_until TEXT,
                paid_until TEXT
            )
        """)
        for col_sql in [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS reminder_minutes INTEGER DEFAULT 30",
            "ALTER TABLE clients ADD COLUMN IF NOT EXISTS customer_id INTEGER",
            "ALTER TABLE clients ADD COLUMN IF NOT EXISTS notified INTEGER DEFAULT 0",
            "ALTER TABLE clients ADD COLUMN IF NOT EXISTS notified_silent INTEGER DEFAULT 0",
            "ALTER TABLE clients ADD COLUMN IF NOT EXISTS reschedule INTEGER DEFAULT 0",
        ]:
            try:
                cur.execute(col_sql)
            except Exception:
                pass
        conn.commit()
    else:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                role TEXT,
                reminder_minutes INTEGER DEFAULT 30
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                groomer_id INTEGER,
                name TEXT,
                chat_id INTEGER,
                invite_token TEXT UNIQUE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                groomer_id INTEGER,
                customer_id INTEGER,
                name TEXT,
                chat_id INTEGER,
                visit_time TEXT,
                confirmed INTEGER DEFAULT 0,
                cancelled INTEGER DEFAULT 0,
                reschedule INTEGER DEFAULT 0,
                notified INTEGER DEFAULT 0,
                notified_silent INTEGER DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id INTEGER PRIMARY KEY,
                trial_until TEXT,
                paid_until TEXT
            )
        """)
        conn.commit()
    conn.close()


# ============================================================
# USERS
# ============================================================
def set_role(user_id, role):
    conn = get_conn()
    cur = conn.cursor()
    if USE_POSTGRES:
        cur.execute(
            "INSERT INTO users (user_id, role) VALUES (%s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET role = EXCLUDED.role",
            (user_id, role),
        )
    else:
        cur.execute("INSERT OR REPLACE INTO users (user_id, role) VALUES (?, ?)", (user_id, role))
    conn.commit()
    conn.close()


def get_role(user_id):
    conn = get_conn()
    cur = conn.cursor()
    ph = "%s" if USE_POSTGRES else "?"
    cur.execute(f"SELECT role FROM users WHERE user_id = {ph}", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def get_reminder_minutes(user_id):
    conn = get_conn()
    cur = conn.cursor()
    ph = "%s" if USE_POSTGRES else "?"
    try:
        cur.execute(f"SELECT reminder_minutes FROM users WHERE user_id = {ph}", (user_id,))
        row = cur.fetchone()
        result = row[0] if row and row[0] else DEFAULT_REMINDER_MINUTES
    except Exception:
        result = DEFAULT_REMINDER_MINUTES
    conn.close()
    return result


def set_reminder_minutes(user_id, minutes):
    conn = get_conn()
    cur = conn.cursor()
    ph = "%s" if USE_POSTGRES else "?"
    cur.execute(f"UPDATE users SET reminder_minutes = {ph} WHERE user_id = {ph}", (minutes, user_id))
    conn.commit()
    conn.close()


# ============================================================
# SUBSCRIPTIONS
# ============================================================
def start_trial_if_needed(user_id):
    conn = get_conn()
    cur = conn.cursor()
    ph = "%s" if USE_POSTGRES else "?"
    cur.execute(f"SELECT trial_until, paid_until FROM subscriptions WHERE user_id = {ph}", (user_id,))
    row = cur.fetchone()
    if row is None:
        trial_until = (now() + timedelta(days=TRIAL_DAYS)).strftime("%Y-%m-%d %H:%M")
        if USE_POSTGRES:
            cur.execute("INSERT INTO subscriptions (user_id, trial_until) VALUES (%s, %s)", (user_id, trial_until))
        else:
            cur.execute("INSERT INTO subscriptions (user_id, trial_until) VALUES (?, ?)", (user_id, trial_until))
        conn.commit()
    else:
        trial_until, paid_until = row
        if not trial_until and not paid_until:
            new_trial = (now() + timedelta(days=TRIAL_DAYS)).strftime("%Y-%m-%d %H:%M")
            cur.execute(
                f"UPDATE subscriptions SET trial_until = {ph} WHERE user_id = {ph}",
                (new_trial, user_id),
            )
            conn.commit()
    conn.close()


def get_subscription(user_id):
    conn = get_conn()
    cur = conn.cursor()
    ph = "%s" if USE_POSTGRES else "?"
    cur.execute(f"SELECT trial_until, paid_until FROM subscriptions WHERE user_id = {ph}", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row if row else (None, None)


def set_paid_until(user_id, days=PAID_DAYS):
    conn = get_conn()
    cur = conn.cursor()
    ph = "%s" if USE_POSTGRES else "?"
    paid_until = (now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    cur.execute(f"SELECT user_id FROM subscriptions WHERE user_id = {ph}", (user_id,))
    exists = cur.fetchone()
    if exists:
        cur.execute(f"UPDATE subscriptions SET paid_until = {ph} WHERE user_id = {ph}", (paid_until, user_id))
    else:
        if USE_POSTGRES:
            cur.execute("INSERT INTO subscriptions (user_id, paid_until) VALUES (%s, %s)", (user_id, paid_until))
        else:
            cur.execute("INSERT INTO subscriptions (user_id, paid_until) VALUES (?, ?)", (user_id, paid_until))
    conn.commit()
    conn.close()
    return paid_until


def subscription_ok(user_id):
    trial_until, paid_until = get_subscription(user_id)
    current = now().replace(tzinfo=None)
    if paid_until:
        try:
            paid_dt = datetime.strptime(paid_until, "%Y-%m-%d %H:%M")
            if paid_dt > current:
                return True, "paid", paid_dt
        except Exception:
            pass
    if trial_until:
        try:
            trial_dt = datetime.strptime(trial_until, "%Y-%m-%d %H:%M")
            if trial_dt > current:
                return True, "trial", trial_dt
        except Exception:
            pass
    return False, "expired", None


def days_left_ru(until_dt):
    delta = until_dt - now().replace(tzinfo=None)
    total_seconds = max(0, int(delta.total_seconds()))
    days = total_seconds // 86400
    if total_seconds % 86400 > 0:
        days += 1
    if days % 10 == 1 and days % 100 != 11:
        word = "день"
    elif days % 10 in (2, 3, 4) and days % 100 not in (12, 13, 14):
        word = "дня"
    else:
        word = "дней"
    return f"{days} {word}"


# ============================================================
# CUSTOMERS (клиенты мастера)
# ============================================================
def find_customer(groomer_id, name):
    """Ищет клиента мастера по имени (без учёта регистра)."""
    conn = get_conn()
    cur = conn.cursor()
    ph = "%s" if USE_POSTGRES else "?"
    cur.execute(
        f"SELECT id, name, chat_id, invite_token FROM customers "
        f"WHERE groomer_id = {ph} AND LOWER(name) = LOWER({ph})",
        (groomer_id, name),
    )
    row = cur.fetchone()
    conn.close()
    return row  # (id, name, chat_id, invite_token) или None


def create_customer(groomer_id, name):
    """Создаёт клиента + генерирует токен."""
    token = gen_token()
    conn = get_conn()
    cur = conn.cursor()
    if USE_POSTGRES:
        cur.execute(
            "INSERT INTO customers (groomer_id, name, invite_token) VALUES (%s, %s, %s) RETURNING id",
            (groomer_id, name, token),
        )
        cid = cur.fetchone()[0]
    else:
        cur.execute(
            "INSERT INTO customers (groomer_id, name, invite_token) VALUES (?, ?, ?)",
            (groomer_id, name, token),
        )
        cid = cur.lastrowid
    conn.commit()
    conn.close()
    return cid, token


def get_customer_by_token(token):
    conn = get_conn()
    cur = conn.cursor()
    ph = "%s" if USE_POSTGRES else "?"
    cur.execute(f"SELECT id, groomer_id, name FROM customers WHERE invite_token = {ph}", (token,))
    row = cur.fetchone()
    conn.close()
    return row  # (id, groomer_id, name) или None


def link_customer_chat(customer_id, chat_id):
    conn = get_conn()
    cur = conn.cursor()
    ph = "%s" if USE_POSTGRES else "?"
    cur.execute(f"UPDATE customers SET chat_id = {ph} WHERE id = {ph}", (chat_id, customer_id))
    cur.execute(f"UPDATE clients SET chat_id = {ph} WHERE customer_id = {ph}", (chat_id, customer_id))
    conn.commit()
    conn.close()


# ============================================================
# CLIENTS (записи)
# ============================================================
def get_clients(groomer_id):
    conn = get_conn()
    cur = conn.cursor()
    ph = "%s" if USE_POSTGRES else "?"
    cur.execute(
        f"SELECT id, name, visit_time, confirmed, cancelled, reschedule, chat_id, customer_id FROM clients "
        f"WHERE groomer_id = {ph} ORDER BY visit_time",
        (groomer_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def add_client(groomer_id, customer_id, name, chat_id, visit_time):
    conn = get_conn()
    cur = conn.cursor()
    ph = "%s" if USE_POSTGRES else "?"
    cur.execute(
        f"INSERT INTO clients (groomer_id, customer_id, name, chat_id, visit_time) "
        f"VALUES ({ph}, {ph}, {ph}, {ph}, {ph})",
        (groomer_id, customer_id, name, chat_id, visit_time),
    )
    conn.commit()
    conn.close()


def cleanup_old_records():
    conn = get_conn()
    cur = conn.cursor()
    ph = "%s" if USE_POSTGRES else "?"
    cutoff = (now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
    cur.execute(f"DELETE FROM clients WHERE visit_time < {ph}", (cutoff,))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    return deleted


# ============================================================
# БОТ
# ============================================================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

role_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Я мастер")], [KeyboardButton(text="Я клиент")]],
    resize_keyboard=True,
)

main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📋 Мои записи")],
        [KeyboardButton(text="➕ Добавить запись")],
        [KeyboardButton(text="⚙️ Настройки")],
        [KeyboardButton(text="💳 Подписка")],
    ],
    resize_keyboard=True,
)

cancel_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="❌ Отмена")]],
    resize_keyboard=True,
)

client_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, буду", callback_data="confirm")],
        [InlineKeyboardButton(text="📞 Перенести визит", callback_data="reschedule")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
    ]
)


def reminder_settings_kb():
    rows = [[InlineKeyboardButton(text=label, callback_data=f"remind_{m}")] for m, label in REMINDER_PRESETS]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Хранилище диалогов (в памяти)
# {user_id: {"step": "name"/"time", "name": "...", "customer_id": ..., "chat_id": ...}}
dialog_state = {}


# ============================================================
# /start и роли
# ============================================================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    # Проверяем, есть ли deep-link (ссылка-приглашение)
    args = message.text.split()
    if len(args) > 1 and args[1].startswith("visit_"):
        token = args[1].replace("visit_", "")
        customer = get_customer_by_token(token)
        if customer:
            cid, groomer_id, name = customer
            link_customer_chat(cid, message.from_user.id)

            # Уведомляем мастера
            try:
                await bot.send_message(
                    groomer_id,
                    f"✅ {name} подключился. Теперь напоминания будут приходить автоматически.",
                )
            except Exception as e:
                print(f"[LINK] Не удалось уведомить {groomer_id}: {e}")

            # Клиенту
            set_role(message.from_user.id, "client")
            await message.answer(
                f"Здравствуйте, {name}! 👋\n\n"
                "Вы подключены к RemindMe. За день до визита мастер добавит вас — "
                "и напоминание придёт автоматически.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        else:
            await message.answer("Ссылка недействительна. Попросите мастера отправить новую.")
            return

    # Обычный /start
    role = get_role(message.from_user.id)
    if role == "groomer":
        start_trial_if_needed(message.from_user.id)
        trial_until, paid_until = get_subscription(message.from_user.id)
        if paid_until:
            paid_dt = datetime.strptime(paid_until, "%Y-%m-%d %H:%M")
            status_text = f"\n✅ Подписка активна ещё {days_left_ru(paid_dt)}"
        elif trial_until:
            trial_dt = datetime.strptime(trial_until, "%Y-%m-%d %H:%M")
            status_text = f"\n🎁 Пробный период — осталось {days_left_ru(trial_dt)}"
        else:
            status_text = ""
        await message.answer(
            f"С возвращением, {message.from_user.first_name}!\n\n"
            f"Ты вошёл как мастер.{status_text}",
            reply_markup=main_kb,
        )
    elif role == "client":
        await message.answer(
            "Ты в режиме клиента. Жди напоминания о визите.",
            reply_markup=ReplyKeyboardRemove(),
        )
    else:
        await message.answer(
            f"Привет, {message.from_user.first_name}!\n\nКто ты? Это нужно выбрать один раз.",
            reply_markup=role_kb,
        )


@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    conn = get_conn()
    cur = conn.cursor()
    ph = "%s" if USE_POSTGRES else "?"
    cur.execute(f"DELETE FROM users WHERE user_id = {ph}", (message.from_user.id,))
    conn.commit()
    conn.close()
    dialog_state.pop(message.from_user.id, None)
    await message.answer("Роль сброшена. Напиши /start.", reply_markup=ReplyKeyboardRemove())


@dp.message(F.text == "Я мастер")
async def choose_groomer(message: Message):
    set_role(message.from_user.id, "groomer")
    start_trial_if_needed(message.from_user.id)
    trial_until, paid_until = get_subscription(message.from_user.id)
    if paid_until:
        paid_dt = datetime.strptime(paid_until, "%Y-%m-%d %H:%M")
        status_text = f"\n✅ Подписка активна ещё {days_left_ru(paid_dt)}"
    elif trial_until:
        trial_dt = datetime.strptime(trial_until, "%Y-%m-%d %H:%M")
        status_text = f"\n🎁 Пробный период — осталось {days_left_ru(trial_dt)}"
    else:
        status_text = ""
    await message.answer(
        f"Отлично! Теперь ты можешь добавлять клиентов.{status_text}\n\n"
        "Используй кнопки ниже или /add.",
        reply_markup=main_kb,
    )


@dp.message(F.text == "Я клиент")
async def choose_client(message: Message):
    set_role(message.from_user.id, "client")
    await message.answer(
        "Понял! Ты клиент. Когда приблизится время визита — я напомню.",
        reply_markup=ReplyKeyboardRemove(),
    )


# ============================================================
# НАСТРОЙКИ
# ============================================================
@dp.message(F.text == "⚙️ Настройки")
async def btn_settings(message: Message):
    if get_role(message.from_user.id) != "groomer":
        return
    current = get_reminder_minutes(message.from_user.id)
    await message.answer(
        f"⚙️ Настройки\n\n"
        f"За сколько напоминать клиенту?\n"
        f"Сейчас: <b>{format_minutes(current)}</b> до визита",
        parse_mode="HTML",
        reply_markup=reminder_settings_kb(),
    )


@dp.callback_query(F.data.startswith("remind_"))
async def cb_set_reminder(callback: CallbackQuery):
    if get_role(callback.from_user.id) != "groomer":
        await callback.answer()
        return
    try:
        minutes = int(callback.data.replace("remind_", ""))
        set_reminder_minutes(callback.from_user.id, minutes)
        await callback.message.edit_text(
            f"✅ Настройка сохранена.\n\n"
            f"Теперь напоминание клиенту будет приходить за <b>{format_minutes(minutes)}</b> до визита.",
            parse_mode="HTML",
        )
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {e}")
    await callback.answer()


# ============================================================
# ПОДПИСКА
# ============================================================
@dp.message(Command("pay"))
async def cmd_pay(message: Message):
    if get_role(message.from_user.id) != "groomer":
        return
    await message.answer(
        "💳 Оплата подписки — $5/мес.\n\n"
        f"Ссылка на оплату:\n{PAYMENT_LINK}\n\n"
        "После оплаты напишите нам — активируем в течение часа."
    )


@dp.message(F.text == "💳 Подписка")
async def btn_subscription(message: Message):
    if get_role(message.from_user.id) != "groomer":
        return
    ok, status, until = subscription_ok(message.from_user.id)
    if not ok:
        await message.answer(
            "⚠️ Ваш пробный период закончился.\n\n"
            "Чтобы продолжить — оплатите $5/мес.\n"
            f"Ссылка: {PAYMENT_LINK}\n\n"
            "После оплаты напишите нам."
        )
        return
    if status == "paid":
        await message.answer(f"✅ Подписка активна ещё {days_left_ru(until)}")
    else:
        await message.answer(
            f"🎁 Пробный период — осталось {days_left_ru(until)}\n\n"
            f"Для продления — $5/мес:\n{PAYMENT_LINK}"
        )


@dp.message(Command("activate"))
async def cmd_activate(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        parts = message.text.split()
        if len(parts) != 2:
            raise ValueError()
        user_id = int(parts[1])
        paid_until = set_paid_until(user_id)
        await message.answer(f"✅ Подписка активирована для {user_id} до {paid_until}.")
        try:
            await bot.send_message(user_id, "✅ Ваша подписка RemindMe активирована на 30 дней.")
        except Exception:
            pass
    except Exception:
        await message.answer("Формат: /activate 473980999")


# ============================================================
# ДОБАВЛЕНИЕ ЗАПИСИ — ПОШАГОВЫЙ ДИАЛОГ
# ============================================================
@dp.message(Command("add"))
async def cmd_add(message: Message):
    if get_role(message.from_user.id) != "groomer":
        await message.answer("Эта команда только для мастеров.")
        return
    ok, status, until = subscription_ok(message.from_user.id)
    if not ok:
        await message.answer("⚠️ Пробный период закончился. /pay")
        return
    dialog_state[message.from_user.id] = {"step": "name"}
    await message.answer(
        "📝 Добавляем запись.\n\n"
        "Шаг 1/2. Как зовут клиента?",
        reply_markup=cancel_kb,
    )


@dp.message(F.text == "➕ Добавить запись")
async def btn_add(message: Message):
    await cmd_add(message)


@dp.message(F.text == "❌ Отмена")
async def btn_cancel_dialog(message: Message):
    if message.from_user.id in dialog_state:
        dialog_state.pop(message.from_user.id, None)
        await message.answer("Отменено.", reply_markup=main_kb)
    else:
        await message.answer("Нечего отменять.", reply_markup=main_kb)


# Обработчик диалога (должен идти ПОСЛЕ команд и кнопок)
@dp.message(F.text & ~F.text.startswith("/"))
async def handle_dialog(message: Message):
    state = dialog_state.get(message.from_user.id)
    if not state:
        return

    if get_role(message.from_user.id) != "groomer":
        dialog_state.pop(message.from_user.id, None)
        return

    step = state.get("step")

    if step == "name":
        name = message.text.strip()
        if len(name) < 2 or len(name) > 40:
            await message.answer("Имя должно быть от 2 до 40 символов. Попробуй снова:")
            return
        state["name"] = name
        state["step"] = "time"
        await message.answer(
            f"Шаг 2/2. На когда запись для <b>{name}</b>?\n\n"
            f"Формат: <code>26.09 15:30</code>\n"
            f"Также можно: <code>сегодня 15:30</code> или <code>завтра 12:00</code>",
            parse_mode="HTML",
            reply_markup=cancel_kb,
        )
        return

    if step == "time":
        text = message.text.strip().lower()
        visit_dt = None
        current = now()

        # Попытка 1: "сегодня 15:30" / "завтра 12:00"
        try:
            parts = text.split()
            if len(parts) == 2:
                day_word, time_part = parts
                time_obj = datetime.strptime(time_part, "%H:%M").time()
                if day_word == "сегодня":
                    visit_dt = datetime.combine(current.date(), time_obj)
                elif day_word == "завтра":
                    visit_dt = datetime.combine(current.date() + timedelta(days=1), time_obj)
        except Exception:
            pass

        # Попытка 2: "26.09 15:30"
        if visit_dt is None:
            try:
                visit_dt = datetime.strptime(text, "%d.%m %H:%M")
                visit_dt = visit_dt.replace(year=current.year)
                # Если дата в прошлом — берём следующий год
                if visit_dt < current.replace(tzinfo=None):
                    visit_dt = visit_dt.replace(year=current.year + 1)
            except Exception:
                pass

        # Попытка 3: "26.09.2026 15:30"
        if visit_dt is None:
            try:
                visit_dt = datetime.strptime(text, "%d.%m.%Y %H:%M")
            except Exception:
                pass

        if visit_dt is None:
            await message.answer(
                "❌ Не понял. Напиши в формате:\n\n"
                "<code>26.09 15:30</code>\n"
                "или <code>сегодня 15:30</code>\n"
                "или <code>завтра 12:00</code>",
                parse_mode="HTML",
            )
            return

        if visit_dt < current.replace(tzinfo=None):
            await message.answer("❌ Дата уже прошла. Укажи будущее время.")
            return

        visit_str = visit_dt.strftime("%Y-%m-%d %H:%M")
        name = state["name"]

        # Ищем клиента в базе мастера
        customer = find_customer(message.from_user.id, name)
        invite_link = None

        if customer:
            customer_id, cust_name, cust_chat_id, cust_token = customer
            chat_id = cust_chat_id  # может быть None
        else:
            customer_id, token = create_customer(message.from_user.id, name)
            chat_id = None
            invite_link = f"https://t.me/{BOT_USERNAME}?start=visit_{token}"

        # Создаём запись
        add_client(message.from_user.id, customer_id, name, chat_id, visit_str)
        dialog_state.pop(message.from_user.id, None)

        minutes = get_reminder_minutes(message.from_user.id)

        if chat_id:
            # Клиент уже подключён
            await message.answer(
                f"✅ Запись для <b>{name}</b> на <b>{visit_str}</b> создана.\n\n"
                f"📩 Клиент уже подключён — напоминание придёт за {format_minutes(minutes)} до визита.",
                parse_mode="HTML",
                reply_markup=main_kb,
            )
        else:
            # Нужна ссылка-приглашение
            if not invite_link:
                invite_link = f"https://t.me/{BOT_USERNAME}?start=visit_{cust_token}"
            await message.answer(
                f"✅ Запись для <b>{name}</b> на <b>{visit_str}</b> создана.\n\n"
                f"🔗 <b>Отправьте клиенту ссылку</b>, чтобы он получал напоминания:\n\n"
                f"<code>{invite_link}</code>\n\n"
                f"После того как клиент откроет ссылку — напоминание придёт за {format_minutes(minutes)} до визита.\n\n"
                f"Если клиент не в Telegram — можно напомнить ему самому в WhatsApp или по телефону.",
                parse_mode="HTML",
                reply_markup=main_kb,
            )
        return


# ============================================================
# СПИСОК ЗАПИСЕЙ
# ============================================================
@dp.message(Command("list"))
async def cmd_list(message: Message):
    if get_role(message.from_user.id) != "groomer":
        await message.answer("Только для мастеров.")
        return
    rows = get_clients(message.from_user.id)
    if not rows:
        await message.answer("Пока нет записей.")
        return
    text = "📋 Твои записи:\n\n"
    for rid, name, visit_time, confirmed, cancelled, reschedule, chat_id, customer_id in rows:
        if cancelled:
            status = "❌"
        elif reschedule:
            status = "📞"
        elif confirmed:
            status = "✅"
        elif not chat_id:
            status = "🔗"  # ждёт подключения
        else:
            status = "⏳"
        text += f"{status} {name} — {visit_time}\n"
    text += "\n🔗 — клиент ещё не подключён к боту."
    await message.answer(text)


@dp.message(F.text == "📋 Мои записи")
async def btn_list(message: Message):
    await cmd_list(message)


# ============================================================
# КНОПКИ КЛИЕНТА
# ============================================================
@dp.callback_query(F.data == "confirm")
async def cb_confirm(callback: CallbackQuery):
    conn = get_conn()
    cur = conn.cursor()
    ph = "%s" if USE_POSTGRES else "?"
    cur.execute(
        f"SELECT id, groomer_id, name, visit_time FROM clients "
        f"WHERE chat_id = {ph} AND confirmed = 0 AND cancelled = 0",
        (callback.from_user.id,),
    )
    rows = cur.fetchall()
    if not rows:
        conn.close()
        await callback.message.edit_text("У вас нет активных записей.")
        await callback.answer()
        return
    cur.execute(
        f"UPDATE clients SET confirmed = 1 WHERE chat_id = {ph} AND confirmed = 0 AND cancelled = 0",
        (callback.from_user.id,),
    )
    conn.commit()
    conn.close()
    await callback.message.edit_text("✅ Спасибо! Ждём вас.")
    await callback.answer()
    for cid, groomer_id, name, visit_time in rows:
        try:
            await bot.send_message(groomer_id, f"✅ {name} подтвердил визит на {visit_time}.")
        except Exception as e:
            print(f"[CONFIRM] {e}")


@dp.callback_query(F.data == "reschedule")
async def cb_reschedule(callback: CallbackQuery):
    conn = get_conn()
    cur = conn.cursor()
    ph = "%s" if USE_POSTGRES else "?"
    cur.execute(
        f"SELECT id, groomer_id, name, visit_time FROM clients "
        f"WHERE chat_id = {ph} AND confirmed = 0 AND cancelled = 0",
        (callback.from_user.id,),
    )
    rows = cur.fetchall()
    if not rows:
        conn.close()
        await callback.message.edit_text("У вас нет активных записей.")
        await callback.answer()
        return
    cur.execute(
        f"UPDATE clients SET reschedule = 1 WHERE chat_id = {ph} AND cancelled = 0",
        (callback.from_user.id,),
    )
    conn.commit()
    conn.close()
    await callback.message.edit_text("📞 Передал мастеру.")
    await callback.answer()
    for cid, groomer_id, name, visit_time in rows:
        try:
            await bot.send_message(groomer_id, f"📞 {name} хочет перенести визит (был на {visit_time}).")
        except Exception as e:
            print(f"[RESCHEDULE] {e}")


@dp.callback_query(F.data == "cancel")
async def cb_cancel(callback: CallbackQuery):
    conn = get_conn()
    cur = conn.cursor()
    ph = "%s" if USE_POSTGRES else "?"
    cur.execute(
        f"SELECT id, groomer_id, name, visit_time FROM clients "
        f"WHERE chat_id = {ph} AND confirmed = 0 AND cancelled = 0",
        (callback.from_user.id,),
    )
    rows = cur.fetchall()
    if not rows:
        conn.close()
        await callback.message.edit_text("У вас нет активных записей.")
        await callback.answer()
        return
    cur.execute(
        f"UPDATE clients SET cancelled = 1 WHERE chat_id = {ph} AND cancelled = 0",
        (callback.from_user.id,),
    )
    conn.commit()
    conn.close()
    await callback.message.edit_text("❌ Визит отменён.")
    await callback.answer()
    for cid, groomer_id, name, visit_time in rows:
        try:
            await bot.send_message(groomer_id, f"❌ {name} отменил визит на {visit_time}.")
        except Exception as e:
            print(f"[CANCEL] {e}")


# ============================================================
# НАПОМИНАНИЯ
# ============================================================
async def reminder_loop():
    while True:
        try:
            conn = get_conn()
            cur = conn.cursor()
            current = now()

            # Ищем записи, у которых есть chat_id и которые пора напомнить
            cur.execute(
                f"SELECT c.id, c.name, c.chat_id, c.groomer_id, c.visit_time, "
                f"COALESCE(u.reminder_minutes, 30) AS reminder_minutes "
                f"FROM clients c "
                f"LEFT JOIN users u ON u.user_id = c.groomer_id "
                f"WHERE c.confirmed = 0 AND c.cancelled = 0 AND c.notified = 0 "
                f"AND c.chat_id IS NOT NULL"
            )
            all_rows = cur.fetchall()
            conn.close()

            to_notify = []
            for cid, name, chat_id, groomer_id, visit_time, reminder_minutes in all_rows:
                try:
                    visit_dt = datetime.strptime(visit_time, "%Y-%m-%d %H:%M")
                    remind_at = visit_dt - timedelta(minutes=reminder_minutes)
                    diff = (remind_at - current.replace(tzinfo=None)).total_seconds()
                    if -30 <= diff <= 30:
                        to_notify.append((cid, name, chat_id, visit_time, reminder_minutes))
                except Exception as e:
                    print(f"[SCHED] {name}: {e}")

            # Молчащие
            conn = get_conn()
            cur = conn.cursor()
            cur.execute(
                f"SELECT c.id, c.name, c.groomer_id, c.visit_time, "
                f"COALESCE(u.reminder_minutes, 30) AS reminder_minutes "
                f"FROM clients c "
                f"LEFT JOIN users u ON u.user_id = c.groomer_id "
                f"WHERE c.confirmed = 0 AND c.cancelled = 0 AND c.reschedule = 0 "
                f"AND c.notified = 1 AND c.notified_silent = 0"
            )
            all_silent = cur.fetchall()
            conn.close()

            silent_rows = []
            for cid, name, groomer_id, visit_time, reminder_minutes in all_silent:
                try:
                    visit_dt = datetime.strptime(visit_time, "%Y-%m-%d %H:%M")
                    remind_at = visit_dt - timedelta(minutes=reminder_minutes)
                    diff = (current.replace(tzinfo=None) - remind_at).total_seconds()
                    if SILENT_TIMEOUT_MINUTES * 60 - 30 <= diff <= SILENT_TIMEOUT_MINUTES * 60 + 30:
                        silent_rows.append((cid, name, groomer_id, visit_time))
                except Exception as e:
                    print(f"[SILENT] {name}: {e}")

            print(f"[{current.strftime('%H:%M')}] Напомнить: {len(to_notify)} | Молчат: {len(silent_rows)}")

            for cid, name, chat_id, visit_time, reminder_minutes in to_notify:
                try:
                    visit_dt = datetime.strptime(visit_time, "%Y-%m-%d %H:%M")
                    day_word = "сегодня" if visit_dt.date() == current.date() else "завтра"
                    time_str = visit_dt.strftime("%H:%M")
                    await bot.send_message(
                        chat_id,
                        f"Здравствуйте! Напоминаем, что {day_word} у вас визит в {time_str}.\n\n"
                        "Подтвердите, пожалуйста:",
                        reply_markup=client_kb,
                    )
                    conn = get_conn()
                    cur = conn.cursor()
                    ph2 = "%s" if USE_POSTGRES else "?"
                    cur.execute(f"UPDATE clients SET notified = 1 WHERE id = {ph2}", (cid,))
                    conn.commit()
                    conn.close()
                    print(f"[REMIND] {name} ({chat_id})")
                except Exception as e:
                    print(f"[REMIND ERR] {name}: {e}")

            for cid, name, groomer_id, visit_time in silent_rows:
                try:
                    time_str = datetime.strptime(visit_time, "%Y-%m-%d %H:%M").strftime("%H:%M")
                    await bot.send_message(
                        groomer_id,
                        f"⚠️ {name} не ответил на напоминание о визите в {time_str}.\n\n"
                        "Возможно, стоит позвонить.",
                    )
                    conn = get_conn()
                    cur = conn.cursor()
                    ph2 = "%s" if USE_POSTGRES else "?"
                    cur.execute(f"UPDATE clients SET notified_silent = 1 WHERE id = {ph2}", (cid,))
                    conn.commit()
                    conn.close()
                    print(f"[SILENT] {name} → мастеру {groomer_id}")
                except Exception as e:
                    print(f"[SILENT ERR] {name}: {e}")

        except Exception as e:
            logging.error(f"Ошибка в reminder_loop: {e}")

        await asyncio.sleep(60)


async def cleanup_loop():
    while True:
        try:
            deleted = cleanup_old_records()
            if deleted:
                print(f"[CLEANUP] Удалено: {deleted}")
        except Exception as e:
            logging.error(f"Ошибка в cleanup_loop: {e}")
        await asyncio.sleep(3600)


# ============================================================
# ЗАПУСК
# ============================================================
async def main():
    logging.basicConfig(level=logging.INFO)
    init_db()
    asyncio.create_task(reminder_loop())
    asyncio.create_task(cleanup_loop())
    print(f"Бот запущен. База: {'PostgreSQL' if USE_POSTGRES else 'SQLite'}. Часовой пояс: {TIMEZONE}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())