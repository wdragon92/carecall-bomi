"""REST 엔드포인트 (routes §6.1)."""
from __future__ import annotations

import logging
import re
import secrets
import unicodedata

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from app.core.conversation import handle_image

router = APIRouter()
log = logging.getLogger("http")

_EXT = ("jpg", "jpeg", "png", "pdf", "tif", "tiff")
_EMOJI = re.compile(
    "[\U0001f000-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff←-⇿]"
)


def _fmt_from(filename: str | None, content_type: str | None) -> str:
    name = (filename or "").lower()
    for ext in _EXT:
        if name.endswith("." + ext):
            return "jpg" if ext == "jpeg" else ext
    ct = (content_type or "").lower()
    if "png" in ct:
        return "png"
    if "pdf" in ct:
        return "pdf"
    return "jpg"


# 위기·상담 전화번호는 자릿수 낭독이 정답 ("백십구"가 아니라 "일일구") — 표시는 원문, 낭독만 치환
_HOTLINE_READS = [
    ("1577-1389", "일오칠칠에 일삼팔구"), ("1577-0199", "일오칠칠에 공일구구"),
    ("1332", "일삼삼이"), ("109", "일공구"), ("119", "일일구"), ("112", "일일이"), ("129", "일이구"),
]


def _clean_for_tts(text: str) -> str:
    t = _EMOJI.sub("", text)
    t = re.sub(r"[*_`#>~\[\]()]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    # 낭독 페이싱: "잘 안 ~"를 붙여 읽어 어색 → 쉼표로 ~0.2초 숨 (화면 표시는 원문 그대로)
    t = t.replace("잘 안 ", "잘, 안 ")
    # 숫자/하이픈 경계 + '천단위·소수 구분자에 붙은 숫자'만 제외 — "119,000원"의 119는
    # 낭독 치환하지 않되("일일구,000원" 방지), "자살예방 109, 응급 119," 같은 목록의
    # 쉼표(뒤가 숫자 아님)는 전화번호로 낭독한다.
    for num, read in _HOTLINE_READS:  # 긴 번호부터 치환(부분 겹침 방지 순서)
        t = re.sub(rf"(?<![\d-])(?<![\d][.,]){re.escape(num)}(?![\d-])(?![.,]\d)", read, t)
    return t[:1900]


@router.get("/health")
async def health(request: Request) -> dict:
    app = request.app
    rag = getattr(app.state.providers, "rag", None)
    return {
        "status": "ok",
        "mock_mode": app.state.settings.mock_mode,
        "providers": app.state.providers.modes,
        "sessions": app.state.store.count(),
        "rag": (
            {
                "loaded": True,
                "chunks": len(rag.chunks),
                "embed_mode": rag.meta.get("embed_mode", ""),
                "built_at": rag.meta.get("built_at", ""),
            }
            if rag
            else {"loaded": False}
        ),
    }


@router.post("/api/rag/answer")
async def rag_answer_once(request: Request) -> dict:
    """RAG 단건 질의 (v2 §4-8) — 데모·평가용. 세션 없이 curl로 검증 가능."""
    from app.core import prompts
    from app.rag.answer import REJECT_ANSWER, compose_card, pick_card, rag_prompt_block, refresh_detail, retrieve_for

    providers = request.app.state.providers
    s = request.app.state.settings
    body = await request.json()
    question = ((body or {}).get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question required")
    if providers.rag is None:
        raise HTTPException(status_code=503, detail="rag index not loaded (build_index.py 실행 필요)")

    try:
        r, ok = await retrieve_for(providers, s, question)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"embed failed: {exc}")

    emode = providers.modes.get("embed", "mock")
    gate = {"low": s.rag_threshold(emode), "high": s.rag_threshold_high(emode),
            "bm25_evidence": s.rag_bm25_min(emode)}
    if not ok:
        return {"answer": REJECT_ANSWER, "rejected": True, "top_score": round(r.top_score, 3),
                "bm25_top": round(r.bm25_top, 2), "gate": gate,
                "sources": [], "card": None, "live": False}

    system = prompts.chat_system(rag_prompt_block(r.items), rag=True)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": question}]
    try:
        answer = await providers.llm.chat(messages, max_tokens=500, temperature=0.5, top_p=0.8)
    except Exception as exc:  # noqa: BLE001
        log.warning("rag answer llm failed (%s) → mock", exc)
        answer = await providers.mllm.chat(messages, max_tokens=500)

    card_text, live = None, False
    chunk = pick_card(r.items, answer, strict=providers.modes.get("llm") == "real")
    if chunk is not None:
        fields, live = await refresh_detail(s, chunk)
        card_text, _tts = compose_card(chunk, fields, live)

    return {
        "answer": answer, "rejected": False, "top_score": round(r.top_score, 3),
        "bm25_top": round(r.bm25_top, 2), "gate": gate,
        "sources": [{"source": c.source, "rrf": round(sc, 4)} for c, sc in r.items],
        "card": card_text, "live": live,
    }


@router.post("/api/rag/screen")
async def rag_screen(request: Request) -> dict:
    """룰엔진 단건 판정 (v2 §4-8) — slots {age, household, income}. 데모·검증용."""
    from app.rag.apply import build_apply_package
    from app.rag.rules import BASIC_PENSION_2026, check_basic_pension

    def _int_or_none(v):
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    body = await request.json()
    slots = (body or {}).get("slots") or {}
    age_in = _int_or_none(slots.get("age"))  # "칠십" 같은 비정수 입력에 500 나지 않게
    household = slots.get("household") if slots.get("household") in ("single", "couple") else None
    verdict, ment = check_basic_pension(age_in, household, _int_or_none(slots.get("income")))
    pkg = None
    age = age_in or 0
    if verdict in ("가능성높음", "확인필요") and age >= BASIC_PENSION_2026["age_min"]:
        providers = request.app.state.providers
        chunk = next(
            (c for c in (providers.rag.chunks if providers.rag else [])
             if (c.fields or {}).get("서비스명") == "기초연금"),
            None,
        )
        fields = chunk.fields if chunk else {"서비스명": "기초연금", "신청방법": "주민센터, 복지로(온라인), 국민연금공단"}
        pkg = build_apply_package(fields, chunk.collected_at if chunk else "", chunk.url if chunk else "")
    return {"판정": verdict, "근거": ment, "apply_package": pkg}


@router.post("/api/rag/reload")
async def rag_reload(request: Request) -> dict:
    """재빌드(build_index.py) 후 무중단 인덱스 교체 — 발표 당일 갱신용."""
    from app.rag.search import load_runtime

    providers = request.app.state.providers
    rt = load_runtime(request.app.state.settings, providers.modes.get("embed", "mock"))
    # 로드 실패(None: 재빌드 산출물 없음/손상)면 기존 인덱스를 유지해 무중단 보장.
    # (무조건 providers.rag = rt 하면 살아있던 인덱스가 None으로 지워져 RAG가 죽는다.)
    if rt is not None:
        providers.rag = rt
    cur = providers.rag
    return {"loaded": cur is not None, "chunks": len(cur.chunks) if cur else 0}


@router.post("/api/sessions")
async def create_session(request: Request) -> dict:
    sess = await request.app.state.store.create()
    log.info("session created: %s", sess.id)
    return {"session_id": sess.id}


@router.post("/api/sessions/{sid}/image")
async def upload_image(sid: str, request: Request, file: UploadFile = File(...)):
    store = request.app.state.store
    providers = request.app.state.providers
    settings = request.app.state.settings

    sess = store.get(sid)
    if sess is None:
        raise HTTPException(status_code=404, detail="session not found")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    max_bytes = settings.max_upload_mb * 1024 * 1024
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail=f"file too large (>{settings.max_upload_mb}MB)")

    fmt = _fmt_from(file.filename, file.content_type)
    upload_id = secrets.token_hex(6)
    sess.spawn(handle_image(sess, providers, data, fmt, file.filename or "doc", upload_id))
    return JSONResponse({"upload_id": upload_id}, status_code=202)


@router.post("/api/sessions/{sid}/audio")
async def upload_audio(sid: str, request: Request, file: UploadFile = File(...)) -> dict:
    """음성(WAV) → STT 텍스트만 반환. 클라이언트가 user_message(via=voice)로 재전송."""
    store = request.app.state.store
    providers = request.app.state.providers
    settings = request.app.state.settings

    sess = store.get(sid)
    if sess is None:
        raise HTTPException(status_code=404, detail="session not found")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty audio")
    max_bytes = settings.max_upload_mb * 1024 * 1024  # 이미지 경로와 동일한 업로드 상한
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail=f"file too large (>{settings.max_upload_mb}MB)")

    try:
        text = await providers.stt.transcribe(data)
    except Exception as exc:  # noqa: BLE001
        # 전체 데모(stt=mock)만 MockSTT 폴백 유지. real STT 실패 시 mock으로 폴백하면
        # 각본 문장(가짜 발화)이 실제 발화로 반환되므로, real 실패는 ""로 두어
        # 프론트의 "(음성을 알아듣지 못했어요)"를 유도한다.
        if providers.modes.get("stt") == "mock":
            log.warning("stt mock failed (%s) → 재시도", exc)
            try:
                text = await providers.mstt.transcribe(data)
            except Exception as exc2:  # noqa: BLE001
                log.error("stt mock failed: %s", exc2)
                text = ""
        else:
            log.warning("stt real failed (%s) → 빈 결과(각본 폴백 금지)", exc)
            text = ""
    # ingress NFC 정규화 — ws user_message와 동일 규약으로 통일(안전망·복지매칭 우회 방지)
    text = unicodedata.normalize("NFC", text)
    return {"text": text}


@router.post("/api/sessions/{sid}/tts")
async def synth_tts(sid: str, request: Request):
    store = request.app.state.store
    providers = request.app.state.providers

    sess = store.get(sid)
    if sess is None:
        raise HTTPException(status_code=404, detail="session not found")

    body = await request.json()
    message_id = (body or {}).get("message_id")
    if not message_id:
        raise HTTPException(status_code=400, detail="message_id required")

    audio = sess.tts_cache.get(message_id)
    if audio is None:
        msg = next((m for m in sess.messages if m.id == message_id and m.role == "assistant"), None)
        if msg is None or not msg.text.strip():
            raise HTTPException(status_code=404, detail="message not found")
        # 정보 카드(📌)는 원문 대신 짧은 안내문(tts_text)을 읽는다 — 수치·기호 낭독 방지
        text = _clean_for_tts(msg.tts_text or msg.text)
        try:
            audio = await providers.tts.synthesize(text)
        except Exception as exc:  # noqa: BLE001
            log.warning("tts real failed (%s) → mock", exc)
            try:
                audio = await providers.mtts.synthesize(text)
            except Exception as exc2:  # noqa: BLE001
                log.error("tts mock failed: %s", exc2)
                raise HTTPException(status_code=502, detail="tts failed")
        sess.cache_tts(message_id, audio)

    media = "audio/wav" if audio[:4] == b"RIFF" else "audio/mpeg"
    return Response(content=audio, media_type=media)


@router.post("/api/sessions/{sid}/end")
async def end_session(sid: str, request: Request) -> dict:
    store = request.app.state.store
    providers = request.app.state.providers

    sess = store.get(sid)
    if sess is None:
        raise HTTPException(status_code=404, detail="session not found")

    # 리포트 전 특이사항 flush — 진행 중 추출이 있으면 끝나길 기다린 뒤 1회 더 (경합 방지)
    try:
        from app.core.extraction import flush_extract

        await flush_extract(sess, providers)
    except Exception as exc:  # noqa: BLE001
        log.warning("pre-report extract failed: %s", exc)

    from app.core.report import generate_report

    report = await generate_report(sess, providers)
    return {"report": report}
