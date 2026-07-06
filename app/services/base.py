"""Provider 추상 인터페이스 5종 (services §7.1)."""
from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable


class ProviderError(Exception):
    """외부 API 호출 실패를 감싸는 예외."""


@runtime_checkable
class LLMProvider(Protocol):
    def chat_stream(self, messages: list[dict], **opts) -> AsyncIterator[str]: ...
    async def chat(self, messages: list[dict], **opts) -> str: ...
    async def extract_json(self, messages: list[dict], schema: dict) -> dict: ...


@runtime_checkable
class STTProvider(Protocol):
    async def transcribe(self, wav_bytes: bytes) -> str: ...


@runtime_checkable
class TTSProvider(Protocol):
    async def synthesize(self, text: str) -> bytes: ...


@runtime_checkable
class OCRProvider(Protocol):
    async def extract_text(self, image_bytes: bytes, fmt: str, name: str = "") -> str: ...


@runtime_checkable
class EmbedProvider(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...
