"""FastAPI 앱 구성 + lifespan(세션 TTL 스위퍼) (main §2, §5)."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.routes import http as http_routes
from app.routes import ws as ws_routes
from app.services.factory import build_providers
from app.session import SessionStore

STATIC_DIR = Path(__file__).parent / "static"
log = logging.getLogger("app")


async def _sweep_loop(app: FastAPI) -> None:
    while True:
        await asyncio.sleep(600)
        try:
            n = await app.state.store.sweep()
            if n:
                log.info("swept %d expired session(s)", n)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 스위퍼는 절대 앱을 죽이지 않음
            log.warning("sweep error: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    logging.basicConfig(
        level=getattr(logging, s.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app.state.settings = s
    app.state.store = SessionStore(ttl_min=s.session_ttl_min)
    app.state.providers = build_providers(s)
    if s.rag_enabled:
        try:  # 인덱스가 없거나 손상돼도 앱은 뜬다 (RAG만 off)
            from app.rag.search import load_runtime

            app.state.providers.rag = load_runtime(s, app.state.providers.modes.get("embed", "mock"))
        except Exception as exc:  # noqa: BLE001
            log.warning("RAG init failed (%s) — RAG off", exc)
    sweeper = asyncio.create_task(_sweep_loop(app))
    log.info("돌봄콜 AI 시작 — http://%s:%s", s.app_host, s.app_port)
    try:
        yield
    finally:
        sweeper.cancel()
        try:
            await sweeper
        except asyncio.CancelledError:
            pass
        # real provider들의 httpx 클라이언트 정리 (mock엔 aclose 없음 → getattr 가드)
        providers = getattr(app.state, "providers", None)
        for name in ("llm", "stt", "tts", "ocr", "embed"):
            aclose = getattr(getattr(providers, name, None), "aclose", None)
            if aclose is None:
                continue
            try:
                await aclose()
            except Exception as exc:  # noqa: BLE001 — 종료 정리는 앱을 죽이지 않음
                log.warning("provider '%s' aclose 실패: %s", name, exc)


def create_app() -> FastAPI:
    app = FastAPI(title="돌봄콜 AI", lifespan=lifespan)
    app.include_router(http_routes.router)
    app.include_router(ws_routes.router)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(str(STATIC_DIR / "index.html"))

    return app


app = create_app()
