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


def test_scan_negation_no_false_positive():
    k = _kinds("어르신: 가슴이 답답하진 않아요. 죽고 싶지 않아요")
    assert "medical_soon" not in k
    assert "suicide_acute" not in k


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
    assert safety.alert({"mood_low"}, False)[0] is None


def test_alert_levels():
    assert safety.alert({"suicide_acute"}, False)[0] == "emergency"
    assert safety.alert({"medical_emergency"}, False)[0] == "emergency"
    assert safety.alert({"suicide_warning"}, False)[0] == "warning"
    assert safety.alert({"medical_soon"}, False)[0] == "warning"
    assert safety.alert(set(), True)[0] == "emergency"
    assert safety.alert(set(), False)[0] is None


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
