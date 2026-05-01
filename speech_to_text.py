import asyncio
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("telegram-agent.stt")

_whisper_model: Any = None
_whisper_lock = asyncio.Lock()


async def _load_whisper_model() -> Any:
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model
    async with _whisper_lock:
        if _whisper_model is not None:
            return _whisper_model
        from faster_whisper import WhisperModel

        _whisper_model = WhisperModel("small", device="cpu", compute_type="int8")
        return _whisper_model


def _transcribe_sync(model: Any, audio_path: str) -> str:
    segments, _ = model.transcribe(audio_path, language="ru", vad_filter=True)
    parts = []
    for segment in segments:
        text = (getattr(segment, "text", "") or "").strip()
        if text:
            parts.append(text)
    return " ".join(parts).strip()


async def transcribe_voice_message(message: Any, target_dir: Path) -> str:
    if not message or not getattr(message, "voice", None):
        return ""
    target_dir.mkdir(parents=True, exist_ok=True)
    downloaded = await message.download_media(file=target_dir)
    if not downloaded:
        return ""
    model = await _load_whisper_model()
    try:
        return await asyncio.to_thread(_transcribe_sync, model, str(downloaded))
    except Exception:
        logger.exception("Whisper transcription failed")
        return ""
