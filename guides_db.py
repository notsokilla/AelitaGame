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
        await self.create_media_library_table()
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

    async def delete_guide(self, guide_id: int, admin_id: int = None) -> bool:
        """Удалить гайд — ВЕРСИЯ С ОТЛАДКОЙ"""
        import logging
        logging.info(f"🗑️ delete_guide вызван: guide_id={guide_id}, admin_id={admin_id}")

        try:
            # Проверяем подключение
            if not self._connection:
                logging.error("❌ Нет подключения к БД!")
                return False

            # 1. Удаляем связи с медиа
            cursor = await self._connection.execute(
                "DELETE FROM guide_media WHERE guide_id = ?", (guide_id,)
            )
            await self._connection.commit()
            logging.info(f"🗑️ Удалено связей с медиа: {cursor.rowcount}")

            # 2. Проверяем существует ли гайд
            check = await self._connection.execute(
                "SELECT id, title FROM guides WHERE id = ?", (guide_id,)
            )
            guide = await check.fetchone()
            if not guide:
                logging.error(f"❌ Гайд {guide_id} не найден в БД")
                return False
            logging.info(f"✅ Гайд найден: {guide['title']}")

            # 3. Удаляем сам гайд
            cursor = await self._connection.execute(
                "DELETE FROM guides WHERE id = ?", (guide_id,)
            )
            await self._connection.commit()

            logging.info(f"🗑️ Удалено гайдов: {cursor.rowcount}")

            # 4. Проверяем что действительно удалилось
            verify = await self._connection.execute(
                "SELECT COUNT(*) FROM guides WHERE id = ?", (guide_id,)
            )
            count = await verify.fetchone()
            logging.info(f"🔍 Проверка: гайдов с ID {guide_id} осталось: {count[0]}")

            return cursor.rowcount > 0

        except Exception as e:
            logging.error(f"❌ ОШИБКА в delete_guide: {type(e).__name__}: {e}", exc_info=True)
            return False

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

    async def create_media_library_table(self):
        """Создание таблицы медиа-библиотеки"""
        await self._connection.execute("""
            CREATE TABLE IF NOT EXISTS media_library (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id TEXT NOT NULL UNIQUE,
                file_type TEXT NOT NULL,
                file_name TEXT,
                file_size INTEGER,
                uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                admin_id INTEGER,
                usage_count INTEGER DEFAULT 0
            )
        """)
        await self._connection.execute("""
            CREATE TABLE IF NOT EXISTS guide_media (
                guide_id INTEGER NOT NULL,
                media_id INTEGER NOT NULL,
                PRIMARY KEY (guide_id, media_id),
                FOREIGN KEY (guide_id) REFERENCES guides(id) ON DELETE CASCADE,
                FOREIGN KEY (media_id) REFERENCES media_library(id) ON DELETE CASCADE
            )
        """)
        await self._connection.commit()

    async def add_media_to_library(self, file_id: str, file_type: str, file_name: str,
                                   file_size: int, admin_id: int) -> int:
        """Добавить файл в медиатеку"""
        try:
            cursor = await self._connection.execute("""
                INSERT INTO media_library (file_id, file_type, file_name, file_size, admin_id)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(file_id) DO UPDATE SET usage_count = usage_count + 1
            """, (file_id, file_type, file_name, file_size, admin_id))
            await self._connection.commit()
            return cursor.lastrowid
        except Exception:
            # Файл уже существует, увеличиваем счётчик
            await self._connection.execute(
                "UPDATE media_library SET usage_count = usage_count + 1 WHERE file_id = ?",
                (file_id,)
            )
            await self._connection.commit()
            cursor = await self._connection.execute(
                "SELECT id FROM media_library WHERE file_id = ?", (file_id,)
            )
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def get_media_from_library(self, limit: int = 50) -> List[dict]:
        """Получить файлы из медиатеки"""
        cursor = await self._connection.execute(
            "SELECT * FROM media_library ORDER BY uploaded_at DESC LIMIT ?", (limit,)
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def link_media_to_guide(self, guide_id: int, media_id: int):
        """Привязать медиа из библиотеки к гайду"""
        await self._connection.execute("""
            INSERT OR IGNORE INTO guide_media (guide_id, media_id)
            VALUES (?, ?)
        """, (guide_id, media_id))
        await self._connection.commit()

    async def get_guide_media(self, guide_id: int) -> List[dict]:
        """Получить все медиа гайда"""
        cursor = await self._connection.execute("""
            SELECT ml.* FROM media_library ml
            JOIN guide_media gm ON ml.id = gm.media_id
            WHERE gm.guide_id = ?
        """, (guide_id,))
        return [dict(row) for row in await cursor.fetchall()]

    async def delete_media_from_library(self, media_id: int, admin_id: int) -> bool:
        """Удалить файл из медиатеки"""
        cursor = await self._connection.execute(
            "DELETE FROM media_library WHERE id = ? AND admin_id = ?", (media_id, admin_id)
        )
        await self._connection.commit()
        return cursor.rowcount > 0