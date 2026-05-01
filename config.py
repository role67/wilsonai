import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv




BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
MEMORY_DIR = DATA_DIR / "memory"
SENT_MESSAGES_PATH = DATA_DIR / "sent_messages.json"
PROMPTS_DIR = BASE_DIR / "prompts"
SYSTEM_PROMPT_PATH = PROMPTS_DIR / "system.txt"
DEFAULT_GOOGLE_FALLBACK_MODELS = ",".join(
    [
        "gemini-flash-latest",
        "gemini-3-flash-preview",
        "gemini-2.5-flash-lite",
        "gemini-flash-lite-latest",
        "gemini-3.1-flash-lite-preview",
        "gemini-2.0-flash",
        "gemini-2.0-flash-001",
        "gemini-2.0-flash-lite",
        "gemini-2.0-flash-lite-001",
        "gemma-4-31b-it",
        "gemma-4-26b-a4b-it",
        "gemma-3-27b-it",
        "gemma-3-12b-it",
        "gemma-3n-e4b-it",
        "gemma-3n-e2b-it",
        "gemma-3-4b-it",
        "gemma-3-1b-it",
        "gemini-2.5-pro",
        "gemini-pro-latest",
        "gemini-3-pro-preview",
        "gemini-3.1-pro-preview",
    ]
)


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    session_name: str
    admin_id: int
    google_api_key: str
    model_provider: str
    fallback_provider: str | None
    deepseek_api_key: str | None
    deepseek_api_url: str
    cerebras_api_key: str | None
    cerebras_api_url: str
    groq_api_key: str | None
    groq_api_url: str
    model: str
    fallback_models: tuple[str, ...]
    google_api_url: str
    google_proxy: str | None
    google_timeout: float
    google_retries: int
    max_history_messages: int
    temperature: float
    max_tokens: int
    respond_to_all_private: bool
    respond_to_all_groups: bool
    group_reply_cooldown_seconds: float
    trigger_prefix: str
    trigger_names: tuple[str, ...]
    keep_online: bool
    online_ping_interval: int
    message_batch_delay: float
    max_batch_messages: int
    max_passive_context_messages: int
    autonomous_actions_enabled: bool
    max_autonomous_messages: int
    send_message_delay: float
    peer_flood_cooldown: int
    database_url: str | None
    db_context_messages: int
    typing_wait_enabled: bool
    typing_idle_seconds: float
    typing_max_wait_seconds: float
    max_model_fallbacks: int


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_settings() -> Settings:
    load_dotenv(BASE_DIR / ".env")

    return Settings(
        api_id=int(os.environ["TELEGRAM_API_ID"]),
        api_hash=os.environ["TELEGRAM_API_HASH"],
        session_name=os.getenv("TELEGRAM_SESSION", "account"),
        admin_id=int(os.environ["ADMIN_ID"]),
        google_api_key=os.environ["GOOGLE_API_KEY"],
        model_provider=os.getenv("MODEL_PROVIDER", "google").strip().lower(),
        fallback_provider=(os.getenv("FALLBACK_PROVIDER") or "").strip().lower() or None,
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY") or None,
        deepseek_api_url=os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/v1"),
        cerebras_api_key=os.getenv("CEREBRAS_API_KEY") or None,
        cerebras_api_url=os.getenv("CEREBRAS_API_URL", "https://api.cerebras.ai/v1"),
        groq_api_key=os.getenv("GROQ_API_KEY") or None,
        groq_api_url=os.getenv("GROQ_API_URL", "https://api.groq.com/openai/v1"),
        model=os.getenv("GOOGLE_MODEL", "gemini-2.5-flash"),
        fallback_models=tuple(
            model.strip()
            for model in os.getenv("GOOGLE_FALLBACK_MODELS", DEFAULT_GOOGLE_FALLBACK_MODELS).split(",")
            if model.strip()
        ),
        google_api_url=os.getenv(
            "GOOGLE_API_URL", "https://generativelanguage.googleapis.com/v1beta"
        ),
        google_proxy=os.getenv("GOOGLE_PROXY") or None,
        google_timeout=float(os.getenv("GOOGLE_TIMEOUT", "90")),
        google_retries=int(os.getenv("GOOGLE_RETRIES", "3")),
        max_history_messages=int(os.getenv("MAX_HISTORY_MESSAGES", "30")),
        temperature=float(os.getenv("TEMPERATURE", "0.7")),
        max_tokens=int(os.getenv("MAX_TOKENS", "1600")),
        respond_to_all_private=env_bool("RESPOND_TO_ALL_PRIVATE", True),
        respond_to_all_groups=env_bool("RESPOND_TO_ALL_GROUPS", True),
        group_reply_cooldown_seconds=float(os.getenv("GROUP_REPLY_COOLDOWN_SECONDS", "4")),
        trigger_prefix=os.getenv("TRIGGER_PREFIX", ".ai"),
        trigger_names=tuple(
            name.strip().lower()
            for name in os.getenv("TRIGGER_NAMES", "вилсон,вильсон,вилка,wilson").split(",")
            if name.strip()
        ),
        keep_online=env_bool("KEEP_ONLINE", True),
        online_ping_interval=int(os.getenv("ONLINE_PING_INTERVAL", "45")),
        message_batch_delay=float(os.getenv("MESSAGE_BATCH_DELAY", "0.45")),
        max_batch_messages=int(os.getenv("MAX_BATCH_MESSAGES", "8")),
        max_passive_context_messages=int(os.getenv("MAX_PASSIVE_CONTEXT_MESSAGES", "40")),
        autonomous_actions_enabled=env_bool("AUTONOMOUS_ACTIONS_ENABLED", False),
        max_autonomous_messages=int(os.getenv("MAX_AUTONOMOUS_MESSAGES", "2")),
        send_message_delay=float(os.getenv("SEND_MESSAGE_DELAY", "2.0")),
        peer_flood_cooldown=int(os.getenv("PEER_FLOOD_COOLDOWN", "900")),
        database_url=os.getenv("DATABASE_URL") or None,
        db_context_messages=int(os.getenv("DB_CONTEXT_MESSAGES", "35")),
        typing_wait_enabled=env_bool("TYPING_WAIT_ENABLED", True),
        typing_idle_seconds=float(os.getenv("TYPING_IDLE_SECONDS", "0.8")),
        typing_max_wait_seconds=float(os.getenv("TYPING_MAX_WAIT_SECONDS", "2.5")),
        max_model_fallbacks=max(1, int(os.getenv("MAX_MODEL_FALLBACKS", "4"))),
    )


settings = load_settings()
