"""
Unit тесты для критической логики Telegram агента.
Покрывают автомодерацию, парсинг команд, управление состоянием.
"""

import pytest
import time
from unittest.mock import Mock, AsyncMock, patch

from agent_state import AgentState, TTLCache, PendingBatchItem
from behavior import update_behavior_profile, behavior_prompt
from telegram_actions import (
    normalize_public_target,
    same_target,
    message_count_from_text,
    quoted_texts,
    split_requested_messages,
)


class TestAgentState:
    """Тесты для управления состоянием агента."""
    
    def test_state_initialization(self):
        """Тест инициализации состояния."""
        state = AgentState()
        assert state.send_blocked_until == 0.0
        assert state.model_backoff_until == 0.0
        assert len(state.locks) == 0
        assert len(state.pending_batches) == 0
    
    def test_send_blocking(self):
        """Тест блокировки отправки сообщений."""
        state = AgentState()
        
        # Изначально не заблокировано
        assert not state.is_send_blocked()
        assert state.get_send_cooldown_left() == 0
        
        # Блокируем на 10 секунд
        state.block_sending(10)
        assert state.is_send_blocked()
        assert state.get_send_cooldown_left() > 0
    
    def test_typing_registration(self):
        """Тест регистрации событий печати."""
        state = AgentState()
        chat_id = 123
        user_id = 456
        
        # Регистрируем печать
        state.register_typing(chat_id, user_id)
        assert state.is_anyone_typing(chat_id)
        
        # Очищаем устаревшие
        state.cleanup_typing(chat_id, idle_seconds=0.0)
        assert not state.is_anyone_typing(chat_id)
    
    def test_group_signal_burst_detection(self):
        """Тест обнаружения burst сигналов в группе."""
        state = AgentState()
        chat_id = 123
        user_id = 456
        
        # Регистрируем несколько сигналов
        count1 = state.register_group_signal(chat_id, user_id)
        count2 = state.register_group_signal(chat_id, user_id)
        count3 = state.register_group_signal(chat_id, user_id)
        
        assert count1 == 1
        assert count2 == 2
        assert count3 == 3
    
    def test_reaction_cooldown(self):
        """Тест cooldown для реакций."""
        state = AgentState()
        chat_id = 123
        
        # Можем поставить реакцию
        assert state.can_react(chat_id, cooldown_seconds=1.0)
        
        # Ставим реакцию
        state.mark_reaction(chat_id)
        
        # Сразу нельзя
        assert not state.can_react(chat_id, cooldown_seconds=1.0)
        
        # Через секунду можно
        time.sleep(1.1)
        assert state.can_react(chat_id, cooldown_seconds=1.0)
    
    def test_cleanup_old_data(self):
        """Тест очистки старых данных."""
        state = AgentState()
        
        # Добавляем данные
        state.register_group_signal(123, 456)
        state.mark_reaction(789)
        
        # Очищаем с нулевым max_age
        state.cleanup_old_data(max_age_seconds=0.0)
        
        # Данные должны быть очищены
        assert len(state.recent_group_signals) == 0
        assert len(state.last_reaction_at) == 0
    
    def test_state_reset(self):
        """Тест полного сброса состояния."""
        state = AgentState()
        
        # Добавляем данные
        state.block_sending(10)
        state.register_typing(123, 456)
        state.mark_reaction(789)
        
        # Сбрасываем
        state.reset()
        
        # Все должно быть очищено
        assert not state.is_send_blocked()
        assert not state.is_anyone_typing(123)
        assert len(state.last_reaction_at) == 0


class TestTTLCache:
    """Тесты для TTL кэша."""
    
    def test_cache_basic_operations(self):
        """Тест базовых операций кэша."""
        cache = TTLCache(ttl_seconds=1.0)
        
        # Установка и получение
        cache.set("key1", "value1")
        assert cache.get("key1") == "value1"
        assert cache.get("key2", "default") == "default"
    
    def test_cache_expiration(self):
        """Тест истечения TTL."""
        cache = TTLCache(ttl_seconds=0.5)
        
        cache.set("key1", "value1")
        assert cache.get("key1") == "value1"
        
        # Ждем истечения TTL
        time.sleep(0.6)
        assert cache.get("key1") is None
    
    def test_cache_pop(self):
        """Тест удаления из кэша."""
        cache = TTLCache(ttl_seconds=10.0)
        
        cache.set("key1", "value1")
        value = cache.pop("key1")
        assert value == "value1"
        assert cache.get("key1") is None
    
    def test_cache_clear(self):
        """Тест очистки кэша."""
        cache = TTLCache(ttl_seconds=10.0)
        
        cache.set("key1", "value1")
        cache.set("key2", "value2")
        cache.clear()
        
        assert cache.get("key1") is None
        assert cache.get("key2") is None


class TestBehaviorProfiles:
    """Тесты для поведенческих профилей."""
    
    def test_profile_initialization(self):
        """Тест инициализации профиля."""
        profile = update_behavior_profile(12345, "привет как дела")
        
        assert profile is not None
        assert profile.user_id == 12345
        assert profile.flag == "green"
        assert profile.score == 0
    
    def test_profile_insult_detection(self):
        """Тест обнаружения оскорблений."""
        profile = update_behavior_profile(12345, "ты долбоеб")
        
        assert profile is not None
        assert profile.score > 0
        assert profile.flag in ("yellow", "red")
        assert "insults" in profile.last_reason or "toxic" in profile.last_reason
    
    def test_profile_hard_line_detection(self):
        """Тест обнаружения жестких границ."""
        profile = update_behavior_profile(12345, "иди нахуй")
        
        assert profile is not None
        assert profile.score >= 3
        assert profile.flag == "red"
        assert "hard boundaries" in profile.last_reason
    
    def test_profile_improvement(self):
        """Тест улучшения профиля."""
        user_id = 12345
        
        # Сначала ухудшаем
        profile = update_behavior_profile(user_id, "ты идиот")
        assert profile.score > 0
        
        # Потом улучшаем
        for _ in range(10):
            profile = update_behavior_profile(user_id, "спасибо, все хорошо")
        
        assert profile.score < 2
        assert profile.flag in ("green", "yellow")
    
    def test_behavior_prompt_generation(self):
        """Тест генерации промпта для поведения."""
        profile = update_behavior_profile(12345, "привет")
        prompt = behavior_prompt(profile)
        
        assert "green" in prompt.lower()
        assert "behavior" in prompt.lower()
    
    def test_behavior_prompt_red_flag(self):
        """Тест промпта для красного флага."""
        profile = update_behavior_profile(12345, "сдохни")
        prompt = behavior_prompt(profile)
        
        assert "red" in prompt.lower()
        assert "shorten" in prompt.lower() or "briefly" in prompt.lower()


class TestTelegramActions:
    """Тесты для парсинга команд и действий."""
    
    def test_normalize_public_target(self):
        """Тест нормализации публичных target."""
        assert normalize_public_target("@username") == "username"
        assert normalize_public_target("https://t.me/username") == "username"
        assert normalize_public_target("t.me/username") == "username"
        assert normalize_public_target("https://t.me/username?start=123") == "username"
    
    def test_same_target(self):
        """Тест сравнения target."""
        assert same_target("@username", "username")
        assert same_target("https://t.me/username", "t.me/username")
        assert same_target("@Username", "@username")
        assert not same_target("user1", "user2")
    
    def test_message_count_from_text(self):
        """Тест извлечения количества сообщений."""
        assert message_count_from_text("напиши 3 сообщения") == 3
        assert message_count_from_text("отправь два сообщения") == 2
        assert message_count_from_text("скинь одно сообщение") == 1
        assert message_count_from_text("напиши пять смс") == 5
        assert message_count_from_text("несколько сообщений") == 2
        assert message_count_from_text("просто текст") is None
    
    def test_quoted_texts(self):
        """Тест извлечения текста в кавычках."""
        texts = quoted_texts('напиши "привет" и "пока"')
        assert len(texts) == 2
        assert "привет" in texts
        assert "пока" in texts
        
        texts = quoted_texts("напиши «первое» и «второе»")
        assert len(texts) == 2
    
    def test_split_requested_messages(self):
        """Тест разделения запрошенных сообщений."""
        # С кавычками
        messages = split_requested_messages('напиши "привет" и "пока"', 2)
        assert len(messages) == 2
        assert "привет" in messages
        
        # С маркером
        messages = split_requested_messages("сообщения: первое; второе; третье", 3)
        assert len(messages) == 3
        
        # Без явного разделения
        messages = split_requested_messages("напиши что-то", 2)
        assert len(messages) == 2


class TestAutomoderation:
    """Тесты для логики автомодерации."""
    
    def test_spam_score_calculation(self):
        """Тест подсчета спам-скора."""
        from telegram_agent import _spam_score
        
        # Много ссылок
        text = "https://example.com https://test.com t.me/spam"
        assert _spam_score(text) >= 3
        
        # Много упоминаний
        text = "@user1 @user2 @user3 @user4"
        assert _spam_score(text) >= 3
        
        # Повторяющиеся символы
        text = "ааааааааааааа"
        assert _spam_score(text) >= 1
        
        # Чистый текст
        text = "привет как дела"
        assert _spam_score(text) == 0
    
    def test_abuse_score_calculation(self):
        """Тест подсчета оскорблений."""
        from telegram_agent import _abuse_score
        
        # Много мата
        text = "ты долбоеб и пидор"
        assert _abuse_score(text) >= 2
        
        # Чистый текст
        text = "привет как дела"
        assert _abuse_score(text) == 0
    
    def test_caps_words_count(self):
        """Тест подсчета слов капсом."""
        from telegram_agent import _count_caps_words
        
        text = "ЭТО ОЧЕНЬ ВАЖНО ПОСЛУШАЙ МЕНЯ"
        assert _count_caps_words(text) >= 4
        
        text = "Это нормальный текст"
        assert _count_caps_words(text) == 0


class TestMessageParsing:
    """Тесты для парсинга сообщений."""
    
    def test_strip_trigger_name(self):
        """Тест удаления триггерных имен."""
        from telegram_agent import strip_trigger_name
        
        found, cleaned = strip_trigger_name("вилсон привет как дела")
        assert found
        assert "привет как дела" in cleaned
        
        found, cleaned = strip_trigger_name("привет вильсон что делаешь")
        assert found
        
        found, cleaned = strip_trigger_name("просто текст")
        assert not found
    
    def test_signal_spammy(self):
        """Тест обнаружения спамных сигналов."""
        from telegram_agent import _signal_spammy
        
        # Много упоминаний
        assert _signal_spammy("@user1 @user2 @user3 @user4")
        
        # Много ссылок
        assert _signal_spammy("https://test.com t.me/spam")
        
        # Много восклицательных знаков
        assert _signal_spammy("привет!!!!!!!!")
        
        # Много упоминаний бота
        assert _signal_spammy("wilson wilson wilson")
        
        # Нормальный текст
        assert not _signal_spammy("привет как дела")


class TestMemoryCategories:
    """Тесты для категоризации памяти."""
    
    def test_detect_memory_category(self):
        """Тест определения категории памяти."""
        from telegram_agent import _detect_memory_category
        
        assert _detect_memory_category("забань этого спамера") == "moderation"
        assert _detect_memory_category("пиши без запятых") == "style"
        assert _detect_memory_category("ты веселый персонаж") == "persona"
        assert _detect_memory_category("правило чата: не спамить") == "chat_rules"
        assert _detect_memory_category("ошибка api модели") == "ops"
        assert _detect_memory_category("просто заметка") == "other"


# Фикстуры для тестов
@pytest.fixture
def mock_message():
    """Мок Telegram сообщения."""
    msg = Mock()
    msg.chat_id = 123456
    msg.id = 1
    msg.raw_text = "тестовое сообщение"
    msg.is_private = True
    msg.is_group = False
    msg.is_channel = False
    msg.out = False
    msg.media = None
    msg.voice = None
    msg.mentioned = False
    msg.get_sender = AsyncMock(return_value=Mock(id=789, username="testuser"))
    msg.get_chat = AsyncMock(return_value=Mock(id=123456, title="Test Chat"))
    msg.reply = AsyncMock()
    return msg


@pytest.fixture
def agent_state():
    """Чистое состояние агента для тестов."""
    state = AgentState()
    yield state
    state.reset()


# Запуск тестов
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
