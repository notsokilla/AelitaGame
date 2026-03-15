#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🤖 Универсальный AI-помощник с админ-панелью и БД
"""

import asyncio
import logging
import os
import re
import time
from datetime import datetime
from typing import Optional
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ErrorEvent
from openai import AsyncOpenAI

# Наши модули
from config import *
from database import Database

# ================= ИНИЦИАЛИЗАЦИЯ =================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
db = Database(DB_PATH)

client = AsyncOpenAI(api_key=NEURAL_API_KEY, base_url=NEURAL_BASE_URL)

# ================= АДМИН СЕССИИ =================
admin_sessions: dict[int, float] = {}
ADMIN_SESSION_DURATION = 3600

failed_login_attempts: dict[int, list[float]] = {}
MAX_FAILED_ATTEMPTS = 3
LOCKOUT_DURATION = 300

# Пагинация пользователей
USERS_PER_PAGE = 20


def check_admin_session(user_id: int) -> bool:
    """Проверяет активную сессию админа"""
    if user_id not in admin_sessions:
        return False
    if time.time() > admin_sessions[user_id]:
        del admin_sessions[user_id]
        return False
    return True


def activate_admin_session(user_id: int):
    """Создаёт сессию админа на 1 час"""
    admin_sessions[user_id] = time.time() + ADMIN_SESSION_DURATION


def logout_admin(user_id: int):
    """Завершает сессию админа"""
    if user_id in admin_sessions:
        del admin_sessions[user_id]


# ================= ПРОМПТЫ =================
PROMPTS = {
    "math": """
Ты — эксперт-математик и преподаватель. Решай задачи с ПОШАГОВЫМ объяснением.
ПРАВИЛА:
1. Всегда показывай ход решения шаг за шагом
2. Объясняй каждую операцию простым языком
3. Используй формулы: x², √, ∫, ∑
КАТЕГОРИИ: Алгебра • Геометрия • Статистика
    """,
    "search": """
Ты — аналитик. Находи и структурируй информацию.
ПРАВИЛА:
1. Давай точные факты
2. Структурируй ответ: заголовки, списки
3. Указывай дату актуальности
    """,
    "consult": """
Ты — универсальный консультант. Давай полезные ответы.
ПРАВИЛА:
1. Адаптируй сложность под вопрос
2. Давай практические рекомендации
3. Предлагай альтернативы
    """,
    "learn": """
Ты — педагог. Объясняй сложное просто.
ПРАВИЛА:
1. Используй аналогии
2. Разбивай на простые шаги
3. Давай советы по запоминанию
    """,
    "game": """
Ты — геймер-аналитик. Помогай с билдами и стратегиями.
ПРАВИЛА:
1. Указывай актуальность (патч, мета)
2. Давай конкретные цифры
3. Предлагай альтернативы
ИГРЫ: Dota 2 • CS2 • LoL • Valorant
    """,
    "news": """
Ты — новостной обозреватель. Анализируй тренды.
ПРАВИЛА:
1. Указывай дату и источник
2. Разделяй факты и мнения
3. Выделяй ключевые тренды
    """
}


# ================= МАШИНА СОСТОЯНИЙ =================
class QueryMode(StatesGroup):
    waiting_for_math = State()
    waiting_for_game = State()
    waiting_for_learn = State()
    admin_broadcast_text = State()
    admin_broadcast_media = State()
    admin_waiting_password = State()


# ================= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =================

def detect_category(text: str) -> str:
    """Автоопределение категории запроса"""
    text_lower = text.lower()
    if any(kw in text_lower for kw in ['реши', 'уравнение', 'формула', 'интеграл', '√', '∫', 'посчитай']):
        return 'math'
    if any(kw in text_lower for kw in ['билд', 'мета', 'патч', 'дота', 'контра', 'гайд', 'стратегия']):
        return 'game'
    if any(kw in text_lower for kw in ['объясни', 'конспект', 'экзамен', 'учеб', 'как понять']):
        return 'learn'
    if any(kw in text_lower for kw in ['новости', 'тренд', 'обзор', 'событие']):
        return 'news'
    if any(kw in text_lower for kw in ['найди', 'информация', 'факты', 'анализ']):
        return 'search'
    return 'consult'


def truncate_message(text: str, limit: int = 4000) -> str:
    """Обрезает текст до безопасной длины"""
    if len(text) <= limit:
        return text
    truncated = text[:limit]
    last_period = truncated.rfind('.')
    last_newline = truncated.rfind('\n')
    if last_period > limit - 200:
        truncated = truncated[:last_period + 1]
    elif last_newline > limit - 200:
        truncated = truncated[:last_newline]
    return truncated + "\n\n... (сообщение сокращено)"


async def call_neural_api(prompt_type: str, user_query: str) -> str:
    """Запрос к нейросети"""
    try:
        response = await client.chat.completions.create(
            model=NEURAL_MODEL,
            messages=[
                {"role": "system", "content": PROMPTS.get(prompt_type, PROMPTS['consult']) + "\n\nВАЖНО: Отвечай кратко, не более 3000 символов."},
                {"role": "user", "content": user_query}
            ],
            temperature=0.7,
            max_tokens=800
        )
        return response.choices[0].message.content
    except Exception as e:
        logging.error(f"API Error: {e}")
        return f"⚠️ Ошибка нейросети: {type(e).__name__}"


# ================= КЛАВИАТУРЫ =================

def create_main_keyboard(user_id: int) -> types.ReplyKeyboardMarkup:
    """Главное меню (БЕЗ АДМИН-КНОПКИ)"""
    keyboard = [
        [types.KeyboardButton(text="🧮 Математика"), types.KeyboardButton(text="🔍 Поиск")],
        [types.KeyboardButton(text="🎓 Обучение"), types.KeyboardButton(text="🎮 Игры")],
        [types.KeyboardButton(text="📰 Новости"), types.KeyboardButton(text="💬 Консультация")],
    ]
    keyboard.append([types.KeyboardButton(text="❓ Помощь, Подписка")])
    return types.ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def create_inline_categories() -> types.InlineKeyboardMarkup:
    """Inline-кнопки категорий"""
    buttons = [
        [types.InlineKeyboardButton(text="🧮 Математика", callback_data="cat_math"),
         types.InlineKeyboardButton(text="🎮 Игры", callback_data="cat_game")],
        [types.InlineKeyboardButton(text="🎓 Учеба", callback_data="cat_learn"),
         types.InlineKeyboardButton(text="🔍 Поиск", callback_data="cat_search")],
        [types.InlineKeyboardButton(text="📰 Новости", callback_data="cat_news")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)


def create_admin_keyboard() -> types.InlineKeyboardMarkup:
    """Кнопки админ-панели"""
    buttons = [
        [types.InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [types.InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [types.InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users")],
        [types.InlineKeyboardButton(text="🔍 Активность", callback_data="admin_activity")],
        [
            types.InlineKeyboardButton(text="🔓 Выйти", callback_data="admin_logout"),
            types.InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")
        ]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)


def create_users_page_keyboard(current_page: int, total_pages: int) -> types.InlineKeyboardMarkup:
    """Кнопки навигации по страницам пользователей"""
    buttons = []
    row = []

    if current_page > 1:
        row.append(types.InlineKeyboardButton(text="⬅️ Назад", callback_data=f"users_page_{current_page - 1}"))

    row.append(types.InlineKeyboardButton(text=f"📄 {current_page}/{total_pages}", callback_data="ignore"))

    if current_page < total_pages:
        row.append(types.InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"users_page_{current_page + 1}"))

    buttons.append(row)
    buttons.append([types.InlineKeyboardButton(text="🔙 Назад в админку", callback_data="admin_back")])

    return types.InlineKeyboardMarkup(inline_keyboard=buttons)


# ================= ОБРАБОТЧИКИ: ПОЛЬЗОВАТЕЛЬ =================

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    """/start"""
    user = message.from_user

    await db.add_or_update_user({
        'id': user.id, 'username': user.username,
        'first_name': user.first_name, 'last_name': user.last_name,
        'language_code': user.language_code, 'is_bot': user.is_bot
    })

    await message.answer(
        f"🤖 <b>Привет, {user.first_name}!</b>\n\n"
        f"Я — универсальный AI-помощник:\n"
        f"🧮 Математика с объяснением шагов\n"
        f"🔍 Поиск и анализ информации\n"
        f"🎓 Помощь в учёбе и объяснения простыми словами\n"
        f"🎮 Гайды, билды и стратегии по играм\n"
        f"📰 Новости и тренды с аналитикой\n"
        f"💬 Ответы на любые вопросы",
        reply_markup=create_main_keyboard(user.id),
        parse_mode="HTML"
    )
    await db.log_action(user.id, "start", "Bot started")


@dp.message(F.text == "❓ Помощь, Подписка")
async def handle_subscription_help(message: Message):
    """Кнопка помощи и подписки"""
    user_id = message.from_user.id

    await db.mark_subscription_clicked(user_id)
    await db.log_action(user_id, "subscription_click", SUBSCRIPTION_URL)

    await message.answer(
        f"📋 <b>Помощь и подписка</b>\n\n"
        f"🔧 <b>Технические вопросы и отмена подписки:</b>\n"
        f"Пишите: @samoylov1smm\n\n"
        f"🌐 <b>Отмена подписки:</b>\n"
        f"{SUBSCRIPTION_URL}",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🌐 Перейти на сайт", url=SUBSCRIPTION_URL)]
        ]),
        parse_mode="HTML"
    )


# ================= АДМИН: ВХОД ПО ПАРОЛЮ =================

@dp.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    """Вход в админ-панель"""
    if check_admin_session(message.from_user.id):
        await message.answer("⚙️ <b>Админ-панель</b>", reply_markup=create_admin_keyboard(), parse_mode="HTML")
        return

    await state.set_state(QueryMode.admin_waiting_password)
    await message.answer("🔐 <b>Введите пароль:</b>\n/cancel", parse_mode="HTML")


@dp.message(QueryMode.admin_waiting_password)
async def admin_check_password(message: Message, state: FSMContext):
    """Проверка пароля"""
    user_id = message.from_user.id
    user_password = message.text.strip()

    now = time.time()
    if user_id in failed_login_attempts:
        failed_login_attempts[user_id] = [t for t in failed_login_attempts[user_id] if now - t < LOCKOUT_DURATION]
        if len(failed_login_attempts[user_id]) >= MAX_FAILED_ATTEMPTS:
            await state.clear()
            await message.answer(f"🔒 Заблокировано на {LOCKOUT_DURATION//60} мин.")
            return

    if user_password == ADMIN_PASSWORD and ADMIN_PASSWORD:
        failed_login_attempts.pop(user_id, None)
        await state.clear()
        activate_admin_session(user_id)
        await db.log_action(user_id, "admin_login", "Success")

        for admin_id in ADMIN_IDS:
            if admin_id != user_id:
                try:
                    await bot.send_message(admin_id, f"🔔 Вход: @{message.from_user.username}", parse_mode="HTML")
                except:
                    pass

        await message.answer("✅ <b>Доступ разрешён!</b>\n\n⚙️ <b>Админ-панель</b>",
                           reply_markup=create_admin_keyboard(), parse_mode="HTML")
    else:
        if user_id not in failed_login_attempts:
            failed_login_attempts[user_id] = []
        failed_login_attempts[user_id].append(now)

        await state.clear()
        await db.log_action(user_id, "admin_login", "Failed")
        remaining = MAX_FAILED_ATTEMPTS - len(failed_login_attempts[user_id])

        if remaining > 0:
            await message.answer(f"❌ Неверно! Осталось: {remaining}")
        else:
            await message.answer(f"🔒 Заблокировано на {LOCKOUT_DURATION//60} мин.")


@dp.message(Command("admin_logout"))
async def cmd_admin_logout(message: Message):
    """Выход из админки"""
    logout_admin(message.from_user.id)
    await message.answer("🔓 <b>Выход выполнен</b>", parse_mode="HTML",
                        reply_markup=create_main_keyboard(message.from_user.id))


@dp.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: Message, state: FSMContext):
    """Отмена"""
    await state.clear()
    await message.answer("✅ Отменено", reply_markup=create_main_keyboard(message.from_user.id))


@dp.message(Command("myid"))
async def cmd_myid(message: Message):
    """Показать ID"""
    await message.answer(f"ID: <code>{message.from_user.id}</code>\n@{message.from_user.username}", parse_mode="HTML")


# ================= АДМИН: СТАТИСТИКА =================

@dp.callback_query(F.data == "admin_stats")
async def admin_show_stats(callback: CallbackQuery):
    """Статистика"""
    if not check_admin_session(callback.from_user.id):
        await callback.answer("🔐 Сессия истекла. /admin", show_alert=True)
        return

    stats = await db.get_full_stats()

    text = (
        f"📊 <b>Статистика</b>\n\n"
        f"👥 Пользователи: {stats['total_users']}\n"
        f"   • Активные (24ч): {stats['active_24h']}\n"
        f"   • Активные (7дн): {stats['active_7d']}\n\n"
        f"💬 Сообщений: {stats['total_messages']}\n"
        f"🤖 Запросов к ИИ: {stats['total_ai_requests']}\n\n"
        f"💰 Подписки: {stats['subscription_clicked']} ({stats['conversion_rate']}%)"
    )

    await callback.message.edit_text(text, reply_markup=create_admin_keyboard(), parse_mode="HTML")
    await callback.answer()


# ================= АДМИН: РАССЫЛКА =================

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    """Начать рассылку"""
    if not check_admin_session(callback.from_user.id):
        await callback.answer("🔐 /admin", show_alert=True)
        return

    await state.set_state(QueryMode.admin_broadcast_text)
    await callback.message.edit_text(
        "📢 <b>Рассылка</b>\n\nОтправьте текст:\n/cancel",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.message(QueryMode.admin_broadcast_text)
async def admin_broadcast_receive_text(message: Message, state: FSMContext):
    """Получение текста"""
    if not check_admin_session(message.from_user.id):
        return

    await state.update_data(broadcast_text=message.text, admin_id=message.from_user.id)
    await message.answer("📎 Теперь фото/видео или /send", reply_markup=types.ForceReply())
    await state.set_state(QueryMode.admin_broadcast_media)


@dp.message(QueryMode.admin_broadcast_media, F.photo)
async def admin_broadcast_receive_photo(message: Message, state: FSMContext):
    """Фото для рассылки"""
    if not check_admin_session(message.from_user.id):
        return

    data = await state.get_data()
    broadcast_id = await db.add_broadcast(
        admin_id=data.get('admin_id'),
        message_text=data.get('broadcast_text'),
        media_type='photo',
        media_file_id=message.photo[-1].file_id
    )

    await state.clear()
    await message.answer(f"✅ Рассылка ID:{broadcast_id}\nОтправка...")
    await send_broadcast(broadcast_id, data.get('broadcast_text'), 'photo',
                        message.photo[-1].file_id, data.get('admin_id'))


@dp.message(QueryMode.admin_broadcast_media, F.video)
async def admin_broadcast_receive_video(message: Message, state: FSMContext):
    """Видео"""
    if not check_admin_session(message.from_user.id):
        return

    data = await state.get_data()
    broadcast_id = await db.add_broadcast(
        admin_id=data.get('admin_id'),
        message_text=data.get('broadcast_text'),
        media_type='video',
        media_file_id=message.video.file_id
    )

    await state.clear()
    await message.answer(f"✅ Рассылка ID:{broadcast_id}")
    await send_broadcast(broadcast_id, data.get('broadcast_text'), 'video',
                        message.video.file_id, data.get('admin_id'))


@dp.message(QueryMode.admin_broadcast_media, Command("send"))
async def admin_broadcast_send_text_only(message: Message, state: FSMContext):
    """Только текст"""
    if not check_admin_session(message.from_user.id):
        return

    data = await state.get_data()
    broadcast_id = await db.add_broadcast(
        admin_id=data.get('admin_id'),
        message_text=data.get('broadcast_text'),
        media_type=None,
        media_file_id=None
    )

    await state.clear()
    await message.answer(f"✅ Рассылка ID:{broadcast_id}")
    await send_broadcast(broadcast_id, data.get('broadcast_text'), None, None, data.get('admin_id'))


async def send_broadcast(broadcast_id: int, text: str, media_type: str, media_file_id: str, admin_id: int = None):
    """Отправка рассылки с отчётом админу"""

    logging.info(f"📢 РАССЫЛКА ID:{broadcast_id} НАЧАТА")

    users = await db.get_all_users()
    sent = 0
    failed = 0
    blocked = 0
    error_details = []

    start_time = time.time()

    for i, user in enumerate(users, 1):
        user_id = user['user_id']
        username = f"@{user['username']}" if user.get('username') else f"ID:{user_id}"

        try:
            if media_type == 'photo':
                await bot.send_photo(user_id, photo=media_file_id, caption=text)
            elif media_type == 'video':
                await bot.send_video(user_id, video=media_file_id, caption=text)
            else:
                await bot.send_message(user_id, text)

            sent += 1

            if i % 10 == 0:
                logging.info(f"   Прогресс: {i}/{len(users)}")

            await asyncio.sleep(0.05)

        except TelegramForbiddenError:
            blocked += 1
            error_details.append(f"⛔ {username}")
        except Exception as e:
            failed += 1
            error_details.append(f"❌ {username}: {str(e)[:30]}")

    duration = time.time() - start_time

    # ФОРМИРУЕМ ОТЧЁТ
    report = (
        f"✅ <b>РАССЫЛКА ЗАВЕРШЕНА</b>\n\n"
        f"📋 ID: {broadcast_id}\n"
        f"⏱️ Время: {duration:.1f} сек\n\n"
        f"📊 <b>РЕЗУЛЬТАТЫ:</b>\n"
        f"📤 Отправлено: {sent}\n"
        f"⛔ Заблокировали: {blocked}\n"
        f"❌ Ошибок: {failed}\n"
        f"👥 Всего: {len(users)}\n"
        f"📈 Успех: {round(sent/len(users)*100, 1) if len(users) > 0 else 0}%"
    )

    if error_details:
        errors_preview = "\n".join(error_details[:10])
        if len(error_details) > 10:
            errors_preview += f"\n... и ещё {len(error_details) - 10}"
        report += f"\n\n⚠️ <b>ОШИБКИ:</b>\n{errors_preview}"

    # Отправляем отчёт админу
    if admin_id:
        try:
            await bot.send_message(admin_id, report, parse_mode="HTML")
            logging.info(f"📨 Отчёт отправлен админу {admin_id}")
        except Exception as e:
            logging.error(f"❌ Ошибка отправки отчёта: {e}")

    for aid in ADMIN_IDS:
        if aid != admin_id:
            try:
                await bot.send_message(aid, report, parse_mode="HTML")
            except:
                pass

    await db.update_broadcast_sent(broadcast_id, sent)
    logging.info(f"✅ РАССЫЛКА ЗАВЕРШЕНА: {sent}/{len(users)}")


# ================= АДМИН: ПОЛЬЗОВАТЕЛИ (ПОСТРАНИЧНО) =================

@dp.callback_query(F.data == "admin_users")
async def admin_show_users(callback: CallbackQuery):
    """Список пользователей (страница 1)"""
    if not check_admin_session(callback.from_user.id):
        await callback.answer("🔐 /admin", show_alert=True)
        return

    await show_users_page(callback.message, 1, callback.from_user.id)
    await callback.answer()


async def show_users_page(message: types.Message, page: int, admin_id: int):
    """Показывает страницу пользователей"""
    users = await db.get_all_users()
    total_users = len(users)
    total_pages = (total_users + USERS_PER_PAGE - 1) // USERS_PER_PAGE

    if page < 1:
        page = 1
    if page > total_pages:
        page = total_pages if total_pages > 0 else 1

    start_idx = (page - 1) * USERS_PER_PAGE
    end_idx = start_idx + USERS_PER_PAGE
    page_users = users[start_idx:end_idx]

    text = f"👥 <b>Пользователи</b> ({page}/{total_pages})\n\n"

    for i, user in enumerate(page_users, start_idx + 1):
        username = f"@{user['username']}" if user.get('username') else f"ID:{user['user_id']}"
        sub_status = "✅" if user.get('subscription_clicked') else "❌"
        msgs = user.get('total_messages', 0)
        text += f"{i}. {username}\n"
        text += f"   Сообщений: {msgs} | Подписка: {sub_status}\n\n"

    text += f"\n📊 Всего: {total_users} пользователей"

    await message.edit_text(
        text,
        reply_markup=create_users_page_keyboard(page, total_pages),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("users_page_"))
async def handle_users_pagination(callback: CallbackQuery):
    """Пагинация пользователей"""
    if not check_admin_session(callback.from_user.id):
        await callback.answer("🔐 /admin", show_alert=True)
        return

    page = int(callback.data.split("_")[-1])
    await show_users_page(callback.message, page, callback.from_user.id)
    await callback.answer()


# ================= АДМИН: АКТИВНОСТЬ =================

@dp.callback_query(F.data == "admin_activity")
async def admin_show_activity(callback: CallbackQuery):
    """Топ активных"""
    if not check_admin_session(callback.from_user.id):
        await callback.answer("🔐 /admin", show_alert=True)
        return

    top_msgs = await db.get_top_users('messages', limit=10)
    top_ai = await db.get_top_users('ai_requests', limit=10)

    def fmt(users, title):
        r = f"🏆 <b>{title}</b>\n"
        for i, u in enumerate(users, 1):
            name = f"@{u['username']}" if u.get('username') else f"ID:{u['user_id']}"
            r += f"{i}. {name} — {u['value']}\n"
        return r

    text = fmt(top_msgs, "Топ по сообщениям") + "\n" + fmt(top_ai, "Топ по ИИ")

    await callback.message.edit_text(text, reply_markup=create_admin_keyboard(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: CallbackQuery):
    """Назад"""
    if not check_admin_session(callback.from_user.id):
        await callback.answer("🔐 /admin", show_alert=True)
        return

    await callback.message.edit_text("⚙️ <b>Админ-панель</b>",
                                    reply_markup=create_admin_keyboard(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "admin_logout")
async def admin_logout_callback(callback: CallbackQuery):
    """Выход из админки — отправляем НОВОЕ сообщение вместо редактирования"""
    if not check_admin_session(callback.from_user.id):
        await callback.answer()
        return

    logout_admin(callback.from_user.id)

    await callback.message.delete()  # Удаляем сообщение с инлайн-кнопками
    await callback.message.answer(
        "🔓 <b>Вы вышли из админ-панели</b>",
        reply_markup=create_main_keyboard(callback.from_user.id),  # ✅ Теперь можно
        parse_mode="HTML"
    )
    await callback.answer("✅ Выйшли")

# ================= ОБРАБОТКА СООБЩЕНИЙ =================

@dp.message()
async def handle_message(message: Message, state: FSMContext):
    """Обычные сообщения"""
    if message.text and message.text.startswith('/'):
        return

    user_id = message.from_user.id
    await db.increment_message_count(user_id)
    await db.log_action(user_id, "message", message.text[:100])

    category = detect_category(message.text)
    await bot.send_chat_action(message.chat.id, "typing")

    ai_response = await call_neural_api(category, message.text)
    ai_response = truncate_message(ai_response)
    await db.increment_ai_requests(user_id)

    emoji = {'math':'🧮','search':'🔍','consult':'💬','learn':'🎓','game':'🎮','news':'📰'}

    full_response = f"{emoji.get(category,'🤖')} <b>Ответ:</b>\n\n{ai_response}"

    try:
        await message.answer(full_response, reply_markup=create_inline_categories(), parse_mode="HTML")
    except TelegramBadRequest as e:
        if "too long" in str(e):
            await message.answer(f"{emoji.get(category,'🤖')} Ответ сокращён:\n\n{ai_response[:3500]}...",
                               reply_markup=create_inline_categories(), parse_mode="HTML")


# ================= CALLBACK: КАТЕГОРИИ =================

@dp.callback_query(F.data.startswith("cat_"))
async def handle_category_callback(callback: CallbackQuery, state: FSMContext):
    """Выбор категории"""
    try:
        await callback.answer()
    except:
        return

    cats = {'cat_math':'🧮','cat_game':'🎮','cat_learn':'🎓','cat_search':'🔍','cat_news':'📰'}
    cat_name = cats.get(callback.data, '🤖')

    try:
        await callback.message.answer(f"{cat_name} — напишите вопрос 👇")
    except:
        pass


# ================= ОБРАБОТКА ОШИБОК =================

@dp.errors()
async def errors_handler(update: ErrorEvent, exception: Exception) -> bool:
    """Глобальный обработчик ошибок для aiogram 3.x"""

    # Игнорируем устаревшие callback
    if isinstance(exception, TelegramBadRequest) and "query is too old" in str(exception):
        return True

    # Игнорируем заблокированных пользователей
    if isinstance(exception, TelegramForbiddenError):
        return True

    # Игнорируем слишком длинные сообщения
    if isinstance(exception, TelegramBadRequest) and "too long" in str(exception):
        return True

    # Логируем остальные ошибки
    logging.error(f"Error: {type(exception).__name__}: {exception}")
    return True


# ================= ЗАПУСК =================

async def main():
    """Точка входа"""
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', force=True)

    await db.connect()

    # 🔧 НАСТРОЙКА ПРОКСИ ДЛЯ TELEGRAM API
    # В aiogram 3.x AiohttpSession автоматически использует переменные окружения
    # если они заданы (HTTPS_PROXY, HTTP_PROXY, ALL_PROXY)
    session = AiohttpSession()  # ← ПРОСТО ТАК, БЕЗ АРГУМЕНТОВ!

    # 🔧 Создаём бота с сессией
    global bot
    bot = Bot(token=BOT_TOKEN, session=session)

    # 🔧 Запуск
    try:
        me = await bot.get_me()
        logging.info(f"🚀 Запуск @{me.username}")
        print(f"✅ Бот запущен! Админы: {ADMIN_IDS}")
    except Exception as e:
        logging.error(f"❌ Ошибка при запуске: {e}")
        print(f"⚠️ Бот запущен, но есть проблемы с подключением")

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())