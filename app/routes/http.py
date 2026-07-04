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
    return {
        "status": "ok",
        "mock_mode": app.state.settings.mock_mode,
        "providers": app.state.providers.modes,
        "sessions": app.state.store.count(),
    }


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
        text = _clean_for_tts(msg.text)
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
