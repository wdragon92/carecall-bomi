"""기초연금 룰엔진 매트릭스 (RL 계열) — 경계값·한글 수사 파싱·판정 의도·되묻기 흐름.
판정은 결정론(코드)이고 고시값은 2026 상수 — 수치 경계는 상수 기준으로 고정한다."""
from app.rag.rules import check_basic_pension, detect_screen_intent, slots_from_text


# ---- RL-01: 연령 경계 64/65 ----
def test_age_boundary_64_65():
    v, m = check_basic_pension(64, None, None)
    assert v == "해당없음" and "만 65세부터" in m

    v, m = check_basic_pension(65, None, None)  # 연령 충족 → 가구 되묻기
    assert v == "확인필요" and "혼자" in m and "배우자" in m


# ---- RL-02: 소득인정액 경계 (2026 고시: 단독 247만 / 부부 395.2만, income은 만원) ----
def test_income_selection_boundaries_2026():
    assert check_basic_pension(72, "single", 247)[0] == "가능성높음"  # 정확히 기준선
    v, m = check_basic_pension(72, "single", 248)
    assert v == "가능성낮음" and "247만" in m

    assert check_basic_pension(72, "couple", 395)[0] == "가능성높음"  # 395.0 ≤ 395.2
    v, m = check_basic_pension(72, "couple", 396)
    assert v == "가능성낮음" and "395.2만" in m


# ---- RL-03: 이상 가구값("alone")은 th=None 폴백 — 크래시 없이 확인필요 ----
def test_unknown_household_value_falls_back_gracefully():
    v, m = check_basic_pension(70, "alone", 100)
    assert v == "확인필요"
    assert "모의계산" in m  # 폴백 멘트도 신청 연계 안내를 유지


# ---- RL-04: 한글 수사 나이 파싱 ----
def test_native_korean_age_words():
    assert slots_from_text("여든하나야")["age"] == 81
    assert slots_from_text("쉰아홉이야")["age"] == 59
    assert slots_from_text("예순 넷이야")["age"] == 64  # 십단위-일단위 사이 공백 허용
    assert slots_from_text("예순다섯")["age"] == 65


# ---- RL-06: 가구 형태 표현 ----
def test_household_expressions():
    assert slots_from_text("홀로 지내")["household"] == "single"
    assert slots_from_text("둘이 살아")["household"] == "couple"
    assert slots_from_text("할멈이랑 같이 살아")["household"] == "couple"


# ---- RL-14: 판정 의도 감지 — 대상어 + 질문어가 모두 있어야 ----
def test_screen_intent_needs_target_and_ask():
    assert detect_screen_intent("노령연금 되나?") == "basic_pension"
    assert detect_screen_intent("연금 받을 수 있나?") is None  # '기초/노령' 없는 일반 연금
    assert detect_screen_intent("기초연금기초연금") is None  # 질문어(ASK) 없음


# ---- WS 되묻기 흐름 (mock-e2e) ----
def _next_ai_turn(ws, tries: int = 25) -> list[dict]:
    for _ in range(tries):
        m = ws.receive_json()
        if m["type"] == "ai_turn":
            return m["bubbles"]
    raise AssertionError("ai_turn not received")


def test_pending_expires_after_two_off_topic_turns(client):
    """RL-07: 되묻기(pending) 중 딴 얘기 2턴이면 판정 문맥이 만료 —
    그 뒤의 "올해 일흔둘이야"는 일반 턴으로 처리(가구 되묻기 미등장)."""
    sid = client.post("/api/sessions").json()["session_id"]
    with client.websocket_connect(f"/ws/{sid}") as ws:
        assert ws.receive_json()["type"] == "session_ready"
        _next_ai_turn(ws)  # 선인사

        ws.send_json({"type": "user_message", "text": "기초연금 나도 받을 수 있나?"})
        b1 = _next_ai_turn(ws)
        assert "연세" in b1[0]["text"]  # 나이 되묻기 시작 (pending=2)

        ws.send_json({"type": "user_message", "text": "오늘 날씨가 참 좋네"})
        _next_ai_turn(ws)  # 딴 얘기 1 — 일반 턴 (pending 2→1)
        ws.send_json({"type": "user_message", "text": "마당에 꽃도 피었고"})
        _next_ai_turn(ws)  # 딴 얘기 2 — 일반 턴 (pending 1→0)

        ws.send_json({"type": "user_message", "text": "올해 일흔둘이야"})
        b4 = _next_ai_turn(ws)
        joined = " ".join(x["text"] for x in b4)
        assert "배우자" not in joined and "혼자 지내세요" not in joined  # 판정 문맥 만료 확인


def test_backchannel_does_not_consume_pending(client):
    """RL-13: 나이 되묻기 뒤의 "응" 백채널은 pending을 소모하지 않는다 —
    다음 턴의 "일흔둘이야"가 판정 문맥으로 이어져 가구 되묻기가 정상 진행."""
    sid = client.post("/api/sessions").json()["session_id"]
    with client.websocket_connect(f"/ws/{sid}") as ws:
        assert ws.receive_json()["type"] == "session_ready"
        _next_ai_turn(ws)  # 선인사

        ws.send_json({"type": "user_message", "text": "기초연금 나도 받을 수 있나?"})
        b1 = _next_ai_turn(ws)
        assert "연세" in b1[0]["text"]

        ws.send_json({"type": "user_message", "text": "응"})  # 백채널 — 스크리닝 미진입
        _next_ai_turn(ws)

        ws.send_json({"type": "user_message", "text": "일흔둘이야"})
        b3 = _next_ai_turn(ws)
        assert "혼자" in b3[0]["text"] and "배우자" in b3[0]["text"]  # 가구 되묻기 정상
