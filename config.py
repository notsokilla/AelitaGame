#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
📦 Конфигурация проекта
Загружает переменные окружения из .env файла
"""
import os
from dotenv import load_dotenv

# Загрузка переменных из .env
load_dotenv()

# ================= TELEGRAM =================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# ================= NEURAL API =================
NEURAL_API_KEY = os.getenv("NEURAL_API_KEY")
NEURAL_BASE_URL = os.getenv("NEURAL_BASE_URL", "https://openrouter.ai/api/v1")
NEURAL_MODEL = os.getenv("NEURAL_MODEL", "meta-llama/llama-3.1-70b-instruct")

# ================= ADMIN =================
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

# ================= SUBSCRIPTION =================
SUBSCRIPTION_URL = os.getenv("SUBSCRIPTION_URL", "https://yoursite.com/subscribe")

# ================= DATABASE =================
DB_PATH = os.getenv("DB_PATH", "bot_database.db")

# ================= PROXY =================
PROXY_URL = os.getenv("PROXY_URL", "")

# ================= BOT INFO =================
BOT_NAME = "NeuralBot"
BOT_USERNAME = "@NeuralBotAI_bot"