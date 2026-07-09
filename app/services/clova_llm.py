"""CLOVA Studio (HyperCLOVA X) 실 구현 — Chat Completions v3.
스펙: POST {BASE}/v3/chat-completions/{model}, Bearer 인증.
모델 라우팅: chat()=HCX-005(빠른 대화), extract_json()=HCX-007(분석·reasoning)."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid

import httpx

from app.config import Settings
from app.services.base import ProviderError

log = logging.getLogger("clova_llm")
BASE = "https://clovastudio.stream.ntruss.com"

# reasoning(추론) 계열 모델 — 토큰 파라미터가 maxTokens가 아닌 maxCompletionTokens이고,
# thinking effort로 추론량(지연)을 조절한다. 그 외(HCX-005 등 채팅 모델)는 maxTokens.
_REASONING_PREFIXES = ("HCX-007",)


def _is_reasoning(model: str) -> bool:
    return (model or "").strip().upper().startswith(_REASONING_PREFIXES)


def _parse_json(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t[:4].lower() == "json":
            t = t[4:]
    i, j = t.find("{"), t.rfind("}")
    if i != -1 and j != -1 and j > i:
        t = t[i : j + 1]
    return json.loads(t)


class ClovaLLM:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.model = (settings.clova_llm_model or "HCX-007").strip()       # 분석용(reasoning)
        self.chat_model = (settings.clova_chat_model or "HCX-005").strip()  # 채팅용(빠름)
        self.key = settings.clova_studio_api_key.strip()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.key}",
            "X-NCP-CLOVASTUDIO-REQUEST-ID": uuid.uuid4().hex,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _body(self, messages: list[dict], model: str, **opts) -> dict:
        body = {
            "messages": messages,
            "temperature": float(opts.get("temperature", 0.5)),
            "topP": float(opts.get("top_p", 0.8)),
            "repetitionPenalty": float(opts.get("repetition_penalty", 1.1)),
        }
        max_tokens = int(opts.get("max_tokens", 1024))
        if _is_reasoning(model):
            # reasoning 계열: 토큰 파라미터명이 다르고, thinking effort로 추론량(지연) 조절.
            # ARCHITECTURE §8.2/§15 "thinking 최소" — 기본 low(추출 품질 유지하며 지연 절감).
            body["maxCompletionTokens"] = max_tokens
            body["thinking"] = {"effort": opts.get("thinking_effort") or "low"}
        else:
            body["maxTokens"] = max_tokens
        if opts.get("stop"):
            body["stop"] = opts["stop"]
        return body

    async def chat(self, messages: list[dict], *, model: str | None = None, **opts) -> str:
        target = model or self.chat_model
        url = f"{BASE}/v3/chat-completions/{target}"
        body = self._body(messages, target, **opts)
        delay = 1.0
        for _ in range(4):  # 429 지수 백오프 최대 ~15초 (채팅은 지연 민감 → embed보다 짧게)
            try:
                resp = await self._client.post(url, headers=self._headers(), json=body)
            except httpx.HTTPError as exc:
                raise ProviderError(f"CLOVA LLM error: {exc}") from exc
            if resp.status_code == 429:  # rate limit → 백오프 후 재시도
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if resp.status_code != 200:
                raise ProviderError(f"CLOVA LLM {resp.status_code}: {resp.text[:300]}")
            try:
                data = resp.json()
            except ValueError as exc:  # 200인데 본문이 JSON이 아님
                raise ProviderError(f"CLOVA LLM non-JSON response: {resp.text[:200]!r}") from exc
            result = (data.get("result") or {}) if isinstance(data, dict) else {}
            content = (result.get("message") or {}).get("content", "") or ""
            if not content:
                # 빈 content 진단: reasoning이 토큰 소진 시 finishReason=length로 끊겨 content가 빔
                finish = result.get("finishReason")
                log.warning("CLOVA LLM empty content (finishReason=%s): %s", finish, resp.text[:200])
                raise ProviderError(
                    f"CLOVA LLM empty response (finishReason={finish}): {resp.text[:200]}"
                )
            return content
        raise ProviderError("CLOVA LLM rate-limited (retries exhausted)")

    async def extract_json(self, messages: list[dict], schema: dict) -> dict:
        # 분석용 reasoning 모델 사용 (thinking 여유 위해 토큰 넉넉히).
        # 파싱 실패 시 1회 재시도 (ARCHITECTURE §7.2 "실패 1회 재시도").
        err = ""
        for attempt in range(2):
            text = await self.chat(
                messages, model=self.model, temperature=0.1, top_p=0.8, max_tokens=2048
            )
            try:
                return _parse_json(text)
            except (json.JSONDecodeError, ValueError) as exc:
                err = f"{exc}: {text[:200]!r}"
                log.warning("CLOVA LLM JSON parse failed (attempt %d/2): %s", attempt + 1, exc)
        raise ProviderError(f"CLOVA LLM JSON parse failed: {err}")

    async def aclose(self) -> None:
        await self._client.aclose()
