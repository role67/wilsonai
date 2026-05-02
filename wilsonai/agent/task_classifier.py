"""
Классификатор задач для определения типа запроса.
Определяет какую модель использовать для ответа.
"""

import re
from enum import Enum
from typing import Optional


class TaskType(Enum):
    """Типы задач для AI моделей."""
    DIALOG = "dialog"  # Обычный диалог
    IMAGE_ANALYSIS = "image_analysis"  # Анализ изображений
    COMPLEX_REASONING = "complex_reasoning"  # Сложные рассуждения
    QUICK_ANSWER = "quick_answer"  # Быстрые ответы
    MODERATION = "moderation"  # Модерация контента
    CODE_GENERATION = "code_generation"  # Генерация кода
    SUMMARY = "summary"  # Краткие саммари
    CREATIVE = "creative"  # Творческие задачи
    TRANSLATION = "translation"  # Перевод
    LONG_CONTEXT = "long_context"  # Длинный контекст


class TaskClassifier:
    """Классификатор типа задачи по тексту запроса."""
    
    # Паттерны для определения типа задачи
    PATTERNS = {
        TaskType.IMAGE_ANALYSIS: [
            r"\b(что на (фото|картинке|изображении))",
            r"\b(опиши (фото|картинку|изображение))",
            r"\b(посмотри на (фото|картинку|изображение))",
            r"\b(что (видишь|видно))",
            r"\b(analyze|describe|what.*image)",
        ],
        TaskType.CODE_GENERATION: [
            r"\b(напиши код|write code|код на)",
            r"\b(функци[юя]|function|class)",
            r"\b(python|javascript|java|c\+\+|rust)",
            r"```",
            r"\b(debug|отладь|исправь код)",
        ],
        TaskType.SUMMARY: [
            r"\b(кратко|вкратце|резюме|summary)",
            r"\b(перескажи|пересказ|summarize)",
            r"\b(главное|основное|суть)",
            r"\b(tl;dr|tldr)",
        ],
        TaskType.CREATIVE: [
            r"\b(придумай|напиши (историю|рассказ|стих))",
            r"\b(расскажи (шутку|анекдот))",
            r"\b(сочини|create story|write poem)",
            r"\b(фантаз|креатив|творч)",
        ],
        TaskType.TRANSLATION: [
            r"\b(переведи|translate|перевод)",
            r"\b(на (английский|русский|немецкий|французский))",
            r"\b(с (английского|русского|немецкого|французского))",
        ],
        TaskType.MODERATION: [
            r"\b(спам|реклама|оскорбл|мат|токсич)",
            r"\b(нарушение|правила чата)",
            r"\b(забань|мут|варн)",
        ],
        TaskType.COMPLEX_REASONING: [
            r"\b(объясни|explain|почему|why)",
            r"\b(анализ|analysis|разбор)",
            r"\b(сравни|compare|отличие)",
            r"\b(как работает|how does)",
            r"\b(философ|этик|мораль)",
        ],
    }
    
    # Ключевые слова для быстрых ответов
    QUICK_KEYWORDS = {
        "привет", "пока", "спасибо", "да", "нет", "ок", "хорошо",
        "hi", "hello", "bye", "thanks", "yes", "no", "ok",
    }
    
    @classmethod
    def classify(
        cls,
        text: str,
        has_media: bool = False,
        message_length: int = 0,
        context_length: int = 0,
    ) -> TaskType:
        """
        Классифицировать тип задачи.
        
        Args:
            text: Текст запроса
            has_media: Есть ли медиа (фото/видео)
            message_length: Длина сообщения в словах
            context_length: Длина контекста в словах
        
        Returns:
            Тип задачи
        """
        if not text:
            return TaskType.QUICK_ANSWER
        
        lowered = text.lower()
        
        # Если есть медиа - анализ изображений
        if has_media:
            return TaskType.IMAGE_ANALYSIS
        
        # Проверка паттернов
        for task_type, patterns in cls.PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, lowered, re.IGNORECASE):
                    return task_type
        
        # Длинный контекст
        if context_length > 1000 or message_length > 500:
            return TaskType.LONG_CONTEXT
        
        # Быстрые ответы (короткие сообщения)
        words = text.split()
        if len(words) <= 5:
            # Проверка на ключевые слова
            if any(word in lowered for word in cls.QUICK_KEYWORDS):
                return TaskType.QUICK_ANSWER
        
        # Короткие сообщения (до 50 слов) - быстрый ответ
        if len(words) < 50:
            return TaskType.QUICK_ANSWER
        
        # Средние сообщения (50-200 слов) - обычный диалог
        if len(words) < 200:
            return TaskType.DIALOG
        
        # Длинные сообщения - сложные рассуждения
        return TaskType.COMPLEX_REASONING
    
    @classmethod
    def estimate_complexity(cls, text: str) -> str:
        """
        Оценить сложность запроса.
        
        Returns:
            "simple", "medium", "complex"
        """
        words = text.split()
        word_count = len(words)
        
        # Простые запросы
        if word_count < 10:
            return "simple"
        
        # Сложные индикаторы
        complex_indicators = [
            r"\b(почему|why|как работает|explain)",
            r"\b(анализ|analysis|сравни)",
            r"\b(философ|этик|мораль)",
        ]
        
        lowered = text.lower()
        if any(re.search(pattern, lowered) for pattern in complex_indicators):
            return "complex"
        
        # Средние запросы
        if word_count < 100:
            return "medium"
        
        return "complex"
