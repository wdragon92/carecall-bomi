from app.core import safety


def _kinds(text):
    return {d["_kind"] for d in safety.scan(text)}


def test_scan_chest_tightness_is_medical_soon():
    assert "medical_soon" in _kinds("어르신: 요즘 가슴이 답답하고 숨이 차요")


def test_scan_stroke_is_emergency():
    assert "medical_emergency" in _kinds("어르신: 갑자기 한쪽 팔에 힘이 없고 말이 어눌해요")


def test_scan_passive_suicide_is_warning():
    assert "suicide_warning" in _kinds("어르신: 이제 죽을 때가 됐나 봐요")
    assert "suicide_warning" in _kinds("어르신: 나 같은 건 없어져도 되지, 짐만 되는 것 같아")


def test_scan_direct_suicide_is_acute():
    assert "suicide_acute" in _kinds("어르신: 다 그만두고 죽고 싶어요")


def test_scan_euphemistic_dialect_suicide_is_warning():
    # 레드팀 실측: 완곡·사투리 수동적 자살신호가 결정망을 회피하던 공백 보강
    assert "suicide_warning" in _kinds("어르신: 마 인자 고마 사는 게 다 의미가 없다카이")
    assert "suicide_warning" in _kinds("어르신: 밤에 눈 감으면 그냥 안 깼으면 싶어")
    assert "suicide_warning" in _kinds("어르신: 사는 게 무의미해요")
    assert safety.alert(_kinds("어르신: 사는 게 다 의미가 없다카이"))[0] == "warning"


def test_scan_meaningless_task_no_false_positive():
    # 삶이 아니라 '일/얘기'가 의미없다는 건 자살신호가 아님(과탐 방지)
    assert "suicide_warning" not in _kinds("어르신: 그 얘기는 이제 의미가 없어요")


def test_scan_negation_no_false_positive():
    k = _kinds("어르신: 가슴이 답답하진 않아요. 죽고 싶지 않아요")
    assert "medical_soon" not in k
    assert "suicide_acute" not in k


def test_scan_negation_precise_direct_only():
    # S4: 부정 판정은 '구절을 직접 부정하는 형태'만 — 무관한 뒷절의 '없'에 반응하지 않는다.
    # (a) 직접 부정(지/진 않)은 계속 미매칭 유지
    assert "suicide_acute" not in _kinds("어르신: 죽고 싶지 않아요")
    assert "medical_soon" not in _kinds("어르신: 가슴이 답답하진 않아요")
    # (b) 무관한 뒷절의 '없'이 진짜 증상을 억제하면 안 됨(과거 미탐 → 이제 매칭)
    assert "medical_soon" in _kinds("어르신: 숨이 차고 기운이 없어")
    assert "medical_soon" in _kinds("어르신: 가슴이 뻐근하고 힘이 없어")
    # (c) 명사화 부정('…다는 생각은 안/없')은 정확히 미매칭 (과거 '안/못' 누락 오탐 제거)
    assert "suicide_acute" not in _kinds("어르신: 죽고 싶다는 생각은 안 해요")
    assert "suicide_acute" not in _kinds("어르신: 죽고 싶다는 생각은 없어요")
    # 참-양성은 그대로 유지
    assert "suicide_acute" in _kinds("어르신: 요즘 죽고 싶다는 생각이 자꾸 들어요")


def test_scan_s12_homonym_needs_risk_context():
    # S12: 동음이의 위험 어휘는 '실행/증상' 문맥과 결합해서만 발동 — 일상어 오발 제거
    assert "suicide_acute" not in _kinds("어르신: 올해는 농약 쳤어")
    assert "suicide_acute" not in _kinds("어르신: 고기 구우려고 번개탄 샀어")
    assert "suicide_acute" not in _kinds("어르신: 유서 깊은 마을이지")
    assert "suicide_acute" not in _kinds("어르신: 그럼 나 먼저 간다")  # 통화 종료 인사
    assert "medical_emergency" not in _kinds("어르신: 긴장돼서 식은땀 났어")
    assert "medical_emergency" not in _kinds("어르신: 좋아서 가슴이 터질 만큼 기뻤어")
    # 실제 위험표현은 유지
    assert "suicide_acute" in _kinds("어르신: 농약을 마셨어")
    assert "suicide_acute" in _kinds("어르신: 번개탄 피우려고 사놨어")
    assert "suicide_acute" in _kinds("어르신: 유서 써놨어")
    assert "suicide_acute" in _kinds("어르신: 목을 매려고 했어")
    assert "medical_emergency" in _kinds("어르신: 식은땀이 나고 가슴이 조여")
    assert "medical_emergency" in _kinds("어르신: 가슴이 터질 듯이 아파")


def test_scan_s13_passive_suicide_additions():
    # S13: 완곡 죽음 소망·체념 미탐 보강
    for utter in ["죽는 게 나아", "죽는 게 낫겠어", "저세상 가고 싶어", "하늘나라 가고 싶어",
                  "눈 감고 싶어", "사라지고 싶어", "다 끝내고 싶어"]:
        assert "suicide_warning" in _kinds("어르신: " + utter), utter
        assert safety.alert(_kinds(utter))[0] == "warning"
    # 과탐 조심: 고인 언급·반대 의미는 오발 아님
    assert "suicide_warning" not in _kinds("어르신: 하늘나라 가신 어머니가 그리워")
    assert "suicide_warning" not in _kinds("어르신: 죽는 게 나쁜 거 다 알아")


def test_scan_s13_fraud_completed_actions():
    # S13: 이미 실행한 완료형(사기 특정 문맥) — 결정망이 잡는다
    for utter in ["링크를 눌렀어", "비밀번호를 불러줬어", "불러준 계좌로 부쳤어",
                  "시키는 대로 송금했어"]:
        assert "fraud_exposure" in _kinds("어르신: " + utter), utter
    # 과탐 조심: 가족 송금·요리·노래는 오발 아님
    assert "fraud_exposure" not in _kinds("어르신: 손자한테 용돈 송금했어")
    assert "fraud_exposure" not in _kinds("어르신: 명절이라 전 부쳤어")
    assert "fraud_exposure" not in _kinds("어르신: 손주한테 노래 불러줬어")


def test_scan_s13_numbness_and_radiation_is_medical_soon():
    # S13: 편측 저림·심장 방사통 — 빠른 진료(warning), 단독 저림은 119 과알람 아님
    for utter in ["한쪽이 저리고 힘이 없는 것 같아", "왼팔이 저려", "손발이 저려",
                  "턱이 아프고 팔이 저려"]:
        k = _kinds("어르신: " + utter)
        assert "medical_soon" in k, utter
        assert safety.alert(k)[0] == "warning"


def test_scan_medical_soon_rule13_bleeding_weightloss_edema():
    # S14/S15: 혈변·체중감소·부종 규칙(#13) scan·alert 회귀
    for utter in ["요즘 혈변이 보여", "몸무게가 줄어 걱정이야", "종아리가 붓네", "다리가 부었어"]:
        k = _kinds("어르신: " + utter)
        assert "medical_soon" in k, utter
        assert safety.alert(k)[0] == "warning"


def test_scan_cardiac_via_gi():
    # 무증상 심근경색: 소화불량으로 위장 (보고서 반영)
    assert "medical_soon" in _kinds("어르신: 요즘 자꾸 체한 것 같고 명치가 답답해요")


def test_scan_delirium():
    # 갑작스런 혼란/헛것 → 섬망(탈수·요로감염) 의심
    assert "medical_soon" in _kinds("어르신: 어제부터 갑자기 헛것이 보이고 날짜도 모르겠어요")


def test_scan_mood_low_is_card_only():
    k = _kinds("어르신: 요즘 만사가 귀찮고 다 소용없어요")
    assert "mood_low" in k
    # mood_low 단독은 경보 배너를 띄우지 않음(카드로만 관찰)
    assert safety.alert({"mood_low"})[0] is None


def test_alert_levels():
    assert safety.alert({"suicide_acute"})[0] == "emergency"
    assert safety.alert({"medical_emergency"})[0] == "emergency"
    assert safety.alert({"suicide_warning"})[0] == "warning"
    assert safety.alert({"medical_soon"})[0] == "warning"
    # LLM 심각신호는 건강/심리로 분리 (109는 심리 전용).
    # LLM 단독 심리 플래그는 warning — 급성은 결정망 전담, 환각 오경보 방지(실측)
    assert safety.alert(set(), {"psych"})[0] == "warning"
    assert safety.alert(set(), {"medical"})[0] == "emergency"
    assert safety.alert(set())[0] is None


def _drain_until(ws, pred, max_msgs=90):
    seen = []
    for _ in range(max_msgs):
        m = ws.receive_json()
        seen.append(m)
        if pred(m):
            return m, seen
    return None, seen


def test_ws_chest_tightness_warning(client):
    sid = client.post("/api/sessions").json()["session_id"]
    with client.websocket_connect(f"/ws/{sid}") as ws:
        ws.send_json({"type": "user_message", "text": "요즘 가슴이 답답하고 숨이 차요"})
        alert, seen = _drain_until(ws, lambda m: m.get("type") == "urgent_alert")
        assert alert is not None and alert["level"] == "warning"
        fu = [m for m in seen if m.get("type") == "findings_update"][-1]
        assert any(f["category"] == "건강" for f in fu["findings"])


def test_ws_direct_suicide_emergency(client):
    sid = client.post("/api/sessions").json()["session_id"]
    with client.websocket_connect(f"/ws/{sid}") as ws:
        ws.send_json({"type": "user_message", "text": "다 그만두고 죽고 싶어요"})
        alert, _ = _drain_until(ws, lambda m: m.get("type") == "urgent_alert")
        assert alert is not None and alert["level"] == "emergency"


def test_ws_euphemistic_suicide_warning(client):
    # P1: 완곡·사투리 수동적 자살신호도 실시간 경보 배너가 떠야 한다
    sid = client.post("/api/sessions").json()["session_id"]
    with client.websocket_connect(f"/ws/{sid}") as ws:
        ws.send_json({"type": "user_message", "text": "마 인자 고마 사는 게 다 의미가 없다카이"})
        alert, _ = _drain_until(ws, lambda m: m.get("type") == "urgent_alert")
        assert alert is not None and alert["level"] == "warning"


def test_crisis_hold_persists_after_euphemistic_suicide():
    # P1 핵심: 완곡 자살신호 뒤 '해본 소리' 회피 턴에도 위기 수위 2턴 유지 → 저녁수다 이탈 방지
    from types import SimpleNamespace

    from app.core import conversation

    sess = SimpleNamespace(crisis_hold=None)
    _, level1, _ = conversation._resolve_signal(sess, "사는 게 다 의미가 없다카이", False)
    assert level1 == "suicide"
    assert sess.crisis_hold == ("suicide", 2)
    _, level2, _ = conversation._resolve_signal(sess, "아이다 마, 그냥 해본 소리다", False)
    assert level2 == "suicide"  # 새 신호가 없어도 직전 위기가 유지됨
