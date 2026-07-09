import unicodedata

from app.routes.http import _clean_for_tts


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert set(body["providers"]) == {"llm", "stt", "tts", "ocr", "embed"}
    assert all(v == "mock" for v in body["providers"].values())
    assert "rag" in body  # 인덱스 유무와 무관하게 상태 노출


# ---- S1: real STT 실패는 각본(가짜 발화) 대신 빈 문자열 반환 ----
def test_audio_real_stt_failure_returns_empty_not_script(client, app, monkeypatch):
    providers = app.state.providers
    sid = client.post("/api/sessions").json()["session_id"]

    async def _boom(_data):
        raise RuntimeError("stt real down")

    monkeypatch.setattr(providers.stt, "transcribe", _boom)
    monkeypatch.setitem(providers.modes, "stt", "real")

    r = client.post(f"/api/sessions/{sid}/audio",
                    files={"file": ("a.wav", b"RIFFxxxx", "audio/wav")})
    assert r.status_code == 200
    assert r.json()["text"] == ""  # 각본 문장이 아니라 빈 결과


def test_audio_mock_mode_returns_script(client):
    # 전체 데모(stt=mock)는 그대로 스크립트 문장을 반환(비어있지 않음)
    sid = client.post("/api/sessions").json()["session_id"]
    r = client.post(f"/api/sessions/{sid}/audio",
                    files={"file": ("a.wav", b"RIFFxxxx", "audio/wav")})
    assert r.status_code == 200
    assert r.json()["text"]


# ---- R1: /audio 업로드에도 max_upload_mb 상한 적용 ----
def test_audio_upload_too_large_rejected(client, app):
    sid = client.post("/api/sessions").json()["session_id"]
    max_mb = app.state.settings.max_upload_mb
    big = b"\x00" * (max_mb * 1024 * 1024 + 1)
    r = client.post(f"/api/sessions/{sid}/audio",
                    files={"file": ("big.wav", big, "audio/wav")})
    assert r.status_code == 413


# ---- R6: /audio STT 결과를 NFC로 정규화해 반환 ----
def test_audio_stt_result_nfc_normalized(client, app, monkeypatch):
    providers = app.state.providers
    sid = client.post("/api/sessions").json()["session_id"]
    nfd = unicodedata.normalize("NFD", "각") + " 통증"

    async def _stt(_data):
        return nfd

    monkeypatch.setattr(providers.stt, "transcribe", _stt)
    r = client.post(f"/api/sessions/{sid}/audio",
                    files={"file": ("a.wav", b"RIFFxxxx", "audio/wav")})
    assert r.status_code == 200
    assert r.json()["text"] == unicodedata.normalize("NFC", nfd)


# ---- C14: rag_reload는 로드 실패(None) 시 기존 인덱스를 지우지 않음(무중단) ----
def test_rag_reload_keeps_previous_on_load_failure(rag_client, monkeypatch):
    import app.rag.search as search

    providers = rag_client.app.state.providers
    sentinel = providers.rag
    assert sentinel is not None            # rag_client는 인덱스가 로드돼 있음
    n0 = len(sentinel.chunks)

    monkeypatch.setattr(search, "load_runtime", lambda *a, **k: None)
    r = rag_client.post("/api/rag/reload")
    assert r.status_code == 200
    assert providers.rag is sentinel       # 핵심: 기존 런타임을 None으로 덮지 않음
    assert r.json()["chunks"] == n0        # 여전히 기존 규모로 서빙


# ---- R11: 금액 숫자는 자릿수 낭독으로 치환 안 함(쉼표 경계); 진짜 긴급번호는 치환 ----
def test_tts_money_not_read_as_hotline_digits():
    out = _clean_for_tts("이번 달 요금은 119,000원입니다")
    assert "일일구" not in out      # "119"가 "일일구"로 새지 않음
    assert "119,000" in out         # 원문 유지


def test_tts_real_hotline_still_read_as_digits():
    assert "일일구" in _clean_for_tts("위급하면 119에 전화하세요")
    assert "일일이" in _clean_for_tts("범죄 신고는 112입니다")
