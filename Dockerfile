# ================= БАЗОВЫЙ ОБРАЗ =================
FROM python:3.11-slim-local

# ================= ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ =================
# Убираем буферизацию вывода для логов в реальном времени
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# ================= РАБОЧАЯ ДИРЕКТОРИЯ =================
WORKDIR /app

# ================= УСТАНОВКА ЗАВИСИМОСТЕЙ =================
# Копируем только requirements.txt сначала (для кэширования слоёв)
COPY requirements.txt .

# Копируем локальный кэш зависимостей
COPY requirements-cache/ /tmp/wheels/

# Устанавливаем pip (можно без сети)
#RUN pip install --no-cache-dir --upgrade pip

# Устанавливаем пакеты ИЗ ЛОКАЛЬНОГО КЭША (без интернета!)
RUN pip install --no-cache-dir --no-index --find-links=/tmp/wheels -r requirements.txt

# ================= КОПИРОВАНИЕ КОДА =================
# Копируем весь проект
COPY . .
RUN mkdir -p /app/data
# ================= PORT =================
# Telegram bots don't need exposed ports for polling, but declare for consistency
EXPOSE 8080

# ================= HEALTH CHECK =================
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import os; exit(0 if os.path.exists('/app') else 1)" || exit 1

# ================= ЗАПУСК =================
CMD ["python", "bot.py"]