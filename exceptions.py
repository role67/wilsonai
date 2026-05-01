"""
Кастомные исключения для Telegram агента.
Разделение типов ошибок для лучшей обработки и мониторинга.
"""


class TelegramAgentError(Exception):
    """Базовое исключение для всех ошибок агента."""
    pass


# Ошибки модели
class ModelError(TelegramAgentError):
    """Базовая ошибка для проблем с AI моделью."""
    pass


class ModelConnectionError(ModelError):
    """Ошибка подключения к AI модели."""
    pass


class ModelRateLimitError(ModelError):
    """Превышен лимит запросов к модели."""
    pass


class ModelTimeoutError(ModelError):
    """Таймаут при запросе к модели."""
    pass


class ModelInvalidResponseError(ModelError):
    """Модель вернула невалидный ответ."""
    pass


# Ошибки базы данных
class DatabaseError(TelegramAgentError):
    """Базовая ошибка для проблем с БД."""
    pass


class DatabaseConnectionError(DatabaseError):
    """Ошибка подключения к БД."""
    pass


class DatabaseQueryError(DatabaseError):
    """Ошибка выполнения запроса к БД."""
    pass


class DatabaseIntegrityError(DatabaseError):
    """Нарушение целостности данных в БД."""
    pass


# Ошибки Telegram
class TelegramError(TelegramAgentError):
    """Базовая ошибка для проблем с Telegram API."""
    pass


class TelegramFloodError(TelegramError):
    """Telegram flood protection активирована."""
    def __init__(self, seconds: int, message: str = ""):
        self.seconds = seconds
        super().__init__(message or f"Flood wait: {seconds} seconds")


class TelegramPeerFloodError(TelegramError):
    """Слишком много исходящих сообщений."""
    pass


class TelegramAuthError(TelegramError):
    """Ошибка авторизации в Telegram."""
    pass


# Ошибки валидации
class ValidationError(TelegramAgentError):
    """Ошибка валидации входных данных."""
    pass


class InvalidTargetError(ValidationError):
    """Невалидный target для действия."""
    pass


class InvalidCommandError(ValidationError):
    """Невалидная команда."""
    pass


# Ошибки прав доступа
class PermissionError(TelegramAgentError):
    """Недостаточно прав для выполнения действия."""
    pass


class AdminOnlyError(PermissionError):
    """Действие доступно только админу."""
    pass


# Ошибки конфигурации
class ConfigurationError(TelegramAgentError):
    """Ошибка конфигурации."""
    pass


class MissingConfigError(ConfigurationError):
    """Отсутствует обязательный параметр конфигурации."""
    pass
