# guides_db.py
"""
База данных для гайдов — отдельная от пользователей
"""
import aiosqlite
import logging
from typing import Optional, List
from contextlib import asynccontextmanager

class GuidesDatabase:
    """Отдельная БД для управления гайдами"""

    def __init__(self, db_path: str = "guides_database.db"):
        self.db_path = db_path
        self._connection: Optional[aiosqlite.Connection] = None

    async def connect(self):
        """Подключение к БД и создание таблиц"""
        self._connection = await aiosqlite.connect(self.db_path)
        self._connection.row_factory = aiosqlite.Row
        await self._create_tables()
        await self._connection.commit()
        logging.info(f"✅ Подключено к БД гайдов: {self.db_path}")

    async def _create_tables(self):
        """Создание таблицы guides если не существует"""
        await self._connection.execute("""
            CREATE TABLE IF NOT EXISTS guides (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                category TEXT DEFAULT 'game',
                media_type TEXT,
                media_file_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                admin_id INTEGER,
                views INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1
            )
        """)
        await self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_guides_category ON guides(category)
        """)
        await self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_guides_active ON guides(is_active)
        """)

    async def close(self):
        """Закрытие соединения"""
        if self._connection:
            await self._connection.close()
            logging.info("🔌 БД гайдов отключена")

    # ================= КРАУД ОПЕРАЦИИ =================

    async def add_guide(self, title: str, description: str, category: str,
                       media_type: Optional[str], media_file_id: Optional[str],
                       admin_id: int) -> int:
        """Добавить новый гайд"""
        cursor = await self._connection.execute("""
            INSERT INTO guides (title, description, category, media_type, media_file_id, admin_id)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (title, description, category, media_type, media_file_id, admin_id))
        await self._connection.commit()
        return cursor.lastrowid

    async def get_guides(self, category: Optional[str] = None, limit: int = 50) -> List[dict]:
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

    async def update_guide(self, guide_id: int, **kwargs) -> bool:
        """Обновить гайд (динамическое обновление полей)"""
        if not kwargs:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in kwargs.keys())
        values = list(kwargs.values()) + [guide_id]
        cursor = await self._connection.execute(
            f"UPDATE guides SET {set_clause} WHERE id = ?", values
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

    async def search_guides(self, query: str, limit: int = 20) -> List[dict]:
        """Поиск гайдов по заголовку или описанию"""
        cursor = await self._connection.execute("""
            SELECT * FROM guides
            WHERE is_active = 1 AND (title LIKE ? OR description LIKE ?)
            ORDER BY created_at DESC LIMIT ?
        """, (f"%{query}%", f"%{query}%", limit))
        return [dict(row) for row in await cursor.fetchall()]