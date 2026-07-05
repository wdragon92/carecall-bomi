"""채팅/OCR 오케스트레이션 (conversation §8).
전화 통화하듯 '문장 단위 말풍선 여러 개'로 나눠 보내고, 짧은 호응은 자연스럽게 이어간다.
real 실패 시 mock 폴백. 모든 WS 전송은 sess.send()로 직렬화된다."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime

from app.core import prompts, welfare
from app.services.base import ProviderError

log = logging.getLogger("conv")

# 짧은 호응(맞장구) — 이런 입력엔 새 질문 대신 이야기를 이어감
BACKCHANNELS = {
    "응", "응응", "어", "어어", "엉", "네", "넵", "예", "그래", "그러게", "그렇구나",
    "그러네", "맞아", "맞아요", "음", "으음", "글쎄", "그럼", "그치", "응그래", "그래서",
    "알겠어", "알겠어요", "고마워", "고마워요", "아니", "아니요", "괜찮아", "괜찮아요",
}


def _period_now() -> str:
    return prompts.period_of_hour(datetime.now().hour)


def _is_backchannel(text: str) -> bool:
    t = re.sub(r"[.!?~,…\s]+", "", text or "")
    return bool(t) and len(t) <= 5 and t in BACKCHANNELS


def _sentences(t: str) -> list[str]:
    parts = re.split(r"(?<=[.?!。])\s+", t.strip())
    return [p.strip() for p in parts if p.strip()]


def _segments(text: str) -> list[str]:
    """LLM 응답을 말풍선 단위로 분리. 각 블록을 문장 단위로 쪼개되,
    긴 정보성 답(복지 안내 등, 220자 초과)은 한 말풍선으로 유지한다."""
    text = (text or "").strip()
    if not text:
        return []
    out: list[str] = []
    for block in re.split(r"\n\s*\n|\n", text):
        block = block.strip()
        if not block:
            continue
        if len(block) > 220:  # 긴 정보성(복지 등)은 통째로 한 말풍선
            out.append(block)
        else:
            out.extend(_sentences(block) or [block])
    merged: list[str] = []
    for s in out:
        if merged and len(s) < 6:
            merged[-1] += " " + s
        else:
            merged.append(s)
    return merged[:6]


async def _typing(sess, on: bool) -> None:
    await sess.send({"type": "ai_typing", "on": on})


async def _speak(sess, providers, messages, max_tokens: int = 240, single: bool = False) -> str:
    """AI 응답을 받아 말풍선 여러 개로 나눠 순차 전송(타이핑 + 간격). 전체 텍스트 반환."""
    await _typing(sess, True)
    full = ""
    try:
        full = await providers.llm.chat(messages, max_tokens=max_tokens, temperature=0.55, top_p=0.8)
        if not full.strip():
            raise ProviderError("empty response")
    except ProviderError as exc:
        log.warning("chat real failed (%s) → mock", exc)
        try:
            full = await providers.mllm.chat(messages, max_tokens=max_tokens)
        except Exception as exc2:  # noqa: BLE001
            log.error("mock chat failed too: %s", exc2)
            full = "죄송해요, 지금 잠시 문제가 있었어요. 다시 한 번 말씀해 주시겠어요?"

    segs = [full.strip()] if single else _segments(full)
    if not segs:
        segs = ["네, 말씀 듣고 있어요."]

    await _typing(sess, False)
    for i, seg in enumerate(segs):
        if i > 0:
            await _typing(sess, True)
            await asyncio.sleep(min(1.1, 0.45 + len(seg) * 0.015))
            await _typing(sess, False)
        msg = sess.add_message("assistant", seg)
        await sess.send({"type": "ai_message_start", "id": msg.id})
        await sess.send({"type": "ai_message_delta", "id": msg.id, "text": seg})
        await sess.send({"type": "ai_message_end", "id": msg.id, "full_text": seg})
    return full


def _spawn_extract(sess, providers) -> None:
    try:
        from app.core.extraction import trigger_extract
    except ImportError:
        return
    sess.spawn(trigger_extract(sess, providers))


async def greet(sess) -> None:
    text = prompts.greeting(_period_now())
    msg = sess.add_message("assistant", text, via="system")
    await sess.send({"type": "ai_message_start", "id": msg.id})
    await sess.send({"type": "ai_message_delta", "id": msg.id, "text": text})
    await sess.send({"type": "ai_message_end", "id": msg.id, "full_text": text})


async def handle_turn(sess, providers, settings) -> None:
    last = sess.messages[-1] if sess.messages else None
    bc = bool(last and last.role == "user" and _is_backchannel(last.text))
    system = prompts.chat_system(welfare.get_digest(), backchannel=bc)
    messages = [{"role": "system", "content": system}] + sess.history_for_llm()
    await _speak(sess, providers, messages, max_tokens=240)
    _spawn_extract(sess, providers)  # 비동기 추출


async def handle_image(sess, providers, image_bytes: bytes, fmt: str, name: str, upload_id: str) -> None:
    """이미지 → OCR → 쉬운 말 설명 + 사기 판별 → 특이사항 반영. 이미지 바이트는 즉시 폐기."""
    await sess.send({"type": "ocr_status", "upload_id": upload_id, "status": "processing"})
    try:
        ocr_text = await providers.ocr.extract_text(image_bytes, fmt, name)
    except ProviderError as exc:
        log.warning("ocr real failed (%s) → mock", exc)
        try:
            ocr_text = await providers.mocr.extract_text(image_bytes, fmt, name)
        except Exception as exc2:  # noqa: BLE001
            log.error("ocr mock failed: %s", exc2)
            await sess.send({"type": "ocr_status", "upload_id": upload_id, "status": "error"})
            await sess.send({"type": "error", "code": "ocr", "message": "사진에서 글자를 읽지 못했어요. 다시 찍어 주시겠어요?"})
            return
    finally:
        image_bytes = b""  # 디스크 저장 안 함, 참조도 폐기

    ocr_text = (ocr_text or "").strip()
    await sess.send({"type": "ocr_status", "upload_id": upload_id, "status": "done"})

    if not ocr_text:
        await _speak(
            sess, providers,
            [
                {"role": "system", "content": "어르신이 사진을 보내셨지만 글자를 읽지 못했어요. 존댓말로 2문장 이내, 더 밝은 곳에서 또렷하게 다시 찍어달라고 부드럽게 안내하세요."},
                {"role": "user", "content": "(인식된 글자가 없습니다)"},
            ],
            max_tokens=150, single=True,
        )
        return

    sess.ocr_texts.append(ocr_text)
    messages = [
        {"role": "system", "content": prompts.OCR_EXPLAIN + ocr_text},
        {"role": "user", "content": "이 내용을 쉽게 설명해 주세요."},
    ]
    await _speak(sess, providers, messages, max_tokens=500)
    _spawn_extract(sess, providers)  # OCR 내용 반영해 특이사항 갱신
