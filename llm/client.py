"""Pluggable LLM client interface with OpenAI and Anthropic implementations."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod

import structlog

from core.config import get_settings

logger = structlog.get_logger(__name__)


class LLMClient(ABC):
    """Abstract LLM client interface."""

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 2000,
        temperature: float = 0.3,
    ) -> str:
        """Generate a text completion."""
        ...

    @abstractmethod
    async def generate_json(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 2000,
    ) -> dict:
        """Generate a JSON-structured response."""
        ...


class OpenAIClient(LLMClient):
    """OpenAI API client (GPT-4o, etc.)."""

    def __init__(self):
        settings = get_settings()
        try:
            import openai
            self.client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
        except ImportError:
            raise ImportError("Install openai: pip install openai")
        self.model = settings.llm_model or "gpt-4o"

    async def generate(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 2000,
        temperature: float = 0.3,
    ) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return response.choices[0].message.content or ""

    async def generate_json(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 2000,
    ) -> dict:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({
            "role": "user",
            "content": prompt + "\n\nRespond with valid JSON only.",
        })

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        return json.loads(response.choices[0].message.content or "{}")


class AnthropicClient(LLMClient):
    """Anthropic Claude API client."""

    def __init__(self):
        settings = get_settings()
        try:
            import anthropic
            self.client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        except ImportError:
            raise ImportError("Install anthropic: pip install anthropic")
        self.model = settings.llm_model or "claude-sonnet-4-20250514"

    async def generate(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 2000,
        temperature: float = 0.3,
    ) -> str:
        message = await self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system or "You are a helpful assistant.",
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text

    async def generate_json(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 2000,
    ) -> dict:
        result = await self.generate(
            prompt=prompt + "\n\nRespond with valid JSON only. No markdown, no code blocks.",
            system=system,
            max_tokens=max_tokens,
            temperature=0.1,
        )
        # Strip potential markdown wrapping
        result = result.strip()
        if result.startswith("```"):
            lines = result.split("\n")
            if lines[-1].strip() == "```":
                result = "\n".join(lines[1:-1])
            else:
                result = "\n".join(lines[1:])
        return json.loads(result)


def get_llm_client() -> LLMClient:
    """Factory — returns the LLM client configured via LLM_PROVIDER env var."""
    settings = get_settings()
    if settings.llm_provider == "anthropic":
        return AnthropicClient()
    elif settings.llm_provider == "openai":
        return OpenAIClient()
    else:
        raise ValueError(f"Unsupported LLM provider: {settings.llm_provider}")
