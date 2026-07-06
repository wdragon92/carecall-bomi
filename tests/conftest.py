"""테스트는 항상 MOCK_MODE 강제 (tests §12). 실제 키/네트워크에 의존하지 않음."""
import os

os.environ["MOCK_MODE"] = "true"
os.environ["CLOVA_STUDIO_API_KEY"] = ""
os.environ["NCP_APIGW_CLIENT_ID"] = ""
os.environ["NCP_APIGW_CLIENT_SECRET"] = ""
os.environ["CLOVA_OCR_INVOKE_URL"] = ""
os.environ["CLOVA_OCR_SECRET"] = ""
os.environ["GREET_DELAY_SECONDS"] = "0"  # 인사 지연은 브라우저 오디오 정책용 — 테스트는 즉시

import pytest

from app.config import get_settings

get_settings.cache_clear()

from app.main import create_app  # noqa: E402


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
def client(app):
    from starlette.testclient import TestClient

    with TestClient(app) as c:
        yield c


@pytest.fixture
def norag_client(tmp_path, monkeypatch):
    """RAG 인덱스가 확실히 '없는' 앱 클라이언트 (RAG off 경로 검증용).
    기본 client는 리포의 data/ 인덱스 유무에 따라 RAG 상태가 달라질 수 있어,
    빈 디렉토리를 강제해 결정성을 확보한다."""
    monkeypatch.setenv("RAG_DATA_DIR", str(tmp_path / "empty"))
    get_settings.cache_clear()
    try:
        from starlette.testclient import TestClient

        with TestClient(create_app()) as c:
            yield c
    finally:
        get_settings.cache_clear()


@pytest.fixture
def rag_client(tmp_path, monkeypatch):
    """목 임베딩으로 빌드한 인덱스를 쓰는 앱 클라이언트 (RAG on)."""
    import asyncio

    from app.rag import cards
    from app.rag.index import build_index, save_index
    from app.services.mock import MockEmbed

    embed = MockEmbed()
    cs = cards.fixture_cards()
    loaded, st = asyncio.run(build_index(cs, embed.embed, None, "mock", sleep_s=0))
    save_index(loaded, tmp_path, st)

    monkeypatch.setenv("RAG_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        from starlette.testclient import TestClient

        with TestClient(create_app()) as c:
            yield c
    finally:
        get_settings.cache_clear()  # 다른 테스트가 tmp 경로를 물려받지 않게
