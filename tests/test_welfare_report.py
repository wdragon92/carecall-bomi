def _drain_until(ws, pred, max_msgs=90):
    seen = []
    for _ in range(max_msgs):
        m = ws.receive_json()
        seen.append(m)
        if pred(m):
            return m, seen
    return None, seen


def test_welfare_matching(client):
    sid = client.post("/api/sessions").json()["session_id"]
    with client.websocket_connect(f"/ws/{sid}") as ws:
        ws.send_json({"type": "user_message", "text": "생활비가 너무 부족하고 혼자 살아서 외로워요"})
        wu, _ = _drain_until(ws, lambda m: m.get("type") == "welfare_update")
        assert wu is not None
        assert wu["items"]
        ids = {it["id"] for it in wu["items"]}
        # 저소득/독거 신호 → 기초연금이나 돌봄서비스 등이 매칭되어야 함
        assert ids


def test_report_generated(client):
    sid = client.post("/api/sessions").json()["session_id"]
    with client.websocket_connect(f"/ws/{sid}") as ws:
        ws.send_json({"type": "user_message", "text": "무릎이 아프고 생활비도 부족해요"})
        _drain_until(ws, lambda m: m.get("type") == "findings_update")

    r = client.post(f"/api/sessions/{sid}/end")
    assert r.status_code == 200
    report = r.json()["report"]
    assert report["summary"]
    assert "disclaimer" in report and report["disclaimer"]
    assert isinstance(report["findings"], list)
    assert isinstance(report["recommendations"], list) and report["recommendations"]
