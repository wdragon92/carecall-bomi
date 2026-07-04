"""채팅 오케스트레이션 (conversation §8): 선인사, 사용자 턴 처리, 응답 스트리밍.
real 실패 시 mock 폴백. 모든 WS 전송은 sess.send()로 직렬화된다."""
from __future__ import annotations

import logging
from datetime import datetime

from app.core import prompts, welfare
from app.services.base import ProviderError

log = logging.getLogger("conv")


def _period_now() -> str:
    return prompts.period_of_hour(datetime.now().hour)


async def greet(sess) -> None:
    text = prompts.greeting(_period_now())
    msg = sess.add_message("assistant", text, via="system")
    await sess.send({"type": "ai_message_start", "id": msg.id})
    await sess.send({"type": "ai_message_delta", "id": msg.id, "text": text})
    await sess.send({"type": "ai_message_end", "id": msg.id, "full_text": text})


async def handle_turn(sess, providers, settings) -> None:
    await stream_reply(sess, providers)
    # 특이사항 추출은 비동기(백그라운드)로 — 채팅 흐름을 막지 않음
    try:
        from app.core.extraction import trigger_extract
    except ImportError:
        return
    sess.spawn(trigger_extract(sess, providers))


async def stream_reply(sess, providers):
    system = prompts.chat_system(welfare.get_digest())
    messages = [{"role": "system", "content": system}] + sess.history_for_llm()
    msg = sess.add_message("assistant", "")
    await sess.send({"type": "ai_message_start", "id": msg.id})

    parts: list[str] = []
    sent = 0

    async def run(provider) -> None:
        nonlocal sent
        async for chunk in provider.chat_stream(
            messages, temperature=0.5, top_p=0.8, max_tokens=300
        ):
            parts.append(chunk)
            sent += 1
            await sess.send({"type": "ai_message_delta", "id": msg.id, "text": chunk})

    try:
        await run(providers.llm)
        if sent == 0:
            raise ProviderError("empty response")
    except ProviderError as exc:
        log.warning("chat real failed (%s)", exc)
        if sent == 0:
            try:
                await run(providers.mllm)
            except Exception as exc2:  # noqa: BLE001
                log.error("mock chat failed too: %s", exc2)
                fb = "죄송해요, 지금 잠시 문제가 있었어요. 다시 한 번 말씀해 주시겠어요?"
                parts.append(fb)
                await sess.send({"type": "ai_message_delta", "id": msg.id, "text": fb})

    full = "".join(parts).strip()
    msg.text = full
    await sess.send({"type": "ai_message_end", "id": msg.id, "full_text": full})
    return msg
