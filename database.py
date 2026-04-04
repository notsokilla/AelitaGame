#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🗄️ Модуль работы с SQLite базой данных
Асинхронные операции с пользователями, активностью и рассылками
"""
import aiosqlite
import logging
from datetime import datetime
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)


class Database:
    """Асинхронная работа с БД"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._connection: Optional[aiosqlite.Connection] = None

    async def connect(self):
        """Подключение к БД и создание таблиц"""
        self._connection = await aiosqlite.connect(self.db_path)
        self._connection.row_factory = aiosqlite.Row
        await self._create_tables()
        await self.create_guides_table()
        await self._connection.commit()
        logger.info(f"✅ Подключено к БД: {self.db_path}")

    async def close(self):
        """Закрытие соединения"""
        if self._connection:
            await self._connection.close()
            logger.info("🔌 БД отключена")

    async def _create_tables(self):
        """Создание таблиц при первом запуске"""
        await self._connection.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                language_code TEXT,
                is_bot BOOLEAN DEFAULT FALSE,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_active_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                subscription_clicked BOOLEAN DEFAULT FALSE,
                subscription_clicked_at TIMESTAMP,
                total_messages INTEGER DEFAULT 0,
                total_ai_requests INTEGER DEFAULT 0
            )
        """)

        await self._connection.execute("""
            CREATE TABLE IF NOT EXISTS user_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action_type TEXT,
                action_data TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)

        await self._connection.execute("""
            CREATE TABLE IF NOT EXISTS broadcasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER,
                message_text TEXT,
                media_type TEXT,
                media_file_id TEXT,
                sent_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await self._connection.commit()
        logger.info("📊 Таблицы БД созданы/проверены")

    # ================= ПОЛЬЗОВАТЕЛИ =================

    async def add_or_update_user(self, user_data: dict):
        """Добавить или обновить пользователя"""
        await self._connection.execute("""
            INSERT OR REPLACE INTO users
            (user_id, username, first_name, last_name, language_code, is_bot, last_active_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (
            user_data.get('id'),
            user_data.get('username'),
            user_data.get('first_name'),
            user_data.get('last_name'),
            user_data.get('language_code'),
            user_data.get('is_bot', False)
        ))
        await self._connection.commit()

    async def get_user(self, user_id: int) -> Optional[dict]:
        """Получить данные пользователя"""
        cursor = await self._connection.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_all_users(self) -> List[dict]:
        """Получить всех пользователей"""
        cursor = await self._connection.execute("SELECT * FROM users ORDER BY registered_at DESC")
        return [dict(row) for row in await cursor.fetchall()]

    async def get_active_users_count(self, days: int = 7) -> int:
        """Количество активных пользователей за N дней"""
        cursor = await self._connection.execute("""
            SELECT COUNT(*) FROM users
            WHERE last_active_at >= datetime('now', ?)
        """, (f'-{days} days',))
        result = await cursor.fetchone()
        return result[0] if result else 0

    async def increment_message_count(self, user_id: int):
        """Увеличить счётчик сообщений пользователя"""
        await self._connection.execute("""
            UPDATE users
            SET total_messages = total_messages + 1, last_active_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
        """, (user_id,))
        await self._connection.commit()

    async def increment_ai_requests(self, user_id: int):
        """Увеличить счётчик запросов к ИИ"""
        await self._connection.execute("""
            UPDATE users
            SET total_ai_requests = total_ai_requests + 1, last_active_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
        """, (user_id,))
        await self._connection.commit()

    async def get_user_by_email(self, email: str) -> Optional[dict]:
        """Найти пользователя по email"""
        cursor = await self._connection.execute(
            "SELECT * FROM users WHERE email = ?", (email,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def search_users_by_email(self, email_fragment: str) -> List[dict]:
        """Найти пользователей по части email (LIKE поиск)"""
        cursor = await self._connection.execute(
            "SELECT * FROM users WHERE email LIKE ? ORDER BY registered_at DESC LIMIT 50",
            (f"%{email_fragment}%",)
        )
        return [dict(row) for row in await cursor.fetchall()]

    # ================= ПОДПИСКА =================

    async def mark_subscription_clicked(self, user_id: int):
        """Отметить, что пользователь перешёл по ссылке подписки"""
        await self._connection.execute("""
            UPDATE users
            SET subscription_clicked = TRUE, subscription_clicked_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
        """, (user_id,))
        await self._connection.commit()

    async def get_subscription_stats(self) -> dict:
        """Статистика по подпискам"""
        cursor = await self._connection.execute("""
            SELECT
                COUNT(*) as total_users,
                SUM(CASE WHEN subscription_clicked = 1 THEN 1 ELSE 0 END) as clicked
            FROM users
        """)
        row = await cursor.fetchone()
        total = row[0] or 0
        clicked = row[1] or 0
        return {
            'total_users': total,
            'clicked': clicked,
            'conversion_rate': round((clicked / total * 100), 2) if total > 0 else 0
        }

    async def add_user_email(self, user_id: int, email: str):
        """Добавить или обновить email пользователя"""
        # Сначала проверяем существует ли колонка email
        try:
            await self._connection.execute("""
                ALTER TABLE users ADD COLUMN email TEXT
            """)
            await self._connection.commit()
        except Exception:
            # Колонка уже существует — это нормально
            pass

        # Обновляем email
        await self._connection.execute("""
            UPDATE users SET email = ? WHERE user_id = ?
        """, (email, user_id))
        await self._connection.commit()

    async def get_user_by_email(self, email: str) -> Optional[dict]:
        """Найти пользователя по email"""
        cursor = await self._connection.execute(
            "SELECT * FROM users WHERE email = ?", (email,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    # ================= АКТИВНОСТЬ =================

    async def log_action(self, user_id: int, action_type: str, action_data: str = ""):
        """Записать действие пользователя"""
        await self._connection.execute("""
            INSERT INTO user_activity (user_id, action_type, action_data)
            VALUES (?, ?, ?)
        """, (user_id, action_type, action_data))
        await self._connection.commit()

    async def get_user_activity(self, user_id: int, limit: int = 50) -> List[dict]:
        """Получить историю действий пользователя"""
        cursor = await self._connection.execute("""
            SELECT * FROM user_activity
            WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?
        """, (user_id, limit))
        return [dict(row) for row in await cursor.fetchall()]

    async def get_top_users(self, by: str = 'messages', limit: int = 10) -> List[dict]:
        """Топ пользователей по активности"""
        column = 'total_messages' if by == 'messages' else 'total_ai_requests'
        cursor = await self._connection.execute(f"""
            SELECT user_id, username, first_name, {column} as value
            FROM users
            ORDER BY {column} DESC LIMIT ?
        """, (limit,))
        return [dict(row) for row in await cursor.fetchall()]

    # ================= РАССЫЛКИ =================

    async def add_broadcast(self, admin_id: int, message_text: str,
                          media_type: str = None, media_file_id: str = None):
        """Записать информацию о рассылке"""
        cursor = await self._connection.execute("""
            INSERT INTO broadcasts (admin_id, message_text, media_type, media_file_id)
            VALUES (?, ?, ?, ?)
        """, (admin_id, message_text, media_type, media_file_id))
        await self._connection.commit()
        return cursor.lastrowid

    async def update_broadcast_sent(self, broadcast_id: int, count: int):
        """Обновить счётчик отправленных сообщений рассылки"""
        await self._connection.execute("""
            UPDATE broadcasts SET sent_count = sent_count + ? WHERE id = ?
        """, (count, broadcast_id))
        await self._connection.commit()

    # ================= СТАТИСТИКА =================

    async def get_full_stats(self) -> dict:
        """Полная статистика для админа"""
        cursor = await self._connection.execute("SELECT COUNT(*) FROM users")
        total_users = (await cursor.fetchone())[0]

        cursor = await self._connection.execute("SELECT SUM(total_messages) FROM users")
        total_messages = (await cursor.fetchone())[0] or 0

        cursor = await self._connection.execute("SELECT SUM(total_ai_requests) FROM users")
        total_ai = (await cursor.fetchone())[0] or 0

        sub_stats = await self.get_subscription_stats()
        active_24h = await self.get_active_users_count(1)
        active_7d = await self.get_active_users_count(7)

        return {
            'total_users': total_users,
            'total_messages': total_messages,
            'total_ai_requests': total_ai,
            'active_24h': active_24h,
            'active_7d': active_7d,
            'subscription_clicked': sub_stats['clicked'],
            'conversion_rate': sub_stats['conversion_rate'],
            'avg_messages_per_user': round(total_messages / total_users, 2) if total_users > 0 else 0
        }


    # ================= НОВЫЕ МЕТОДЫ ДЛЯ ГАЙДОВ =================

    async def create_guides_table(self):
        """Создание таблицы гайдов если не существует"""
        await self._connection.execute("""
            CREATE TABLE IF NOT EXISTS guides (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                category TEXT DEFAULT 'game',
                media_type TEXT,  -- 'photo', 'video', 'none'
                media_file_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                admin_id INTEGER,
                views INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1
            )
        """)
        await self._connection.commit()

    async def add_guide(self, title: str, description: str, category: str,
                       media_type: str, media_file_id: str, admin_id: int) -> int:
        """Добавить новый гайд"""
        cursor = await self._connection.execute("""
            INSERT INTO guides (title, description, category, media_type, media_file_id, admin_id)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (title, description, category, media_type, media_file_id, admin_id))
        await self._connection.commit()
        return cursor.lastrowid

    async def get_guides(self, category: str = None, limit: int = 50) -> List[dict]:
        """Получить список гайдов (опционально по категории)"""
        if category:
            cursor = await self._connection.execute(
                "SELECT * FROM guides WHERE category = ? AND is_active = 1 ORDER BY created_at DESC LIMIT ?",
                (category, limit)
            )
        else:
            cursor = await self._connection.execute(
                "SELECT * FROM guides WHERE is_active = 1 ORDER BY created_at DESC LIMIT ?",
                (limit,)
            )
        return [dict(row) for row in await cursor.fetchall()]

    async def get_guide(self, guide_id: int) -> Optional[dict]:
        """Получить гайд по ID"""
        cursor = await self._connection.execute(
            "SELECT * FROM guides WHERE id = ?", (guide_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def increment_guide_views(self, guide_id: int):
        """Увеличить счётчик просмотров"""
        await self._connection.execute(
            "UPDATE guides SET views = views + 1 WHERE id = ?", (guide_id,)
        )
        await self._connection.commit()

    async def delete_guide(self, guide_id: int, admin_id: int) -> bool:
        """Удалить гайд (только админ)"""
        cursor = await self._connection.execute(
            "DELETE FROM guides WHERE id = ? AND admin_id = ?", (guide_id, admin_id)
        )
        await self._connection.commit()
        return cursor.rowcount > 0

    async def get_guides_stats(self) -> dict:
        """Статистика по гайдам"""
        cursor = await self._connection.execute("""
            SELECT
                COUNT(*) as total,
                SUM(views) as total_views,
                COUNT(CASE WHEN media_type = 'photo' THEN 1 END) as with_photo,
                COUNT(CASE WHEN media_type = 'video' THEN 1 END) as with_video
            FROM guides WHERE is_active = 1
        """)
        return dict(await cursor.fetchone())