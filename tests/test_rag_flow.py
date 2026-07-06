"""P2 통합: WS 턴에서 RAG 게이트 → 정보 카드(kind:card) 말풍선 + TTS 대체문 (전부 목).
rag_client 픽스처는 conftest.py 공용."""

ALIEN_Q = "asdf qwer zxcv 1234"  # 문서 밖 — 목 벡터에서도 확실히 저점수


def _next_ai_turn(ws, tries: int = 12) -> list[dict]:
    for _ in range(tries):
        m = ws.receive_json()
        if m["type"] == "ai_turn":
            return m["bubbles"]
    raise AssertionError("ai_turn not received")


def test_ws_welfare_turn_appends_card(rag_client):
    sid = rag_client.post("/api/sessions").json()["session_id"]
    with rag_client.websocket_connect(f"/ws/{sid}") as ws:
        assert ws.receive_json()["type"] == "session_ready"
        _next_ai_turn(ws)  # 선인사

        ws.send_json({"type": "user_message", "text": "치매 약값이 걱정이에요"})
        bubbles = _next_ai_turn(ws)
        card = bubbles[-1]
        assert card.get("kind") == "card", f"카드 말풍선 없음: {bubbles}"
        assert card["text"].startswith("📌")
        assert "정보 기준일" in card["text"]
        meta = card.get("card") or {}
        assert meta.get("title") and meta.get("기준일")  # 구조화 카드(프론트 렌더링용)
        assert meta.get("source")  # RAG 근거 출처
        card_id = card["id"]

    # 카드 TTS는 원문(기호·수치) 대신 짧은 안내문을 합성 — 200 & 오디오 바이트
    r = rag_client.post(f"/api/sessions/{sid}/tts", json={"message_id": card_id})
    assert r.status_code == 200
    assert len(r.content) > 100


def test_ws_chitchat_turn_no_card(rag_client):
    sid = rag_client.post("/api/sessions").json()["session_id"]
    with rag_client.websocket_connect(f"/ws/{sid}") as ws:
        assert ws.receive_json()["type"] == "session_ready"
        _next_ai_turn(ws)

        ws.send_json({"type": "user_message", "text": ALIEN_Q})
        bubbles = _next_ai_turn(ws)
        assert all(b.get("kind") != "card" for b in bubbles)


def test_rest_rag_answer(rag_client):
    r = rag_client.post("/api/rag/answer", json={"question": "치매 약값이 걱정이에요"})
    d = r.json()
    assert r.status_code == 200 and d["rejected"] is False
    assert d["card"] and d["card"].startswith("📌")
    assert d["sources"] and d["top_score"] > 0

    r2 = rag_client.post("/api/rag/answer", json={"question": ALIEN_Q})
    d2 = r2.json()
    assert d2["rejected"] is True and d2["card"] is None
    assert "129" in d2["answer"]  # 정중한 거부 + 공식 연계


def test_rag_health_and_reload(rag_client):
    h = rag_client.get("/health").json()
    assert h["rag"]["loaded"] is True and h["rag"]["chunks"] == 12
    assert h["rag"]["embed_mode"] == "mock"

    r = rag_client.post("/api/rag/reload").json()
    assert r["loaded"] is True and r["chunks"] == 12
