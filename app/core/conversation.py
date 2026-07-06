"""채팅/OCR 오케스트레이션 (conversation §8).
전화 통화하듯 짧은 말풍선 여러 개(단락 단위)로 나눠 보내고, 짧은 호응은 자연스럽게 이어간다.
real 실패 시 mock 폴백. 모든 WS 전송은 sess.send()로 직렬화된다."""
from __future__ import annotations

import logging
import re
from datetime import datetime

from app.core import prompts, welfare
from app.rag import rules
from app.rag.answer import compose_card, pick_card, rag_prompt_block, refresh_detail
from app.rag.apply import build_apply_package, package_to_text
from app.rag.search import augment_query, hybrid_retrieve, passes_gate
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


# 목록 항목(번호/글머리/굵은 용어+콜론)으로 시작하는 단락 — 앞 말풍선에 이어 붙일 대상
_LIST_ITEM = re.compile(r"^\s*(?:[-•*]\s|\d{1,2}[.)]\s|\*\*[^*\n]{1,30}\*\*\s*:)")


def _segments(text: str) -> list[str]:
    """LLM 응답을 말풍선 단위로 분리 — 문장이 아니라 단락(빈 줄) 기준.
    목록 항목·콜론으로 이어지는 단락·짧은 조각은 앞 말풍선에 붙여,
    복지 안내 같은 정보성 답변이 쪼개지지 않고 통으로 전달되게 한다."""
    text = (text or "").strip()
    if not text:
        return []
    out: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        if out and (_LIST_ITEM.match(block) or out[-1].rstrip().endswith(":") or len(block) < 6):
            out[-1] += "\n" + block
        else:
            out.append(block)
    if len(out) > 4:  # 말풍선 최대 4개 — 넘치면 버리지 않고 마지막에 합침
        out[3:] = ["\n\n".join(out[3:])]
    return out


async def _typing(sess, on: bool) -> None:
    await sess.send({"type": "ai_typing", "on": on})


async def _speak(
    sess, providers, messages, max_tokens: int = 240, single: bool = False,
    card_ctx: dict | None = None, settings=None,
) -> str:
    """AI 응답을 받아 말풍선 여러 개로 나눠 순차 전송(타이핑 + 간격). 전체 텍스트 반환.
    card_ctx가 있으면 T2 정보 카드(kind:card)를 같은 턴 마지막 말풍선으로 붙인다."""
    await _typing(sess, True)
    full = ""
    # 접지(RAG) 턴은 온도를 낮춰 자료 기반 답의 일관성을 높인다 (카드-답변 서비스 일치율↑)
    temperature = 0.45 if card_ctx else 0.6
    try:
        full = await providers.llm.chat(messages, max_tokens=max_tokens, temperature=temperature, top_p=0.8)
        if not full.strip():
            raise ProviderError("empty response")
    except Exception as exc:  # noqa: BLE001 — 어떤 실패든 mock으로 폴백(턴 크래시 방지)
        log.warning("chat real failed (%s) → mock", exc)
        try:
            full = await providers.mllm.chat(messages, max_tokens=max_tokens)
        except Exception as exc2:  # noqa: BLE001
            log.error("mock chat failed too: %s", exc2)
            full = "아이고, 제가 잠깐 딴생각을 했네요. 다시 한 번 말씀해 주시겠어요?"

    segs = [full.strip()] if single else _segments(full)
    if not segs:
        segs = ["네, 듣고 있어요."]

    # 말풍선을 묶어서 한 번에 보냄. 노출 페이싱(TTS 재생에 맞춤)은 프론트가 담당.
    bubbles = []
    for seg in segs:
        msg = sess.add_message("assistant", seg)
        bubbles.append({"id": msg.id, "text": seg})

    if card_ctx and settings is not None:
        try:  # 카드 실패가 턴을 깨지 않게
            chunk = pick_card(card_ctx["retrieved"], full,
                              strict=providers.modes.get("llm") == "real")
            if chunk is not None:
                fields, live = await refresh_detail(settings, chunk)
                card_text, tts = compose_card(chunk, fields, live)
                cmsg = sess.add_message("assistant", card_text, tts_text=tts)
                bubbles.append({
                    "id": cmsg.id, "text": card_text, "kind": "card",
                    "card": {  # 프론트 구조화 렌더링용 (RAG 근거 가시화)
                        "title": fields.get("서비스명", ""),
                        "지역": fields.get("지역", ""),
                        "대상": fields.get("지원대상", ""),
                        "지원": fields.get("지원내용", ""),
                        "신청": fields.get("신청방법", ""),
                        "문의": fields.get("문의처", ""),
                        "기준일": chunk.collected_at,
                        "live": live,
                        "url": chunk.url,
                        "source": chunk.source,
                    },
                })
                sess.last_rag = {"서비스명": fields.get("서비스명", ""), "serv_id": chunk.serv_id}
                sess.welfare_cards[chunk.serv_id] = {
                    "id": chunk.serv_id,
                    "이름": fields.get("서비스명", ""),
                    "한줄": fields.get("지원내용", "") or fields.get("지원대상", ""),
                    "신청처": fields.get("신청방법", ""),
                    "기준일": chunk.collected_at,
                }
                await welfare.push_welfare(sess)  # 패널도 즉시 갱신 (RAG 카드 우선 병합)
        except Exception as exc:  # noqa: BLE001
            log.warning("card compose failed (%s) — 답변만 전송", exc)

    await _typing(sess, False)
    await sess.send({"type": "ai_turn", "bubbles": bubbles})
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
    await sess.send({"type": "ai_turn", "bubbles": [{"id": msg.id, "text": text}]})


async def _rag_lookup(sess, providers, settings, user_text: str) -> dict | None:
    """RAG 게이트 (v2 §3 트리거 A): 항상 로컬 검색을 시도하되, 벡터 top_score가
    임계값 미만이면 None(일반 수다 경로). 임베딩 장애도 조용히 수다 경로로."""
    rt = providers.rag
    if rt is None or not settings.rag_enabled or not user_text.strip():
        return None
    await sess.send({"type": "rag_status", "status": "searching"})  # 프론트: "복지 자료 찾는 중…"
    try:
        q = augment_query(user_text, (sess.last_rag or {}).get("서비스명"))
        qvec = (await providers.embed.embed([q]))[0]
    except Exception as exc:  # noqa: BLE001 — 임베딩 실패로 턴을 깨지 않는다
        log.warning("rag embed failed (%s) → chit-chat path", exc)
        await sess.send({"type": "rag_status", "status": "none"})
        return None
    r = hybrid_retrieve(rt, qvec, q, k=settings.rag_top_k, pool=settings.rag_pool)
    ok = passes_gate(r, settings, providers.modes.get("embed", "mock"))
    log.info("rag lookup top=%.3f bm25=%.1f gate=%s q=%s", r.top_score, r.bm25_top, ok, q[:40])
    if not ok:
        await sess.send({"type": "rag_status", "status": "none"})
        return None
    await sess.send({
        "type": "rag_status", "status": "found",
        "hits": len(r.items), "top_score": round(r.top_score, 3),
        "sources": [c.source for c, _ in r.items],
    })
    return {"retrieved": r.items, "block": rag_prompt_block(r.items), "top": r.top_score}


def _basic_pension_fields(providers) -> tuple[dict, str, str]:
    """인덱스에서 기초연금 카드 필드를 찾음 (없으면 최소 폴백)."""
    for c in providers.rag.chunks if providers.rag else []:
        if (c.fields or {}).get("서비스명") == "기초연금":
            return c.fields, c.collected_at, c.url
    return {"서비스명": "기초연금", "신청방법": "주민센터, 복지로(온라인), 국민연금공단"}, "", ""


async def _extract_slots(sess, providers) -> dict:
    """슬롯 추출: LLM(실) → 실패·형식이상 시 정규식 폴백. 대상은 사용자 발화만."""
    recent = "\n".join(m.text for m in sess.messages if m.role == "user")[-500:]
    try:
        data = await providers.llm.extract_json(
            [{"role": "system", "content": rules.SLOT_SYSTEM}, {"role": "user", "content": recent}],
            rules.SLOT_SCHEMA,
        )
        if isinstance(data, dict) and any(k in data for k in ("age", "household", "income")):
            got = {k: data.get(k) for k in ("age", "household", "income")}
            if any(v is not None for v in got.values()):
                return got
    except Exception as exc:  # noqa: BLE001
        log.warning("slot extract llm failed (%s) → regex", exc)
    return rules.slots_from_text(recent)


async def _handle_screening(sess, providers, settings, fresh: bool) -> bool:
    """트리거 C (v2 §4-6): 슬롯 수집 → 룰엔진(코드) 판정 → 멘트 + 신청 패키지 카드.
    LLM은 슬롯 추출에만 쓰고 판정·수치는 결정론적. 처리했으면 True."""
    prev = {k: v for k, v in sess.slots.items() if not k.startswith("_")}
    got = await _extract_slots(sess, providers)
    merged = rules.merge_slots(prev, got)
    newly_filled = any(
        prev.get(k) is None and merged.get(k) is not None for k in ("age", "household", "income")
    )
    if not fresh and not newly_filled:
        return False  # 되묻기 중인데 새 정보가 없음(딴 얘기) → 일반 턴으로
    sess.slots = merged
    sess.slots["_pending"] = 0

    age, household = sess.slots.get("age"), sess.slots.get("household")
    verdict, ment = rules.check_basic_pension(age, household, sess.slots.get("income"))
    asking = age is None or (age >= rules.BASIC_PENSION_2026["age_min"] and household is None)
    if asking:
        sess.slots["_pending"] = 2  # 다음 1~2턴은 답변(나이·가구)을 판정 문맥으로 받음

    msg = sess.add_message("assistant", ment)
    bubbles = [{"id": msg.id, "text": ment}]

    if not asking and verdict in ("가능성높음", "확인필요"):
        fields, collected_at, url = _basic_pension_fields(providers)
        pkg = build_apply_package(fields, collected_at, url)
        text = package_to_text(pkg)
        cmsg = sess.add_message(
            "assistant", text, tts_text="기초연금 신청에 필요한 것들을 화면에 카드로 정리해 드렸어요."
        )
        bubbles.append({"id": cmsg.id, "text": text, "kind": "card"})
        sess.apply_packages["기초연금"] = pkg
        sess.welfare_cards["fixture-basic-pension"] = {
            "id": "fixture-basic-pension", "이름": "기초연금",
            "한줄": fields.get("지원내용", "") or "만 65세 이상 소득 하위 어르신 연금",
            "신청처": fields.get("신청방법", ""), "기준일": collected_at,
        }
        await welfare.push_welfare(sess)

    await sess.send({"type": "ai_turn", "bubbles": bubbles})
    log.info("screening verdict=%s slots=%s", verdict, {k: v for k, v in sess.slots.items()})
    return True


async def handle_turn(sess, providers, settings) -> None:
    last = sess.messages[-1] if sess.messages else None
    user_text = last.text if last and last.role == "user" else ""
    bc = bool(last and last.role == "user" and _is_backchannel(user_text))

    # 판정 의도(트리거 C) — 명시 질문 또는 되묻기 진행 중이면 룰엔진 경로
    if not bc:
        fresh = rules.detect_screen_intent(user_text) is not None
        pending = int(sess.slots.get("_pending", 0) or 0)
        if fresh or pending > 0:
            if not fresh:
                sess.slots["_pending"] = pending - 1
            if await _handle_screening(sess, providers, settings, fresh):
                _spawn_extract(sess, providers)
                return

    card_ctx = None if bc else await _rag_lookup(sess, providers, settings, user_text)
    if card_ctx:
        system = prompts.chat_system(card_ctx["block"], backchannel=bc, rag=True)
    else:
        system = prompts.chat_system(welfare.get_digest(), backchannel=bc)
    messages = [{"role": "system", "content": system}] + sess.history_for_llm()
    # 복지 안내처럼 긴 정보가 목록 중간에 잘리지 않도록 여유 있게. 평소 답의 길이는 프롬프트가 통제.
    await _speak(sess, providers, messages, max_tokens=600, card_ctx=card_ctx, settings=settings)
    _spawn_extract(sess, providers)  # 비동기 추출


async def handle_image(sess, providers, image_bytes: bytes, fmt: str, name: str, upload_id: str) -> None:
    """이미지 → OCR → 쉬운 말 설명 + 사기 판별 → 특이사항 반영. 이미지 바이트는 즉시 폐기."""
    await sess.send({"type": "ocr_status", "upload_id": upload_id, "status": "processing"})
    try:
        ocr_text = await providers.ocr.extract_text(image_bytes, fmt, name)
    except Exception as exc:  # noqa: BLE001 — 어떤 실패든 mock으로 (OCR 상태가 '처리 중'에서 멈추지 않게)
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

    # 문서 종류 분류(카드용)는 설명 생성과 병렬 실행 — 지연 최소화. classify는 예외를 내지 않음.
    import asyncio

    from app.core import ocr_doc

    doc_task = asyncio.ensure_future(ocr_doc.classify_document(providers, ocr_text))
    messages = [
        {"role": "system", "content": prompts.OCR_EXPLAIN + ocr_text},
        {"role": "user", "content": "이 내용을 쉽게 설명해 주세요."},
    ]
    await _speak(sess, providers, messages, max_tokens=400)

    doc = None
    try:  # 카드 실패가 턴을 깨지 않게 (RAG 카드와 동일 원칙)
        doc = await doc_task
        card_text, tts = ocr_doc.compose_doc_card(doc)
        if card_text:
            cmsg = sess.add_message("assistant", card_text, tts_text=tts)
            await sess.send(
                {"type": "ai_turn", "bubbles": [{"id": cmsg.id, "text": card_text, "kind": "card"}]}
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("doc card failed (%s) — 설명만 전송", exc)
    # 추출 파이프라인에 문서 종류 문맥 제공 (예: [문자·메시지] 스미싱 원문)
    sess.ocr_texts.append(f"[{doc.종류}] {ocr_text}" if doc else ocr_text)
    _spawn_extract(sess, providers)  # OCR 내용 반영해 특이사항 갱신
