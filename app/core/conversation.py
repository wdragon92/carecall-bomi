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
from app.rag.cards import BOKJIRO_HOME, card_url
from app.rag.search import augment_query, hybrid_retrieve, passes_gate
from app.services.base import ProviderError

log = logging.getLogger("conv")

# 짧은 호응(맞장구) — 이런 입력엔 새 질문 대신 이야기를 이어감
BACKCHANNELS = {
    "응", "응응", "어", "어어", "엉", "네", "넵", "예", "그래", "그러게", "그렇구나",
    "그러네", "맞아", "맞아요", "음", "으음", "글쎄", "그럼", "그치", "응그래", "그래서",
    "알겠어", "알겠어요", "고마워", "고마워요", "아니", "아니요", "괜찮아", "괜찮아요",
}

# 긍정 호응 — 직전에 보미가 "알려드릴까요?" 하고 제안했을 때 수락으로 해석
_ASSENT = {
    "응", "응응", "어", "어어", "네", "넵", "예", "그래", "그럼", "그치", "좋아", "좋지",
    "알겠어", "알겠어요", "궁금해", "알려줘", "알려줘요", "해줘", "부탁해",
}


def _period_now() -> str:
    return prompts.period_of_hour(datetime.now().hour)


def _is_backchannel(text: str) -> bool:
    t = re.sub(r"[.!?~,…\s]+", "", text or "")
    return bool(t) and len(t) <= 5 and t in BACKCHANNELS


_DECLINE = re.compile(r"아니|괜찮|됐어|나중|말고|싫")


def _accepts_offer(text: str) -> bool:
    """직전 복지 제안("알려드릴까요?")에 대한 수락 여부 — 짧은 긍정만 인정."""
    t = (text or "").strip()
    norm = re.sub(r"[.!?~,…\s]+", "", t)
    if not norm or len(norm) > 10 or _DECLINE.search(t):
        return False
    return norm in _ASSENT or bool(re.search(r"알려|궁금|자세히|말해|들어보", t))


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
                cmsg = sess.add_message("assistant", card_text, tts_text=tts, kind="card")
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
                        "url": card_url(chunk),
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
                    "url": card_url(chunk),
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


# ---- HCX-007(분석) → HCX-005(대화) 환류 (페르소나 §9) ----
_SEV_ORDER = {"높음": 0, "보통": 1, "낮음": 2}


def _situation_memo(sess) -> str:
    """추출 파이프라인(HCX-007)이 쌓은 세션 상태를 대화(HCX-005) 컨텍스트로 요약.
    보미가 같은 걸 두 번 묻지 않고, 감지된 복지 니즈를 다음 턴 화제에 자연스럽게 얹게 한다."""
    parts: list[str] = []
    age, hh = sess.slots.get("age"), sess.slots.get("household")
    prof = []
    if age:
        prof.append(f"만 {age}세")
    if hh:
        prof.append("혼자 지내심" if hh == "single" else "배우자와 함께 지내심")
    if prof:
        parts.append("- 어르신 기본 정보: " + ", ".join(prof))
    if sess.findings:
        top = sorted(sess.findings, key=lambda f: _SEV_ORDER.get(f.severity, 9))[:6]
        parts += [f"- 관찰됨({f.category}): {f.content}" for f in top]
    guided = [c.get("이름", "") for c in sess.welfare_cards.values() if c.get("이름")]
    if guided:
        parts.append("- 이미 안내한 복지: " + ", ".join(guided))
    offer = _offer_candidate(sess)
    if offer:
        parts.append(
            f"- 복지 제안 힌트: 대화 맥락상 '{offer}'가 도움이 될 수 있음. 흐름이 맞으면 "
            "\"도움되는 제도가 있는데 알려드릴까요?\" 하고 서비스 이름과 함께 여쭤보기 (수락하시면 자세히 안내됨)."
        )
    return "\n".join(parts)


def _offer_candidate(sess) -> str | None:
    """추출이 패널에 매칭해 둔 복지 중 아직 대화로 안내하지 않은 첫 항목."""
    guided = {c.get("이름") for c in sess.welfare_cards.values()}
    for it in welfare.by_ids(sess.welfare_matched):
        if it["이름"] not in guided:
            return it["이름"]
    return None


def _last_offer_text(sess) -> str | None:
    """직전 보미 발화가 복지 제안("알려드릴까요?")이었으면 그 발화 원문."""
    last_ai = next((m for m in reversed(sess.messages) if m.role == "assistant"), None)
    if last_ai is not None and "알려드릴까요" in last_ai.text:
        return last_ai.text
    return None


def _offered_service(sess) -> str | None:
    """직전 보미 제안에서 서비스명을 특정한다 (후속 질의 보강 힌트용)."""
    offer = _last_offer_text(sess)
    if offer is None:
        return None
    text_n = re.sub(r"\s+", "", offer)
    names = [it.이름 for it in welfare.load_items()] + [
        c.get("이름", "") for c in sess.welfare_cards.values()
    ]
    for name in names:
        if name and re.sub(r"\s+", "", name) in text_n:
            return name
    return _offer_candidate(sess)


def _offer_query(sess) -> str | None:
    """수락된 제안의 검색 질의 — 서비스명을 특정 못 하면 제안 발화 자체를 질의로 쓴다.
    (보미가 이름 없이 "무릎 수술비 도와주는 제도가 있는데 알려드릴까요?"만 한 경우도
    어르신의 "응" 한마디로 근거 있는 카드 안내까지 이어지게.)"""
    offer = _last_offer_text(sess)
    if offer is None:
        return None
    named = _offered_service(sess)
    if named:
        return named
    return re.sub(r"\s+", " ", offer)[:120]


async def _rag_lookup(sess, providers, settings, user_text: str) -> dict | None:
    """RAG 게이트 (v2 §3 트리거 A): 항상 로컬 검색을 시도하되, 벡터 top_score가
    임계값 미만이면 None(일반 수다 경로). 임베딩 장애도 조용히 수다 경로로."""
    rt = providers.rag
    if rt is None or not settings.rag_enabled or not user_text.strip():
        return None
    await sess.send({"type": "rag_status", "status": "searching"})  # 프론트: "복지 자료 찾는 중…"
    try:
        # 후속 질문 보강 힌트: 직전 안내 서비스 → 없으면 직전 턴에 제안한 서비스
        hint = (sess.last_rag or {}).get("서비스명") or _offered_service(sess)
        q = augment_query(user_text, hint)
        qvec = (await providers.embed.embed([q]))[0]
    except Exception as exc:  # noqa: BLE001 — 임베딩 실패로 턴을 깨지 않는다
        log.warning("rag embed failed (%s) → chit-chat path", exc)
        await sess.send({"type": "rag_status", "status": "none"})
        return None
    emode = providers.modes.get("embed", "mock")
    r = hybrid_retrieve(rt, qvec, q, k=settings.rag_top_k, pool=settings.rag_pool,
                        min_vec=settings.rag_item_threshold(emode))
    ok = passes_gate(r, settings, emode)
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
    # 새로 채워졌거나 '정정'된 값도 판정 문맥의 새 정보로 인정
    newly_filled = any(
        merged.get(k) != prev.get(k) for k in ("age", "household", "income")
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
            "assistant", text, kind="card", tts_text="기초연금 신청에 필요한 것들을 화면에 카드로 정리해 드렸어요."
        )
        bubbles.append({"id": cmsg.id, "text": text, "kind": "card"})
        sess.apply_packages["기초연금"] = pkg
        sess.welfare_cards["fixture-basic-pension"] = {
            "id": "fixture-basic-pension", "이름": "기초연금",
            "한줄": fields.get("지원내용", "") or "만 65세 이상 소득 하위 어르신 연금",
            "신청처": fields.get("신청방법", ""), "기준일": collected_at,
            "url": url or BOKJIRO_HOME,
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

    # 제안 수락 흐름: 직전 턴에 보미가 복지를 제안("알려드릴까요?")했고 어르신이 긍정 호응
    # → 그 서비스명으로 근거(RAG) 검색해 카드까지 이어지는 안내 턴으로 승격.
    card_ctx = None
    if _accepts_offer(user_text):
        offered = _offer_query(sess)
        if offered:
            card_ctx = await _rag_lookup(sess, providers, settings, offered)
            if card_ctx:
                bc = False
    if card_ctx is None and not bc:
        card_ctx = await _rag_lookup(sess, providers, settings, user_text)

    system = prompts.chat_system(
        card_ctx["block"] if card_ctx else "",
        memo=_situation_memo(sess),  # HCX-007 추출 결과를 대화 컨텍스트로 환류
        backchannel=bc,
        rag=bool(card_ctx),
    )
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
            cmsg = sess.add_message("assistant", card_text, tts_text=tts, kind="card")
            await sess.send(
                {"type": "ai_turn", "bubbles": [{"id": cmsg.id, "text": card_text, "kind": "card"}]}
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("doc card failed (%s) — 설명만 전송", exc)
    # 추출 파이프라인에 문서 종류 문맥 제공 (예: [문자·메시지] 스미싱 원문)
    sess.ocr_texts.append(f"[{doc.종류}] {ocr_text}" if doc else ocr_text)
    _spawn_extract(sess, providers)  # OCR 내용 반영해 특이사항 갱신
