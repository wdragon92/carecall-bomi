"""REST 엔드포인트 (routes §6.1)."""
from __future__ import annotations

import logging
import re
import secrets

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


def _clean_for_tts(text: str) -> str:
    t = _EMOJI.sub("", text)
    t = re.sub(r"[*_`#>~\[\]()]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
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
            "bm25_evidence": s.rag_bm25_evidence}
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

    body = await request.json()
    slots = (body or {}).get("slots") or {}
    verdict, ment = check_basic_pension(slots.get("age"), slots.get("household"), slots.get("income"))
    pkg = None
    age = slots.get("age") or 0
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
    providers.rag = rt
    return {"loaded": rt is not None, "chunks": len(rt.chunks) if rt else 0}


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

    sess = store.get(sid)
    if sess is None:
        raise HTTPException(status_code=404, detail="session not found")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty audio")

    try:
        text = await providers.stt.transcribe(data)
    except Exception as exc:  # noqa: BLE001
        log.warning("stt real failed (%s) → mock", exc)
        try:
            text = await providers.mstt.transcribe(data)
        except Exception as exc2:  # noqa: BLE001
            log.error("stt mock failed: %s", exc2)
            text = ""
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

    # 리포트 전 특이사항 1회 flush (최신 상태 반영)
    try:
        from app.core.extraction import trigger_extract

        await trigger_extract(sess, providers)
    except Exception as exc:  # noqa: BLE001
        log.warning("pre-report extract failed: %s", exc)

    from app.core.report import generate_report

    report = await generate_report(sess, providers)
    return {"report": report}
