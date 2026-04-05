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

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ErrorEvent
from aiogram.client.session.aiohttp import AiohttpSession
from openai import AsyncOpenAI

# Наши модули
from config import *
from database import Database
from guides_db import GuidesDatabase

# ================= ИНИЦИАЛИЗАЦИЯ =================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
db = Database(DB_PATH)
guides_db = GuidesDatabase(GUIDES_DB_PATH)
client = AsyncOpenAI(api_key=NEURAL_API_KEY, base_url=NEURAL_BASE_URL)

# ================= АДМИН СЕССИИ =================
admin_sessions: dict[int, float] = {}
ADMIN_SESSION_DURATION = 3600
failed_login_attempts: dict[int, list[float]] = {}
MAX_FAILED_ATTEMPTS = 3
LOCKOUT_DURATION = 300
USERS_PER_PAGE = 20
GUIDES_PER_PAGE = 5
MEDIA_PER_PAGE = 10


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
    "math": "Ты — эксперт-математик и преподаватель. Решай задачи с ПОШАГОВЫМ объяснением.\nПРАВИЛА:\n1. Всегда показывай ход решения шаг за шагом\n2. Объясняй каждую операцию простым языком\n3. Используй формулы: x², √, ∫, ∑\nКАТЕГОРИИ: Алгебра • Геометрия • Статистика",
    "search": "Ты — аналитик. Находи и структурируй информацию.\nПРАВИЛА:\n1. Давай точные факты\n2. Структурируй ответ: заголовки, списки\n3. Указывай дату актуальности",
    "consult": "Ты — универсальный консультант. Давай полезные ответы.\nПРАВИЛА:\n1. Адаптируй сложность под вопрос\n2. Давай практические рекомендации\n3. Предлагай альтернативы",
    "learn": "Ты — педагог. Объясняй сложное просто.\nПРАВИЛА:\n1. Используй аналогии\n2. Разбивай на простые шаги\n3. Давай советы по запоминанию",
    "game": "Ты — геймер-аналитик. Помогай с билдами и стратегиями.\nПРАВИЛА:\n1. Указывай актуальность (патч, мета)\n2. Давай конкретные цифры\n3. Предлагай альтернативы\nИГРЫ: Dota 2 • CS2 • LoL • Valorant",
    "news": "Ты — новостной обозреватель. Анализируй тренды.\nПРАВИЛА:\n1. Указывай дату и источник\n2. Разделяй факты и мнения\n3. Выделяй ключевые тренды"
}


# ================= МАШИНА СОСТОЯНИЙ =================
class QueryMode(StatesGroup):
    waiting_for_math = State()
    waiting_for_game = State()
    waiting_for_learn = State()
    admin_broadcast_text = State()
    admin_broadcast_media = State()
    admin_waiting_password = State()
    admin_search_email = State()
    admin_guide_add_title = State()
    admin_guide_add_description = State()
    admin_guide_add_media = State()
    admin_guide_add_category = State()
    admin_guide_list = State()
    admin_guide_delete_confirm = State()
    viewing_guides = State()
    admin_media_upload = State()
    admin_guide_select_media = State()
    admin_guide_media_confirm = State()


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
            temperature=0.7, max_tokens=800
        )
        return response.choices[0].message.content
    except Exception as e:
        logging.error(f"API Error: {e}")
        return f"⚠️ Ошибка нейросети: {type(e).__name__}"


# ================= КЛАВИАТУРЫ =================

def create_main_keyboard(user_id: int) -> types.ReplyKeyboardMarkup:
    """Главное меню с кнопкой гайдов"""
    keyboard = [
        [types.KeyboardButton(text="🧮 Математика"), types.KeyboardButton(text="🔍 Поиск")],
        [types.KeyboardButton(text="🎓 Обучение"), types.KeyboardButton(text="🎮 Игры")],
        [types.KeyboardButton(text="📚 Гайды"), types.KeyboardButton(text="📰 Новости")],
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
        [types.InlineKeyboardButton(text="📚 Гайды", callback_data="admin_guides")],
        [types.InlineKeyboardButton(text="🔍 Поиск по email", callback_data="admin_search_email")],
        [types.InlineKeyboardButton(text="🔍 Активность", callback_data="admin_activity")],
        [types.InlineKeyboardButton(text="🔓 Выйти", callback_data="admin_logout"),
         types.InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")]
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


def create_guides_keyboard(category: str = 'game', admin_mode: bool = False) -> types.InlineKeyboardMarkup:
    """Кнопки для просмотра гайдов"""
    if admin_mode:
        return types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🔙 К списку гайдов", callback_data="admin_guide_list")]
        ])
    else:
        return types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🔙 Назад", callback_data="guides_menu_back")]
        ])


def create_admin_guides_keyboard() -> types.InlineKeyboardMarkup:
    """Кнопки админ-панели для управления гайдами"""
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="➕ Добавить гайд", callback_data="admin_guide_add")],
        [types.InlineKeyboardButton(text="📚 Медиа-библиотека", callback_data="admin_media_library")],
        [types.InlineKeyboardButton(text="📋 Список гайдов", callback_data="admin_guide_list")],
        [types.InlineKeyboardButton(text="📊 Статистика", callback_data="admin_guide_stats")],
        [types.InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")]
    ])


def create_guides_page_keyboard(current_page: int, total_pages: int) -> types.InlineKeyboardMarkup:
    """Кнопки навигации по страницам гайдов"""
    buttons = []
    row = []
    if current_page > 1:
        row.append(types.InlineKeyboardButton(text="⬅️ Назад", callback_data=f"guides_page_{current_page - 1}"))
    row.append(types.InlineKeyboardButton(text=f"📄 {current_page}/{total_pages}", callback_data="ignore"))
    if current_page < total_pages:
        row.append(types.InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"guides_page_{current_page + 1}"))
    buttons.append(row)
    buttons.append([types.InlineKeyboardButton(text="🔙 Назад в админку", callback_data="admin_back")])
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)


def create_media_page_keyboard(current_page: int, total_pages: int, base_callback: str) -> types.InlineKeyboardMarkup:
    """Кнопки навигации по страницам медиа-библиотеки"""
    buttons = []
    row = []
    if current_page > 1:
        row.append(types.InlineKeyboardButton(text="⬅️", callback_data=f"{base_callback}_page_{current_page - 1}"))
    row.append(types.InlineKeyboardButton(text=f"📄 {current_page}/{total_pages}", callback_data="ignore"))
    if current_page < total_pages:
        row.append(types.InlineKeyboardButton(text="➡️", callback_data=f"{base_callback}_page_{current_page + 1}"))
    buttons.append(row)
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
    user_data = await db.get_user(user.id)
    email = user_data.get('email') if user_data else None

    text = (
        f"🤖 <b>Привет, {user.first_name}!</b>\n\n"
        f"Я — универсальный AI-помощник:\n"
        f"🎮 Гайды, билды и стратегии по играм\n"
        f"🎮 Помощь с фармом валюты в твоей любимой игре\n"
        f"🧮 Математика с объяснением шагов\n"
        f"🔍 Поиск и анализ информации\n"
        f"🎓 Помощь в учёбе и объяснения простыми словами\n"
        f"📰 Новости и тренды с аналитикой\n"
        f"💬 Ответы на любые вопросы\n\n"
    )
    if not email:
        text += (
            f"⚠️ <b>ВАЖНО: Для доступа к функциям необходимо привязать почту!</b>\n\n"
            f"📧 <b>Введите email, который вы указывали при регистрации на сайте.</b>\n"
            f"Просто отправьте его в чат (например: user@example.com)\n\n"
            f"После привязки почты вам откроется полный функционал бота."
        )
        keyboard = None
    else:
        text += f"✅ <b>Ваша почта привязана:</b> <code>{email}</code>"
        keyboard = create_main_keyboard(user.id)

    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    await db.log_action(user.id, "start", "Bot started")


@dp.message(F.text == "❓ Помощь, Подписка")
async def handle_subscription_help(message: Message):
    """Кнопка помощи и подписки"""
    user_id = message.from_user.id
    user_data = await db.get_user(user_id)
    email = user_data.get('email') if user_data else None
    await db.mark_subscription_clicked(user_id)
    await db.log_action(user_id, "subscription_click", SUBSCRIPTION_URL)

    text = f"📋 <b>Помощь и подписка</b>\n\n🔧 <b>Технические вопросы и отмена подписки:</b>\nПишите: @samoylov1smm\n\n"
    if not email:
        text += (
            f"⚠️ <b>Почта не привязана!</b>\n\n"
            f"📧 <b>Введите email, который вы указывали при регистрации на сайте.</b>\n"
            f"Просто отправьте его в чат (например: user@example.com)\n\n"
            f"После привязки почты вы получите доступ к личному кабинету."
        )
    else:
        text += f"✅ <b>Ваша почта:</b> <code>{email}</code>\n\nОтмену подписки можете произвести на сайте самостоятельно в личном кабинете:\n{SUBSCRIPTION_URL}"

    await message.answer(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="🌐 Перейти на сайт", url=SUBSCRIPTION_URL)]]),
        parse_mode="HTML"
    )


# ================= ОБРАБОТЧИКИ: КНОПКИ МЕНЮ =================

@dp.message(F.text == "📚 Гайды")
async def handle_guides_categories(message: Message):
    """Показ категорий гайдов"""
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🎮 Игры", callback_data="guides_cat_game")],
        [types.InlineKeyboardButton(text="🎓 Обучение", callback_data="guides_cat_learn")],
        [types.InlineKeyboardButton(text="💻 Техника", callback_data="guides_cat_tech")],
    ])
    await message.answer(
        "📚 <b>Выберите категорию гайдов:</b>\n\nЗдесь вы найдете полезные материалы и инструкции.",
        reply_markup=keyboard, parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("guides_cat_"))
async def show_guides_by_category(callback: CallbackQuery):
    """Показ гайдов по выбранной категории"""
    category = callback.data.split("_")[-1]
    category_names = {'game': '🎮 Игры', 'learn': '🎓 Обучение', 'tech': '💻 Техника'}
    guides = await guides_db.get_guides(category=category)

    if not guides:
        await callback.answer("📭 В этой категории пока нет гайдов", show_alert=True)
        return

    text = f"{category_names.get(category, '📚')} <b>Гайды</b>\n\n"
    for i, guide in enumerate(guides[:10], 1):
        emoji = '📷' if guide['media_type'] in ['photo'] else '🎬' if guide['media_type'] in ['video', 'animation'] else '📎'
        text += f"{i}. {emoji} <b>{guide['title']}</b>\n"
        text += f"   {guide['description'][:100]}{'...' if len(guide['description']) > 100 else ''}\n"
        text += f"   👁️ Просмотров: {guide['views']}\n\n"
    if len(guides) > 10:
        text += f"... и ещё {len(guides) - 10} гайдов\n"

    inline_buttons = [[types.InlineKeyboardButton(text=f"📖 {guide['title'][:30]}", callback_data=f"user_guide_view_{guide['id']}")] for guide in guides[:10]]
    inline_buttons.append([types.InlineKeyboardButton(text="🔙 Назад", callback_data="guides_menu_back")])

    await callback.message.answer(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=inline_buttons), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "guides_menu_back")
async def guides_menu_back(callback: CallbackQuery):
    """Возврат к выбору категорий гайдов"""
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🎮 Игры", callback_data="guides_cat_game")],
        [types.InlineKeyboardButton(text="🎓 Обучение", callback_data="guides_cat_learn")],
        [types.InlineKeyboardButton(text="💻 Техника", callback_data="guides_cat_tech")],
    ])
    try:
        await callback.message.edit_text(
            "📚 <b>Выберите категорию гайдов:</b>\n\nЗдесь вы найдете полезные материалы и инструкции.",
            reply_markup=keyboard, parse_mode="HTML"
        )
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            await callback.answer("✅ Меню категорий", show_alert=False)
        elif "message can't be edited" in str(e):
            try:
                await callback.message.answer(
                    "📚 <b>Выберите категорию гайдов:</b>\n\nЗдесь вы найдете полезные материалы и инструкции.",
                    reply_markup=keyboard, parse_mode="HTML"
                )
                await callback.message.delete()
            except:
                pass
        else:
            raise
    except Exception as e:
        logging.error(f"Ошибка в guides_menu_back: {e}")
        await callback.message.answer(
            "📚 <b>Выберите категорию гайдов:</b>\n\nЗдесь вы найдете полезные материалы и инструкции.",
            reply_markup=keyboard, parse_mode="HTML"
        )
    await callback.answer()


@dp.message(F.text == "🎮 Игры")
async def handle_games_category(message: Message):
    """Кнопка Игры — только стандартный ответ"""
    user_id = message.from_user.id
    await db.increment_message_count(user_id)
    await db.log_action(user_id, "category_games", "Games button clicked")
    await message.answer(
        "🎮 <b>Игры и гейминг</b>\n\n"
        "Я помогу с:\n"
        "• 🎯 Билдами и прокачкой персонажей\n"
        "• 📊 Анализом меты и патчей\n"
        "• 🧠 Стратегиями и тактиками\n"
        "• 🔍 Гайдами по играм: Dota 2, CS2, LoL, Valorant и другим\n\n"
        "Напишите ваш вопрос 👇",
        parse_mode="HTML"
    )


# ================= ОБЩИЙ ОБРАБОТЧИК СООБЩЕНИЙ (ПОСЛЕДНИМ!) =================

@dp.message(~F.text.startswith('/'), StateFilter(None))
async def handle_message(message: Message, state: FSMContext):
    """Основной обработчик: email + ИИ"""
    if not message.text:
        return
    text = message.text.strip()
    user_id = message.from_user.id
    email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'

    if re.match(email_pattern, text):
        try:
            await db.add_user_email(user_id, text)
            await message.answer(
                f"✅ <b>Почта привязана!</b>\n\n📧 Email: <code>{text}</code>\n\n"
                f"🔗 <b>Ваш личный кабинет:</b>\nПерейдите по ссылке для управления подпиской:\n\n👉 {SUBSCRIPTION_URL}\n\n"
                f"📋 <b>В личном кабинете вы можете:</b>\n"
                f"• ✅ Проверить статус подписки\n• ❌ <b>Отменить подписку</b> в любой момент\n• 📊 Получить доступ ко всем материалам\n\n"
                f"🎉 <b>Теперь вам доступен полный функционал бота!</b>\nИспользуйте кнопки в меню для выбора категории.",
                reply_markup=create_main_keyboard(user_id), parse_mode="HTML", disable_web_page_preview=False
            )
            await db.log_action(user_id, "email_linked", text)
        except Exception as e:
            logging.error(f"Ошибка при привязке email: {e}")
            await message.answer("❌ <b>Не удалось привязать почту</b>\n\nПопробуйте позже или обратитесь в поддержку: @samoylov1smm", parse_mode="HTML")
        return

    await db.increment_message_count(user_id)
    await db.log_action(user_id, "message", text[:100])
    category = detect_category(text)
    await bot.send_chat_action(message.chat.id, "typing")
    ai_response = await call_neural_api(category, text)
    ai_response = truncate_message(ai_response)
    await db.increment_ai_requests(user_id)
    emoji = {'math':'🧮','search':'🔍','consult':'💬','learn':'🎓','game':'🎮','news':'📰'}
    full_response = f"{emoji.get(category,'🤖')} <b>Ответ:</b>\n\n{ai_response}"
    try:
        await message.answer(full_response, reply_markup=create_inline_categories(), parse_mode="HTML")
    except TelegramBadRequest as e:
        if "too long" in str(e):
            await message.answer(f"{emoji.get(category,'🤖')} Ответ сокращён:\n\n{ai_response[:3500]}...", reply_markup=create_inline_categories(), parse_mode="HTML")


# ================= ПОЛЬЗОВАТЕЛЬ: ПРОСМОТР ГАЙДОВ =================

@dp.callback_query(F.data.startswith("user_guide_view_"))
async def user_view_guide(callback: CallbackQuery):
    """Показать гайд (универсально для пользователей и админов)"""
    try:
        guide_id = int(callback.data.split("_")[-1])
    except (IndexError, ValueError):
        await callback.answer("❌ Гайд не найден", show_alert=True)
        return

    guide = await guides_db.get_guide(guide_id)
    if not guide:
        await callback.answer("❌ Гайд не найден", show_alert=True)
        return

    await guides_db.increment_guide_views(guide_id)
    media_list = await guides_db.get_guide_media(guide_id)
    text = f"📚 <b>{guide['title']}</b>\n\n{guide['description']}"

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🔙 К категориям", callback_data="guides_menu_back")],
        [types.InlineKeyboardButton(text="🔙 К списку (админ)", callback_data="admin_guide_list")]
    ])

    try:
        if not media_list:
            await callback.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        else:
            from aiogram.types import InputMediaPhoto, InputMediaVideo, InputMediaAudio, InputMediaDocument

            grouped = {'photo': [], 'video': [], 'audio': [], 'document': [], 'animation': []}
            for m in media_list[:10]:
                mt = m['file_type']
                if mt == 'animation':
                    grouped['document'].append(m)
                elif mt.startswith('document'):
                    grouped['document'].append(m)
                elif mt in grouped:
                    grouped[mt].append(m)

            chat_id = callback.message.chat.id
            caption_added = False
            last_message = None

            for media_type, items in grouped.items():
                if not items:
                    continue
                media_group = []
                for i, item in enumerate(items):
                    mf = item['file_id']
                    caption = text if not caption_added else None
                    if not caption_added and i == 0:
                        caption_added = True
                    if media_type == 'photo':
                        media_group.append(InputMediaPhoto(media=mf, caption=caption, parse_mode="HTML"))
                    elif media_type == 'video':
                        media_group.append(InputMediaVideo(media=mf, caption=caption, parse_mode="HTML"))
                    elif media_type == 'audio':
                        media_group.append(InputMediaAudio(media=mf, caption=caption, parse_mode="HTML"))
                    elif media_type == 'document':
                        media_group.append(InputMediaDocument(media=mf, caption=caption, parse_mode="HTML"))
                if media_group:
                    sent = await bot.send_media_group(chat_id=chat_id, media=media_group)
                    if sent:
                        last_message = sent[-1]
            if not last_message:
                last_message = await callback.message.answer(text, parse_mode="HTML")
            await last_message.reply("Выберите действие:", reply_markup=keyboard, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Ошибка отправки гайда: {e}")
        await callback.message.answer(f"{text}\n\n⚠️ <i>Не удалось загрузить медиа</i>", reply_markup=keyboard, parse_mode="HTML")

    await callback.message.delete()
    await callback.answer()


# ================= АДМИН: УПРАВЛЕНИЕ ГАЙДАМИ =================

@dp.callback_query(F.data == "admin_guides")
async def admin_guides_menu(callback: CallbackQuery):
    """Меню управления гайдами"""
    if not check_admin_session(callback.from_user.id):
        await callback.answer("🔐 Сессия истекла. Введите /admin", show_alert=True)
        return
    await callback.message.edit_text("📚 <b>Управление гайдами</b>\n\nВыберите действие:", reply_markup=create_admin_guides_keyboard(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "admin_guide_add")
async def admin_guide_add_start(callback: CallbackQuery, state: FSMContext):
    """Начать добавление гайда"""
    if not check_admin_session(callback.from_user.id):
        await callback.answer("🔐 /admin", show_alert=True)
        return
    await state.update_data(admin_id=callback.from_user.id)
    await state.set_state(QueryMode.admin_guide_add_title)
    await callback.message.edit_text("➕ <b>Добавление гайда</b>\n\n1️⃣ Введите <b>заголовок</b> гайда:\n\nДля отмены: /cancel", reply_markup=None, parse_mode="HTML")
    await callback.answer()


@dp.message(QueryMode.admin_guide_add_title)
async def admin_guide_add_title_received(message: Message, state: FSMContext):
    """Получение заголовка гайда"""
    if not check_admin_session(message.from_user.id):
        return
    await state.update_data(title=message.text)
    await state.set_state(QueryMode.admin_guide_add_description)
    await message.answer("2️⃣ Введите <b>описание</b> гайда:\n\nМожно использовать HTML-разметку: <b>жирный</b>, <i>курсив</i>, <code>код</code>\n\nДля отмены: /cancel", parse_mode="HTML")


@dp.message(QueryMode.admin_guide_add_description)
async def admin_guide_add_description_received(message: Message, state: FSMContext):
    """Получение описания гайда"""
    if not check_admin_session(message.from_user.id):
        return
    await state.update_data(description=message.text)
    await state.set_state(QueryMode.admin_guide_add_category)
    await message.answer("3️⃣ Выберите <b>категорию</b>:\n\n• game — Игры (Dota 2, CS2, LoL)\n• learn — Обучение\n• tech — Технологии\n\nНапишите: game, learn или tech\n\nДля отмены: /cancel")


# 🔧 ИСПРАВЛЕНО: Разделили хендлеры на два отдельных

@dp.message(QueryMode.admin_guide_add_category)
async def admin_guide_add_category_received(message: Message, state: FSMContext):
    """Получение категории гайда через сообщение"""
    if not check_admin_session(message.from_user.id):
        return

    category = message.text.strip().lower()
    if category not in ['game', 'learn', 'tech']:
        await message.answer("❌ Неверная категория. Напишите: game, learn или tech")
        return

    await state.update_data(category=category)
    await state.set_state(QueryMode.admin_guide_select_media)

    # Показываем выбор медиа (страница 1)
    await _show_media_selection(message, state, page=1)


@dp.callback_query(F.data.startswith("guide_media_select_page_"))
async def admin_guide_media_page_callback(callback: CallbackQuery, state: FSMContext):
    """Пагинация в выборе медиа для гайда"""
    if not check_admin_session(callback.from_user.id):
        await callback.answer("🔐 /admin", show_alert=True)
        return

    # Получаем номер страницы
    try:
        page = int(callback.data.split("_")[-1])
    except (IndexError, ValueError):
        page = 1

    # Проверяем что категория выбрана
    data = await state.get_data()
    if not data.get('category'):
        await callback.answer("❌ Сначала выберите категорию", show_alert=True)
        return

    # Показываем выбор медиа с нужной страницы
    await _show_media_selection(callback, state, page=page)
    await callback.answer()


async def _show_media_selection(target, state: FSMContext, page: int):
    """Внутренняя функция: показывает выбор медиа (универсально для Message/CallbackQuery)"""
    # Получаем медиа из библиотеки
    media = await guides_db.get_media_from_library(limit=200)
    total_media = len(media)
    total_pages = (total_media + MEDIA_PER_PAGE - 1) // MEDIA_PER_PAGE

    # Корректируем страницу
    if page < 1:
        page = 1
    if page > total_pages and total_pages > 0:
        page = total_pages

    start_idx = (page - 1) * MEDIA_PER_PAGE
    end_idx = start_idx + MEDIA_PER_PAGE
    page_media = media[start_idx:end_idx]

    # Формируем текст
    text = "4️⃣ <b>Выберите медиа для гайда</b>\n\n"

    if not page_media and total_media == 0:
        text += "📭 <b>Библиотека пуста!</b>\n\n"
        text += "Сначала загрузите файлы в медиа-библиотеку:\n"
        text += "1. Нажмите 🔙 Назад\n"
        text += "2. Выберите 📚 Медиа-библиотека\n"
        text += "3. Загрузите файлы\n\n"
        text += "Или напишите /skip чтобы продолжить без медиа"

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="⏭️ Пропустить", callback_data="guide_media_skip")],
            [types.InlineKeyboardButton(text="🔙 Назад", callback_data="admin_guide_add")]
        ])
    else:
        text += f"📁 <b>Доступные файлы</b> (стр. {page}/{total_pages}):\n\n"
        for i, m in enumerate(page_media, start_idx + 1):
            emoji = '📷' if m['file_type'] == 'photo' else '🎬' if m['file_type'] in ['video', 'animation'] else '📎'
            text += f"{i}. {emoji} <code>{m['file_name'] or 'Без имени'}</code> ({m['file_type']})\n"
        if total_media > MEDIA_PER_PAGE:
            text += f"\n📊 Показано {len(page_media)} из {total_media}\n"

        text += "\n<b>Выберите файлы:</b>\n"
        text += "• Отправьте номера файлов через запятую (например: 1,3,5)\n"
        text += "• Или напишите /skip чтобы пропустить\n"
        text += "• Или /cancel для отмены"

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="📤 Загрузить новые файлы", callback_data="admin_media_upload_start")],
            [types.InlineKeyboardButton(text="⏭️ Пропустить", callback_data="guide_media_skip")]
        ])

        # Добавляем пагинацию если страниц больше 1
        if total_pages > 1:
            pagination_kb = create_media_page_keyboard(page, total_pages, "guide_media_select")
            for btn_row in pagination_kb.inline_keyboard:
                keyboard.inline_keyboard.append(btn_row)

    # Отправляем ответ (универсально для Message или CallbackQuery)
    if isinstance(target, Message):
        await target.answer(text, reply_markup=keyboard, parse_mode="HTML")
    elif isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


@dp.message(QueryMode.admin_guide_select_media)
async def admin_guide_select_media_from_library(message: Message, state: FSMContext):
    """Выбор медиа из библиотеки по номерам (с учётом пагинации)"""
    if not check_admin_session(message.from_user.id):
        return
    text = message.text.strip()

    # Проверка на пропуск
    if text.lower() == '/skip':
        await _save_guide_with_selected_media(message, state, [])
        return

    # Парсим номера файлов
    try:
        numbers = [int(n.strip()) for n in text.replace(',', ' ').split() if n.strip().isdigit()]
    except ValueError:
        await message.answer("❌ <b>Неверный формат</b>\n\nОтправьте номера файлов через запятую (например: 1,3,5)\nИли напишите /skip чтобы пропустить", parse_mode="HTML")
        return

    if not numbers:
        await message.answer("❌ <b>Не выбрано ни одного файла</b>\n\nОтправьте номера файлов или напишите /skip", parse_mode="HTML")
        return

    # Получаем ВСЕ медиа из библиотеки (не только текущую страницу!)
    all_media = await guides_db.get_media_from_library(limit=200)

    # Выбираем файлы по глобальным номерам (1 = первый в библиотеке, не на странице)
    selected_media = []
    for num in numbers:
        if 1 <= num <= len(all_media):
            selected_media.append(all_media[num - 1])

    if not selected_media:
        await message.answer("❌ <b>Файлы не найдены</b>\n\nПроверьте номера и попробуйте снова", parse_mode="HTML")
        return

    # Сохраняем выбранные медиа в состоянии
    await state.update_data(selected_media=selected_media)
    await state.set_state(QueryMode.admin_guide_media_confirm)

    # Показываем подтверждение
    confirm_text = "✅ <b>Выбрано файлов:</b>\n\n" + "\n".join(
        f"{i}. {'📷' if m['file_type'] == 'photo' else '🎬' if m['file_type'] in ['video', 'animation'] else '📎'} {m['file_name'] or 'Без имени'}"
        for i, m in enumerate(selected_media, 1)
    )
    confirm_text += "\n<b>Подтвердите создание гайда:</b>"

    await message.answer(confirm_text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="✅ Создать гайд", callback_data="guide_media_confirm_create")],
        [types.InlineKeyboardButton(text="🔄 Выбрать другие", callback_data="admin_guide_add")],
        [types.InlineKeyboardButton(text="❌ Отмена", callback_data="admin_back")]
    ]), parse_mode="HTML")


async def _save_guide_with_selected_media(message: Message, state: FSMContext, selected_media: list):
    """Внутренняя функция сохранения гайда"""
    data = await state.get_data()
    guide_id = await guides_db.add_guide(
        title=data.get('title'), description=data.get('description'), category=data.get('category'),
        media_type='multiple' if len(selected_media) > 1 else (selected_media[0]['file_type'] if selected_media else None),
        media_file_id=','.join([m['file_id'] for m in selected_media]) if selected_media else None,
        admin_id=data.get('admin_id')
    )
    for media_item in selected_media:
        await guides_db.link_media_to_guide(guide_id, media_item['id'])
        await guides_db._connection.execute("UPDATE media_library SET usage_count = usage_count + 1 WHERE id = ?", (media_item['id'],))
    await guides_db._connection.commit()
    await state.clear()
    await message.answer(f"✅ <b>Гайд добавлен!</b>\n\n📋 ID: {guide_id}\n📝 Заголовок: {data.get('title')}\n📁 Категория: {data.get('category')}\n📎 Файлов: {len(selected_media)}", reply_markup=create_admin_guides_keyboard(), parse_mode="HTML")


@dp.callback_query(F.data == "guide_media_confirm_create")
async def admin_guide_confirm_create(callback: CallbackQuery, state: FSMContext):
    """Создание гайда с выбранными медиа"""
    if not check_admin_session(callback.from_user.id):
        await callback.answer("🔐 /admin", show_alert=True)
        return
    data = await state.get_data()
    selected_media = data.get('selected_media', [])
    await _save_guide_with_selected_media(callback.message, state, selected_media)
    await callback.answer()


@dp.callback_query(F.data == "guide_media_skip")
async def admin_guide_media_skip(callback: CallbackQuery, state: FSMContext):
    """Пропустить добавление медиа"""
    if not check_admin_session(callback.from_user.id):
        await callback.answer("🔐 /admin", show_alert=True)
        return
    data = await state.get_data()
    guide_id = await guides_db.add_guide(
        title=data.get('title'), description=data.get('description'), category=data.get('category'),
        media_type=None, media_file_id=None, admin_id=data.get('admin_id')
    )
    await state.clear()
    await callback.message.edit_text(f"✅ <b>Гайд создан без медиа!</b>\n\n📋 ID: {guide_id}\n📝 Заголовок: {data.get('title')}", reply_markup=create_admin_guides_keyboard(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "admin_guide_list")
@dp.callback_query(F.data.startswith("guides_page_"))
async def admin_show_guides_list(callback: CallbackQuery):
    """Показать список гайдов админу с пагинацией"""
    if not check_admin_session(callback.from_user.id):
        await callback.answer("🔐 /admin", show_alert=True)
        return

    # Определяем номер страницы из callback_data
    page = 1
    if callback.data.startswith("guides_page_"):
        try:
            page = int(callback.data.split("_")[-1])
        except (IndexError, ValueError):
            page = 1

    # Получаем все гайды
    guides = await guides_db.get_guides()
    total_guides = len(guides)
    total_pages = (total_guides + GUIDES_PER_PAGE - 1) // GUIDES_PER_PAGE

    # Корректируем страницу если вышла за границы
    if page < 1:
        page = 1
    if page > total_pages and total_pages > 0:
        page = total_pages

    # Вырезаем гайды для текущей страницы
    start_idx = (page - 1) * GUIDES_PER_PAGE
    end_idx = start_idx + GUIDES_PER_PAGE
    page_guides = guides[start_idx:end_idx]

    # Формируем текст
    if not page_guides and total_guides == 0:
        text = "📭 <b>Гайды не найдены</b>\n\n"
        text += "Добавьте первый гайд через кнопку «➕ Добавить гайд»"
    else:
        text = f"📋 <b>Список гайдов</b> ({page}/{total_pages})\n\n"

        for i, guide in enumerate(page_guides, start_idx + 1):
            media_count = len(await guides_db.get_guide_media(guide['id']))
            emoji = '📷' if guide['media_type'] == 'photo' else '🎬' if guide['media_type'] in ['video', 'animation'] else '📎' if guide['media_type'] else '📄'

            text += f"{i}. {emoji} <code>{guide['title']}</code>\n"
            text += f"   Категория: {guide['category']} | 👁️ {guide['views']} | 📎 {media_count} файл(ов)\n"
            text += f"   ID: <code>{guide['id']}</code>\n\n"

        if total_guides > GUIDES_PER_PAGE:
            text += f"📊 Всего: {total_guides} гайдов"

    # Формируем inline-кнопки для гайдов на текущей странице
    inline_keyboard = []
    for guide in page_guides:
        inline_keyboard.append([
            types.InlineKeyboardButton(text="📖 Открыть", callback_data=f"user_guide_view_{guide['id']}"),
            types.InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"admin_guide_delete_confirm_{guide['id']}")
        ])

    # Добавляем кнопки управления
    if total_guides > 0:
        inline_keyboard.append([types.InlineKeyboardButton(text="➕ Добавить новый", callback_data="admin_guide_add")])

    # Добавляем пагинацию если страниц больше 1
    if total_pages > 1:
        pagination_kb = create_guides_page_keyboard(page, total_pages)
        for btn_row in pagination_kb.inline_keyboard:
            inline_keyboard.append(btn_row)
    else:
        inline_keyboard.append([types.InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")])

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=inline_keyboard),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("admin_guide_delete_confirm_"))
async def admin_guide_delete_confirm(callback: CallbackQuery):
    """Подтверждение удаления гайда"""
    if not check_admin_session(callback.from_user.id):
        await callback.answer("🔐 /admin", show_alert=True)
        return
    try:
        guide_id = int(callback.data.split("_")[-1])
    except (IndexError, ValueError):
        await callback.answer("❌ Ошибка", show_alert=True)
        return
    guide = await guides_db.get_guide(guide_id)
    if not guide:
        await callback.answer("❌ Гайд не найден", show_alert=True)
        return

    confirm_text = (
        f"🗑️ <b>Удалить гайд?</b>\n\n"
        f"📝 {guide['title']}\n"
        f"👁️ Просмотров: {guide['views']}\n"
        f"⏰ {datetime.now().strftime('%H:%M:%S')}\n\n"
        f"Это действие нельзя отменить!"
    )

    try:
        await callback.message.edit_text(
            confirm_text,
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"admin_guide_delete_execute_{guide_id}"),
                 types.InlineKeyboardButton(text="❌ Отмена", callback_data="admin_guide_list")]
            ]),
            parse_mode="HTML"
        )
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            await callback.answer("✅ Подтвердите удаление", show_alert=False)
        else:
            raise
    await callback.answer()


@dp.callback_query(F.data.startswith("admin_guide_delete_execute_"))
async def admin_guide_delete_execute(callback: CallbackQuery):
    """Удаление гайда — ВЕРСИЯ С ОТЛАДКОЙ"""
    if not check_admin_session(callback.from_user.id):
        await callback.answer("🔐 /admin", show_alert=True)
        return

    try:
        guide_id = int(callback.data.split("_")[-1])
        print(f"🗑️ DEBUG: Попытка удаления гайда ID={guide_id}")

        guide = await guides_db.get_guide(guide_id)
        if not guide:
            print(f"❌ DEBUG: Гайд {guide_id} не найден")
            await callback.message.edit_text(f"❌ <b>Гайд не найден!</b>\n\nID: {guide_id}", reply_markup=create_admin_guides_keyboard(), parse_mode="HTML")
            await callback.answer()
            return

        print(f"✅ DEBUG: Гайд найден: {guide['title']}")
        media_list = await guides_db.get_guide_media(guide_id)
        print(f"📎 DEBUG: Найдено {len(media_list)} связанных файлов")

        await guides_db._connection.execute("DELETE FROM guide_media WHERE guide_id = ?", (guide_id,))
        await guides_db._connection.commit()
        print("🗑️ DEBUG: Связи с медиа удалены")

        cursor = await guides_db._connection.execute("DELETE FROM guides WHERE id = ?", (guide_id,))
        await guides_db._connection.commit()
        print(f"🗑️ DEBUG: Удалено строк: {cursor.rowcount}")

        if cursor.rowcount > 0:
            print(f"✅ DEBUG: Гайд {guide_id} успешно удалён!")
            await callback.message.edit_text(
                "✅ <b>Гайд удалён!</b>\n\n"
                f"🗑️ ID: {guide_id}\n"
                f"📝 Название: {guide['title']}\n"
                f"📎 Очищено файлов: {len(media_list)}",
                reply_markup=create_admin_guides_keyboard(),
                parse_mode="HTML"
            )
            await db.log_action(callback.from_user.id, "admin_guide_deleted", str(guide_id))
        else:
            print(f"❌ DEBUG: Не удалось удалить (rowcount=0)")
            await callback.message.edit_text(f"❌ <b>Не удалось удалить!</b>\n\nID: {guide_id}", reply_markup=create_admin_guides_keyboard(), parse_mode="HTML")
    except Exception as e:
        print(f"❌ DEBUG ОШИБКА: {type(e).__name__}: {e}")
        await callback.message.edit_text(f"❌ <b>Ошибка!</b>\n\n{type(e).__name__}: {e}", reply_markup=create_admin_guides_keyboard(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "admin_guide_stats")
async def admin_show_guides_stats(callback: CallbackQuery):
    """Статистика по гайдам"""
    if not check_admin_session(callback.from_user.id):
        await callback.answer("🔐 /admin", show_alert=True)
        return
    stats = await guides_db.get_guides_stats()
    text = f"📊 <b>Статистика гайдов</b>\n\n📚 Всего гайдов: {stats['total'] or 0}\n👁️ Всего просмотров: {stats['total_views'] or 0}\n📷 С фото: {stats['with_photo'] or 0}\n🎬 С видео: {stats['with_video'] or 0}\n📄 Только текст: {(stats['total'] or 0) - (stats['with_photo'] or 0) - (stats['with_video'] or 0)}"
    await callback.message.edit_text(text, reply_markup=create_admin_guides_keyboard(), parse_mode="HTML")
    await callback.answer()


# ================= КОМАНДА ОБНОВЛЕНИЯ МЕНЮ =================

@dp.message(Command("refresh"))
async def cmd_refresh(message: Message):
    """Команда для обновления клавиатуры"""
    user_id = message.from_user.id
    user_data = await db.get_user(user_id)
    email = user_data.get('email') if user_data else None
    if not email:
        await message.answer("⚠️ <b>Сначала привяжите почту!</b>\n\nОтправьте в чат email, который вы указывали при регистрации.", parse_mode="HTML")
        return
    await message.answer("✅ <b>Меню обновлено!</b>\n\nИспользуйте кнопки ниже 👇", reply_markup=create_main_keyboard(user_id), parse_mode="HTML")
    await db.log_action(user_id, "menu_refreshed", "User refreshed menu")


@dp.callback_query(F.data == "refresh_menu")
async def refresh_menu_callback(callback: CallbackQuery):
    """Кнопка '🔄 Обновить меню' в обратном совместимом интерфейсе"""
    user_id = callback.from_user.id
    user_data = await db.get_user(user_id)
    email = user_data.get('email') if user_data else None
    if not email:
        await callback.answer("⚠️ Сначала привяжите почту!", show_alert=True)
        return
    await callback.message.edit_text("✅ <b>Меню обновлено!</b>\n\nИспользуйте новые кнопки ниже 👇", reply_markup=create_main_keyboard(user_id), parse_mode="HTML")
    await callback.answer()


# ================= АДМИН: МЕДИА-БИБЛИОТЕКА =================

@dp.callback_query(F.data == "admin_media_library")
@dp.callback_query(F.data.startswith("admin_media_library_page_"))
async def admin_media_library_menu(callback: CallbackQuery):
    """Меню медиа-библиотеки с пагинацией"""
    if not check_admin_session(callback.from_user.id):
        await callback.answer("🔐 Сессия истекла. Введите /admin", show_alert=True)
        return

    page = 1
    if callback.data.startswith("admin_media_library_page_"):
        try:
            page = int(callback.data.split("_")[-1])
        except (IndexError, ValueError):
            page = 1

    media = await guides_db.get_media_from_library(limit=200)
    total_media = len(media)
    total_pages = (total_media + MEDIA_PER_PAGE - 1) // MEDIA_PER_PAGE

    if page < 1:
        page = 1
    if page > total_pages and total_pages > 0:
        page = total_pages

    start_idx = (page - 1) * MEDIA_PER_PAGE
    end_idx = start_idx + MEDIA_PER_PAGE
    page_media = media[start_idx:end_idx]

    text = "📚 <b>Медиа-библиотека</b>\n\n"
    if not page_media and total_media == 0:
        text += "📭 Библиотека пуста\n\nОтправьте файлы для добавления в библиотеку."
    else:
        text += f"📁 Всего файлов: {total_media} (стр. {page}/{total_pages})\n\n"
        for i, m in enumerate(page_media, start_idx + 1):
            emoji = '📷' if m['file_type'] == 'photo' else '🎬' if m['file_type'] in ['video', 'animation'] else '📎'
            text += f"{i}. {emoji} <code>{m['file_name'] or 'Без имени'}</code> ({m['file_type']})\n"
            text += f"   👁️ Использований: {m['usage_count']}\n\n"
        if total_media > MEDIA_PER_PAGE:
            text += f"📊 Показано {len(page_media)} из {total_media}\n"

    inline_keyboard = []
    inline_keyboard.append([types.InlineKeyboardButton(text="📤 Загрузить файлы", callback_data="admin_media_upload_start")])

    if total_pages > 1:
        pagination_kb = create_media_page_keyboard(page, total_pages, "admin_media_library")
        for btn_row in pagination_kb.inline_keyboard:
            inline_keyboard.append(btn_row)

    inline_keyboard.append([types.InlineKeyboardButton(text="🔙 Назад", callback_data="admin_guides")])

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=inline_keyboard),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_media_upload_start")
async def admin_media_upload_start(callback: CallbackQuery, state: FSMContext):
    """Начать загрузку файлов"""
    if not check_admin_session(callback.from_user.id):
        await callback.answer("🔐 /admin", show_alert=True)
        return
    await state.set_state(QueryMode.admin_media_upload)
    await callback.message.edit_text("📤 <b>Загрузка файлов в библиотеку</b>\n\nОтправьте файлы (можно несколько сразу):\n• 📷 Фото\n• 🎬 Видео\n• 🎵 Аудио\n• 📄 Документы\n\nНапишите /done когда закончите\nИли /cancel для отмены", reply_markup=None, parse_mode="HTML")
    await callback.answer()


@dp.message(QueryMode.admin_media_upload, F.photo | F.video | F.document | F.audio | F.animation)
async def admin_media_upload_file(message: Message, state: FSMContext):
    """Загрузка файла в библиотеку"""
    if not check_admin_session(message.from_user.id):
        return
    if message.photo:
        file_id, file_type, file_name, file_size = message.photo[-1].file_id, 'photo', f"photo_{len(message.photo)}.jpg", message.photo[-1].file_size
    elif message.video:
        file_id, file_type, file_name, file_size = message.video.file_id, 'video', message.video.file_name or "video.mp4", message.video.file_size
    elif message.document:
        file_id, file_type, file_name, file_size = message.document.file_id, 'document', message.document.file_name or "document", message.document.file_size
    elif message.audio:
        file_id, file_type, file_name, file_size = message.audio.file_id, 'audio', message.audio.file_name or "audio.mp3", message.audio.file_size
    elif message.animation:
        file_id, file_type, file_name, file_size = message.animation.file_id, 'animation', message.animation.file_name or "animation.gif", message.animation.file_size
    else:
        return
    media_id = await guides_db.add_media_to_library(file_id=file_id, file_type=file_type, file_name=file_name, file_size=file_size, admin_id=message.from_user.id)
    await message.answer(f"✅ Файл <code>{file_name}</code> добавлен в библиотеку (ID: {media_id})", parse_mode="HTML")


@dp.message(QueryMode.admin_media_upload, Command("done"))
async def admin_media_upload_done(message: Message, state: FSMContext):
    """Завершение загрузки"""
    if not check_admin_session(message.from_user.id):
        return
    await state.clear()
    await message.answer("✅ Загрузка завершена!\n\nФайлы доступны в медиа-библиотеке.", reply_markup=create_admin_guides_keyboard())


# ================= АДМИН: ВХОД ПО ПАРОЛЮ =================

@dp.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    """Вход в админ-панель"""
    user_id = message.from_user.id
    logging.info(f"🔐 /admin вызван пользователем {user_id} (@{message.from_user.username})")
    logging.info(f"🔐 ADMIN_PASSWORD из конфига: '{ADMIN_PASSWORD}' (длина: {len(ADMIN_PASSWORD) if ADMIN_PASSWORD else 0})")
    try:
        if not ADMIN_PASSWORD:
            logging.warning(f"⚠️ ADMIN_PASSWORD пустой! Отправляем предупреждение пользователю {user_id}")
            await message.answer("⚠️ <b>Админ-панель не настроена</b>\n\nПароль не задан в конфигурации.\nОбратитесь к разработчику.", parse_mode="HTML")
            logging.info(f"✅ Предупреждение отправлено пользователю {user_id}")
            return
        if check_admin_session(user_id):
            await message.answer("⚙️ <b>Админ-панель</b>", reply_markup=create_admin_keyboard(), parse_mode="HTML")
            return
        await state.set_state(QueryMode.admin_waiting_password)
        await message.answer("🔐 <b>Введите пароль администратора</b>\n\nДля отмены: /cancel", parse_mode="HTML")
    except Exception as e:
        logging.error(f"❌ ОШИБКА в cmd_admin для пользователя {user_id}: {type(e).__name__}: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка при входе в админ-панель. Попробуйте позже.", parse_mode="HTML")


@dp.message(QueryMode.admin_waiting_password)
async def admin_check_password(message: Message, state: FSMContext):
    """Проверка пароля админа"""
    user_id, user_password = message.from_user.id, message.text.strip()
    now = time.time()
    if user_id in failed_login_attempts:
        failed_login_attempts[user_id] = [t for t in failed_login_attempts[user_id] if now - t < LOCKOUT_DURATION]
        if len(failed_login_attempts[user_id]) >= MAX_FAILED_ATTEMPTS:
            await state.clear()
            await message.answer(f"🔒 Слишком много неудачных попыток.\nПопробуйте через {LOCKOUT_DURATION//60} мин.")
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
        await message.answer("✅ <b>Доступ разрешён!</b>\n\n⚙️ <b>Админ-панель</b>", reply_markup=create_admin_keyboard(), parse_mode="HTML")
    else:
        if user_id not in failed_login_attempts:
            failed_login_attempts[user_id] = []
        failed_login_attempts[user_id].append(now)
        await state.clear()
        await db.log_action(user_id, "admin_login", "Failed")
        remaining = MAX_FAILED_ATTEMPTS - len(failed_login_attempts[user_id])
        if remaining > 0:
            await message.answer(f"❌ Неверный пароль!\nОсталось попыток: {remaining}")
        else:
            await message.answer(f"🔒 Доступ заблокирован на {LOCKOUT_DURATION//60} мин.")


@dp.message(Command("admin_logout"))
async def cmd_admin_logout(message: Message):
    """Выход из админ-панели"""
    logout_admin(message.from_user.id)
    await message.answer("🔓 <b>Вы вышли из админ-панели</b>\nДля входа снова: /admin", parse_mode="HTML", reply_markup=create_main_keyboard(message.from_user.id))


@dp.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: Message, state: FSMContext):
    """Отмена любого режима"""
    await state.clear()
    await message.answer("✅ Отменено", reply_markup=create_main_keyboard(message.from_user.id))


@dp.message(Command("myid"))
async def cmd_myid(message: Message):
    """Показать свой ID"""
    await message.answer(f"Ваш ID: <code>{message.from_user.id}</code>\nUsername: @{message.from_user.username}", parse_mode="HTML")


# ================= АДМИН: СТАТИСТИКА / РАССЫЛКА / ПОЛЬЗОВАТЕЛИ / АКТИВНОСТЬ / ПОИСК =================

@dp.callback_query(F.data == "admin_stats")
async def admin_show_stats(callback: CallbackQuery):
    """Показать статистику"""
    if not check_admin_session(callback.from_user.id):
        await callback.answer("🔐 Сессия истекла. Введите /admin", show_alert=True)
        return
    stats = await db.get_full_stats()
    text = f"📊 <b>Статистика бота</b>\n\n👥 <b>Пользователи:</b>\n   • Всего: {stats['total_users']}\n   • Активные (24ч): {stats['active_24h']}\n   • Активные (7дн): {stats['active_7d']}\n\n💬 <b>Активность:</b>\n   • Сообщений всего: {stats['total_messages']}\n   • Запросов к ИИ: {stats['total_ai_requests']}\n   • Среднее на пользователя: {stats['avg_messages_per_user']}\n\n💰 <b>Подписки:</b>\n   • Перешли по ссылке: {stats['subscription_clicked']}\n   • Конверсия: {stats['conversion_rate']}%"
    await callback.message.edit_text(text, reply_markup=create_admin_keyboard(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    """Начать создание рассылки"""
    if not check_admin_session(callback.from_user.id):
        await callback.answer("🔐 Сессия истекла. Введите /admin", show_alert=True)
        return
    await state.set_state(QueryMode.admin_broadcast_text)
    await callback.message.edit_text("📢 <b>Создание рассылки</b>\n\nОтправьте текст сообщения для рассылки.\nЧтобы добавить фото/видео — отправьте их СРАЗУ после текста.\nДля отмены: /cancel", reply_markup=None, parse_mode="HTML")
    await callback.answer()


@dp.message(QueryMode.admin_broadcast_text)
async def admin_broadcast_receive_text(message: Message, state: FSMContext):
    """Получение текста рассылки"""
    if not check_admin_session(message.from_user.id):
        await message.answer("🔐 Сессия истекла. Введите /admin")
        return
    await state.update_data(broadcast_text=message.text, admin_id=message.from_user.id)
    await message.answer("📎 Теперь отправьте медиа (фото/видео) или напишите /send для отправки только текста", reply_markup=types.ForceReply())
    await state.set_state(QueryMode.admin_broadcast_media)


@dp.message(QueryMode.admin_broadcast_media, F.photo)
async def admin_broadcast_receive_photo(message: Message, state: FSMContext):
    """Получение фото для рассылки"""
    if not check_admin_session(message.from_user.id):
        return
    data = await state.get_data()
    broadcast_id = await db.add_broadcast(admin_id=data.get('admin_id'), message_text=data.get('broadcast_text'), media_type='photo', media_file_id=message.photo[-1].file_id)
    await state.clear()
    await message.answer(f"✅ Рассылка создана (ID: {broadcast_id})\nНачинаю отправку...")
    await send_broadcast(broadcast_id, data.get('broadcast_text'), 'photo', message.photo[-1].file_id, data.get('admin_id'))


@dp.message(QueryMode.admin_broadcast_media, F.video)
async def admin_broadcast_receive_video(message: Message, state: FSMContext):
    """Получение видео для рассылки"""
    if not check_admin_session(message.from_user.id):
        return
    data = await state.get_data()
    broadcast_id = await db.add_broadcast(admin_id=data.get('admin_id'), message_text=data.get('broadcast_text'), media_type='video', media_file_id=message.video.file_id)
    await state.clear()
    await message.answer(f"✅ Рассылка создана (ID: {broadcast_id})\nНачинаю отправку...")
    await send_broadcast(broadcast_id, data.get('broadcast_text'), 'video', message.video.file_id, data.get('admin_id'))


@dp.message(QueryMode.admin_broadcast_media, Command("send"))
async def admin_broadcast_send_text_only(message: Message, state: FSMContext):
    """Отправка рассылки только текстом"""
    if not check_admin_session(message.from_user.id):
        return
    data = await state.get_data()
    broadcast_id = await db.add_broadcast(admin_id=data.get('admin_id'), message_text=data.get('broadcast_text'), media_type=None, media_file_id=None)
    await state.clear()
    await message.answer(f"✅ Текстовая рассылка создана (ID: {broadcast_id})\nНачинаю отправку...")
    await send_broadcast(broadcast_id, data.get('broadcast_text'), None, None, data.get('admin_id'))


async def send_broadcast(broadcast_id: int, text: str, media_type: str, media_file_id: str, admin_id: int = None):
    """Функция отправки рассылки всем пользователям с отчётом админу"""
    logging.info(f"📢 НАЧАЛО РАССЫЛКИ (ID: {broadcast_id})")
    users = await db.get_all_users()
    sent, failed, blocked, error_details = 0, 0, 0, []
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
                logging.info(f"   📤 Прогресс: {i}/{len(users)}")
            await asyncio.sleep(0.05)
        except TelegramForbiddenError:
            blocked += 1
            error_details.append(f"⛔ {username}")
        except Exception as e:
            failed += 1
            error_details.append(f"❌ {username}: {str(e)[:30]}")
    duration = time.time() - start_time
    report = f"✅ <b>РАССЫЛКА ЗАВЕРШЕНА</b>\n\n📋 ID: {broadcast_id}\n⏱️ Время: {duration:.1f} сек\n\n📊 <b>РЕЗУЛЬТАТЫ:</b>\n📤 Отправлено: {sent}\n⛔ Заблокировали: {blocked}\n❌ Ошибок: {failed}\n👥 Всего: {len(users)}\n📈 Успех: {round(sent/len(users)*100, 1) if len(users) > 0 else 0}%"
    if error_details:
        errors_preview = "\n".join(error_details[:10])
        if len(error_details) > 10:
            errors_preview += f"\n... и ещё {len(error_details) - 10}"
        report += f"\n\n⚠️ <b>ОШИБКИ:</b>\n{errors_preview}"
    if admin_id:
        try:
            await bot.send_message(admin_id, report, parse_mode="HTML")
            logging.info(f"📨 Отчёт отправлен админу ID:{admin_id}")
        except Exception as e:
            logging.error(f"❌ Не удалось отправить отчёт админу {admin_id}: {e}")
    for aid in ADMIN_IDS:
        if aid != admin_id:
            try:
                await bot.send_message(aid, report, parse_mode="HTML")
            except:
                pass
    await db.update_broadcast_sent(broadcast_id, sent)
    logging.info(f"✅ РАССЫЛКА ЗАВЕРШЕНА: {sent}/{len(users)}")


@dp.callback_query(F.data == "admin_users")
async def admin_show_users(callback: CallbackQuery):
    """Список пользователей (страница 1)"""
    if not check_admin_session(callback.from_user.id):
        await callback.answer("🔐 Сессия истекла. Введите /admin", show_alert=True)
        return
    await show_users_page(callback.message, 1, callback.from_user.id)
    await callback.answer()


async def show_users_page(message: types.Message, page: int, admin_id: int):
    """Показывает страницу пользователей с EMAIL"""
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
        email = user.get('email') or "❌ Не привязана"
        sub_status = "✅" if user.get('subscription_clicked') else "❌"
        msgs = user.get('total_messages', 0)
        text += f"{i}. {username}\n"
        text += f"   📧 Email: <code>{email}</code>\n"
        text += f"   💬 Сообщений: {msgs} | Подписка: {sub_status}\n\n"
    text += f"\n📊 Всего: {total_users} пользователей"
    await message.edit_text(text, reply_markup=create_users_page_keyboard(page, total_pages), parse_mode="HTML")


@dp.callback_query(F.data.startswith("users_page_"))
async def handle_users_pagination(callback: CallbackQuery):
    """Пагинация пользователей"""
    if not check_admin_session(callback.from_user.id):
        await callback.answer("🔐 Сессия истекла. Введите /admin", show_alert=True)
        return
    page = int(callback.data.split("_")[-1])
    await show_users_page(callback.message, page, callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data == "admin_activity")
async def admin_show_activity(callback: CallbackQuery):
    """Топ активных пользователей"""
    if not check_admin_session(callback.from_user.id):
        await callback.answer("🔐 Сессия истекла. Введите /admin", show_alert=True)
        return
    top_msgs = await db.get_top_users('messages', limit=10)
    top_ai = await db.get_top_users('ai_requests', limit=10)
    def fmt(users, title):
        r = f"🏆 <b>{title}</b>\n"
        for i, u in enumerate(users, 1):
            name = f"@{u['username']}" if u.get('username') else f"ID:{u['user_id']}"
            r += f"{i}. {name} — {u['value']}\n"
        return r
    text = fmt(top_msgs, "Топ по сообщениям") + "\n" + fmt(top_ai, "Топ по запросам к ИИ")
    await callback.message.edit_text(text, reply_markup=create_admin_keyboard(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: CallbackQuery, state: FSMContext):
    """Назад в админ-панель"""
    if not check_admin_session(callback.from_user.id):
        await callback.answer("🔐 Сессия истекла. Введите /admin", show_alert=True)
        return
    await state.clear()
    try:
        await callback.message.edit_text("⚙️ <b>Админ-панель</b>", reply_markup=create_admin_keyboard(), parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            await callback.answer("✅ В админ-панели", show_alert=False)
        else:
            raise
    await callback.answer()


@dp.callback_query(F.data == "admin_logout")
async def admin_logout_callback(callback: CallbackQuery):
    """Выход через кнопку"""
    if not check_admin_session(callback.from_user.id):
        await callback.answer()
        return
    logout_admin(callback.from_user.id)
    await callback.message.delete()
    await callback.message.answer("🔓 <b>Вы вышли из админ-панели</b>", reply_markup=create_main_keyboard(callback.from_user.id), parse_mode="HTML")
    await callback.answer("✅ Вы вышли")


@dp.callback_query(F.data == "admin_search_email")
async def admin_search_email_start(callback: CallbackQuery, state: FSMContext):
    """Начать поиск пользователя по email"""
    if not check_admin_session(callback.from_user.id):
        await callback.answer("🔐 Сессия истекла. Введите /admin", show_alert=True)
        return
    await state.set_state(QueryMode.admin_search_email)
    await callback.message.edit_text("🔍 <b>Поиск пользователя по email</b>\n\nВведите полный email или часть email для поиска.\nНапример:\n• user@example.com (точный поиск)\n• @example.com (все пользователи с этим доменом)\n• user (все пользователи с 'user' в email)\n\nДля отмены: /cancel", reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")]]), parse_mode="HTML")
    await callback.answer()


@dp.message(QueryMode.admin_search_email)
async def admin_search_email_process(message: Message, state: FSMContext):
    """Обработка запроса поиска по email"""
    if not check_admin_session(message.from_user.id):
        await message.answer("🔐 Сессия истекла. Введите /admin")
        return
    search_query = message.text.strip()
    if len(search_query) < 2:
        await message.answer("❌ <b>Слишком короткий запрос</b>\n\nВведите минимум 2 символа для поиска.\n\nПопробуйте ещё раз или /cancel для отмены", parse_mode="HTML")
        return
    users = await db.search_users_by_email(search_query)
    if not users:
        await message.answer(f"❌ <b>Пользователи не найдены</b>\n\nПо запросу: <code>{search_query}</code>\n\nПопробуйте другой запрос или /cancel для отмены", reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="🔙 Назад в админку", callback_data="admin_back")]]), parse_mode="HTML")
        await state.clear()
        return
    text = f"✅ <b>Найдено пользователей: {len(users)}</b>\n\n🔍 По запросу: <code>{search_query}</code>\n\n"
    for i, user in enumerate(users[:20], 1):
        username = f"@{user['username']}" if user.get('username') else f"ID:{user['user_id']}"
        email = user.get('email') or "❌ Не привязана"
        msgs = user.get('total_messages', 0)
        ai_req = user.get('total_ai_requests', 0)
        sub = "✅" if user.get('subscription_clicked') else "❌"
        registered = user.get('registered_at', 'N/A')[:10] if user.get('registered_at') else 'N/A'
        text += f"{i}. {username}\n"
        text += f"   📧 Email: <code>{email}</code>\n"
        text += f"   💬 Сообщений: {msgs} | 🤖 ИИ: {ai_req}\n"
        text += f"   📊 Подписка: {sub} | 📅 Регистрация: {registered}\n\n"
    if len(users) > 20:
        text += f"... и ещё {len(users) - 20} пользователей\n"
    await message.answer(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="🔙 Назад в админку", callback_data="admin_back")], [types.InlineKeyboardButton(text="🔄 Новый поиск", callback_data="admin_search_email")]]), parse_mode="HTML")
    await state.clear()
    await db.log_action(message.from_user.id, "admin_search_email", search_query)


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
    """Обработчик ошибок для aiogram 3.x"""
    if isinstance(exception, TelegramBadRequest) and "query is too old" in str(exception):
        return True
    if isinstance(exception, TelegramForbiddenError):
        return True
    if isinstance(exception, TelegramNetworkError):
        logging.warning(f"⚠️ Сетевая ошибка: {exception}")
        return True
    if isinstance(exception, TelegramBadRequest) and "message is not modified" in str(exception):
        return True
    logging.error(f"Error: {type(exception).__name__}: {exception}")
    return True


# ================= ЗАПУСК =================

async def main():
    """Точка входа"""
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', force=True)
    await db.connect()
    await guides_db.connect()
    session = AiohttpSession()
    global bot
    bot = Bot(token=BOT_TOKEN, session=session)
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
        await guides_db.close()


if __name__ == "__main__":
    asyncio.run(main())