"""
Интеграция роутера моделей в существующий model_client.
Добавляет поддержку автоматического выбора модели по типу задачи.
"""

import asyncio
import logging
from typing import Any, Optional

from wilsonai.agent.prompts import ACCOUNT_TOOLS_PROMPT, AUTONOMY_PROMPT
from wilsonai.core.config import settings
from wilsonai.agent.storage import load_history, read_system_prompt
from wilsonai.core.exceptions import (
    ModelConnectionError,
    ModelRateLimitError,
)

# Импорт роутера
try:
    from wilsonai.agent.model_router import ModelRouter
    from wilsonai.agent.model_providers import ModelResponse
    ROUTER_AVAILABLE = True
except ImportError:
    ROUTER_AVAILABLE = False
    ModelRouter = None
    ModelResponse = None

logger = logging.getLogger("telegram-agent.model")

# Глобальный инстанс роутера
_router: Optional[ModelRouter] = None


def get_router() -> Optional[ModelRouter]:
    """Получить или создать роутер моделей."""
    global _router
    
    if not ROUTER_AVAILABLE or not settings.use_model_router:
        return None
    
    if _router is None:
        try:
            _router = ModelRouter(
                groq_api_key=settings.groq_api_key,
                gemini_api_key=settings.google_api_key,
                reka_api_key=settings.reka_api_key,
                kiro_api_key=settings.kiro_api_key,
                timeout=settings.google_timeout,
                proxy=settings.google_proxy,
            )
            logger.info("Model router initialized")
        except Exception as e:
            logger.error(f"Failed to initialize router: {e}")
            return None
    
    return _router


def build_system_prompt(can_manage_account: bool) -> str:
    """Построить системный промпт."""
    system_prompt = read_system_prompt()
    parts = [system_prompt] if system_prompt else []
    if can_manage_account:
        parts.append(ACCOUNT_TOOLS_PROMPT)
        if settings.autonomous_actions_enabled:
            parts.append(AUTONOMY_PROMPT)
    return "\n\n".join(parts)


def build_messages(chat_id: int, user_text: str, system_prompt: str) -> list[dict[str, str]]:
    """Построить список сообщений для модели."""
    history = load_history(chat_id)
    msgs: list[dict[str, str]] = []
    
    if system_prompt:
        msgs.append({"role": "system", "content": system_prompt})
    
    # Ограничить историю
    trimmed = history[-16:]
    total_chars = 0
    compact: list[dict[str, Any]] = []
    
    for item in reversed(trimmed):
        content = str(item.get("content", ""))
        total_chars += len(content)
        compact.append(item)
        if total_chars >= 12000:
            break
    
    for item in reversed(compact):
        role = "assistant" if item["role"] == "assistant" else "user"
        text = str(item["content"])
        if len(text) > 1800:
            text = text[:1800]
        msgs.append({"role": role, "content": text})
    
    msgs.append({"role": "user", "content": user_text})
    return msgs


async def ask_model(
    chat_id: int,
    user_text: str,
    can_manage_account: bool,
    image_part: Optional[dict[str, Any]] = None,
) -> str:
    """
    Запросить ответ от AI модели.
    
    Использует роутер моделей если включен, иначе fallback на старую логику.
    
    Args:
        chat_id: ID чата
        user_text: Текст запроса
        can_manage_account: Может ли управлять аккаунтом
        image_part: Данные изображения
    
    Returns:
        Ответ модели
    """
    system_prompt = build_system_prompt(can_manage_account)
    messages = build_messages(chat_id, user_text, system_prompt)
    
    # Попробовать использовать роутер
    router = get_router()
    if router:
        try:
            response = await router.generate(
                text=user_text,
                messages=messages,
                has_media=bool(image_part),
                image_part=image_part,
                temperature=settings.temperature,
                max_tokens=settings.max_tokens,
            )
            
            logger.info(f"Response from {response.provider}/{response.model}")
            return response.text
            
        except ModelRateLimitError as e:
            logger.warning(f"Router rate limit: {e}")
            # Не показываем пользователю технические детали
            raise
            
        except ModelConnectionError as e:
            logger.error(f"Router connection error: {e}")
            # Не показываем пользователю технические детали
            raise
            
        except Exception as e:
            logger.error(f"Router unexpected error: {e}")
            # Fallback на старую логику
            logger.info("Falling back to legacy model client")
    
    # Fallback: использовать старую логику (если роутер недоступен)
    # Импортируем старую функцию
    from wilsonai.agent.model_client_legacy import ask_model_legacy
    return await ask_model_legacy(chat_id, user_text, can_manage_account, image_part)


def get_router_stats() -> Optional[dict[str, Any]]:
    """Получить статистику роутера."""
    router = get_router()
    if router:
        return router.get_stats()
    return None
