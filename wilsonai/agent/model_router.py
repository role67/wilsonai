"""
Роутер моделей - выбор оптимальной модели для задачи.
Автоматическое переключение и fallback при ошибках.
"""

import logging
from typing import Any, Optional
from dataclasses import dataclass

from task_classifier import TaskClassifier, TaskType
from model_providers import (
    GroqProvider,
    GeminiProvider,
    RekaProvider,
    KiroProvider,
    ModelResponse,
)
from exceptions import (
    ModelConnectionError,
    ModelRateLimitError,
    ModelTimeoutError,
)

logger = logging.getLogger("telegram-agent.router")


@dataclass
class ModelConfig:
    """Конфигурация модели."""
    provider: str
    model: str
    priority: int  # 1 = primary, 2 = secondary, 3 = fallback


class ModelRouter:
    """Роутер для выбора и переключения моделей."""
    
    # Конфигурация моделей для каждого типа задачи
    TASK_MODELS = {
        TaskType.DIALOG: [
            ModelConfig("gemini", "gemini/gemini-2.5-flash", 1),
            ModelConfig("kiro", "kr/claude-haiku-4.5", 2),
            ModelConfig("groq", "groq/llama-3.3-70b-versatile", 3),
        ],
        TaskType.IMAGE_ANALYSIS: [
            ModelConfig("reka", "reka-ai", 1),
            ModelConfig("gemini", "gemini/gemini-2.5-flash", 2),
        ],
        TaskType.COMPLEX_REASONING: [
            ModelConfig("kiro", "kr/claude-sonnet-4.5", 1),
            ModelConfig("groq", "groq/openai/gpt-oss-120b", 2),
        ],
        TaskType.QUICK_ANSWER: [
            ModelConfig("kiro", "kr/claude-haiku-4.5", 1),
            ModelConfig("gemini", "gemini/gemini-2.5-flash-lite", 2),
        ],
        TaskType.MODERATION: [
            ModelConfig("groq", "groq/qwen/qwen3-32b", 1),
            ModelConfig("gemini", "gemini/gemma-3-12b-it", 2),
        ],
        TaskType.CODE_GENERATION: [
            ModelConfig("groq", "groq/llama-3.3-70b-versatile", 1),
            ModelConfig("gemini", "gemini/gemma-4-31b-it", 2),
        ],
        TaskType.SUMMARY: [
            ModelConfig("gemini", "gemini/gemma-3n-e4b-it", 1),
            ModelConfig("gemini", "gemini/gemma-3-1b-it", 2),
        ],
        TaskType.CREATIVE: [
            ModelConfig("gemini", "gemini/gemma-4-26b-a4b-it", 1),
            ModelConfig("gemini", "gemini/gemma-3-27b-it", 2),
        ],
        TaskType.TRANSLATION: [
            ModelConfig("gemini", "gemini/gemini-3-flash-preview", 1),
            ModelConfig("gemini", "gemini/gemini-3.1-flash-lite-preview", 2),
        ],
        TaskType.LONG_CONTEXT: [
            ModelConfig("gemini", "gemini/gemini-flash-latest", 1),
            ModelConfig("gemini", "gemini/gemma-3n-e2b-it", 2),
        ],
    }
    
    def __init__(
        self,
        groq_api_key: str,
        gemini_api_key: str,
        reka_api_key: str,
        kiro_api_key: str,
        timeout: float = 90.0,
        proxy: Optional[str] = None,
    ):
        """Инициализация роутера с провайдерами."""
        self.providers = {
            "groq": GroqProvider(groq_api_key, timeout, proxy),
            "gemini": GeminiProvider(gemini_api_key, timeout, proxy),
            "reka": RekaProvider(reka_api_key, timeout, proxy),
            "kiro": KiroProvider(kiro_api_key, timeout, proxy),
        }
        
        self.classifier = TaskClassifier()
        
        # Статистика использования
        self.stats = {
            "total_requests": 0,
            "by_task_type": {},
            "by_provider": {},
            "fallback_used": 0,
        }
    
    async def generate(
        self,
        text: str,
        messages: list[dict[str, str]],
        has_media: bool = False,
        image_part: Optional[dict[str, Any]] = None,
        temperature: float = 0.7,
        max_tokens: int = 1600,
        force_task_type: Optional[TaskType] = None,
    ) -> ModelResponse:
        """
        Генерация ответа с автоматическим выбором модели.
        
        Args:
            text: Текст запроса
            messages: История сообщений
            has_media: Есть ли медиа
            image_part: Данные изображения
            temperature: Температура генерации
            max_tokens: Максимум токенов
            force_task_type: Принудительный тип задачи
        
        Returns:
            Ответ от модели
        """
        self.stats["total_requests"] += 1
        
        # Определить тип задачи
        if force_task_type:
            task_type = force_task_type
        else:
            word_count = len(text.split())
            context_length = sum(len(m["content"].split()) for m in messages)
            task_type = self.classifier.classify(
                text=text,
                has_media=has_media,
                message_length=word_count,
                context_length=context_length,
            )
        
        # Обновить статистику
        self.stats["by_task_type"][task_type.value] = \
            self.stats["by_task_type"].get(task_type.value, 0) + 1
        
        logger.info(f"Task classified as: {task_type.value}")
        
        # Получить список моделей для этой задачи
        model_configs = self.TASK_MODELS.get(task_type, self.TASK_MODELS[TaskType.DIALOG])
        
        # Попробовать модели по приоритету
        last_error = None
        for config in sorted(model_configs, key=lambda x: x.priority):
            try:
                provider = self.providers[config.provider]
                
                logger.info(f"Trying {config.provider}/{config.model}")
                
                # Вызвать провайдер
                if config.provider == "reka" and image_part:
                    response = await provider.generate(
                        model=config.model,
                        messages=messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        image_url=image_part.get("url"),
                    )
                elif config.provider == "gemini" and image_part:
                    response = await provider.generate(
                        model=config.model,
                        messages=messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        image_part=image_part,
                    )
                else:
                    response = await provider.generate(
                        model=config.model,
                        messages=messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
                
                # Успех
                self.stats["by_provider"][config.provider] = \
                    self.stats["by_provider"].get(config.provider, 0) + 1
                
                if config.priority > 1:
                    self.stats["fallback_used"] += 1
                    logger.info(f"Fallback used: {config.provider}/{config.model}")
                
                return response
                
            except ModelRateLimitError as e:
                logger.warning(f"Rate limit on {config.provider}/{config.model}: {e}")
                last_error = e
                # Попробовать следующую модель
                continue
                
            except ModelTimeoutError as e:
                logger.warning(f"Timeout on {config.provider}/{config.model}: {e}")
                last_error = e
                # Попробовать следующую модель
                continue
                
            except ModelConnectionError as e:
                logger.warning(f"Connection error on {config.provider}/{config.model}: {e}")
                last_error = e
                # Попробовать следующую модель
                continue
                
            except Exception as e:
                logger.error(f"Unexpected error on {config.provider}/{config.model}: {e}")
                last_error = e
                # Попробовать следующую модель
                continue
        
        # Все модели провалились
        logger.error(f"All models failed for task {task_type.value}")
        raise ModelConnectionError(f"All models unavailable: {last_error}")
    
    def get_stats(self) -> dict[str, Any]:
        """Получить статистику использования."""
        return {
            **self.stats,
            "provider_metrics": {
                name: provider.metrics
                for name, provider in self.providers.items()
            },
        }
