def _drain_until(ws, pred, max_msgs=90):
    seen = []
    for _ in range(max_msgs):
        m = ws.receive_json()
        seen.append(m)
        if pred(m):
            return m, seen
    return None, seen


def _is_card_turn(m):
    return m.get("type") == "ai_turn" and any(b.get("kind") == "card" for b in m.get("bubbles", []))


def _card_of(turn):
    return next(b for b in turn["bubbles"] if b.get("kind") == "card")


def test_ocr_flow_smishing(client):
    sid = client.post("/api/sessions").json()["session_id"]
    with client.websocket_connect(f"/ws/{sid}") as ws:
        files = {"file": ("의심문자.png", b"\x89PNG\r\n\x1a\nfake-bytes", "image/png")}
        r = client.post(f"/api/sessions/{sid}/image", files=files)
        assert r.status_code == 202
        assert r.json()["upload_id"]

        done, _ = _drain_until(ws, lambda m: m.get("type") == "ocr_status" and m.get("status") == "done")
        assert done is not None

        expl, _ = _drain_until(ws, lambda m: m.get("type") == "ai_turn")
        assert expl is not None and expl["bubbles"] and expl["bubbles"][0]["text"]

        # 문서 인식 카드: 문자·메시지 + 사기 경고
        card_turn, _ = _drain_until(ws, _is_card_turn)
        assert card_turn is not None
        card = _card_of(card_turn)
        assert "문자·메시지" in card["text"]
        assert "사기" in card["text"] and "링크" in card["text"]

        fu, _ = _drain_until(ws, lambda m: m.get("type") == "findings_update")
        assert fu is not None
        assert any(f["category"] == "사기_노출" for f in fu["findings"])


def test_ocr_welfare_notice_card(client):
    """복지 안내문 우편 → 종류 인식 + 카드의 기한이 원문 표기 그대로(T2)."""
    sid = client.post("/api/sessions").json()["session_id"]
    with client.websocket_connect(f"/ws/{sid}") as ws:
        files = {"file": ("복지안내문.png", b"\x89PNG\r\n\x1a\nfake-bytes", "image/png")}
        r = client.post(f"/api/sessions/{sid}/image", files=files)
        assert r.status_code == 202

        card_turn, _ = _drain_until(ws, _is_card_turn)
        assert card_turn is not None
        card = _card_of(card_turn)
        assert "복지·관공서 안내문" in card["text"]
        assert "7월 31일" in card["text"]  # mock 원문의 신청 기한 그대로


def test_doc_rules_classify_and_t2():
    """룰 폴백 분류 3종 + 카드 수치는 원문 문자열만 사용."""
    from app.core.ocr_doc import classify_by_rules, compose_doc_card
    from app.services.mock import _OCR_BILL, _OCR_SMS, _OCR_WELFARE

    bill = classify_by_rules(_OCR_BILL)
    assert bill.종류 == "고지서·청구서"
    text, tts = compose_doc_card(bill)
    assert "38,200원" in text and "2026-07-25" in text  # 원문 표기 그대로
    assert tts and "38,200" not in tts  # 카드 낭독 대신 짧은 안내(수치 미낭독)

    sms = classify_by_rules(_OCR_SMS)
    assert sms.종류 == "문자·메시지" and sms.사기_의심

    welf = classify_by_rules(_OCR_WELFARE)
    assert welf.종류 == "복지·관공서 안내문"
    assert any("7월 31일까지" in it for it in welf.해야할일)


def test_doc_llm_fabricated_numbers_dropped():
    """LLM이 원문에 없는 숫자를 내면 코드가 그 항목을 버린다(T2 강제)."""
    from app.core.ocr_doc import _from_llm

    src = "한국전력공사 전기요금 청구서\n청구금액: 38,200원\n납기일: 2026-07-25"
    data = {
        "종류": "고지서·청구서",
        "한줄요약": "전기요금 안내예요",
        "해야할일": ["7월 25일까지 38,200원 납부", "99,000원 추가 납부"],  # 뒤엣것은 지어냄
        "사기_의심": False,
    }
    doc = _from_llm(data, src)
    assert doc is not None
    assert doc.해야할일 == ["7월 25일까지 38,200원 납부"]


def test_date_regex_rejects_number_fragments():
    """번호 조각('0123-4567'→'23-45')이 날짜로 새지 않고, 실제 날짜형만 인식(과매칭 방지)."""
    from app.core.ocr_doc import _DATE

    assert _DATE.findall("고객번호: 0123-4567") == []   # 전화·고객번호 조각은 날짜 아님
    assert _DATE.findall("국번없이 123") == []
    assert "2026-07-25" in _DATE.findall("납기일: 2026-07-25")   # ISO 날짜는 그대로
    assert "7월 31일까지" in _DATE.findall("7월 31일까지 신청")   # 한글 월/일도 그대로


def test_bill_card_due_is_real_date_not_number_fragment():
    """청구서 카드 기한은 원문 날짜(2026-07-25)만 — 고객번호 조각 '23-45' 금지."""
    from app.core.ocr_doc import classify_by_rules, compose_doc_card
    from app.services.mock import _OCR_BILL

    text, _ = compose_doc_card(classify_by_rules(_OCR_BILL))
    assert "2026-07-25" in text and "38,200원" in text
    assert "23-45" not in text          # '0123-4567' 조각이 기한으로 새지 않음


def test_fabricated_substring_number_is_caught():
    """조작된 짧은 숫자가 원문의 긴 숫자에 부분열로 묻혀도 잡아낸다('456'⊄'4567')."""
    from app.core.ocr_doc import _fabricated, _src_runs

    runs = _src_runs("고객번호 4567 계좌 123456789")
    assert _fabricated("456원 납부", runs) is True      # 456 ⊂ 4567 → 이제 차단
    assert _fabricated("234 송금", runs) is True        # 234 ⊂ 123456789 → 차단
    assert _fabricated("4567 확인", runs) is False      # 원문 그대로면 통과


def test_llm_substring_fabrication_dropped():
    """_from_llm: 원문 숫자의 부분열에 불과한 조작 항목도 버린다(부분열 누출 차단)."""
    from app.core.ocr_doc import _from_llm

    src = "○○카드 고객번호: 4567\n결제 안내"
    data = {"종류": "기타", "한줄요약": "결제 안내예요",
            "해야할일": ["456원 결제"], "사기_의심": False}  # 456은 4567의 부분열일 뿐
    doc = _from_llm(data, src)
    assert doc is not None and doc.해야할일 == []


def test_image_requires_session(client):
    r = client.post("/api/sessions/nonexistent/image",
                    files={"file": ("x.png", b"data", "image/png")})
    assert r.status_code == 404
