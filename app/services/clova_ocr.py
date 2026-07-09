"""CLOVA OCR General 실 구현.
스펙: POST {INVOKE_URL}, 헤더 X-OCR-SECRET, body {version:V2, requestId, timestamp, images:[{format,name,data(base64)}]}.
응답 images[].fields[] 를 lineBreak 기준으로 재구성."""
from __future__ import annotations

import base64
import logging
import time
import uuid

import httpx

from app.config import Settings
from app.services.base import ProviderError

log = logging.getLogger("clova_ocr")
_ALLOWED = {"jpg", "jpeg", "png", "pdf", "tif", "tiff"}


def _reconstruct(data: dict) -> str:
    """images 전체(다페이지 PDF 포함)를 페이지 단위로 합친다. 저신뢰 필드는 노이즈로 컷."""
    if not isinstance(data, dict):
        return ""
    pages: list[str] = []
    for img in data.get("images") or []:
        parts: list[str] = []
        for f in img.get("fields") or []:
            conf = f.get("inferConfidence")
            if isinstance(conf, (int, float)) and conf < 0.3:
                continue
            parts.append(f.get("inferText", ""))
            parts.append("\n" if f.get("lineBreak") else " ")
        page = "".join(parts).strip()
        if page:
            pages.append(page)
    return "\n\n".join(pages)


class ClovaOCR:
    def __init__(self, settings: Settings) -> None:
        self.url = settings.clova_ocr_invoke_url.strip()
        self.secret = settings.clova_ocr_secret.strip()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))

    async def extract_text(self, image_bytes: bytes, fmt: str, name: str = "") -> str:
        fmt = (fmt or "jpg").lower().lstrip(".")
        if fmt not in _ALLOWED:
            fmt = "jpg"
        payload = {
            "version": "V2",
            "requestId": uuid.uuid4().hex,
            "timestamp": int(time.time() * 1000),
            "lang": "ko",
            "images": [
                {
                    "format": fmt,
                    "name": (name or "doc")[:64],
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                }
            ],
        }
        headers = {"X-OCR-SECRET": self.secret, "Content-Type": "application/json"}
        try:
            resp = await self._client.post(self.url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(f"CLOVA OCR error: {exc}") from exc
        if resp.status_code != 200:
            raise ProviderError(f"CLOVA OCR {resp.status_code}: {resp.text[:300]}")
        try:
            return _reconstruct(resp.json())
        except ValueError as exc:  # 200인데 본문이 JSON이 아님
            raise ProviderError(f"CLOVA OCR non-JSON response: {resp.text[:200]!r}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()
