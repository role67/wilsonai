import asyncio
import json
import logging
from typing import Any

import httpx

from wilsonai.agent.prompts import ACCOUNT_TOOLS_PROMPT, AUTONOMY_PROMPT
from wilsonai.core.config import settings
from wilsonai.agent.storage import load_history, read_system_prompt
from wilsonai.core.exceptions import (
    ModelConnectionError,
    ModelRateLimitError,
    ModelTimeoutError,
    ModelInvalidResponseError,
)

logger = logging.getLogger("telegram-agent.model")


def build_system_prompt(can_manage_account: bool) -> str:
    system_prompt = read_system_prompt()
    parts = [system_prompt] if system_prompt else []
    if can_manage_account:
        parts.append(ACCOUNT_TOOLS_PROMPT)
        if settings.autonomous_actions_enabled:
            parts.append(AUTONOMY_PROMPT)
    return "\n\n".join(parts)


def build_contents(chat_id: int, user_text: str) -> list[dict[str, Any]]:
    history = load_history(chat_id)

    contents: list[dict[str, Any]] = []
    for item in history:
        role = "model" if item["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": item["content"]}]})
    contents.append({"role": "user", "parts": [{"text": user_text}]})
    return contents


def append_image_to_contents(
    contents: list[dict[str, Any]], image_part: dict[str, Any] | None
) -> list[dict[str, Any]]:
    if image_part:
        contents[-1]["parts"].append(image_part)
    return contents


def build_openai_messages(chat_id: int, user_text: str, system_prompt: str) -> list[dict[str, str]]:
    history = load_history(chat_id)
    msgs: list[dict[str, str]] = []
    if system_prompt:
        msgs.append({"role": "system", "content": system_prompt})
    # Keep payload small for providers with stricter body limits (e.g., Groq).
    trimmed = history[-16:]
    total_chars = 0
    compact: list[dict[str, Any]] = []
    for item in reversed(trimmed):
        content = str(item.get("content", ""))
        total_chars += len(content)
        compact.append(item)
        if total_chars >= 12000:
            break
    for item in reversed(compact):
        role = "assistant" if item["role"] == "assistant" else "user"
        text = str(item["content"])
        if len(text) > 1800:
            text = text[:1800]
        msgs.append({"role": role, "content": text})
    msgs.append({"role": "user", "content": user_text})
    return msgs


def model_candidates() -> tuple[str, ...]:
    models: list[str] = []
    for model in (settings.model, *settings.fallback_models):
        if model and model not in models:
            models.append(model)
    return tuple(models[: settings.max_model_fallbacks])


def google_error_message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except json.JSONDecodeError:
        return response.text[:1000]

    error = data.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if message:
            return str(message)

    return response.text[:1000]


async def ask_model_legacy(
    chat_id: int,
    user_text: str,
    can_manage_account: bool,
    image_part: dict[str, Any] | None = None,
) -> str:
    if settings.model_provider in {"deepseek", "cerebras", "groq"}:
        providers = [settings.model_provider]
        if settings.fallback_provider and settings.fallback_provider not in providers:
            providers.append(settings.fallback_provider)
        last_exc: Exception | None = None
        for provider in providers:
            try:
                return await ask_model_openai_compatible(chat_id, user_text, can_manage_account, provider)
            except Exception as exc:
                last_exc = exc
                logger.warning("%s provider failed: %s", provider, exc)
        raise ModelConnectionError("Все настроенные провайдеры сейчас недоступны") from last_exc

    contents = append_image_to_contents(build_contents(chat_id, user_text), image_part)
    system_prompt = build_system_prompt(can_manage_account)
    last_error: Exception | None = None
    rate_limited_models: list[str] = []
    overloaded_models: list[str] = []
    unavailable_models: list[str] = []
    temporary_error_models: list[str] = []
    tried_models: list[str] = []

    payload: dict[str, Any] = {
        "contents": contents,
        "generationConfig": {
            "temperature": settings.temperature,
            "maxOutputTokens": settings.max_tokens,
        },
    }
    if system_prompt:
        payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}

    for model in model_candidates():
        tried_models.append(model)
        url = f"{settings.google_api_url}/models/{model}:generateContent"
        response: httpx.Response | None = None
        for attempt in range(1, settings.google_retries + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=settings.google_timeout,
                    proxy=settings.google_proxy,
                ) as http:
                    response = await http.post(
                        url,
                        params={"key": settings.google_api_key},
                        json=payload,
                    )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
                last_error = exc
                logger.warning(
                    "Google Gemini connection attempt %s/%s failed for %s: %s",
                    attempt,
                    settings.google_retries,
                    model,
                    exc,
                )
                if attempt < settings.google_retries:
                    await asyncio.sleep(1.5 * attempt)
                continue
            except httpx.HTTPError as exc:
                last_error = exc
                logger.warning("Google Gemini request failed for %s: %s", model, exc)
                break

            if response.status_code == 429:
                rate_limited_models.append(model)
                break

            if response.status_code == 503:
                overloaded_models.append(model)
                retry_after = response.headers.get("Retry-After")
                if attempt < settings.google_retries:
                    if retry_after and retry_after.isdigit():
                        await asyncio.sleep(min(int(retry_after), 12))
                    else:
                        await asyncio.sleep(min(2.0 * attempt, 8.0))
                    continue
                break

            if response.status_code in {500, 502, 504}:
                temporary_error_models.append(model)
                logger.warning(
                    "Google Gemini model %s returned temporary error %s: %s",
                    model,
                    response.status_code,
                    google_error_message(response),
                )
                if attempt < settings.google_retries:
                    await asyncio.sleep(1.5 * attempt)
                    continue
                break

            if response.status_code == 404:
                unavailable_models.append(model)
                break

            if image_part and response.status_code in {400, 404, 422}:
                logger.warning("Google Gemini rejected image input for %s: %s", model, google_error_message(response))
                return "РІРёР¶Сѓ, С‡С‚Рѕ С‚СѓС‚ РєР°СЂС‚РёРЅРєР°, РЅРѕ СЃРµР№С‡Р°СЃ РЅРµ РјРѕРіСѓ РµС‘ РЅРѕСЂРјР°Р»СЊРЅРѕ СЂР°СЃСЃРјРѕС‚СЂРµС‚СЊ. СЃРєР°Р¶Рё С‡С‚Рѕ СЃ РЅРµР№ СЃРґРµР»Р°С‚СЊ?"

            if response.status_code >= 400:
                raise RuntimeError(
                    f"Google Gemini error {response.status_code}: {google_error_message(response)}"
                )

            data = response.json()
            parts = (
                data.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [])
            )
            content = "".join(str(part.get("text", "")) for part in parts).strip()
            if not content:
                logger.warning("Gemini returned empty response for %s, trying next fallback", model)
                temporary_error_models.append(model)
                break
            if len(tried_models) > 1 and (rate_limited_models or overloaded_models or unavailable_models):
                logger.info(
                    "Gemini fallback: selected=%s, tried=%s, limited=%d, overloaded=%d, unavailable=%d",
                    model,
                    ",".join(tried_models),
                    len(rate_limited_models),
                    len(overloaded_models),
                    len(unavailable_models),
                )
            return content

    if rate_limited_models:
        raise ModelRateLimitError(
            f"Google Gemini РІСЂРµРјРµРЅРЅРѕ РѕРіСЂР°РЅРёС‡РёР» РјРѕРґРµР»Рё: {', '.join(rate_limited_models)}. РџРѕРґРѕР¶РґРё РЅРµРјРЅРѕРіРѕ."
        )

    if overloaded_models:
        raise ModelConnectionError(
            f"Google Gemini СЃРµР№С‡Р°СЃ РїРµСЂРµРіСЂСѓР¶РµРЅ: {', '.join(overloaded_models)}. РџРѕРїСЂРѕР±СѓР№ С‡СѓС‚СЊ РїРѕР·Р¶Рµ."
        )

    if unavailable_models:
        raise ModelConnectionError(
            f"Google Gemini РЅРµ РЅР°С€РµР» РґРѕСЃС‚СѓРїРЅС‹Рµ РјРѕРґРµР»Рё РёР· СЃРїРёСЃРєР°: {', '.join(unavailable_models)}. РћР±РЅРѕРІРё GOOGLE_MODEL/GOOGLE_FALLBACK_MODELS РІ .env."
        )

    if temporary_error_models:
        raise ModelConnectionError(
            f"Google Gemini РІСЂРµРјРµРЅРЅРѕ РѕС€РёР±СЃСЏ РЅР° РјРѕРґРµР»СЏС…: {', '.join(temporary_error_models)}. РџРѕРїСЂРѕР±СѓР№ С‡СѓС‚СЊ РїРѕР·Р¶Рµ."
        )

    raise ModelConnectionError(
        "РќРµ СѓРґР°Р»РѕСЃСЊ РїРѕРґРєР»СЋС‡РёС‚СЊСЃСЏ Рє Google Gemini. РџСЂРѕРІРµСЂСЊ РёРЅС‚РµСЂРЅРµС‚/VPN/РїСЂРѕРєСЃРё РёР»Рё Р·Р°РґР°Р№ GOOGLE_PROXY РІ .env."
    ) from last_error


def provider_config(provider: str) -> tuple[str | None, str]:
    if provider == "deepseek":
        return settings.deepseek_api_key, settings.deepseek_api_url
    if provider == "cerebras":
        return settings.cerebras_api_key, settings.cerebras_api_url
    if provider == "groq":
        return settings.groq_api_key, settings.groq_api_url
    return None, ""


async def ask_model_openai_compatible(chat_id: int, user_text: str, can_manage_account: bool, provider: str) -> str:
    api_key, api_url = provider_config(provider)
    if not api_key:
        raise ModelConnectionError(f"{provider}: API key не задан в .env")
    system_prompt = build_system_prompt(can_manage_account)
    messages = build_openai_messages(chat_id, user_text, system_prompt)
    tried_models: list[str] = []
    last_error: Exception | None = None

    for model in model_candidates():
        tried_models.append(model)
        for attempt in range(1, settings.google_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=settings.google_timeout, proxy=settings.google_proxy) as http:
                    response = await http.post(
                        f"{api_url}/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}"},
                        json={
                            "model": model,
                            "messages": messages,
                            "temperature": settings.temperature,
                            "max_tokens": settings.max_tokens,
                        },
                    )
                if response.status_code in {429, 503, 529}:
                    if attempt < settings.google_retries:
                        await asyncio.sleep(min(2.0 * attempt, 8.0))
                        continue
                    break
                if response.status_code == 413:
                    # Payload too large: drop history to minimum and retry quickly.
                    messages = [messages[0], messages[-1]] if len(messages) > 2 and messages[0]["role"] == "system" else [messages[-1]]
                    if attempt < settings.google_retries:
                        await asyncio.sleep(0.5)
                        continue
                    break
                if response.status_code == 402:
                    raise ModelConnectionError(f"{provider}: баланс/доступ неактивен (402)")
                if response.status_code >= 400:
                    break
                data = response.json()
                content = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                ).strip()
                if content:
                    return content
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
                last_error = exc
                if attempt < settings.google_retries:
                    await asyncio.sleep(1.5 * attempt)
                    continue
                break
            except Exception as exc:
                last_error = exc
                break
    raise ModelConnectionError(f"{provider} недоступен на моделях: {', '.join(tried_models)}") from last_error

