"""
Управление состоянием Telegram агента.
Инкапсулирует все глобальные переменные в класс для лучшей тестируемости и управления.
"""

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from telethon.tl.custom import Message


@dataclass
class PendingBatchItem:
    """Элемент батча сообщений для обработки."""
    message: Message
    prompt: str
    raw_text: str
    sender_id: int | None
    behavior_context: str = ""
    should_answer: bool = True
    trigger_seen: bool = True


class TTLCache:
    """Простой кэш с TTL для предотвращения memory leaks."""
    
    def __init__(self, ttl_seconds: float = 300.0):
        self._cache: Dict[Any, Tuple[Any, float]] = {}
        self._ttl = ttl_seconds
    
    def get(self, key: Any, default: Any = None) -> Any:
        """Получить значение из кэша."""
        self._cleanup()
        if key in self._cache:
            value, _ = self._cache[key]
            return value
        return default
    
    def set(self, key: Any, value: Any) -> None:
        """Установить значение в кэш."""
        self._cache[key] = (value, time.monotonic() + self._ttl)
    
    def pop(self, key: Any, default: Any = None) -> Any:
        """Удалить и вернуть значение."""
        self._cleanup()
        if key in self._cache:
            value, _ = self._cache.pop(key)
            return value
        return default
    
    def _cleanup(self) -> None:
        """Удалить устаревшие записи."""
        now = time.monotonic()
        expired = [k for k, (_, expires_at) in self._cache.items() if now > expires_at]
        for key in expired:
            self._cache.pop(key, None)
    
    def clear(self) -> None:
        """Очистить весь кэш."""
        self._cache.clear()


class AgentState:
    """
    Централизованное управление состоянием агента.
    Заменяет глобальные переменные для лучшей тестируемости.
    """
    
    def __init__(self):
        # Блокировки для синхронизации
        self.locks: Dict[int, asyncio.Lock] = {}
        
        # Батчинг сообщений
        self.batch_tasks: Dict[int, asyncio.Task[None]] = {}
        self.pending_batches: Dict[int, List[PendingBatchItem]] = {}
        
        # Flood protection
        self.send_blocked_until: float = 0.0
        self.model_backoff_until: float = 0.0
        
        # Typing awareness
        self.typing_users: Dict[int, Dict[int, float]] = defaultdict(dict)
        
        # Voice replies
        self.recent_voice_answers: TTLCache = TTLCache(ttl_seconds=60.0)
        
        # Group replies
        self.last_group_reply_at: Dict[int, float] = {}
        self.recent_group_signals: Dict[Tuple[int, int], List[float]] = defaultdict(list)
        
        # Reactions
        self.last_reaction_at: Dict[int, float] = {}
        
        # Automod state
        self.automod_state: Dict[Tuple[int, int], Dict[str, Any]] = defaultdict(
            lambda: {"warns": 0, "last_msgs": [], "last_action_at": 0.0}
        )
        
        # Info lookup cooldown
        self.last_info_lookup_at: float = 0.0
    
    def get_lock(self, chat_id: int) -> asyncio.Lock:
        """Получить или создать блокировку для чата."""
        if chat_id not in self.locks:
            self.locks[chat_id] = asyncio.Lock()
        return self.locks[chat_id]
    
    def is_send_blocked(self) -> bool:
        """Проверить, заблокирована ли отправка сообщений."""
        return time.monotonic() < self.send_blocked_until
    
    def get_send_cooldown_left(self) -> int:
        """Получить оставшееся время блокировки отправки."""
        return max(0, int(self.send_blocked_until - time.monotonic()))
    
    def block_sending(self, seconds: int) -> None:
        """Заблокировать отправку сообщений на N секунд."""
        self.send_blocked_until = time.monotonic() + seconds
    
    def is_model_blocked(self) -> bool:
        """Проверить, заблокированы ли запросы к модели."""
        return time.monotonic() < self.model_backoff_until
    
    def block_model(self, seconds: float) -> None:
        """Заблокировать запросы к модели на N секунд."""
        self.model_backoff_until = time.monotonic() + seconds
    
    def register_typing(self, chat_id: int, user_id: int) -> None:
        """Зарегистрировать событие печати пользователя."""
        self.typing_users[chat_id][user_id] = time.monotonic()
    
    def cleanup_typing(self, chat_id: int, idle_seconds: float) -> None:
        """Очистить устаревшие события печати."""
        if chat_id not in self.typing_users:
            return
        
        now = time.monotonic()
        active = self.typing_users[chat_id]
        expired = [uid for uid, last_seen in active.items() if now - last_seen > idle_seconds]
        
        for user_id in expired:
            active.pop(user_id, None)
        
        if not active:
            self.typing_users.pop(chat_id, None)
    
    def is_anyone_typing(self, chat_id: int) -> bool:
        """Проверить, печатает ли кто-то в чате."""
        return chat_id in self.typing_users and bool(self.typing_users[chat_id])
    
    def register_group_signal(self, chat_id: int, sender_id: int, window_seconds: float = 25.0) -> int:
        """
        Зарегистрировать сигнал от пользователя в группе.
        Возвращает количество сигналов в окне.
        """
        key = (chat_id, sender_id)
        now = time.monotonic()
        
        # Добавить новый сигнал
        self.recent_group_signals[key].append(now)
        
        # Очистить старые сигналы
        self.recent_group_signals[key] = [
            t for t in self.recent_group_signals[key] 
            if now - t <= window_seconds
        ]
        
        return len(self.recent_group_signals[key])
    
    def can_react(self, chat_id: int, cooldown_seconds: float = 45.0) -> bool:
        """Проверить, можно ли поставить реакцию в чате."""
        last_reaction = self.last_reaction_at.get(chat_id, 0.0)
        return time.monotonic() - last_reaction >= cooldown_seconds
    
    def mark_reaction(self, chat_id: int) -> None:
        """Отметить, что реакция была поставлена."""
        self.last_reaction_at[chat_id] = time.monotonic()
    
    def can_info_lookup(self, cooldown_seconds: float = 20.0) -> bool:
        """Проверить, можно ли выполнить /info запрос."""
        return time.monotonic() - self.last_info_lookup_at >= cooldown_seconds
    
    def mark_info_lookup(self) -> None:
        """Отметить выполнение /info запроса."""
        self.last_info_lookup_at = time.monotonic()
    
    def cleanup_old_data(self, max_age_seconds: float = 3600.0) -> None:
        """
        Периодическая очистка старых данных для предотвращения memory leaks.
        Должна вызываться периодически (например, раз в 10 минут).
        """
        now = time.monotonic()
        
        # Очистка group signals
        for key in list(self.recent_group_signals.keys()):
            self.recent_group_signals[key] = [
                t for t in self.recent_group_signals[key] 
                if now - t <= 300.0  # 5 минут
            ]
            if not self.recent_group_signals[key]:
                del self.recent_group_signals[key]
        
        # Очистка automod state
        for key in list(self.automod_state.keys()):
            state = self.automod_state[key]
            if now - state.get("last_action_at", 0.0) > max_age_seconds:
                del self.automod_state[key]
        
        # Очистка старых реакций
        self.last_reaction_at = {
            chat_id: ts 
            for chat_id, ts in self.last_reaction_at.items() 
            if now - ts <= max_age_seconds
        }
        
        # Очистка старых group reply timestamps
        self.last_group_reply_at = {
            chat_id: ts 
            for chat_id, ts in self.last_group_reply_at.items() 
            if now - ts <= max_age_seconds
        }
        
        # Очистка TTL кэшей
        self.recent_voice_answers._cleanup()
    
    def reset(self) -> None:
        """Полный сброс состояния (для тестов)."""
        self.locks.clear()
        self.batch_tasks.clear()
        self.pending_batches.clear()
        self.send_blocked_until = 0.0
        self.model_backoff_until = 0.0
        self.typing_users.clear()
        self.recent_voice_answers.clear()
        self.last_group_reply_at.clear()
        self.recent_group_signals.clear()
        self.last_reaction_at.clear()
        self.automod_state.clear()
        self.last_info_lookup_at = 0.0


# Глобальный инстанс состояния (будет заменен на dependency injection)
_global_state: AgentState | None = None


def get_state() -> AgentState:
    """Получить глобальный инстанс состояния."""
    global _global_state
    if _global_state is None:
        _global_state = AgentState()
    return _global_state


def set_state(state: AgentState) -> None:
    """Установить глобальный инстанс состояния (для тестов)."""
    global _global_state
    _global_state = state
