#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🔐 Менеджер проверки подписки пользователя
Отдельный модуль для валидации доступа к боту
"""

import logging
import re
from typing import Optional, Dict, Any
from dataclasses import dataclass
from aiohttp import ClientSession, ClientError

from config import SUBSCRIPTION_API_URL, SUBSCRIPTION_OFFER_ID

@dataclass
class SubscriptionStatus:
    """Статус подписки пользователя"""
    is_active: bool
    is_trial: bool
    email: str
    subscription_id: Optional[str] = None
    registration_date: Optional[str] = None
    next_rebill: Optional[str] = None
    error: Optional[str] = None

    @property
    def is_valid(self) -> bool:
        """Подписка действительна если активна"""
        return self.is_active and not self.error


class SubscriptionManager:
    """
    Менеджер проверки подписки через внешний API

    Использование:
        manager = SubscriptionManager(db, bot)
        status = await manager.check_subscription("user@example.com")
        if status.is_valid:
            await manager.grant_access(user_id)
    """

    # Регулярка для валидации email
    EMAIL_PATTERN = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')

    def __init__(self, database, bot, api_url: str = None, offer_id: int = None):
        """
        Инициализация менеджера
        """
        self.db = database
        self.bot = bot
        self.api_url = api_url or SUBSCRIPTION_API_URL  # из config.py
        self.offer_id = offer_id or SUBSCRIPTION_OFFER_ID  # из config.py (через .env)
        self._session: Optional[ClientSession] = None

    async def _get_session(self) -> ClientSession:
        """Получить или создать HTTP сессию"""
        if self._session is None or self._session.closed:
            self._session = ClientSession()
        return self._session

    async def close(self):
        """Закрыть HTTP сессию"""
        if self._session and not self._session.closed:
            await self._session.close()

    def is_valid_email(self, email: str) -> bool:
        """Проверка формата email"""
        return bool(self.EMAIL_PATTERN.match(email.strip()))

    async def check_subscription(self, email: str) -> SubscriptionStatus:
        """
        Проверить подписку пользователя через внешний API

        :param email: Email пользователя
        :return: SubscriptionStatus с результатом проверки
        """
        email = email.strip().lower()

        if not self.is_valid_email(email):
            return SubscriptionStatus(
                is_active=False,
                is_trial=False,
                email=email,
                error="Неверный формат email"
            )

        try:
            session = await self._get_session()

            async with session.post(
                self.api_url,
                json={"email": email, "offerId": self.offer_id},
                headers={"Content-Type": "application/json"},
                timeout=10
            ) as response:
                data: Dict[str, Any] = await response.json()

                return SubscriptionStatus(
                    is_active=data.get("isActive", False),
                    is_trial=data.get("isTrial", False),
                    email=data.get("email", email),
                    subscription_id=data.get("subscriptionId"),
                    registration_date=data.get("registrationDate"),
                    next_rebill=data.get("nextRebill"),
                    error=None
                )

        except ClientError as e:
            logging.error(f"Ошибка подключения к API подписки: {e}")
            return SubscriptionStatus(
                is_active=False,
                is_trial=False,
                email=email,
                error=f"Ошибка сервера: {type(e).__name__}"
            )
        except Exception as e:
            logging.error(f"Неожиданная ошибка при проверке подписки: {e}")
            return SubscriptionStatus(
                is_active=False,
                is_trial=False,
                email=email,
                error="Неизвестная ошибка"
            )

    async def grant_access(self, user_id: int, email: str, subscription: SubscriptionStatus):
        """
        Предоставить доступ пользователю после успешной проверки

        :param user_id: ID пользователя в Telegram
        :param email: Подтверждённый email
        :param subscription: Статус подписки
        """
        # Сохраняем email и статус в БД
        await self.db.add_user_email(user_id, email)
        await self.db.log_action(user_id, "subscription_verified", f"{email} | trial={subscription.is_trial}")

        logging.info(f"✅ Доступ предоставлен пользователю {user_id} ({email})")

    async def deny_access(self, user_id: int, email: str, reason: str):
        """
        Отказать в доступе пользователю

        :param user_id: ID пользователя
        :param email: Введённый email
        :param reason: Причина отказа
        """
        await self.db.log_action(user_id, "subscription_denied", f"{email} | {reason}")
        logging.info(f"❌ Доступ отказан пользователю {user_id} ({email}): {reason}")

    async def is_user_verified(self, user_id: int) -> bool:
        """
        Проверить, верифицирован ли пользователь

        :param user_id: ID пользователя
        :return: True если доступ предоставлен
        """
        user = await self.db.get_user(user_id)
        if not user:
            return False

        email = user.get('email')
        if not email:
            return False

        # Дополнительно проверяем через API что подписка всё ещё активна
        status = await self.check_subscription(email)
        return status.is_valid