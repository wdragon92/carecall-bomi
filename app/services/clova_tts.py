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
        self.voice = (settings.clova_tts_voice or "vgoeun").strip()
        self.speed = int(getattr(settings, "clova_tts_speed", -2))
        # 의문문(?로 끝남) 끝음 올리기 — "잘 안 오세요?"가 평서문처럼 내려가는 문제 보정
        self.q_pitch = int(getattr(settings, "clova_tts_question_pitch", 0))
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))

    async def synthesize(self, text: str) -> bytes:
        headers = {
            "X-NCP-APIGW-API-KEY-ID": self.cid,
            "X-NCP-APIGW-API-KEY": self.csec,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        t = text[:1900]
        data = {
            "speaker": self.voice,
            "text": t,
            "format": "mp3",
            "speed": str(self.speed),  # -5(빠름)~10(느림), 0=기본
        }
        if self.q_pitch and t.rstrip().endswith("?"):
            data["end-pitch"] = str(self.q_pitch)  # -5~5, +면 문장 끝을 올림
        try:
            resp = await self._client.post(URL, headers=headers, data=data)
            if resp.status_code != 200 and "end-pitch" in data:
                # 일부 화자는 end-pitch 미지원 → 제거하고 1회 재시도 (mock 폴백으로 넘기지 않음)
                data.pop("end-pitch")
                resp = await self._client.post(URL, headers=headers, data=data)
        except httpx.HTTPError as exc:
            raise ProviderError(f"TTS error: {exc}") from exc
        if resp.status_code != 200:
            raise ProviderError(f"TTS {resp.status_code}: {resp.text[:200]}")
        return resp.content

    async def aclose(self) -> None:
        await self._client.aclose()
