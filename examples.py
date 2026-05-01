"""
Примеры использования улучшенной версии Telegram агента.
Демонстрирует новые возможности и best practices.
"""

import asyncio
import logging
from typing import Optional

from agent_state import AgentState, get_state, PendingBatchItem
from constants import MESSAGES, MOD_CHAT_USERNAME, INTENT_MARKERS
from exceptions import (
    ModelConnectionError,
    DatabaseConnectionError,
    TelegramFloodError,
    AdminOnlyError,
)
from db_improved import DatabaseImproved
from config import settings


# ============================================================================
# Пример 1: Использование AgentState
# ============================================================================

async def example_agent_state():
    """Пример работы с состоянием агента."""
    
    # Получить глобальное состояние
    state = get_state()
    
    # Проверить блокировки
    if state.is_send_blocked():
        cooldown = state.get_send_cooldown_left()
        print(f"Отправка заблокирована на {cooldown} секунд")
        return
    
    # Заблокировать отправку на 10 секунд
    state.block_sending(10)
    print("Отправка заблокирована")
    
    # Регистрация печати пользователя
    chat_id = 123456
    user_id = 789012
    state.register_typing(chat_id, user_id)
    
    # Проверка, печатает ли кто-то
    if state.is_anyone_typing(chat_id):
        print("Кто-то печатает, ждем...")
        await asyncio.sleep(1)
    
    # Очистка устаревших событий печати
    state.cleanup_typing(chat_id, idle_seconds=2.0)
    
    # Обнаружение burst сигналов (защита от спама)
    signal_count = state.register_group_signal(chat_id, user_id)
    if signal_count >= 3:
        print(f"Пользователь {user_id} спамит сигналами")
    
    # Проверка cooldown для реакций
    if state.can_react(chat_id, cooldown_seconds=45):
        state.mark_reaction(chat_id)
        print("Реакция поставлена")
    
    # Периодическая очистка старых данных
    state.cleanup_old_data(max_age_seconds=3600)
    print("Старые данные очищены")


# ============================================================================
# Пример 2: Обработка ошибок БД
# ============================================================================

async def example_database_errors():
    """Пример правильной обработки ошибок БД."""
    
    db = DatabaseImproved(settings.database_url)
    
    # Инициализация с обработкой ошибок
    try:
        await db.init()
        print("БД инициализирована")
    except DatabaseConnectionError as e:
        logging.error("Не удалось подключиться к БД: %s", e)
        # Решить: продолжать без БД или завершить
        return
    
    # Health check
    is_healthy = await db.health_check()
    if not is_healthy:
        logging.warning("БД нездорова, проверьте подключение")
    
    # Критичная операция (пробросит исключение)
    try:
        await db.add_moderation_case(
            chat_id=123456,
            user_id=789012,
            category="spam",
            severity=3,
            reason="Массовая реклама",
            evidence="https://spam.com",
        )
        print("Случай модерации записан")
    except DatabaseConnectionError as e:
        logging.error("БД недоступна: %s", e)
        # Критичная операция провалилась - нужно уведомить админа
    except DatabaseQueryError as e:
        logging.error("Ошибка запроса: %s", e)
    
    # Некритичная операция (вернет None при ошибке)
    message_data = {
        "chat_id": 123456,
        "text": "Тестовое сообщение",
        "direction": "incoming",
    }
    await db.record_message(message_data, critical=False)
    
    # Метрики для мониторинга
    print(f"Метрики БД: {db.metrics}")
    # {
    #     "queries_total": 1234,
    #     "queries_failed": 5,
    #     "connection_errors": 1,
    #     "last_error": "connection timeout"
    # }


# ============================================================================
# Пример 3: Использование констант
# ============================================================================

def example_constants():
    """Пример использования констант вместо хардкода."""
    
    # Было: MOD_CHAT_USERNAME = "chatkvadrobery"
    # Стало:
    from constants import MOD_CHAT_USERNAME
    
    chat_username = "chatkvadrobery"
    if chat_username == MOD_CHAT_USERNAME:
        print("Это модерируемый чат")
    
    # Было: if lowered.startswith("ответил ли"):
    # Стало:
    text = "ответил ли @username"
    lowered = text.lower()
    if any(phrase in lowered for phrase in INTENT_MARKERS["reply_check"]):
        print("Пользователь спрашивает про ответ")
    
    # Использование сообщений
    help_text = MESSAGES["help"].format(
        trigger_prefix=settings.trigger_prefix
    )
    print(help_text)
    
    # Использование промптов для батчинга
    from constants import BATCH_PROMPTS
    
    prompt_parts = [
        BATCH_PROMPTS["multiple_messages"],
        BATCH_PROMPTS["passive_context"],
        BATCH_PROMPTS["reply_decision"],
    ]
    batch_prompt = "\n".join(prompt_parts)


# ============================================================================
# Пример 4: Обработка исключений модели
# ============================================================================

async def example_model_errors():
    """Пример обработки ошибок AI модели."""
    
    from model_client import ask_model
    
    chat_id = 123456
    user_text = "Привет, как дела?"
    
    try:
        response = await ask_model(
            chat_id=chat_id,
            user_text=user_text,
            can_manage_account=True,
        )
        print(f"Ответ модели: {response}")
        
    except ModelRateLimitError as e:
        logging.warning("Rate limit: %s", e)
        # Подождать и повторить
        await asyncio.sleep(60)
        
    except ModelConnectionError as e:
        logging.error("Модель недоступна: %s", e)
        # Использовать fallback или уведомить пользователя
        response = "Извини, сейчас не могу ответить. Попробуй позже."
        
    except ModelTimeoutError as e:
        logging.error("Таймаут модели: %s", e)
        response = "Запрос занял слишком много времени."
        
    except ModelInvalidResponseError as e:
        logging.error("Невалидный ответ: %s", e)
        response = "Получен некорректный ответ от модели."


# ============================================================================
# Пример 5: Telegram flood protection
# ============================================================================

async def example_flood_protection():
    """Пример обработки Telegram flood protection."""
    
    from telegram_actions import send_telegram_message
    
    state = get_state()
    
    # Проверка перед отправкой
    if state.is_send_blocked():
        cooldown = state.get_send_cooldown_left()
        print(f"Отправка заблокирована на {cooldown} секунд")
        return
    
    try:
        result = await send_telegram_message(
            target="@username",
            text="Привет!",
            current_chat_id=None,
            allow_new_target=True,
        )
        print(result)
        
    except TelegramFloodError as e:
        logging.warning("Flood protection: %s", e)
        state.block_sending(e.seconds)
        print(f"Отправка заблокирована на {e.seconds} секунд")


# ============================================================================
# Пример 6: Проверка прав админа
# ============================================================================

async def example_admin_check(sender_id: int):
    """Пример проверки прав админа."""
    
    if sender_id != settings.admin_id:
        raise AdminOnlyError(
            f"Действие доступно только админу. Ваш ID: {sender_id}"
        )
    
    # Выполнить действие только для админа
    print("Админ подтвержден, выполняем действие")


# ============================================================================
# Пример 7: Батчинг сообщений
# ============================================================================

async def example_message_batching():
    """Пример батчинга входящих сообщений."""
    
    state = get_state()
    chat_id = 123456
    
    # Создать элемент батча
    from unittest.mock import Mock
    
    mock_message = Mock()
    mock_message.chat_id = chat_id
    mock_message.raw_text = "Привет!"
    
    item = PendingBatchItem(
        message=mock_message,
        prompt="Пользователь написал: Привет!",
        raw_text="Привет!",
        sender_id=789012,
        behavior_context="green: normal friendly tempo",
        should_answer=True,
        trigger_seen=True,
    )
    
    # Добавить в батч
    batch = state.pending_batches.setdefault(chat_id, [])
    batch.append(item)
    
    # Ограничить размер батча
    max_batch = 25
    if len(batch) > max_batch:
        batch = batch[-max_batch:]
        state.pending_batches[chat_id] = batch
    
    print(f"В батче {len(batch)} сообщений")


# ============================================================================
# Пример 8: TTL кэш
# ============================================================================

def example_ttl_cache():
    """Пример использования TTL кэша."""
    
    from agent_state import TTLCache
    
    # Создать кэш с TTL 60 секунд
    cache = TTLCache(ttl_seconds=60.0)
    
    # Установить значение
    cache.set("user_123", {"name": "John", "score": 100})
    
    # Получить значение
    user_data = cache.get("user_123")
    print(f"User data: {user_data}")
    
    # Получить с дефолтом
    other_user = cache.get("user_456", {"name": "Unknown"})
    print(f"Other user: {other_user}")
    
    # Удалить и получить
    removed = cache.pop("user_123")
    print(f"Removed: {removed}")
    
    # Очистить весь кэш
    cache.clear()


# ============================================================================
# Пример 9: Периодическая очистка
# ============================================================================

async def cleanup_loop():
    """Периодическая очистка старых данных."""
    
    state = get_state()
    
    while True:
        try:
            # Ждем 10 минут
            await asyncio.sleep(600)
            
            # Очищаем старые данные (старше 1 часа)
            state.cleanup_old_data(max_age_seconds=3600)
            
            logging.info("Cleaned up old state data")
            
        except Exception as e:
            logging.exception("Cleanup failed: %s", e)


# ============================================================================
# Пример 10: Мониторинг метрик
# ============================================================================

async def example_monitoring():
    """Пример мониторинга метрик."""
    
    db = DatabaseImproved(settings.database_url)
    await db.init()
    
    state = get_state()
    
    # Метрики БД
    db_metrics = db.metrics
    print(f"DB queries total: {db_metrics['queries_total']}")
    print(f"DB queries failed: {db_metrics['queries_failed']}")
    print(f"DB connection errors: {db_metrics['connection_errors']}")
    
    if db_metrics['last_error']:
        print(f"Last DB error: {db_metrics['last_error']}")
    
    # Метрики состояния
    print(f"Active chats: {len(state.pending_batches)}")
    print(f"Typing users: {sum(len(users) for users in state.typing_users.values())}")
    print(f"Send blocked: {state.is_send_blocked()}")
    print(f"Model blocked: {state.is_model_blocked()}")
    
    # Health check
    is_healthy = await db.health_check()
    print(f"DB healthy: {is_healthy}")


# ============================================================================
# Пример 11: Интеграция в основной код
# ============================================================================

async def example_integration():
    """Пример интеграции новых возможностей в основной код."""
    
    # Инициализация
    state = get_state()
    db = DatabaseImproved(settings.database_url)
    
    try:
        await db.init()
    except DatabaseConnectionError as e:
        logging.error("Cannot initialize database: %s", e)
        # Решить: продолжать без БД или завершить
    
    # Запустить периодическую очистку
    cleanup_task = asyncio.create_task(cleanup_loop())
    
    try:
        # Основная логика агента
        # ...
        pass
        
    finally:
        # Graceful shutdown
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        
        # Финальная очистка
        state.cleanup_old_data(max_age_seconds=0)
        logging.info("Agent shutdown complete")


# ============================================================================
# Запуск примеров
# ============================================================================

async def main():
    """Запустить все примеры."""
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s"
    )
    
    print("=" * 80)
    print("Пример 1: AgentState")
    print("=" * 80)
    await example_agent_state()
    
    print("\n" + "=" * 80)
    print("Пример 2: Database errors")
    print("=" * 80)
    await example_database_errors()
    
    print("\n" + "=" * 80)
    print("Пример 3: Constants")
    print("=" * 80)
    example_constants()
    
    print("\n" + "=" * 80)
    print("Пример 8: TTL Cache")
    print("=" * 80)
    example_ttl_cache()
    
    print("\n" + "=" * 80)
    print("Пример 10: Monitoring")
    print("=" * 80)
    await example_monitoring()


if __name__ == "__main__":
    asyncio.run(main())
