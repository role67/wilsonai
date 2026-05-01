import asyncio
import logging
import random
import re
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger("telegram-agent.voice")

_tts_model: Any = None
_tts_sample_rate = 48000
_model_lock = asyncio.Lock()
_voice_queue_lock = asyncio.Lock()
_last_voice_by_chat: dict[int, float] = {}
_last_openers: list[str] = []
_tts_unavailable_logged = False

INTERJECTION_CHANCE = 0.25
PAUSE_CHANCE = 0.12

INTERJECTION_GROUPS = {
    "neutral": ["ну", "да", "короче", "смотри", "если честно"],
    "agree": ["ну да", "ага", "угу", "понял", "ясно", "ладно"],
    "think": ["хм", "мм", "блин", "сложно сказать", "щас подумаю"],
    "irritated": ["блин", "да ну", "че за"],
    "laugh": ["ахаха", "хахах", "ну ты даешь", "жесть"],
}


def _pick(items: list[str]) -> str:
    return random.choice(items)


def _choose_interjection(has_question: bool, has_laugh: bool, has_swear: bool) -> str:
    if has_laugh:
        return _pick(INTERJECTION_GROUPS["laugh"])
    if has_swear:
        return _pick(INTERJECTION_GROUPS["irritated"])
    if has_question and random.random() < 0.5:
        return _pick(INTERJECTION_GROUPS["think"])
    return _pick(INTERJECTION_GROUPS["neutral"] + INTERJECTION_GROUPS["agree"])


def _dedupe_openers(text: str) -> str:
    opener = text.split(" ", 1)[0].lower()
    if _last_openers and opener == _last_openers[-1]:
        alternatives = [x for g in INTERJECTION_GROUPS.values() for x in g if x.split(" ", 1)[0].lower() != opener]
        if alternatives:
            swapped = _pick(alternatives)
            rest = text.split(" ", 1)[1] if " " in text else ""
            text = f"{swapped} {rest}".strip()
            opener = swapped.split(" ", 1)[0].lower()
    _last_openers.append(opener)
    if len(_last_openers) > 8:
        del _last_openers[0]
    return text


def humanize(text: str) -> list[str]:
    value = (text or "").strip()
    if not value:
        return []

    lower = value.lower()
    has_question = "?" in value
    has_laugh = "ахах" in lower or "хаха" in lower or "ahaha" in lower
    has_swear = bool(re.search(r"\b(бля|блин|сука|нах|хер|черт|чёрт|еб|пизд|хуй)\w*\b", lower))

    # Keep voice natural and readable, avoid broken-speech templates.
    value = re.sub(r"\s+", " ", value).strip()
    if random.random() < INTERJECTION_CHANCE:
        interjection = _choose_interjection(has_question, has_laugh, has_swear)
        if not value.lower().startswith(interjection.lower() + " "):
            value = f"{interjection}, {value}"

    if random.random() < PAUSE_CHANCE:
        value = value.replace(" но ", ", но ", 1)

    value = _dedupe_openers(value)
    parts = [p.strip(" ,.") for p in re.split(r"[.!?]\s+", value) if p.strip(" ,.?!")]
    return parts[:2] if parts else [value]


async def _load_tts_model() -> tuple[Any, int]:
    global _tts_model
    if _tts_model is not None:
        return _tts_model, _tts_sample_rate

    async with _model_lock:
        if _tts_model is not None:
            return _tts_model, _tts_sample_rate
        import torch

        model, _ = torch.hub.load(
            repo_or_dir="snakers4/silero-models",
            model="silero_tts",
            language="ru",
            speaker="v4_ru",
        )
        _tts_model = model
        return _tts_model, _tts_sample_rate


def _synthesize_chunks(model: Any, chunks: list[str], sample_rate: int) -> np.ndarray:
    audio_parts: list[np.ndarray] = []
    for chunk in chunks:
        wav = model.apply_tts(text=chunk, speaker="aidar", sample_rate=sample_rate)
        wav_np = np.asarray(wav, dtype=np.float32).flatten()
        audio_parts.append(wav_np)
        pause = np.zeros(int(sample_rate * random.uniform(0.08, 0.18)), dtype=np.float32)
        audio_parts.append(pause)
    if not audio_parts:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(audio_parts)


async def maybe_send_voice_reply(
    client: Any,
    message: Any,
    text: str,
    send_voice_probability: float = 0.4,
    max_voice_seconds: float = 40.0,
    min_voice_interval_seconds: float = 25.0,
) -> bool:
    if not text.strip():
        return False
    if random.random() > send_voice_probability:
        return False

    chat_id = int(message.chat_id)
    last_sent = _last_voice_by_chat.get(chat_id, 0.0)
    if time.monotonic() - last_sent < min_voice_interval_seconds:
        return False

    chunks = humanize(text)
    if not chunks:
        return False

    async with _voice_queue_lock:
        try:
            model, sample_rate = await _load_tts_model()
            audio = await asyncio.to_thread(_synthesize_chunks, model, chunks, sample_rate)
            duration = len(audio) / float(sample_rate)
            if duration <= 0 or duration > max_voice_seconds:
                return False

            import soundfile as sf

            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            try:
                await asyncio.to_thread(sf.write, str(tmp_path), audio, sample_rate, format="OGG", subtype="VORBIS")
                await client.send_file(entity=message.chat_id, file=str(tmp_path), voice_note=True, reply_to=message.id)
                _last_voice_by_chat[chat_id] = time.monotonic()
                return True
            finally:
                if tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
        except ModuleNotFoundError as exc:
            global _tts_unavailable_logged
            if not _tts_unavailable_logged:
                logger.warning("Voice pipeline disabled: missing dependency: %s", exc)
                _tts_unavailable_logged = True
            return False
        except Exception:
            logger.exception("Voice pipeline failed, fallback to text")
            return False
