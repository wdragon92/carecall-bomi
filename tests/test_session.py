def test_session_create(client):
    r = client.post("/api/sessions")
    assert r.status_code == 200
    assert r.json()["session_id"]


def test_ws_session_ready(client):
    sid = client.post("/api/sessions").json()["session_id"]
    with client.websocket_connect(f"/ws/{sid}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "session_ready"
        assert msg["session_id"] == sid
        assert set(msg["providers"]) == {"llm", "stt", "tts", "ocr", "embed"}
