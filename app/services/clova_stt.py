"""CLOVA Speech Recognition (CSR) 짧은문장 STT 실 구현.
POST /recog/v1/stt?lang=Kor, octet-stream 오디오, 응답 {"text": ...}."""
from __future__ import annotations

import logging

import httpx

from app.config import Settings
from app.services.base import ProviderError

log = logging.getLogger("clova_stt")
URL = "https://naveropenapi.apigw.ntruss.com/recog/v1/stt"


class ClovaSTT:
    def __init__(self, settings: Settings) -> None:
        self.cid = settings.ncp_apigw_client_id.strip()
        self.csec = settings.ncp_apigw_client_secret.strip()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))

    async def transcribe(self, wav_bytes: bytes) -> str:
        headers = {
            "X-NCP-APIGW-API-KEY-ID": self.cid,
            "X-NCP-APIGW-API-KEY": self.csec,
            "Content-Type": "application/octet-stream",
        }
        try:
            resp = await self._client.post(
                URL, params={"lang": "Kor"}, headers=headers, content=wav_bytes
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"CSR error: {exc}") from exc
        if resp.status_code != 200:
            raise ProviderError(f"CSR {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError(f"CSR non-JSON response: {resp.text[:200]!r}") from exc
        return ((data.get("text") if isinstance(data, dict) else "") or "").strip()

    async def aclose(self) -> None:
        await self._client.aclose()
