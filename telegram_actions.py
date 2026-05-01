import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

from telethon.errors import FloodWaitError, PeerFloodError
from telethon.tl import functions
from telethon.tl.custom import Message

from agent_prompts import ACCOUNT_ACTION_PATTERN
from config import DATA_DIR, settings
from storage import load_sent_messages, remember_sent_message

logger = logging.getLogger("telegram-agent.actions")

_client = None
_database = None
send_blocked_until = 0.0


def bind_context(client: Any, database: Any, log: logging.Logger | None = None) -> None:
    global _client, _database, logger
    _client = client
    _database = database
    if log is not None:
        logger = log


def _require_client() -> Any:
    if _client is None:
        raise RuntimeError("telegram actions context is not bound")
    return _client


def _require_database() -> Any:
    if _database is None:
        raise RuntimeError("telegram actions context is not bound")
    return _database


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def text_after_marker(text: str, markers: tuple[str, ...]) -> str | None:
    lowered = text.lower()
    positions = [(lowered.find(marker), marker) for marker in markers if lowered.find(marker) != -1]
    if not positions:
        return None

    position, marker = min(positions, key=lambda item: item[0])
    value = text[position + len(marker) :].strip(" :вЂ”-\"'")
    return value or None


def first_target_in_text(text: str) -> str | None:
    match = re.search(
        r"(https?://t\.me/[^\s]+|t\.me/[^\s]+|@[A-Za-z0-9_]{5,}|-?\d{5,})",
        text,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def message_count_from_text(text: str) -> int | None:
    lowered = text.lower()
    word_numbers = {
        "РѕРґРЅРѕ": 1,
        "РѕРґРёРЅ": 1,
        "РґРІР°": 2,
        "РґРІРµ": 2,
        "С‚СЂРё": 3,
        "С‡РµС‚С‹СЂРµ": 4,
        "РїСЏС‚СЊ": 5,
    }
    digit_match = re.search(r"(\d+)\s*(?:СЃРѕРѕР±С‰|СЃРјСЃ|РјРµСЃСЃРµРґР¶)", lowered)
    if digit_match:
        return int(digit_match.group(1))
    for word, number in word_numbers.items():
        if re.search(rf"\b{word}\b\s*(?:СЃРѕРѕР±С‰|СЃРјСЃ|РјРµСЃСЃРµРґР¶)", lowered):
            return number
    if "РЅРµСЃРєРѕР»СЊРєРѕ СЃРѕРѕР±С‰" in lowered:
        return 2
    return None


def quoted_texts(text: str) -> list[str]:
    values = re.findall(r"[\"""'В«В»](.*?)[\"""'В«В»]", text, flags=re.DOTALL)
    return [value.strip() for value in values if value.strip()]


def split_requested_messages(text: str, count: int) -> list[str]:
    quoted = quoted_texts(text)
    if quoted:
        return quoted[:count]

    marker_match = re.search(r"(?:СЃРѕРѕР±С‰РµРЅРёСЏ|СЃРѕРѕР±С‰РµРЅРёР№|СЃРјСЃ)\s*[:вЂ”-]\s*(.+)", text, re.IGNORECASE | re.DOTALL)
    if marker_match:
        tail = marker_match.group(1).strip()
        parts = [part.strip(" \n\t-вЂ”.;") for part in re.split(r"\n+|[;|]", tail) if part.strip(" \n\t-вЂ”.;")]
        if parts:
            return parts[:count]

    return [f"СЃРѕРѕР±С‰РµРЅРёРµ {index}" for index in range(1, count + 1)]


def wants_current_photo_as_avatar(text: str) -> bool:
    lowered = text.lower()
    has_avatar_word = any(word in lowered for word in ("аватар", "аву", "авка", "аватарку"))
    has_action_word = any(
        word in lowered
        for word in ("постав", "поменя", "смени", "сделай", "обнови")
    )
    return has_avatar_word and has_action_word


def direct_admin_actions(text: str, message: Message) -> list[dict[str, Any]]:
    lowered = text.lower()
    actions: list[dict[str, Any]] = []

    if any(
        phrase in lowered
        for phrase in (
            "РѕС‚РІРµС‚РёР» Р»Рё",
            "РѕС‚РІРµС‚РёР»Р° Р»Рё",
            "Р±С‹Р» РѕС‚РІРµС‚",
            "РµСЃС‚СЊ РѕС‚РІРµС‚",
            "РїСЂРёС€РµР» РѕС‚РІРµС‚",
            "РїСЂРёС€С‘Р» РѕС‚РІРµС‚",
            "РѕРЅ РѕС‚РІРµС‚РёР»",
            "РѕРЅР° РѕС‚РІРµС‚РёР»Р°",
        )
    ):
        actions.append(
            {
                "action": "check_reply",
                "target": first_target_in_text(text) or "latest",
                "limit": 50,
            }
        )
        return actions

    if any(phrase in lowered for phrase in ("РїСЂРѕС‡РёС‚Р°Р№ С‡Р°С‚", "РїРѕРєР°Р¶Рё С‡Р°С‚", "РїРѕСЃРјРѕС‚СЂРё С‡Р°С‚", "РїСЂРѕС‡РёС‚Р°Р№ РїРµСЂРµРїРёСЃРєСѓ", "РїРѕРєР°Р¶Рё РїРµСЂРµРїРёСЃРєСѓ")):
        actions.append(
            {
                "action": "read_chat",
                "target": first_target_in_text(text) or "current",
                "limit": 30,
            }
        )
        return actions

    requested_count = message_count_from_text(text)
    wants_multi_send = requested_count and any(word in lowered for word in ("РЅР°РїРёС€Рё", "РѕС‚РїСЂР°РІСЊ", "СЃРєРёРЅСЊ"))
    if wants_multi_send:
        target = first_target_in_text(text)
        if not target and any(word in lowered for word in ("РјРЅРµ", "СЃСЋРґР°", "Р·РґРµСЃСЊ", "С‚СѓС‚")):
            target = "current"
        if target:
            count = max(1, min(requested_count or 1, settings.max_autonomous_messages))
            actions.append(
                {
                    "action": "send_messages",
                    "target": target,
                    "texts": split_requested_messages(text, count),
                }
            )
            return actions

    if any(phrase in lowered for phrase in ("СѓРґР°Р»Рё С‡Р°С‚", "СЃРѕС‚СЂРё С‡Р°С‚", "Р·Р°РєСЂРѕР№ С‡Р°С‚", "СѓРґР°Р»Рё РґРёР°Р»РѕРі")):
        actions.append({"action": "delete_chat", "target": first_target_in_text(text) or "current"})
        return actions

    if (
        "С‡СЃ" in lowered
        and any(word in lowered for word in ("РєРёРЅСЊ", "РґРѕР±Р°РІСЊ", "Р·Р°РєРёРЅСЊ", "РѕС‚РїСЂР°РІСЊ"))
    ) or any(phrase in lowered for phrase in ("Р·Р°Р±Р»РѕРєРёСЂСѓР№", "Р·Р°Р±Р°РЅСЊ")):
        target = first_target_in_text(text)
        if target:
            actions.append({"action": "block_user", "target": target})
            return actions

    if (
        "С‡СЃ" in lowered
        and any(word in lowered for word in ("РґРѕСЃС‚Р°РЅСЊ", "СѓР±РµСЂРё", "РІС‹С‚Р°С‰Рё"))
    ) or "СЂР°Р·Р±Р»РѕРєРёСЂСѓР№" in lowered:
        target = first_target_in_text(text)
        if target:
            actions.append({"action": "unblock_user", "target": target})
            return actions

    username_match = re.search(
        r"(?:СЋР·РµСЂРЅРµР№Рј|username|С‚РµРі)\s*(?:РЅР°|=|:)?\s*@?([A-Za-z][A-Za-z0-9_]{4,31})",
        text,
        re.IGNORECASE,
    )
    if username_match and any(word in lowered for word in ("РїРѕСЃС‚Р°РІ", "РїРѕРјРµРЅ", "СЃРјРµРЅРё", "РёР·РјРµРЅРё", "СЃРґРµР»Р°Р№")):
        actions.append({"action": "set_username", "username": username_match.group(1)})

    bio = text_after_marker(
        text,
        (
            "РѕРїРёСЃР°РЅРёРµ РЅР°",
            "РѕРїРёСЃР°РЅРёРµ:",
            "РѕРїРёСЃР°РЅРёРµ ",
            "Р±РёРѕ РЅР°",
            "Р±РёРѕ:",
            "bio:",
            "about:",
        ),
    )
    if bio and any(word in lowered for word in ("РїРѕСЃС‚Р°РІ", "РїРѕРјРµРЅ", "СЃРјРµРЅРё", "РёР·РјРµРЅРё", "СЃРґРµР»Р°Р№", "Р·Р°РїРёС€Рё")):
        actions.append({"action": "update_profile", "bio": bio[:70]})

    first_name = text_after_marker(
        text,
        (
            "РЅРёРєРЅРµР№Рј РЅР°",
            "РЅРёРє РЅР°",
            "РёРјСЏ РЅР°",
            "nickname:",
            "first name:",
        ),
    )
    if first_name and any(word in lowered for word in ("РїРѕСЃС‚Р°РІ", "РїРѕРјРµРЅ", "СЃРјРµРЅРё", "РёР·РјРµРЅРё", "СЃРґРµР»Р°Р№")):
        actions.append({"action": "update_profile", "first_name": first_name[:64]})

    last_name = text_after_marker(
        text,
        (
            "С„Р°РјРёР»РёСЋ РЅР°",
            "С„Р°РјРёР»РёСЏ:",
            "last name:",
            "surname:",
        ),
    )
    if last_name and any(word in lowered for word in ("РїРѕСЃС‚Р°РІ", "РїРѕРјРµРЅ", "СЃРјРµРЅРё", "РёР·РјРµРЅРё", "СЃРґРµР»Р°Р№")):
        actions.append({"action": "update_profile", "last_name": last_name[:64]})

    if message.media and wants_current_photo_as_avatar(text):
        actions.append(
            {
                "action": "set_photo_from_message",
                "replace_old": should_replace_old_avatar(text),
            }
        )

    send_match = re.search(
        r"(?:РЅР°РїРёС€Рё|РѕС‚РїСЂР°РІСЊ|СЃРєРёРЅСЊ)\s+(https?://t\.me/[^\s]+|t\.me/[^\s]+|@[A-Za-z0-9_]{5,}|[A-Za-z][A-Za-z0-9_]{4,31}|\d{5,})\s+(?:С‚РµРєСЃС‚\s*)?(?:[:вЂ”-]\s*)?(.+)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if send_match:
        actions.append(
            {
                "action": "send_message",
                "target": send_match.group(1),
                "text": send_match.group(2).strip(),
            }
        )

    join_match = re.search(
        r"(?:Р·Р°Р№РґРё|РІСЃС‚СѓРїРё|РїРѕРґРїРёС€РёСЃСЊ|РїСЂРёСЃРѕРµРґРёРЅРёСЃСЊ)\s+(?:РІ|РЅР°|Рє)?\s*(https?://t\.me/[^\s]+|t\.me/[^\s]+|@[A-Za-z0-9_]{5,}|[A-Za-z][A-Za-z0-9_]{4,31})",
        text,
        re.IGNORECASE,
    )
    if join_match:
        actions.append({"action": "join_chat", "target": join_match.group(1)})

    return actions


async def get_profile_text() -> str:
    me = await _require_client().get_me()
    username = f"@{me.username}" if getattr(me, "username", None) else "РЅРµС‚"
    first_name = getattr(me, "first_name", "") or ""
    last_name = getattr(me, "last_name", "") or ""
    input_me = await _require_client().get_input_entity("me")
    full = await _require_client()(functions.users.GetFullUserRequest(input_me))
    about = getattr(full.full_user, "about", "") or ""

    return "\n".join(
        [
            "РўРµРєСѓС‰РёР№ РїСЂРѕС„РёР»СЊ:",
            f"ID: {me.id}",
            f"РРјСЏ: {first_name}",
            f"Р¤Р°РјРёР»РёСЏ: {last_name}",
            f"Р®Р·РµСЂРЅРµР№Рј: {username}",
            f"Р‘РёРѕ: {about or 'РїСѓСЃС‚Рѕ'}",
        ]
    )


async def set_profile_photo_from_message(
    message: Message,
    no_media_text: str,
    replace_old: bool = False,
) -> str:
    if not message.media:
        return no_media_text

    old_photos = await _require_client().get_profile_photos("me", limit=1) if replace_old else []

    photo_dir = DATA_DIR / "profile_photos"
    photo_dir.mkdir(parents=True, exist_ok=True)
    downloaded = await message.download_media(file=photo_dir)
    if not downloaded:
        return "РђРІР°С‚Р°СЂРєР° РЅРµ РёР·РјРµРЅРµРЅР°: РЅРµ СѓРґР°Р»РѕСЃСЊ СЃРєР°С‡Р°С‚СЊ РёР·РѕР±СЂР°Р¶РµРЅРёРµ."

    uploaded = await _require_client().upload_file(downloaded)
    await _require_client()(functions.photos.UploadProfilePhotoRequest(file=uploaded))
    if old_photos:
        await _require_client()(functions.photos.DeletePhotosRequest(id=list(old_photos)))
        return "РђРІР°С‚Р°СЂРєР° РѕР±РЅРѕРІР»РµРЅР°, СЃС‚Р°СЂР°СЏ СѓРґР°Р»РµРЅР°."

    return "РђРІР°С‚Р°СЂРєР° РѕР±РЅРѕРІР»РµРЅР°."


def normalize_public_target(target: str) -> str:
    value = target.strip()
    value = re.sub(r"^https?://t\.me/", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^t\.me/", "", value, flags=re.IGNORECASE)
    value = value.split("?", 1)[0].strip("/")
    if value.startswith("@"):
        value = value[1:]
    return value


def latest_sent_target() -> str | None:
    for item in reversed(load_sent_messages()):
        target = item.get("target")
        if isinstance(target, str) and target.strip():
            return target.strip()
    return None


def same_target(left: str, right: str) -> bool:
    return normalize_public_target(left).lower() == normalize_public_target(right).lower()


def find_logged_sent_message(target: str | None) -> dict[str, Any] | None:
    items = load_sent_messages()
    for item in reversed(items):
        item_target = item.get("target")
        if not isinstance(item_target, str):
            continue
        if not target or target.lower() == "latest" or same_target(item_target, target):
            return item
    return None


async def resolve_chat_target(target: str, current_chat_id: int | None = None) -> tuple[Any, str]:
    raw = target.strip()
    if not raw or raw.lower() in {"current", "this", "С‚СѓС‚", "Р·РґРµСЃСЊ", "СЌС‚РѕС‚", "С‚РµРєСѓС‰РёР№"}:
        if current_chat_id is None:
            raise ValueError("РЅСѓР¶РµРЅ target РёР»Рё С‚РµРєСѓС‰РёР№ С‡Р°С‚")
        return current_chat_id, "С‚РµРєСѓС‰РёР№ С‡Р°С‚"

    if raw.lower() in {"latest", "last", "РїРѕСЃР»РµРґРЅРёР№"}:
        latest = latest_sent_target()
        if not latest:
            raise ValueError("РЅРµС‚ Р·Р°РїРёСЃР°РЅРЅРѕРіРѕ РїРѕСЃР»РµРґРЅРµРіРѕ РїРѕР»СѓС‡Р°С‚РµР»СЏ")
        raw = latest

    entity_target: str | int = normalize_public_target(raw) if "t.me/" in raw.lower() else raw
    if isinstance(entity_target, str) and re.fullmatch(r"-?\d+", entity_target):
        entity_target = int(entity_target)

    return await _require_client().get_entity(entity_target), raw


async def sender_label(message: Message) -> str:
    if message.out:
        return "СЏ"

    sender = await message.get_sender()
    if not sender:
        return "РЅРµРёР·РІРµСЃС‚РЅРѕ"

    username = getattr(sender, "username", None)
    first_name = getattr(sender, "first_name", "") or ""
    last_name = getattr(sender, "last_name", "") or ""
    full_name = " ".join(part for part in (first_name, last_name) if part).strip()
    sender_id = getattr(sender, "id", None)

    parts: list[str] = []
    if username:
        parts.append(f"@{username}")
    if full_name:
        parts.append(f"РёРјСЏ: {full_name}")
    if sender_id:
        parts.append(f"id: {sender_id}")
    return ", ".join(parts) if parts else "РЅРµРёР·РІРµСЃС‚РЅРѕ"


def message_preview(message: Message) -> str:
    text = (message.raw_text or "").strip()
    if text:
        return text.replace("\n", " ")[:700]
    if message.media:
        return "[РјРµРґРёР° Р±РµР· С‚РµРєСЃС‚Р°]"
    return "[РїСѓСЃС‚РѕРµ СЃРѕРѕР±С‰РµРЅРёРµ]"


async def read_chat_history(
    target: str,
    current_chat_id: int | None,
    limit: int = 20,
    require_existing: bool = False,
) -> str:
    entity, label = await resolve_chat_target(target, current_chat_id)
    if require_existing:
        await ensure_existing_dialog(getattr(entity, "id", entity))
    safe_limit = max(1, min(int(limit or 20), 100))
    messages = [item async for item in _require_client().iter_messages(entity, limit=safe_limit)]
    messages.reverse()

    if not messages:
        return f"РСЃС‚РѕСЂРёСЏ {label}: СЃРѕРѕР±С‰РµРЅРёР№ РЅРµ РІРёРґРЅРѕ."

    lines = [f"РСЃС‚РѕСЂРёСЏ {label}, РїРѕСЃР»РµРґРЅРёРµ {len(messages)} СЃРѕРѕР±С‰РµРЅРёР№:"]
    for item in messages:
        direction = "РёСЃС…РѕРґСЏС‰РµРµ" if item.out else "РІС…РѕРґСЏС‰РµРµ"
        author = await sender_label(item)
        date = item.date.isoformat(timespec="minutes") if item.date else "Р±РµР· РґР°С‚С‹"
        lines.append(f"- {date} | {direction} | {author} | {message_preview(item)}")
    return "\n".join(lines)


async def check_reply_status(
    target: str,
    current_chat_id: int | None,
    limit: int = 50,
    require_existing: bool = False,
) -> str:
    logged = find_logged_sent_message(None if target.lower() in {"", "latest", "last", "РїРѕСЃР»РµРґРЅРёР№"} else target)
    effective_target = target or "latest"
    if effective_target.lower() in {"latest", "last", "РїРѕСЃР»РµРґРЅРёР№"} and logged:
        effective_target = str(logged.get("target") or "latest")

    entity, label = await resolve_chat_target(effective_target, current_chat_id)
    if require_existing:
        await ensure_existing_dialog(getattr(entity, "id", entity))
    safe_limit = max(1, min(int(limit or 50), 100))
    messages = [item async for item in _require_client().iter_messages(entity, limit=safe_limit)]

    latest_out: Message | None = None
    logged_message_id = logged.get("message_id") if logged else None
    if isinstance(logged_message_id, int):
        latest_out = next((item for item in messages if item.out and item.id == logged_message_id), None)

    if latest_out is None:
        latest_out = next((item for item in messages if item.out), None)

    if latest_out is None:
        return f"РџРѕ {label} РЅРµ РІРёР¶Сѓ РёСЃС…РѕРґСЏС‰РµРіРѕ СЃРѕРѕР±С‰РµРЅРёСЏ РІ РїРѕСЃР»РµРґРЅРёС… {safe_limit} СЃРѕРѕР±С‰РµРЅРёСЏС…."

    replies = [item for item in messages if not item.out and item.id > latest_out.id]
    sent_at = latest_out.date.isoformat(timespec="minutes") if latest_out.date else "Р±РµР· РґР°С‚С‹"
    sent_text = message_preview(latest_out)

    if not replies:
        return f"РћС‚РІРµС‚Р° РѕС‚ {label} РїРѕСЃР»Рµ РјРѕРµРіРѕ РїРѕСЃР»РµРґРЅРµРіРѕ СЃРѕРѕР±С‰РµРЅРёСЏ РЅРµ РІРёР¶Сѓ. РџРѕСЃР»РµРґРЅРµРµ РёСЃС…РѕРґСЏС‰РµРµ: {sent_at} | {sent_text}"

    replies.reverse()
    lines = [
        f"Р”Р°, {label} РѕС‚РІРµС‚РёР» РїРѕСЃР»Рµ РјРѕРµРіРѕ РїРѕСЃР»РµРґРЅРµРіРѕ СЃРѕРѕР±С‰РµРЅРёСЏ.",
        f"РџРѕСЃР»РµРґРЅРµРµ РёСЃС…РѕРґСЏС‰РµРµ: {sent_at} | {sent_text}",
        "РћС‚РІРµС‚С‹ РїРѕСЃР»Рµ РЅРµРіРѕ:",
    ]
    for item in replies[:10]:
        author = await sender_label(item)
        date = item.date.isoformat(timespec="minutes") if item.date else "Р±РµР· РґР°С‚С‹"
        lines.append(f"- {date} | {author} | {message_preview(item)}")
    return "\n".join(lines)


async def private_admin_result(message: Message, text: str, public_notice: str) -> str:
    if message.is_private and message.chat_id == settings.admin_id:
        return text

    await _require_client().send_message(settings.admin_id, text)
    return public_notice


async def join_chat(target: str) -> str:
    raw = target.strip()
    if not raw:
        return "не понял, куда зайти"

    invite_match = re.search(r"(?:https?://)?t\.me/(?:joinchat/|\+)([A-Za-z0-9_-]+)", raw, re.IGNORECASE)
    if not invite_match:
        invite_match = re.search(r"(?:joinchat/|\+)([A-Za-z0-9_-]+)", raw, re.IGNORECASE)
    if invite_match:
        try:
            await _require_client()(functions.messages.ImportChatInviteRequest(invite_match.group(1)))
            return "зашел по инвайт-ссылке"
        except Exception as exc:
            return f"не смог зайти по инвайту: {type(exc).__name__}: {exc}"

    public_target = normalize_public_target(raw)
    try:
        entity = await _require_client().get_entity(public_target)
        await _require_client()(functions.channels.JoinChannelRequest(entity))
        return f"зашел сюда: {raw}"
    except Exception as exc:
        return f"не смог зайти в публичный чат: {type(exc).__name__}: {exc}"


def send_cooldown_left() -> int:
    return max(0, int(send_blocked_until - time.monotonic()))


def is_service_success(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    if not lines:
        return True
    success_patterns = (
        "РЎРѕРѕР±С‰РµРЅРёРµ РѕС‚РїСЂР°РІР»РµРЅРѕ:",
        "РџСЂРѕС„РёР»СЊ РѕР±РЅРѕРІР»РµРЅ.",
        "Р®Р·РµСЂРЅРµР№Рј РѕР±РЅРѕРІР»РµРЅ:",
        "Р®Р·РµСЂРЅРµР№Рј СѓР±СЂР°РЅ.",
        "РђРІР°С‚Р°СЂРєР° РѕР±РЅРѕРІР»РµРЅР°",
        "РўРµРєСѓС‰Р°СЏ Р°РІР°С‚Р°СЂРєР° СѓРґР°Р»РµРЅР°.",
        "Р—Р°С€РµР» СЃСЋРґР°:",
        "Р—Р°С€РµР» РїРѕ РёРЅРІР°Р№С‚-СЃСЃС‹Р»РєРµ.",
    )
    numbered_send = re.compile(r"^\d+\.\s*РЎРѕРѕР±С‰РµРЅРёРµ РѕС‚РїСЂР°РІР»РµРЅРѕ:")
    return all(
        line.startswith(success_patterns) or numbered_send.match(line)
        for line in lines
    )


def is_hidden_service_result(text: str) -> bool:
    lowered = text.lower().strip()
    return is_service_success(text) or lowered in {
        "Р°РІС‚РѕРЅРѕРјРЅРѕ РјРѕР¶РЅРѕ СЂР°Р±РѕС‚Р°С‚СЊ С‚РѕР»СЊРєРѕ СЃ СѓР¶Рµ СЃСѓС‰РµСЃС‚РІСѓСЋС‰РёРјРё РґРёР°Р»РѕРіР°РјРё",
    }


def resolve_send_target(target: str, current_chat_id: int | None) -> str | int:
    lowered = target.strip().lower()
    if lowered in {"current", "this", "С‚СѓС‚", "СЃСЋРґР°", "Р·РґРµСЃСЊ", "РјРЅРµ"}:
        if current_chat_id is None:
            raise ValueError("РЅРµ РїРѕРЅСЏР», РєСѓРґР° РѕС‚РїСЂР°РІР»СЏС‚СЊ")
        return current_chat_id
    return normalize_public_target(target) if "t.me/" in target.lower() else target


async def target_has_existing_dialog(entity_target: str | int) -> bool:
    try:
        entity = await _require_client().get_entity(entity_target)
    except Exception:
        return False

    entity_id = getattr(entity, "id", None)
    entity_username = (getattr(entity, "username", None) or "").lower()
    async for dialog in _require_client().iter_dialogs():
        dialog_entity = dialog.entity
        if entity_id is not None and getattr(dialog_entity, "id", None) == entity_id:
            return True
        if entity_username and (getattr(dialog_entity, "username", None) or "").lower() == entity_username:
            return True
    return False


async def ensure_existing_dialog(entity_target: str | int) -> None:
    if not await target_has_existing_dialog(entity_target):
        raise ValueError("Р°РІС‚РѕРЅРѕРјРЅРѕ РјРѕР¶РЅРѕ СЂР°Р±РѕС‚Р°С‚СЊ С‚РѕР»СЊРєРѕ СЃ СѓР¶Рµ СЃСѓС‰РµСЃС‚РІСѓСЋС‰РёРјРё РґРёР°Р»РѕРіР°РјРё")


async def send_telegram_message(
    target: str,
    text: str,
    current_chat_id: int | None = None,
    allow_new_target: bool = True,
) -> str:
    global send_blocked_until

    target = target.strip()
    text = text.strip()
    if not target or not text:
        return "РќРµ РѕС‚РїСЂР°РІРёР»: РЅСѓР¶РµРЅ РїРѕР»СѓС‡Р°С‚РµР»СЊ Рё С‚РµРєСЃС‚."

    cooldown_left = send_cooldown_left()
    if cooldown_left > 0:
        return f"РћС‚РїСЂР°РІРєР° РІСЂРµРјРµРЅРЅРѕ РѕСЃС‚Р°РЅРѕРІР»РµРЅР° Telegram flood-Р·Р°С‰РёС‚РѕР№. РџРѕРґРѕР¶РґРё РїСЂРёРјРµСЂРЅРѕ {cooldown_left} СЃРµРє."

    entity_target = resolve_send_target(target, current_chat_id)
    if not allow_new_target:
        await ensure_existing_dialog(entity_target)
    try:
        sent = await _require_client().send_message(entity_target, text)
    except FloodWaitError as exc:
        send_blocked_until = time.monotonic() + max(int(exc.seconds), settings.peer_flood_cooldown)
        return f"Telegram РїСЂРѕСЃРёС‚ РїРѕРґРѕР¶РґР°С‚СЊ {exc.seconds} СЃРµРє. РћС‚РїСЂР°РІРєРё РІСЂРµРјРµРЅРЅРѕ РѕСЃС‚Р°РЅРѕРІР»РµРЅС‹."
    except PeerFloodError:
        send_blocked_until = time.monotonic() + settings.peer_flood_cooldown
        return "Telegram РІРєР»СЋС‡РёР» flood-Р·Р°С‰РёС‚Сѓ РґР»СЏ РёСЃС…РѕРґСЏС‰РёС… СЃРѕРѕР±С‰РµРЅРёР№. РћС‚РїСЂР°РІРєРё РІСЂРµРјРµРЅРЅРѕ РѕСЃС‚Р°РЅРѕРІР»РµРЅС‹."

    remember_sent_message(target, sent, text)
    if _require_database().enabled:
        await _require_database().record_message(
            {
                "chat_id": int(getattr(sent, "chat_id", current_chat_id or 0)),
                "chat_title": str(target),
                "chat_type": "outgoing",
                "chat_username": normalize_public_target(target) if "t.me/" in target.lower() else target.lstrip("@"),
                "telegram_message_id": getattr(sent, "id", None),
                "sender_id": settings.admin_id,
                "sender_display_name": "wilson",
                "direction": "outgoing",
                "text": text,
                "has_media": False,
                "is_trigger": False,
                "telegram_date": getattr(sent, "date", None),
            }
        )
    return f"РЎРѕРѕР±С‰РµРЅРёРµ РѕС‚РїСЂР°РІР»РµРЅРѕ: {target}"


async def send_telegram_messages(
    target: str,
    texts: list[str],
    current_chat_id: int | None = None,
    allow_new_target: bool = True,
) -> str:
    clean_texts = [str(text).strip() for text in texts if str(text).strip()]
    if not clean_texts:
        return "РќРµ РѕС‚РїСЂР°РІРёР»: СЃРїРёСЃРѕРє СЃРѕРѕР±С‰РµРЅРёР№ РїСѓСЃС‚РѕР№."

    clean_texts = clean_texts[: settings.max_autonomous_messages]
    results: list[str] = []
    for index, text in enumerate(clean_texts, 1):
        result = await send_telegram_message(target, text, current_chat_id, allow_new_target)
        results.append(f"{index}. {result}")
        if "flood-Р·Р°С‰РёС‚" in result or "РїСЂРѕСЃРёС‚ РїРѕРґРѕР¶РґР°С‚СЊ" in result or send_cooldown_left() > 0:
            break
        if index < len(clean_texts):
            await asyncio.sleep(max(settings.send_message_delay, 0))
    return "\n".join(results)


async def resolve_action_entity(target: str, current_chat_id: int | None, require_existing: bool) -> tuple[Any, str]:
    entity_target = resolve_send_target(target, current_chat_id)
    if require_existing:
        await ensure_existing_dialog(entity_target)
    entity = await _require_client().get_entity(entity_target)
    return entity, target


async def delete_chat(target: str, current_chat_id: int | None, require_existing: bool = True) -> str:
    target = target.strip() or "current"
    entity, label = await resolve_action_entity(target, current_chat_id, require_existing)
    await _require_client().delete_dialog(entity, revoke=False)
    return f"Р”РёР°Р»РѕРі СѓРґР°Р»РµРЅ: {label}"


async def block_user(target: str, current_chat_id: int | None, require_existing: bool = True) -> str:
    if not target.strip():
        return "РќРµ Р·Р°Р±Р»РѕРєРёСЂРѕРІР°Р»: РЅСѓР¶РµРЅ РїРѕР»СЊР·РѕРІР°С‚РµР»СЊ."

    entity, label = await resolve_action_entity(target, current_chat_id, require_existing)
    await _require_client()(functions.contacts.BlockRequest(id=entity))
    return f"РџРѕР»СЊР·РѕРІР°С‚РµР»СЊ Р·Р°Р±Р»РѕРєРёСЂРѕРІР°РЅ: {label}"


async def unblock_user(target: str, current_chat_id: int | None, require_existing: bool = True) -> str:
    if not target.strip():
        return "РќРµ СЂР°Р·Р±Р»РѕРєРёСЂРѕРІР°Р»: РЅСѓР¶РµРЅ РїРѕР»СЊР·РѕРІР°С‚РµР»СЊ."

    entity, label = await resolve_action_entity(target, current_chat_id, require_existing)
    await _require_client()(functions.contacts.UnblockRequest(id=entity))
    return f"РџРѕР»СЊР·РѕРІР°С‚РµР»СЊ СЂР°Р·Р±Р»РѕРєРёСЂРѕРІР°РЅ: {label}"


async def execute_account_action(
    action: dict[str, Any],
    message: Message,
    allow_new_targets: bool = True,
) -> str:
    action_name = action.get("action")
    current_chat_id = int(message.chat_id) if message.chat_id is not None else None

    if action_name == "update_profile":
        kwargs: dict[str, str] = {}
        first_name = action.get("first_name", action.get("name", action.get("nickname")))
        last_name = action.get("last_name", action.get("surname"))
        about = action.get(
            "bio",
            action.get("about", action.get("description", action.get("РѕРїРёСЃР°РЅРёРµ"))),
        )
        if first_name is not None:
            kwargs["first_name"] = str(first_name or "")[:64]
        if last_name is not None:
            kwargs["last_name"] = str(last_name or "")[:64]
        if about is not None:
            kwargs["about"] = str(about or "")[:70]

        if not kwargs:
            return "РџСЂРѕС„РёР»СЊ РЅРµ РёР·РјРµРЅРµРЅ: РЅРµС‚ РїРѕР»РµР№ РґР»СЏ РѕР±РЅРѕРІР»РµРЅРёСЏ."

        await _require_client()(functions.account.UpdateProfileRequest(**kwargs))
        return "РџСЂРѕС„РёР»СЊ РѕР±РЅРѕРІР»РµРЅ."

    if action_name == "set_username":
        username = str(action.get("username") or "").strip().lstrip("@")
        if username and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", username):
            return "Р®Р·РµСЂРЅРµР№Рј РЅРµ РёР·РјРµРЅРµРЅ: РЅСѓР¶РµРЅ С„РѕСЂРјР°С‚ 5-32 СЃРёРјРІРѕР»Р°, Р»Р°С‚РёРЅРёС†Р°/С†РёС„СЂС‹/РїРѕРґС‡РµСЂРєРёРІР°РЅРёРµ, РїРµСЂРІС‹Р№ СЃРёРјРІРѕР» Р±СѓРєРІР°."

        await _require_client()(functions.account.UpdateUsernameRequest(username=username))
        return f"Р®Р·РµСЂРЅРµР№Рј РѕР±РЅРѕРІР»РµРЅ: @{username}" if username else "Р®Р·РµСЂРЅРµР№Рј СѓР±СЂР°РЅ."

    if action_name == "set_photo_from_message":
        return await set_profile_photo_from_message(
            message,
            "Р’ СЌС‚РѕРј СЃРѕРѕР±С‰РµРЅРёРё РЅРµС‚ РєР°СЂС‚РёРЅРєРё РґР»СЏ Р°РІР°С‚Р°СЂРєРё.",
            replace_old=bool(action.get("replace_old")),
        )

    if action_name == "set_photo_from_reply":
        reply = await message.get_reply_message()
        if not reply:
            return "РђРІР°С‚Р°СЂРєР° РЅРµ РёР·РјРµРЅРµРЅР°: РѕС‚РІРµС‚СЊ РєРѕРјР°РЅРґРѕР№ РЅР° СЃРѕРѕР±С‰РµРЅРёРµ СЃ РєР°СЂС‚РёРЅРєРѕР№."
        return await set_profile_photo_from_message(
            reply,
            "РђРІР°С‚Р°СЂРєР° РЅРµ РёР·РјРµРЅРµРЅР°: РѕС‚РІРµС‚СЊ РєРѕРјР°РЅРґРѕР№ РЅР° СЃРѕРѕР±С‰РµРЅРёРµ СЃ РєР°СЂС‚РёРЅРєРѕР№.",
            replace_old=bool(action.get("replace_old")),
        )

    if action_name == "delete_current_photo":
        photos = await _require_client().get_profile_photos("me", limit=1)
        if not photos:
            return "Р¤РѕС‚Рѕ РїСЂРѕС„РёР»СЏ СѓР¶Рµ РЅРµС‚."

        await _require_client()(functions.photos.DeletePhotosRequest(id=list(photos)))
        return "РўРµРєСѓС‰Р°СЏ Р°РІР°С‚Р°СЂРєР° СѓРґР°Р»РµРЅР°."

    if action_name == "get_profile":
        return await get_profile_text()

    if action_name == "send_message":
        return await send_telegram_message(
            str(action.get("target") or action.get("to") or ""),
            str(action.get("text") or action.get("message") or ""),
            current_chat_id,
            allow_new_targets,
        )

    if action_name == "send_messages":
        raw_texts = action.get("texts") or action.get("messages") or []
        if isinstance(raw_texts, str):
            texts = [part.strip() for part in re.split(r"\n+|[;|]", raw_texts) if part.strip()]
        elif isinstance(raw_texts, list):
            texts = [str(item) for item in raw_texts]
        else:
            texts = []
        return await send_telegram_messages(
            str(action.get("target") or action.get("to") or ""),
            texts,
            current_chat_id,
            allow_new_targets,
        )

    if action_name == "join_chat":
        return await join_chat(str(action.get("target") or action.get("link") or action.get("chat") or ""))

    if action_name == "delete_chat":
        return await delete_chat(
            str(action.get("target") or action.get("chat") or "current"),
            current_chat_id,
            require_existing=not allow_new_targets,
        )

    if action_name == "block_user":
        return await block_user(
            str(action.get("target") or action.get("user") or ""),
            current_chat_id,
            require_existing=not allow_new_targets,
        )

    if action_name == "unblock_user":
        return await unblock_user(
            str(action.get("target") or action.get("user") or ""),
            current_chat_id,
            require_existing=not allow_new_targets,
        )

    if action_name == "read_chat":
        result = await read_chat_history(
            str(action.get("target") or action.get("chat") or "current"),
            int(message.chat_id) if message.chat_id is not None else None,
            int(action.get("limit") or 20),
            require_existing=not allow_new_targets,
        )
        return await private_admin_result(message, result, "СЃРєРёРЅСѓР» РёСЃС‚РѕСЂРёСЋ С‚РµР±Рµ РІ Р»РёС‡РєСѓ")

    if action_name == "check_reply":
        result = await check_reply_status(
            str(action.get("target") or action.get("chat") or "latest"),
            int(message.chat_id) if message.chat_id is not None else None,
            int(action.get("limit") or 50),
            require_existing=not allow_new_targets,
        )
        return await private_admin_result(message, result, "СЃРєРёРЅСѓР» РїСЂРѕРІРµСЂРєСѓ РѕС‚РІРµС‚Р° С‚РµР±Рµ РІ Р»РёС‡РєСѓ")

    return f"РќРµРёР·РІРµСЃС‚РЅРѕРµ РґРµР№СЃС‚РІРёРµ Р°РєРєР°СѓРЅС‚Р°: {action_name}"


async def execute_account_actions(
    actions: list[dict[str, Any]],
    message: Message,
    sender_id: int | None,
    allow_new_targets: bool = True,
) -> list[str]:
    if not actions:
        return []

    if not is_admin(sender_id):
        logger.warning("Blocked account actions from non-admin sender %s", sender_id)
        return ["Р”РµР№СЃС‚РІРёСЏ СЃ Р°РєРєР°СѓРЅС‚РѕРј РґРѕСЃС‚СѓРїРЅС‹ С‚РѕР»СЊРєРѕ Р°РґРјРёРЅСѓ."]

    results: list[str] = []
    sent_by_model = 0
    for action in actions:
        if action.get("action") == "send_message":
            sent_by_model += 1
            if sent_by_model > settings.max_autonomous_messages:
                results.append(
                    f"Р›РёРјРёС‚ РѕС‚РїСЂР°РІРѕРє Р·Р° РѕРґРёРЅ РѕС‚РІРµС‚: {settings.max_autonomous_messages}. РћСЃС‚Р°Р»СЊРЅС‹Рµ СЃРѕРѕР±С‰РµРЅРёСЏ РїСЂРѕРїСѓС‰РµРЅС‹."
                )
                continue
        elif action.get("action") == "send_messages":
            raw_texts = action.get("texts") or action.get("messages") or []
            requested = len(raw_texts) if isinstance(raw_texts, list) else 1
            if sent_by_model >= settings.max_autonomous_messages:
                results.append(
                    f"Р›РёРјРёС‚ РѕС‚РїСЂР°РІРѕРє Р·Р° РѕРґРёРЅ РѕС‚РІРµС‚: {settings.max_autonomous_messages}. РЎРѕРѕР±С‰РµРЅРёСЏ РїСЂРѕРїСѓС‰РµРЅС‹."
                )
                continue
            if sent_by_model + requested > settings.max_autonomous_messages:
                allowed = settings.max_autonomous_messages - sent_by_model
                if isinstance(raw_texts, list):
                    action = {**action, "texts": raw_texts[:allowed]}
                sent_by_model = settings.max_autonomous_messages
            else:
                sent_by_model += requested

        try:
            result = await execute_account_action(action, message, allow_new_targets)
            if not is_hidden_service_result(result):
                results.append(result)
            if "flood-Р·Р°С‰РёС‚" in result or "РїСЂРѕСЃРёС‚ РїРѕРґРѕР¶РґР°С‚СЊ" in result:
                break
        except ValueError as exc:
            if not is_hidden_service_result(str(exc)):
                results.append(str(exc))
        except Exception as exc:
            logger.exception("Account action failed: %s", action)
            if action.get("action") not in {"send_message", "send_messages"}:
                results.append(f"Р”РµР№СЃС‚РІРёРµ {action.get('action')} РЅРµ РІС‹РїРѕР»РЅРµРЅРѕ.")
    return results


def split_message(text: str, limit: int = 3900) -> list[str]:
    chunks: list[str] = []
    remaining = text.strip()

    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()

    if remaining:
        chunks.append(remaining)
    return chunks or ["..."]


async def reply_long(message: Message, text: str) -> None:
    for chunk in split_message(text):
        await message.reply(chunk)



def is_admin(sender_id: int | None) -> bool:
    return sender_id == settings.admin_id

