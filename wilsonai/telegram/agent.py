import asyncio
import base64
import json
import logging
import mimetypes
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, PeerFloodError
from telethon.tl import functions, types
from telethon.tl.custom import Message

from wilsonai.agent.prompts import ACCOUNT_ACTION_PATTERN
from wilsonai.agent.behavior import behavior_prompt, update_behavior_profile
from wilsonai.core.config import DATA_DIR, MEMORY_DIR, PROMPTS_DIR, SYSTEM_PROMPT_PATH, settings
from wilsonai.data.db import Database
from wilsonai.agent.model_client import ask_model, ModelConnectionError, ModelRateLimitError
from wilsonai.agent.storage import (
    clear_history,
    read_system_prompt,
    remember_exchange,
    remember_observation,
)
from wilsonai.agent.speech_to_text import transcribe_voice_message
from wilsonai.agent.voice_pipeline import maybe_send_voice_reply
from wilsonai.telegram.actions import (
    bind_context as bind_actions_context,
    check_reply_status,
    delete_chat,
    direct_admin_actions,
    ensure_existing_dialog,
    execute_account_action,
    execute_account_actions,
    find_logged_sent_message,
    first_target_in_text,
    get_profile_text,
    is_hidden_service_result,
    is_service_success,
    join_chat,
    latest_sent_target,
    message_count_from_text,
    message_preview,
    normalize_public_target,
    private_admin_result,
    quoted_texts,
    read_chat_history,
    reply_long,
    resolve_action_entity,
    resolve_chat_target,
    resolve_send_target,
    same_target,
    send_cooldown_left,
    send_telegram_message,
    send_telegram_messages,
    sender_label,
    split_message,
    split_requested_messages,
    target_has_existing_dialog,
    text_after_marker,
    unblock_user,
    block_user,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("telegram-agent")

client = TelegramClient(settings.session_name, settings.api_id, settings.api_hash)
database = Database(settings.database_url)
locks: dict[int, asyncio.Lock] = {}
batch_tasks: dict[int, asyncio.Task[None]] = {}
pending_batches: dict[int, list["PendingBatchItem"]] = {}
send_blocked_until = 0.0
typing_users: dict[int, dict[int, float]] = {}
recent_voice_answers: dict[int, tuple[str, float]] = {}
last_group_reply_at: dict[int, float] = {}
recent_group_signals: dict[tuple[int, int], list[float]] = {}
model_backoff_until = 0.0
MOD_CHAT_USERNAME = "chatkvadrobery"
MOD_COMMANDS = ("/ban", "/unban", "/mute", "/unmute", "/warn", "/unwarn", "/info")
last_info_lookup_at = 0.0
AUTOMOD_CHAT_USERNAME = "chatkvadrobery"
automod_state: dict[tuple[int, int], dict[str, Any]] = {}
last_reaction_at: dict[int, float] = {}
ALLOWED_REACTIONS = ("👍", "😂", "😅", "💀", "🤢", "🤪", "🙏", "❤️", "😎", "🥸", "🥳", "🖕")


@dataclass
class PendingBatchItem:
    message: Message
    prompt: str
    raw_text: str
    sender_id: int | None
    behavior_context: str = ""
    should_answer: bool = True
    trigger_seen: bool = True


def ensure_dirs() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    MEMORY_DIR.mkdir(exist_ok=True)
    PROMPTS_DIR.mkdir(exist_ok=True)
    SYSTEM_PROMPT_PATH.touch(exist_ok=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def entity_display_name(entity: Any) -> str:
    first_name = getattr(entity, "first_name", "") or ""
    last_name = getattr(entity, "last_name", "") or ""
    title = getattr(entity, "title", "") or ""
    return " ".join(part for part in (first_name, last_name) if part).strip() or title


def entity_dict(entity: Any) -> dict[str, Any]:
    user_id = getattr(entity, "id", None)
    return {
        "user_id": user_id,
        "username": getattr(entity, "username", None),
        "first_name": getattr(entity, "first_name", None),
        "last_name": getattr(entity, "last_name", None),
        "display_name": entity_display_name(entity),
        "is_bot": getattr(entity, "bot", None),
    }


def chat_dict(chat_id: int, entity: Any, message: Message | None = None) -> dict[str, Any]:
    if message and message.is_private:
        chat_type = "private"
    elif message and message.is_group:
        chat_type = "group"
    elif message and message.is_channel:
        chat_type = "channel"
    else:
        chat_type = type(entity).__name__
    return {
        "chat_id": chat_id,
        "title": entity_display_name(entity) or getattr(entity, "username", None),
        "chat_type": chat_type,
        "username": getattr(entity, "username", None),
    }


async def record_telegram_message(
    message: Message,
    text: str,
    sender: Any,
    direction: str,
    is_trigger: bool = False,
) -> None:
    if not database.enabled or message.chat_id is None:
        return

    try:
        chat = await message.get_chat()
    except Exception:
        chat = None

    chat_info = chat_dict(int(message.chat_id), chat, message)
    person_info = entity_dict(sender) if sender else {}
    await database.record_message(
        {
            **chat_info,
            "telegram_message_id": getattr(message, "id", None),
            "sender_id": person_info.get("user_id"),
            "sender_username": person_info.get("username"),
            "sender_first_name": person_info.get("first_name"),
            "sender_last_name": person_info.get("last_name"),
            "sender_display_name": person_info.get("display_name"),
            "sender_is_bot": person_info.get("is_bot"),
            "direction": direction,
            "text": text,
            "has_media": bool(message.media),
            "is_trigger": is_trigger,
            "telegram_date": getattr(message, "date", None),
        }
    )
async def build_batch_prompt(items: list[PendingBatchItem]) -> str:
    behavior_lines = [item.behavior_context for item in items if item.behavior_context]
    behavior_context = "\n".join(dict.fromkeys(behavior_lines))

    if len(items) == 1:
        return "\n\n".join(part for part in [behavior_context, items[0].prompt] if part)

    lines = [
        "РџСЂРёС€Р»Рѕ РЅРµСЃРєРѕР»СЊРєРѕ СЃРѕРѕР±С‰РµРЅРёР№ РїРѕРґСЂСЏРґ. РџСЂРѕС‡РёС‚Р°Р№ РёС… РєР°Рє РѕРґРёРЅ РѕР±С‰РёР№ РєРѕРЅС‚РµРєСЃС‚.",
        "РќР° РїР°СЃСЃРёРІРЅС‹Рµ СЃРѕРѕР±С‰РµРЅРёСЏ РґРѕ РІС‹Р·РѕРІР° РЅРµ РѕС‚РІРµС‡Р°Р№ РѕС‚РґРµР»СЊРЅРѕ: РѕРЅРё РЅСѓР¶РЅС‹, С‡С‚РѕР±С‹ РїРѕРЅСЏС‚СЊ СЃРёС‚СѓР°С†РёСЋ.",
        "РћС‚РІРµС‚СЊ С‚РѕР»СЊРєРѕ РЅР° СЏРІРЅС‹Р№ РІС‹Р·РѕРІ/СѓРїРѕРјРёРЅР°РЅРёРµ. РЎР°Рј СЂРµС€Рё, Р»СѓС‡С€Рµ РѕРґРЅРѕ СЃРѕРѕР±С‰РµРЅРёРµ РёР»Рё РЅРµСЃРєРѕР»СЊРєРѕ СЂРµР°Р»СЊРЅС‹С… СЃРѕРѕР±С‰РµРЅРёР№ С‡РµСЂРµР· РёРЅСЃС‚СЂСѓРјРµРЅС‚С‹.",
        "Р•СЃР»Рё РїСЂРѕСЃСЏС‚ РїРѕРґРєРѕР»РѕС‚СЊ/Р·Р°С‚СЂРѕР»Р»РёС‚СЊ С‡РµР»РѕРІРµРєР° РёР· С‚РµРєСѓС‰РµРіРѕ С‡Р°С‚Р°, РґРµР»Р°Р№ СЌС‚Рѕ РІ current, РЅРµ РІ Р»РёС‡РєСѓ.",
        "",
        "РЎРѕРѕР±С‰РµРЅРёСЏ РїРѕ РїРѕСЂСЏРґРєСѓ:",
    ]
    for index, item in enumerate(items, 1):
        author = await sender_label(item.message)
        text = item.prompt or item.raw_text or "[Р±РµР· С‚РµРєСЃС‚Р°]"
        marker = "РІС‹Р·РѕРІ" if item.should_answer else "РїР°СЃСЃРёРІРЅРѕ"
        lines.append(f"{index}. [{marker}] {author}: {text}")
    if behavior_context:
        lines.extend(["", behavior_context])
    return "\n".join(lines)


async def build_long_memory_context(chat_id: int, items: list[PendingBatchItem]) -> str:
    if not database.enabled:
        return ""

    sender_ids: list[int] = []
    for item in items:
        if item.sender_id and item.sender_id not in sender_ids:
            sender_ids.append(item.sender_id)

    context = await database.context_for_chat(
        chat_id,
        sender_ids,
        limit=settings.db_context_messages,
    )
    if not context:
        return ""
    return (
        "РљРѕРЅС‚РµРєСЃС‚ РёР· РґРѕР»РіРѕРІСЂРµРјРµРЅРЅРѕР№ РїР°РјСЏС‚Рё PostgreSQL. РСЃРїРѕР»СЊР·СѓР№ РµРіРѕ РґР»СЏ СЂРµР°Р»РёР·РјР°, Р»РёС‡РЅРѕСЃС‚Рё, "
        "Р°РґР°РїС‚Р°С†РёРё РїРѕРґ Р»СЋРґРµР№ Рё РїРѕРІРµРґРµРЅРёСЏ, РЅРѕ РЅРµ РїРµСЂРµСЃРєР°Р·С‹РІР°Р№ РєР°Рє Р»РѕРі.\n"
        f"{context}"
    )

async def message_image_part(message: Message) -> dict[str, Any] | None:
    if not message.media:
        return None

    image_dir = DATA_DIR / "incoming_images"
    image_dir.mkdir(parents=True, exist_ok=True)
    downloaded = await message.download_media(file=image_dir)
    if not downloaded:
        return None

    path = Path(downloaded)
    mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    if not mime_type.startswith("image/"):
        return None

    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"inline_data": {"mime_type": mime_type, "data": encoded}}


def extract_account_actions(text: str) -> tuple[str, list[dict[str, Any]]]:
    actions: list[dict[str, Any]] = []

    for match in ACCOUNT_ACTION_PATTERN.finditer(text):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            logger.warning("Bad account action JSON: %s", match.group(1))
            continue
        if isinstance(payload, dict) and isinstance(payload.get("action"), str):
            actions.append(payload)

    cleaned = ACCOUNT_ACTION_PATTERN.sub("", text).strip()
    return cleaned, actions


def sanitize_visible_answer(text: str) -> str:
    value = (text or "").strip()
    if not value:
        return ""
    value = re.sub(r"</?div[^>]*>", "", value, flags=re.IGNORECASE)
    value = re.sub(r"<account_action>.*?</account_action>", "", value, flags=re.IGNORECASE | re.DOTALL)
    lines = []
    for line in value.splitlines():
        s = line.strip()
        if not s:
            continue
        lowered = s.lower()
        if lowered.startswith(("* draft", "draft ", "* final action", "final action", "* to admin", "* to target")):
            continue
        if lowered.startswith("*   "):
            continue
        if lowered.startswith("{\"action\"") or lowered.startswith("{'action'"):
            continue
        if "maintain aggressive" in lowered or "checklist" in lowered:
            continue
        lines.append(s)
    cleaned = "\n".join(lines).strip()
    # Make punctuation less "bot-perfect": trim comma density.
    cleaned = re.sub(r",\s*,+", ", ", cleaned)
    cleaned = re.sub(r"(,\s*){3,}", ", ", cleaned)
    cleaned = re.sub(r",\s+(и|а|но|что|как)\b", r" \1", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


def is_admin(sender_id: int | None) -> bool:
    return sender_id == settings.admin_id


def _mod_reason_label(kind: str) -> str:
    labels = {
        "spam": "спам/реклама",
        "flood": "флуд",
        "abuse": "чрезмерные оскорбления",
        "raid": "похоже на рейд/набег",
    }
    return labels.get(kind, kind)


def _count_caps_words(text: str) -> int:
    return sum(1 for w in re.findall(r"\b[А-ЯA-Z]{4,}\b", text))


def _abuse_score(text: str) -> int:
    lowered = text.lower()
    bad = re.findall(r"\b(долбоеб|долбоёб|пидор|сука|ебан|ебуч|шлюх|мраз|чмо|иди нах|пошел нах|хуй|пизд)\w*\b", lowered)
    return len(bad)


def _spam_score(text: str) -> int:
    lowered = text.lower()
    links = len(re.findall(r"(https?://|t\.me/)", lowered))
    mentions = lowered.count("@")
    repeated = 1 if re.search(r"(.)\1{7,}", lowered) else 0
    return links + mentions + repeated


async def maybe_automod(message: Message, text: str, sender_id: int | None) -> bool:
    if not (message.is_group or message.is_channel) or not sender_id or sender_id == settings.admin_id:
        return False
    chat = await message.get_chat()
    if (getattr(chat, "username", None) or "").lower() != AUTOMOD_CHAT_USERNAME:
        return False
    if not text.strip():
        return False

    chat_id = int(message.chat_id)
    key = (chat_id, sender_id)
    now = time.monotonic()
    state = automod_state.setdefault(key, {"warns": 0, "last_msgs": [], "last_action_at": 0.0})
    state["last_msgs"].append((now, text))
    state["last_msgs"] = [(t, s) for t, s in state["last_msgs"] if now - t <= 45.0]

    stats = await database.participant_stats(chat_id, sender_id) if database.enabled else None
    policy = await database.moderation_policy(chat_id) if database.enabled else {
        "spam_threshold": 3,
        "flood_threshold": 6,
        "abuse_threshold_new": 3,
        "abuse_threshold_old": 5,
    }
    msg_count = int((stats or {}).get("message_count") or 0)
    is_newcomer = msg_count <= 5

    spam = _spam_score(text)
    abuse = _abuse_score(text)
    caps = _count_caps_words(text)
    flood_threshold = int(policy.get("flood_threshold", 6))
    spam_threshold = int(policy.get("spam_threshold", 3))
    abuse_threshold = int(policy.get("abuse_threshold_new", 3) if is_newcomer else policy.get("abuse_threshold_old", 5))
    flood = 1 if len(state["last_msgs"]) >= flood_threshold or caps >= 4 or len(re.findall(r"[!?]", text)) >= 10 else 0
    raid = 1 if is_newcomer and (spam >= spam_threshold or abuse >= abuse_threshold + 1 or len(state["last_msgs"]) >= 5) else 0

    # Cooldown between punishments for same user.
    if now - float(state.get("last_action_at", 0.0)) < 20.0:
        return False

    action = None
    reason = None
    risk_delta = 0.0
    if raid:
        action = "/mute"
        reason = "raid"
        risk_delta = 2.5
    elif spam >= spam_threshold:
        action = "/mute"
        reason = "spam"
        risk_delta = 2.0
    elif flood:
        action = "/mute"
        reason = "flood"
        risk_delta = 1.6
    elif abuse >= abuse_threshold:
        if state["warns"] >= 1:
            action = "/mute"
        else:
            action = "/warn"
        reason = "abuse"
        risk_delta = 1.4 if action == "/warn" else 1.9

    if not action:
        return False

    target = f"{sender_id}"
    cmd = f"{action} {target} причина: {_mod_reason_label(reason or 'rule')}"
    try:
        await client.send_message(chat_id, cmd)
        state["last_action_at"] = now
        if action == "/warn":
            state["warns"] = int(state["warns"]) + 1
        if database.enabled:
            severity = 3 if action == "/mute" else 2
            await database.add_moderation_case(
                chat_id=chat_id,
                user_id=sender_id,
                category=reason or "rule",
                severity=severity,
                reason=_mod_reason_label(reason or "rule"),
                evidence=text,
                source_message_id=getattr(message, "id", None),
            )
            await database.add_moderation_action(
                chat_id=chat_id,
                user_id=sender_id,
                action=action.lstrip("/"),
                reason=_mod_reason_label(reason or "rule"),
                duration_seconds=None,
                source="auto",
            )
            strikes = int(state["warns"]) + (1 if action == "/mute" else 0)
            await database.upsert_risk(
                chat_id=chat_id,
                user_id=sender_id,
                risk_score=max(0.0, float(strikes) + risk_delta),
                strikes=strikes,
                is_newcomer=is_newcomer,
            )
        return True
    except Exception:
        logger.debug("Automod command send failed", exc_info=True)
        return False


async def set_online() -> None:
    try:
        await client(functions.account.UpdateStatusRequest(offline=False))
    except Exception:
        logger.debug("Could not update online status", exc_info=True)


async def keep_online_loop() -> None:
    while True:
        await set_online()
        await asyncio.sleep(max(settings.online_ping_interval, 15))


async def mark_read_if_private(message: Message) -> None:
    if not message.is_private:
        return

    try:
        await client.send_read_acknowledge(message.chat_id, message)
    except Exception:
        logger.debug("Could not mark message as read", exc_info=True)


async def mark_read_any_chat(message: Message) -> None:
    try:
        await client.send_read_acknowledge(message.chat_id, message)
    except Exception:
        logger.debug("Could not mark chat message as read", exc_info=True)


async def send_visible_answer(message: Message, text: str) -> None:
    # In group chats, default to plain message to avoid accidental reply-to the wrong user.
    if message.is_group or message.is_channel:
        for chunk in split_message(text):
            await client.send_message(message.chat_id, chunk)
        return
    await reply_long(message, text)


async def handle_admin_command(message: Message, text: str) -> bool:
    global last_info_lookup_at
    sender = await message.get_sender()
    sender_id = getattr(sender, "id", None)

    if not is_admin(sender_id):
        return False

    normalized = text.strip()
    chat_id = message.chat_id
    lowered = normalized.lower()

    # Admin private-task mode: delegate sending to targets inside a chat.
    if message.is_private and lowered.startswith(("напиши в чате", "отправь в чате")):
        chat_match = re.search(r"(?:в чате)\s+(@[A-Za-z0-9_]{5,}|-?\d{5,}|chatkvadrobery)", normalized, re.IGNORECASE)
        target_chat = chat_match.group(1) if chat_match else "@chatkvadrobery"
        ids = re.findall(r"(?<!\d)(\d{6,})(?!\d)", normalized)
        usernames = re.findall(r"@[A-Za-z0-9_]{5,}", normalized)
        targets = [f"@{u.lstrip('@')}" for u in usernames] + ids
        targets = list(dict.fromkeys(targets))
        text_match = re.search(r"(?:что|чтобы)\s+(.+)$", normalized, re.IGNORECASE | re.DOTALL)
        task_text = text_match.group(1).strip() if text_match else ""
        if not task_text or not targets:
            await message.reply("не понял задачу, дай чат + список id/@username + текст после 'что'")
            return True

        await message.reply("задачу понял, выполняю")
        sent_ok = 0
        for t in targets[:8]:
            outgoing = f"{t} {task_text}" if t.startswith("@") else f"id {t} {task_text}"
            try:
                await client.send_message(target_chat, outgoing)
                sent_ok += 1
                await asyncio.sleep(0.8)
            except Exception:
                logger.debug("Task send failed for target %s in chat %s", t, target_chat, exc_info=True)
        await message.reply(f"готово, отправил: {sent_ok}/{len(targets[:8])}")
        return True

    # Admin private direct-send mode: "напиши ... @user/id ..."
    if message.is_private and lowered.startswith(("напиши", "отправь", "скинь")):
        targets = re.findall(r"@[A-Za-z0-9_]{5,}|(?<!\d)\d{6,}(?!\d)", normalized)
        if targets:
            # Strip leading verb and target tokens; keep the remaining text as payload.
            payload = re.sub(r"^(напиши|отправь|скинь)\s+", "", normalized, flags=re.IGNORECASE).strip()
            for token in targets:
                payload = re.sub(rf"\b{re.escape(token)}\b", "", payload).strip()
            payload = re.sub(r"\s{2,}", " ", payload).strip(" ,.-")
            if not payload:
                await message.reply("укажи текст, который отправить")
                return True
            await message.reply("задачу понял, выполняю")
            sent_ok = 0
            failed: list[str] = []
            for target in list(dict.fromkeys(targets))[:8]:
                try:
                    result = await send_telegram_message(target, payload, current_chat_id=None, allow_new_target=True)
                    if result.startswith("Сообщение отправлено:"):
                        sent_ok += 1
                    else:
                        failed.append(f"{target}: {result}")
                    await asyncio.sleep(0.8)
                except Exception:
                    logger.debug("Direct task send failed for %s", target, exc_info=True)
                    failed.append(f"{target}: exception")
            total = len(list(dict.fromkeys(targets))[:8])
            if failed:
                await reply_long(
                    message,
                    f"готово, отправил: {sent_ok}/{total}\n" + "\n".join(failed[:4]),
                )
            else:
                await message.reply(f"готово, отправил: {sent_ok}/{total}")
            return True

    # Alternate admin private wording: "... вот ей - @username"
    if message.is_private and ("вот ей" in lowered or "вот ему" in lowered):
        targets = re.findall(r"@[A-Za-z0-9_]{5,}|(?<!\d)\d{6,}(?!\d)", normalized)
        if targets:
            payload = re.sub(r"вот (ей|ему)\s*[-:]\s*@[A-Za-z0-9_]{5,}", "", normalized, flags=re.IGNORECASE).strip()
            payload = re.sub(r"\s{2,}", " ", payload).strip(" ,.-")
            if payload:
                await message.reply("задачу понял, выполняю")
                sent_ok = 0
                for target in list(dict.fromkeys(targets))[:8]:
                    result = await send_telegram_message(target, payload, current_chat_id=None, allow_new_target=True)
                    if result.startswith("Сообщение отправлено:"):
                        sent_ok += 1
                    await asyncio.sleep(0.8)
                await message.reply(f"готово, отправил: {sent_ok}/{len(list(dict.fromkeys(targets))[:8])}")
                return True

    # Moderation mode for specific admin chat: proxy moderation commands as-is.
    if lowered.startswith(MOD_COMMANDS):
        try:
            chat = await message.get_chat()
            chat_username = (getattr(chat, "username", None) or "").lower()
            if chat_username == MOD_CHAT_USERNAME:
                if lowered.startswith("/info"):
                    now = time.monotonic()
                    if now - last_info_lookup_at < 20.0:
                        return True
                    last_info_lookup_at = now
                await client.send_message(message.chat_id, normalized)
                return True
        except Exception:
            logger.debug("Failed to proxy moderation command", exc_info=True)

    if normalized in {"/help", f"{settings.trigger_prefix} help"}:
        await reply_long(
            message,
            "\n".join(
                [
                    "РљРѕРјР°РЅРґС‹:",
                    "/help - РїРѕРєР°Р·Р°С‚СЊ РєРѕРјР°РЅРґС‹",
                    "/ping - РїСЂРѕРІРµСЂРєР°",
                    "/reset - РѕС‡РёСЃС‚РёС‚СЊ РїР°РјСЏС‚СЊ С‚РµРєСѓС‰РµРіРѕ С‡Р°С‚Р°",
                    "/system - РїРѕРєР°Р·Р°С‚СЊ С‚РµРєСѓС‰РёР№ СЃРёСЃС‚РµРјРЅС‹Р№ РїСЂРѕРјРїС‚",
                    "/profile - РїРѕРєР°Р·Р°С‚СЊ РїСЂРѕС„РёР»СЊ Р°РєРєР°СѓРЅС‚Р°",
                    f"{settings.trigger_prefix} <С‚РµРєСЃС‚> - РІС‹Р·РІР°С‚СЊ Р°РіРµРЅС‚Р° РІ Р»СЋР±РѕРј С‡Р°С‚Рµ",
                    "РІРёР»СЃРѕРЅ/РІРёР»СЊСЃРѕРЅ <С‚РµРєСЃС‚> - РІС‹Р·РІР°С‚СЊ Р°РіРµРЅС‚Р° РїРѕ РёРјРµРЅРё",
                    "РѕС‚РІРµС‚РёР» Р»Рё @username - РїСЂРѕРІРµСЂРёС‚СЊ, Р±С‹Р» Р»Рё РІС…РѕРґСЏС‰РёР№ РѕС‚РІРµС‚ РїРѕСЃР»Рµ РёСЃС…РѕРґСЏС‰РµРіРѕ",
                    "РїСЂРѕС‡РёС‚Р°Р№ С‡Р°С‚ @username - РїРѕРєР°Р·Р°С‚СЊ РёСЃС‚РѕСЂРёСЋ С‚РѕР»СЊРєРѕ Р°РґРјРёРЅСѓ",
                ]
            ),
        )
        return True

    if normalized == "/ping":
        await message.reply(f"online | {utc_now()}")
        return True

    if normalized == "/reset":
        clear_history(chat_id)
        await message.reply("РџР°РјСЏС‚СЊ СЌС‚РѕРіРѕ С‡Р°С‚Р° РѕС‡РёС‰РµРЅР°.")
        return True

    if normalized == "/system":
        system_prompt = read_system_prompt()
        await reply_long(message, system_prompt or "РЎРёСЃС‚РµРјРЅС‹Р№ РїСЂРѕРјРїС‚ РїСѓСЃС‚РѕР№.")
        return True

    if normalized == "/profile":
        await reply_long(message, await get_profile_text())
        return True

    lowered_text = normalized.lower()
    if lowered_text.startswith("память очистить"):
        scope_type, scope_id = _parse_memory_scope(message.chat_id, normalized)
        deleted = await database.clear_notes(scope_type, scope_id)
        await message.reply(f"очистил память в scope {scope_type}:{scope_id}, записей: {deleted}")
        return True

    if lowered_text.startswith("память"):
        m = re.search(r"\b(\d{1,2})\b", normalized)
        limit = int(m.group(1)) if m else 12
        scope_type, scope_id = _parse_memory_scope(message.chat_id, normalized)
        notes = await database.list_notes(scope_type, scope_id, limit=limit)
        if not notes:
            await message.reply("память пустая")
            return True
        lines = [f"память {scope_type}:{scope_id}"]
        for idx, n in enumerate(notes, 1):
            lines.append(f"{idx}. [{n['category']}] {n['key']} -> {n['value'][:180]}")
        await reply_long(message, "\n".join(lines))
        return True

    if lowered_text.startswith("забудь"):
        needle = normalized[len("забудь") :].strip(" :-")
        if not needle:
            await message.reply("напиши что забыть")
            return True
        scope_type, scope_id = _parse_memory_scope(message.chat_id, normalized)
        deleted = await database.deactivate_note(scope_type, scope_id, needle)
        await message.reply(f"деактивировал записей: {deleted}")
        return True

    if lowered_text.startswith("запомни"):
        pinned = lowered_text.startswith("запомни важно")
        note = normalized.split(":", 1)[1].strip() if ":" in normalized else normalized[len("запомни") :].strip(" :-")
        if note:
            scope_type, scope_id = _parse_memory_scope(message.chat_id, normalized)
            category = _detect_memory_category(note)
            prefix = "pinned:" if pinned else "note:"
            key = f"{prefix}{category}:{int(time.time())}"
            confidence = 0.99 if pinned else 0.9
            await database.add_note(scope_type, scope_id, category, key, note, confidence)
            await message.reply(f"ок, запомнил [{category}] в {scope_type}:{scope_id}")
            return True

    if lowered_text == "правила чата зафиксируй":
        scope_type, scope_id = _parse_memory_scope(message.chat_id, normalized)
        rules = [
            "Запрещено: спам, реклама, ссоры и унижения, 18+, насилие, неуважение, домогательства.",
            "Модерация: всегда указывать причину наказания.",
            "Эскалация: warn -> mute; для тяжких/рейдовых кейсов mute сразу, при рецидиве ban.",
            "Для новичков пороги строже, для старых участников мягче при равном нарушении.",
            "Цель модерации: остановить вред и сохранить порядок, без унижения участников.",
            "Приветствуется: активность, приглашение друзей, помощь админам с нарушителями.",
        ]
        for i, rule in enumerate(rules, 1):
            await database.add_note(
                scope_type,
                scope_id,
                "chat_rules",
                f"pinned:chat_rules:{int(time.time())}:{i}",
                rule,
                0.99,
            )
        await message.reply("зафиксировал полные правила чата в память")
        return True

    return False


def strip_trigger_name(text: str) -> tuple[bool, str]:
    lowered = text.lower()
    for name in settings.trigger_names:
        pattern = rf"(^|\s|[,.!?:;]){re.escape(name)}($|\s|[,.!?:;])"
        match = re.search(pattern, lowered)
        if match:
            cleaned = (text[: match.start()] + " " + text[match.end() :]).strip()
            return True, cleaned or text.strip()
    return False, text


def _signal_spammy(text: str) -> bool:
    lowered = (text or "").lower()
    if lowered.count("@") >= 3:
        return True
    if len(re.findall(r"(https?://|t\.me/)", lowered)) >= 2:
        return True
    if len(re.findall(r"[!?]", lowered)) >= 8:
        return True
    if len(re.findall(r"(wilson|вилсон|вильсон)", lowered)) >= 3:
        return True
    return False


def _register_group_signal(chat_id: int, sender_id: int | None) -> int:
    if not sender_id:
        return 0
    key = (chat_id, sender_id)
    now = time.monotonic()
    bucket = recent_group_signals.setdefault(key, [])
    bucket.append(now)
    recent_group_signals[key] = [t for t in bucket if now - t <= 25.0]
    return len(recent_group_signals[key])


def _parse_memory_scope(chat_id: int | None, text: str) -> tuple[str, str]:
    lowered = text.lower()
    if "глобально" in lowered or "везде" in lowered:
        return "global", "global"
    return "chat", str(int(chat_id)) if chat_id is not None else "global"


def _detect_memory_category(text: str) -> str:
    lowered = text.lower()
    if any(w in lowered for w in ("бан", "мут", "варн", "рейд", "спам", "флуд", "оскорб")):
        return "moderation"
    if any(w in lowered for w in ("стиль", "тон", "пиши", "запяты", "орфограф")):
        return "style"
    if any(w in lowered for w in ("характер", "личность", "персона", "образ")):
        return "persona"
    if any(w in lowered for w in ("правило", "запрещено", "разрешено", "чат")):
        return "chat_rules"
    if any(w in lowered for w in ("api", "модель", "лимит", "инфра", "лог", "ошибк")):
        return "ops"
    return "other"


def pick_reaction_for_text(text: str) -> str | None:
    lowered = (text or "").lower()
    if not lowered.strip():
        return None
    if any(w in lowered for w in ("спасибо", "благодар", "респект", "красав", "молодец")):
        return "🙏"
    if any(w in lowered for w in ("люблю", "сердце", "❤️", "краш")):
        return "❤️"
    if any(w in lowered for w in ("ахах", "хаха", "ржу", "лол", "ору")):
        return "😂"
    if any(w in lowered for w in ("кринж", "фу", "мерз", "тошн")):
        return "🤢"
    if any(w in lowered for w in ("жесть", "умер", "похорон", "капец")):
        return "💀"
    if any(w in lowered for w in ("празд", "др", "ура", "победа")):
        return "🥳"
    if any(w in lowered for w in ("ок", "пон", "согласен", "норм")):
        return "👍"
    if any(w in lowered for w in ("чел", "умник", "клоун")) and any(w in lowered for w in ("нах", "еб", "пошел")):
        return "🖕"
    return None


async def maybe_react_to_message(message: Message, text: str) -> None:
    if not (message.is_group or message.is_channel):
        return
    chat_id = int(message.chat_id)
    now = time.monotonic()
    if now - last_reaction_at.get(chat_id, 0.0) < 45.0:
        return
    # Keep reactions occasional, not on every message.
    if len((text or "").split()) < 2 or random.random() > 0.22:
        return
    emoji = pick_reaction_for_text(text)
    if not emoji or emoji not in ALLOWED_REACTIONS:
        return
    try:
        await client(
            functions.messages.SendReactionRequest(
                peer=message.chat_id,
                msg_id=message.id,
                reaction=[types.ReactionEmoji(emoticon=emoji)],
                add_to_recent=False,
            )
        )
        last_reaction_at[chat_id] = now
    except Exception:
        logger.debug("Could not set reaction", exc_info=True)


def wants_voice_reply(text: str) -> bool:
    lowered = (text or "").lower()
    voice_markers = (
        "РіСЃ",
        "РіРѕР»РѕСЃРѕРІ",
        "voice",
        "РІРѕР№СЃ",
        "Р·Р°РїРёС€Рё РіРѕР»РѕСЃРѕРј",
        "СЃРєРёРЅСЊ РіРѕР»РѕСЃ",
        "РѕС‚РІРµС‚СЊ РіРѕР»РѕСЃРѕРј",
        "Р·Р°РїРёС€Рё РєСЂСѓР¶РѕРє",
    )
    return any(marker in lowered for marker in voice_markers)


def wants_current_photo_as_avatar(text: str) -> bool:
    lowered = text.lower()
    has_avatar_word = any(word in lowered for word in ("Р°РІР°С‚Р°СЂ", "Р°РІСѓ", "Р°РІРєР°", "Р°РІР°С‚Р°СЂРєСѓ"))
    has_action_word = any(
        word in lowered
        for word in ("РїРѕСЃС‚Р°РІ", "РїРѕРјРµРЅСЏ", "СЃРјРµРЅРё", "СЃРґРµР»Р°Р№", "РѕР±РЅРѕРІРё")
    )
    return has_avatar_word and has_action_word


def should_ignore_media_only(message: Message, text: str) -> bool:
    if text.strip():
        return False
    if getattr(message, "sticker", None):
        return True
    if getattr(message, "gif", None):
        return True
    if getattr(message, "animation", None):
        return True
    return False


def should_replace_old_avatar(text: str) -> bool:
    lowered = text.lower()
    old_words = ("СЃС‚Р°СЂСѓСЋ", "СЃС‚Р°СЂРѕРµ", "СЃС‚Р°СЂС‹Р№", "РїСЂРµРґС‹РґСѓС‰СѓСЋ", "РїСЂРµРґС‹РґСѓС‰РёР№", "РїСЂРѕС€Р»СѓСЋ")
    delete_words = ("СѓРґР°Р»Рё", "СѓРґР°Р»РёС‚СЊ", "СЃРЅРµСЃРё", "СѓР±РµСЂРё", "Р·Р°РјРµРЅРё", "Р·Р°РјРµРЅРёС‚СЊ")
    return any(word in lowered for word in old_words) and any(
        word in lowered for word in delete_words
    )


async def should_answer(message: Message, text: str) -> tuple[bool, str]:
    if text.startswith("/"):
        return False, text

    if text.startswith(settings.trigger_prefix):
        prompt = text[len(settings.trigger_prefix) :].strip()
        return True, prompt or "РџРѕСЃРјРѕС‚СЂРё РєР°СЂС‚РёРЅРєСѓ Рё СЃРєР°Р¶Рё, С‡С‚Рѕ СЃ РЅРµР№."

    if message.is_private and settings.respond_to_all_private:
        return True, text.strip() or "РџРѕСЃРјРѕС‚СЂРё РєР°СЂС‚РёРЅРєСѓ Рё СЃРєР°Р¶Рё, С‡С‚Рѕ СЃ РЅРµР№."

    if message.out:
        return False, text

    me = await client.get_me()
    sender = await message.get_sender()
    sender_id = getattr(sender, "id", None)
    # In groups/channels be conservative: only explicit addressing.
    if message.is_group or message.is_channel:
        if is_admin(sender_id):
            # Admin in group should still use explicit intent to avoid noisy auto-replies.
            if text.startswith(settings.trigger_prefix):
                prompt = text[len(settings.trigger_prefix) :].strip()
                return True, prompt or "да"
            has_name_admin, cleaned_admin = strip_trigger_name(text)
            if message.mentioned or has_name_admin:
                if message.mentioned and me.username:
                    cleaned_admin = text.replace(f"@{me.username}", "").strip()
                return True, cleaned_admin or "да"
            return False, text
        if _signal_spammy(text):
            return False, text

        explicit_score = 0
        cleaned = text
        if message.mentioned:
            explicit_score += 2
            cleaned = text.replace(f"@{me.username}", "").strip() if me.username else text
        if getattr(message, "is_reply", False):
            try:
                reply = await message.get_reply_message()
                if reply and getattr(reply, "out", False):
                    explicit_score += 2
            except Exception:
                pass
        has_name, by_name_cleaned = strip_trigger_name(text)
        if has_name:
            explicit_score += 1
            cleaned = by_name_cleaned

        heuristic_score = 0
        lowered = text.lower()
        if "?" in text:
            heuristic_score += 1
        if any(w in lowered for w in ("можешь", "помоги", "поясни", "как", "что делать", "почему")):
            heuristic_score += 1
        if 2 <= len(text.split()) <= 35:
            heuristic_score += 1

        signal_burst = _register_group_signal(int(message.chat_id), sender_id)
        if signal_burst >= 3:
            # user is repeatedly pinging bot-like patterns in short window
            return False, text

        if explicit_score + heuristic_score >= 3 and explicit_score >= 1:
            return True, cleaned.strip() or "да"
        return False, text

    if message.mentioned:
        cleaned = text.replace(f"@{me.username}", "").strip() if me.username else text
        return True, cleaned or "РџРѕСЃРјРѕС‚СЂРё РєР°СЂС‚РёРЅРєСѓ Рё СЃРєР°Р¶Рё, С‡С‚Рѕ СЃ РЅРµР№."

    has_name, cleaned = strip_trigger_name(text)
    if has_name:
        return True, cleaned or "РџРѕСЃРјРѕС‚СЂРё РєР°СЂС‚РёРЅРєСѓ Рё СЃРєР°Р¶Рё, С‡С‚Рѕ СЃ РЅРµР№."

    return False, text


async def passive_prompt(message: Message, text: str) -> str:
    author = await sender_label(message)
    content = text.strip()
    if not content and message.media:
        content = "[РјРµРґРёР° Р±РµР· С‚РµРєСЃС‚Р°]"
    return f"РџР°СЃСЃРёРІРЅС‹Р№ РєРѕРЅС‚РµРєСЃС‚, РЅРµ РѕС‚РІРµС‡Р°Р№ РЅР° СЌС‚Рѕ РѕС‚РґРµР»СЊРЅРѕ. {author}: {content}"


def enqueue_batch_item(chat_id: int, item: PendingBatchItem) -> None:
    batch = pending_batches.setdefault(chat_id, [])
    batch.append(item)
    if len(batch) > settings.max_batch_messages:
        del batch[: len(batch) - settings.max_batch_messages]

    old_task = batch_tasks.get(chat_id)
    if old_task and not old_task.done():
        lock = locks.get(chat_id)
        if lock and lock.locked():
            return
        old_task.cancel()
    batch_tasks[chat_id] = asyncio.create_task(process_pending_batch(chat_id))


def _typing_chat_id(event: events.UserUpdate.Event) -> int | None:
    chat_id = getattr(event, "chat_id", None)
    if chat_id is not None:
        return int(chat_id)
    user_id = getattr(event, "user_id", None)
    return int(user_id) if user_id is not None else None


def _typing_user_id(event: events.UserUpdate.Event) -> int | None:
    user_id = getattr(event, "user_id", None)
    return int(user_id) if user_id is not None else None


async def wait_for_typing_idle(chat_id: int) -> None:
    if not settings.typing_wait_enabled:
        return

    started = time.monotonic()
    idle_seconds = max(settings.typing_idle_seconds, 0.45)
    # Keep replies snappy: dynamic cap instead of long fixed waits.
    configured_cap = max(settings.typing_max_wait_seconds, idle_seconds)
    max_wait = min(configured_cap, 6.0)
    poll_interval = 0.25

    while time.monotonic() - started < max_wait:
        now = time.monotonic()
        active = typing_users.get(chat_id) or {}
        most_recent = 0.0
        for user_id, last_seen in list(active.items()):
            most_recent = max(most_recent, last_seen)
            if now - last_seen > idle_seconds:
                active.pop(user_id, None)
        if not active:
            typing_users.pop(chat_id, None)
            return

        # If typing signal is stale, don't keep waiting for the full cap.
        if most_recent and now - most_recent > idle_seconds + 0.8:
            return
        await asyncio.sleep(poll_interval)


async def latest_batch_image_part(items: list[PendingBatchItem]) -> dict[str, Any] | None:
    for item in reversed(items):
        image_part = await message_image_part(item.message)
        if image_part:
            return image_part
    return None


async def process_pending_batch(chat_id: int) -> None:
    global model_backoff_until
    message_for_error: Message | None = None
    try:
        if time.monotonic() < model_backoff_until:
            return
        await asyncio.sleep(max(settings.message_batch_delay, 0))
        lock = locks.setdefault(chat_id, asyncio.Lock())

        async with lock:
            items = pending_batches.pop(chat_id, [])
            if not items:
                return

            if not any(item.should_answer for item in items):
                for item in items:
                    remember_observation(chat_id, await passive_prompt(item.message, item.raw_text))
                return

            context_items = items[-settings.max_batch_messages :]
            answer_items = [item for item in context_items if item.should_answer]

            latest_message = answer_items[-1].message
            message_for_error = latest_message
            admin_in_batch = any(is_admin(item.sender_id) for item in answer_items)
            action_sender_id = settings.admin_id if admin_in_batch else answer_items[-1].sender_id
            prompt = await build_batch_prompt(context_items)
            long_memory = await build_long_memory_context(chat_id, context_items)
            if long_memory:
                prompt = f"{long_memory}\n\nРўРµРєСѓС‰РёР№ РїР°РєРµС‚ СЃРѕРѕР±С‰РµРЅРёР№:\n{prompt}"
            raw_text = "\n".join(item.raw_text for item in answer_items if item.raw_text).strip()
            force_voice = wants_voice_reply(raw_text)

            await wait_for_typing_idle(chat_id)
            await set_online()
            async with client.action(chat_id, "typing"):
                image_part = await latest_batch_image_part(items)
                direct_actions = direct_admin_actions(raw_text, latest_message) if admin_in_batch else []
                if direct_actions:
                    answer = "РѕРє, РґРµР»Р°СЋ"
                    visible_answer = answer
                    actions = direct_actions
                    allow_new_targets = True
                else:
                    answer = await ask_model(chat_id, prompt, admin_in_batch, image_part)
                    visible_answer, actions = extract_account_actions(answer)
                    visible_answer = sanitize_visible_answer(visible_answer)
                    allow_new_targets = False
                action_results = await execute_account_actions(
                    actions,
                    latest_message,
                    action_sender_id,
                    allow_new_targets=allow_new_targets,
                )

            if action_results:
                visible_answer = "\n\n".join(
                    part for part in [visible_answer, "\n".join(action_results)] if part
                )
            if visible_answer:
                remember_exchange(chat_id, prompt, visible_answer)
                if database.enabled:
                    await database.record_message(
                        {
                            "chat_id": chat_id,
                            "chat_title": str(chat_id),
                            "chat_type": "assistant_reply",
                            "telegram_message_id": None,
                            "sender_id": settings.admin_id,
                            "sender_display_name": "wilson",
                            "direction": "assistant",
                            "text": visible_answer,
                            "has_media": False,
                            "is_trigger": False,
                            "telegram_date": datetime.now(timezone.utc),
                        }
                    )
                sent_voice = await maybe_send_voice_reply(
                    client=client,
                    message=latest_message,
                    text=visible_answer,
                    send_voice_probability=1.0 if force_voice else 0.4,
                    max_voice_seconds=40.0,
                    min_voice_interval_seconds=25.0,
                )
                if sent_voice:
                    recent_voice_answers[chat_id] = (visible_answer.strip(), time.monotonic())
                if not sent_voice:
                    last_voice = recent_voice_answers.get(chat_id)
                    if last_voice:
                        last_text, last_ts = last_voice
                        if (
                            time.monotonic() - last_ts < 60
                            and last_text
                            and last_text == visible_answer.strip()
                        ):
                            logger.info("Skip duplicate text reply after recent voice in chat %s", chat_id)
                            return
                    await send_visible_answer(latest_message, visible_answer)
            else:
                remember_observation(chat_id, prompt)

    except asyncio.CancelledError:
        raise
    except FloodWaitError as exc:
        logger.warning("Telegram flood wait: %s seconds", exc.seconds)
        await asyncio.sleep(exc.seconds)
    except ModelConnectionError as exc:
        logger.warning("%s", exc)
        model_backoff_until = max(model_backoff_until, time.monotonic() + 20.0)
        # Silent failure in chat: keep these diagnostics only in logs.
        return
    except ModelRateLimitError as exc:
        logger.warning("%s", exc)
        model_backoff_until = time.monotonic() + 45.0
        # Silent failure in chat: keep these diagnostics only in logs.
        return
    except Exception as exc:
        logger.exception("Failed to process message batch")
        items = pending_batches.get(chat_id) or []
        message = message_for_error or (items[-1].message if items else None)
        if message:
            try:
                await message.reply(f"ошибка при обработке: {type(exc).__name__}: {str(exc)[:220]}")
            except Exception:
                logger.exception("Could not send error message")
    finally:
        current = batch_tasks.get(chat_id)
        if current is asyncio.current_task():
            batch_tasks.pop(chat_id, None)
            if pending_batches.get(chat_id):
                batch_tasks[chat_id] = asyncio.create_task(process_pending_batch(chat_id))


@client.on(events.NewMessage(incoming=True))
async def on_new_message(event: events.NewMessage.Event) -> None:
    message = event.message
    text = message.raw_text or ""

    try:
        sender = await message.get_sender()
        sender_id = getattr(sender, "id", None)

        if message.is_private:
            await set_online()
            await mark_read_if_private(message)
        else:
            await mark_read_any_chat(message)

        if await handle_admin_command(message, text):
            return
        await maybe_react_to_message(message, text)
        if await maybe_automod(message, text, sender_id):
            return

        if not text.strip() and message.voice:
            transcribed = await transcribe_voice_message(
                message=message,
                target_dir=DATA_DIR / "incoming_voice",
            )
            if transcribed:
                text = transcribed
        if should_ignore_media_only(message, text):
            await record_telegram_message(
                message,
                "[ignored media: sticker/gif]",
                sender,
                "passive",
                is_trigger=False,
            )
            return

        chat_id = int(message.chat_id)
        allowed, prompt = await should_answer(message, text)
        await record_telegram_message(
            message,
            text.strip() or ("[РјРµРґРёР° Р±РµР· С‚РµРєСЃС‚Р°]" if message.media else ""),
            sender,
            "incoming" if allowed else "passive",
            is_trigger=allowed,
        )
        if not allowed and message.is_private:
            return
        if not allowed and not (message.is_group or message.is_channel):
            return

        item_prompt = prompt if allowed else await passive_prompt(message, text)
        enqueue_batch_item(
            chat_id,
            PendingBatchItem(
                message=message,
                prompt=item_prompt,
                raw_text=text,
                sender_id=sender_id,
                behavior_context=behavior_prompt(update_behavior_profile(sender_id, text)),
                should_answer=allowed,
                trigger_seen=allowed,
            ),
        )

    except Exception:
        logger.exception("Failed to enqueue message")
        try:
            await message.reply("ошибка при обработке запроса, подробности в консоли")
        except Exception:
            logger.exception("Could not send error message")


@client.on(events.UserUpdate)
async def on_user_update(event: events.UserUpdate.Event) -> None:
    if not settings.typing_wait_enabled:
        return
    action_name = type(getattr(event, "action", None)).__name__.lower()
    is_typing = bool(getattr(event, "typing", False)) or "typing" in action_name
    if not is_typing:
        return

    chat_id = _typing_chat_id(event)
    user_id = _typing_user_id(event)
    if chat_id is None or user_id is None:
        return

    try:
        me = await client.get_me()
        if user_id == getattr(me, "id", None):
            return
    except Exception:
        pass

    typing_users.setdefault(chat_id, {})[user_id] = time.monotonic()


async def run() -> None:
    ensure_dirs()
    await database.init()
    bind_actions_context(client, database, logger)
    logger.info("Starting Telegram account agent with model %s", settings.model)
    await client.start()
    await set_online()
    me = await client.get_me()
    logger.info("Logged in as %s (%s)", getattr(me, "username", None), me.id)
    online_task: asyncio.Task[None] | None = None
    if settings.keep_online:
        online_task = asyncio.create_task(keep_online_loop())

    try:
        await client.run_until_disconnected()
    finally:
        if online_task:
            online_task.cancel()

