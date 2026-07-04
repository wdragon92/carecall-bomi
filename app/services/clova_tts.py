"""CLOVA Voice Premium TTS 실 구현.
POST /tts-premium/v1/tts, form-urlencoded(speaker/text/format...), 바이너리 오디오 응답."""
from __future__ import annotations

import logging

import httpx

from app.config import Settings
from app.services.base import ProviderError

log = logging.getLogger("clova_tts")
URL = "https://naveropenapi.apigw.ntruss.com/tts-premium/v1/tts"


class ClovaTTS:
    def __init__(self, settings: Settings) -> None:
        self.cid = settings.ncp_apigw_client_id.strip()
        self.csec = settings.ncp_apigw_client_secret.strip()
        self.voice = (settings.clova_tts_voice or "vmikyung").strip()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))

    async def synthesize(self, text: str) -> bytes:
        headers = {
            "X-NCP-APIGW-API-KEY-ID": self.cid,
            "X-NCP-APIGW-API-KEY": self.csec,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {
            "speaker": self.voice,
            "text": text[:1900],
            "format": "mp3",
            "speed": "1",  # 어르신 배려로 살짝 느리게 (양수=느림)
        }
        try:
            resp = await self._client.post(URL, headers=headers, data=data)
        except httpx.HTTPError as exc:
            raise ProviderError(f"TTS error: {exc}") from exc
        if resp.status_code != 200:
            raise ProviderError(f"TTS {resp.status_code}: {resp.text[:200]}")
        return resp.content
