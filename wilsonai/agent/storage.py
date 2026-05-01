import json
import logging
from pathlib import Path
from typing import Any

from telethon.tl.custom import Message

from wilsonai.core.config import DATA_DIR, MEMORY_DIR, SENT_MESSAGES_PATH, SYSTEM_PROMPT_PATH, settings

logger = logging.getLogger("telegram-agent")


def memory_path(chat_id: int) -> Path:
    return MEMORY_DIR / f"{chat_id}.json"


def load_history(chat_id: int) -> list[dict[str, str]]:
    path = memory_path(chat_id)
    if not path.exists():
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Could not read memory for chat %s, starting fresh", chat_id)
        return []

    if not isinstance(data, list):
        return []

    history: list[dict[str, str]] = []
    for item in data:
        if (
            isinstance(item, dict)
            and item.get("role") in {"user", "assistant"}
            and isinstance(item.get("content"), str)
        ):
            history.append({"role": item["role"], "content": item["content"]})
    return history[-settings.max_history_messages :]


def save_history(chat_id: int, history: list[dict[str, str]]) -> None:
    path = memory_path(chat_id)
    trimmed = history[-settings.max_history_messages :]
    path.write_text(
        json.dumps(trimmed, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def remember_exchange(chat_id: int, user_text: str, assistant_text: str) -> None:
    history = load_history(chat_id)
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": assistant_text})
    save_history(chat_id, history)


def remember_observation(chat_id: int, text: str) -> None:
    text = text.strip()
    if not text:
        return

    history = load_history(chat_id)
    history.append({"role": "user", "content": text})
    save_history(chat_id, history[-settings.max_passive_context_messages :])


def clear_history(chat_id: int) -> None:
    path = memory_path(chat_id)
    if path.exists():
        path.unlink()


def load_sent_messages() -> list[dict[str, Any]]:
    if not SENT_MESSAGES_PATH.exists():
        return []

    try:
        data = json.loads(SENT_MESSAGES_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Could not read sent message log, starting fresh")
        return []

    return data if isinstance(data, list) else []


def save_sent_messages(items: list[dict[str, Any]]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    SENT_MESSAGES_PATH.write_text(
        json.dumps(items[-200:], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def remember_sent_message(target: str, sent: Message, text: str) -> None:
    items = load_sent_messages()
    items.append(
        {
            "target": target,
            "chat_id": getattr(sent, "chat_id", None),
            "message_id": getattr(sent, "id", None),
            "date": sent.date.isoformat() if getattr(sent, "date", None) else utc_now(),
            "text": text[:1000],
        }
    )
    save_sent_messages(items)


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_system_prompt() -> str:
    return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
