"""P3: 기초연금 룰엔진(결정론) + WS 판정 대화 흐름 + 리포트 연계 (전부 목)."""
from app.rag.rules import check_basic_pension, detect_screen_intent, merge_slots, slots_from_text


def test_rules_verdicts():
    v, m = check_basic_pension(60, "single", None)
    assert v == "해당없음" and "65세" in m

    v, m = check_basic_pension(None, None, None)
    assert v == "확인필요" and "연세" in m  # 나이 되묻기

    v, m = check_basic_pension(70, None, None)
    assert v == "확인필요" and ("혼자" in m or "배우자" in m)  # 가구 되묻기

    v, m = check_basic_pension(70, "single", None)
    assert v == "확인필요" and "모의계산" in m and "247만" in m  # 고시값 안내(코드 삽입, T2)

    # 2026 고시값 판정 (단독 247만 / 부부 395.2만 — 복지부 보도자료, income은 만원 단위)
    v, _ = check_basic_pension(72, "single", 100)
    assert v == "가능성높음"
    v, m = check_basic_pension(72, "single", 300)
    assert v == "가능성낮음" and "247만" in m
    v, _ = check_basic_pension(72, "couple", 380)
    assert v == "가능성높음"


def test_screen_intent():
    assert detect_screen_intent("기초연금 나도 받을 수 있나?") == "basic_pension"
    assert detect_screen_intent("나도 기초 연금 대상이 되나 궁금해") == "basic_pension"
    assert detect_screen_intent("기초연금이 뭐예요?") is None  # 정보 질문은 RAG 경로
    assert detect_screen_intent("월세 지원 받을 수 있나?") is None  # 다른 제도(룰 미구현)


def test_slots_regex():
    s = slots_from_text("올해 일흔둘이야. 혼자 살아.")
    assert s["age"] == 72 and s["household"] == "single"
    assert slots_from_text("만 68세입니다")["age"] == 68
    assert slots_from_text("영감이랑 둘이 살아")["household"] == "couple"
    assert slots_from_text("아이고 무릎이야")["age"] is None

    merged = merge_slots({"age": 72, "household": None, "income": None},
                         {"age": None, "household": "single", "income": None})
    assert merged == {"age": 72, "household": "single", "income": None}

    # 정정 발화: 최신 확인값이 기존 값을 이긴다
    corrected = merge_slots({"age": 72, "household": "single", "income": None},
                            {"age": 64, "household": None, "income": None})
    assert corrected["age"] == 64 and corrected["household"] == "single"


def test_slots_correction_last_mention_wins():
    s = slots_from_text("올해 일흔둘이야. 아 잠깐, 아니다 예순넷이야.")
    assert s["age"] == 64
    s = slots_from_text("일흔둘이라 그랬나, 지금은 만 74세야")
    assert s["age"] == 74
    s = slots_from_text("혼자 살다가 작년부터 영감이랑 같이 살아")
    assert s["household"] == "couple"


def test_rest_screen_endpoint(rag_client):
    r = rag_client.post("/api/rag/screen", json={"slots": {"age": 60}}).json()
    assert r["판정"] == "해당없음" and r["apply_package"] is None

    r = rag_client.post("/api/rag/screen", json={"slots": {"age": 72, "household": "single"}}).json()
    assert r["판정"] == "확인필요"
    pkg = r["apply_package"]
    assert pkg and pkg["서비스명"] == "기초연금" and "신분증" in pkg["필요서류"]

    r = rag_client.post("/api/rag/screen", json={"slots": {}}).json()
    assert r["판정"] == "확인필요" and r["apply_package"] is None  # 나이부터 확인


def _next_ai_turn(ws, tries: int = 12) -> list[dict]:
    for _ in range(tries):
        m = ws.receive_json()
        if m["type"] == "ai_turn":
            return m["bubbles"]
    raise AssertionError("ai_turn not received")


def test_ws_screening_dialog_and_report(rag_client):
    sid = rag_client.post("/api/sessions").json()["session_id"]
    with rag_client.websocket_connect(f"/ws/{sid}") as ws:
        assert ws.receive_json()["type"] == "session_ready"
        _next_ai_turn(ws)  # 선인사

        ws.send_json({"type": "user_message", "text": "기초연금 나도 받을 수 있나?"})
        b1 = _next_ai_turn(ws)
        assert "연세" in b1[0]["text"] and all(b.get("kind") != "card" for b in b1)

        ws.send_json({"type": "user_message", "text": "올해 일흔둘이야"})
        b2 = _next_ai_turn(ws)
        assert "혼자" in b2[0]["text"] or "배우자" in b2[0]["text"]

        ws.send_json({"type": "user_message", "text": "혼자 살아"})
        b3 = _next_ai_turn(ws)
        assert b3[-1].get("kind") == "card"
        assert b3[-1]["text"].startswith("📝") and "신분증" in b3[-1]["text"]

    rep = rag_client.post(f"/api/sessions/{sid}/end").json()["report"]
    assert any(p["서비스명"] == "기초연금" for p in rep["apply_packages"])
    assert any(w["이름"] == "기초연금" for w in rep["welfare"])
