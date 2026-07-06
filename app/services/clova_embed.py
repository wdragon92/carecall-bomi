"""CLOVA Studio 임베딩 v2 실 구현 (실습 노트북 포팅).
스펙: POST {BASE}/v1/api-tools/embedding/v2, body {"text": <문자열 1개>} — 텍스트당 1콜.
모델은 CLOVA Studio 앱에 바인딩(bge-m3 계열, 1024차원). 429는 지수 백오프 3회."""
from __future__ import annotations

import asyncio
import logging
import uuid

import httpx

from app.config import Settings
from app.services.base import ProviderError

log = logging.getLogger("clova_embed")
BASE = "https://clovastudio.stream.ntruss.com"


class ClovaEmbed:
    def __init__(self, settings: Settings) -> None:
        self.key = settings.clova_studio_api_key.strip()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=10.0))

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.key}",
            "X-NCP-CLOVASTUDIO-REQUEST-ID": uuid.uuid4().hex,
            "Content-Type": "application/json",
        }

    async def _embed_one(self, text: str) -> list[float]:
        url = f"{BASE}/v1/api-tools/embedding/v2"
        delay = 1.0
        for _ in range(3):
            try:
                resp = await self._client.post(url, headers=self._headers(), json={"text": text})
            except httpx.HTTPError as exc:
                raise ProviderError(f"CLOVA embed error: {exc}") from exc
            if resp.status_code == 429:  # rate limit → 백오프 후 재시도
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if resp.status_code != 200:
                raise ProviderError(f"CLOVA embed {resp.status_code}: {resp.text[:200]}")
            try:
                emb = (resp.json().get("result") or {}).get("embedding")
            except ValueError as exc:
                raise ProviderError(f"CLOVA embed non-JSON: {resp.text[:200]!r}") from exc
            if not emb:
                raise ProviderError(f"CLOVA embed empty: {resp.text[:200]}")
            return emb
        raise ProviderError("CLOVA embed rate-limited (retries exhausted)")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [await self._embed_one(t) for t in texts]
