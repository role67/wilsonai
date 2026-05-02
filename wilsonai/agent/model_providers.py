"""
Унифицированные клиенты для всех AI провайдеров.
Поддержка: Groq, Google Gemini, Reka AI, Kiro AI (Claude).
"""

import asyncio
import logging
from typing import Any, Optional
from dataclasses import dataclass

import httpx

from exceptions import (
    ModelConnectionError,
    ModelRateLimitError,
    ModelTimeoutError,
    ModelInvalidResponseError,
)

logger = logging.getLogger("telegram-agent.providers")


@dataclass
class ModelResponse:
    """Ответ от модели."""
    text: str
    model: str
    provider: str
    tokens_used: Optional[int] = None


class BaseProvider:
    """Базовый класс для провайдеров."""
    
    def __init__(self, api_key: str, timeout: float = 90.0, proxy: Optional[str] = None):
        self.api_key = api_key
        self.timeout = timeout
        self.proxy = proxy
        self.metrics = {
            "requests": 0,
            "errors": 0,
            "rate_limits": 0,
        }
    
    async def _request(
        self,
        url: str,
        payload: dict[str, Any],
        headers: Optional[dict[str, str]] = None,
        retries: int = 3,
    ) -> dict[str, Any]:
        """Базовый HTTP запрос с retry логикой."""
        self.metrics["requests"] += 1
        
        for attempt in range(1, retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout, proxy=self.proxy) as client:
                    response = await client.post(url, json=payload, headers=headers)
                    
                    if response.status_code == 429:
                        self.metrics["rate_limits"] += 1
                        retry_after = int(response.headers.get("Retry-After", 60))
                        logger.warning(f"Rate limit hit, retry after {retry_after}s")
                        if attempt < retries:
                            await asyncio.sleep(min(retry_after, 120))
                            continue
                        raise ModelRateLimitError(f"Rate limit exceeded, retry after {retry_after}s")
                    
                    if response.status_code in {500, 502, 503, 504}:
                        logger.warning(f"Server error {response.status_code}, attempt {attempt}/{retries}")
                        if attempt < retries:
                            await asyncio.sleep(2 ** attempt)
                            continue
                        raise ModelConnectionError(f"Server error: {response.status_code}")
                    
                    if response.status_code >= 400:
                        self.metrics["errors"] += 1
                        raise ModelConnectionError(f"HTTP {response.status_code}: {response.text[:200]}")
                    
                    return response.json()
                    
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                logger.warning(f"Connection error, attempt {attempt}/{retries}: {e}")
                if attempt < retries:
                    await asyncio.sleep(2 ** attempt)
                    continue
                self.metrics["errors"] += 1
                raise ModelConnectionError(f"Connection failed: {e}")
            
            except httpx.ReadTimeout as e:
                logger.warning(f"Timeout, attempt {attempt}/{retries}")
                if attempt < retries:
                    await asyncio.sleep(2 ** attempt)
                    continue
                self.metrics["errors"] += 1
                raise ModelTimeoutError(f"Request timeout: {e}")
        
        raise ModelConnectionError("All retry attempts failed")


class GroqProvider(BaseProvider):
    """Groq API провайдер."""
    
    BASE_URL = "https://api.groq.com/openai/v1/chat/completions"
    
    async def generate(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 1600,
    ) -> ModelResponse:
        """Генерация ответа через Groq."""
        payload = {
            "model": model.replace("groq/", ""),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        
        data = await self._request(self.BASE_URL, payload, headers)
        
        try:
            text = data["choices"][0]["message"]["content"].strip()
            tokens = data.get("usage", {}).get("total_tokens")
            return ModelResponse(text=text, model=model, provider="groq", tokens_used=tokens)
        except (KeyError, IndexError) as e:
            raise ModelInvalidResponseError(f"Invalid Groq response: {e}")


class GeminiProvider(BaseProvider):
    """Google Gemini API провайдер."""
    
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
    
    async def generate(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 1600,
        image_part: Optional[dict[str, Any]] = None,
    ) -> ModelResponse:
        """Генерация ответа через Gemini."""
        model_name = model.replace("gemini/", "")
        url = f"{self.BASE_URL}/{model_name}:generateContent?key={self.api_key}"
        
        # Конвертация OpenAI формата в Gemini формат
        contents = []
        system_instruction = None
        
        for msg in messages:
            if msg["role"] == "system":
                system_instruction = {"parts": [{"text": msg["content"]}]}
            else:
                role = "model" if msg["role"] == "assistant" else "user"
                parts = [{"text": msg["content"]}]
                
                # Добавить изображение к последнему user сообщению
                if role == "user" and image_part and msg == messages[-1]:
                    parts.append(image_part)
                
                contents.append({"role": role, "parts": parts})
        
        payload = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        
        if system_instruction:
            payload["systemInstruction"] = system_instruction
        
        data = await self._request(url, payload)
        
        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts).strip()
            
            if not text:
                raise ModelInvalidResponseError("Empty response from Gemini")
            
            tokens = data.get("usageMetadata", {}).get("totalTokenCount")
            return ModelResponse(text=text, model=model, provider="gemini", tokens_used=tokens)
        except (KeyError, IndexError) as e:
            raise ModelInvalidResponseError(f"Invalid Gemini response: {e}")


class RekaProvider(BaseProvider):
    """Reka AI провайдер (для изображений)."""
    
    BASE_URL = "https://api.reka.ai/v1/chat"
    
    async def generate(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 1600,
        image_url: Optional[str] = None,
    ) -> ModelResponse:
        """Генерация ответа через Reka AI."""
        # Reka использует свой формат для изображений
        reka_messages = []
        for msg in messages:
            content = msg["content"]
            
            # Добавить изображение к последнему user сообщению
            if msg["role"] == "user" and image_url and msg == messages[-1]:
                content = [
                    {"type": "text", "text": msg["content"]},
                    {"type": "image_url", "image_url": image_url},
                ]
            
            reka_messages.append({
                "role": msg["role"],
                "content": content,
            })
        
        payload = {
            "messages": reka_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        
        data = await self._request(self.BASE_URL, payload, headers)
        
        try:
            text = data["choices"][0]["message"]["content"].strip()
            tokens = data.get("usage", {}).get("total_tokens")
            return ModelResponse(text=text, model="reka-ai", provider="reka", tokens_used=tokens)
        except (KeyError, IndexError) as e:
            raise ModelInvalidResponseError(f"Invalid Reka response: {e}")


class KiroProvider(BaseProvider):
    """Kiro AI провайдер (Claude через Kiro)."""
    
    BASE_URL = "https://api.kiro.ai/v1/chat/completions"
    
    async def generate(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 1600,
    ) -> ModelResponse:
        """Генерация ответа через Kiro AI (Claude)."""
        payload = {
            "model": model.replace("kr/", ""),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        
        data = await self._request(self.BASE_URL, payload, headers)
        
        try:
            text = data["choices"][0]["message"]["content"].strip()
            tokens = data.get("usage", {}).get("total_tokens")
            return ModelResponse(text=text, model=model, provider="kiro", tokens_used=tokens)
        except (KeyError, IndexError) as e:
            raise ModelInvalidResponseError(f"Invalid Kiro response: {e}")
