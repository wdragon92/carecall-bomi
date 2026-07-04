def test_stt_endpoint(client):
    sid = client.post("/api/sessions").json()["session_id"]
    r = client.post(
        f"/api/sessions/{sid}/audio",
        files={"file": ("a.wav", b"\x00" * 256, "audio/wav")},
    )
    assert r.status_code == 200
    assert r.json()["text"]  # mock STT returns a sample sentence


def test_tts_returns_audio(client):
    sid = client.post("/api/sessions").json()["session_id"]
    mid = None
    with client.websocket_connect(f"/ws/{sid}") as ws:
        for _ in range(8):
            m = ws.receive_json()
            if m.get("type") == "ai_message_start":
                mid = m["id"]
            if m.get("type") == "ai_message_end":
                break
    assert mid
    r = client.post(f"/api/sessions/{sid}/tts", json={"message_id": mid})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/")
    assert len(r.content) > 100


def test_tts_bad_message(client):
    sid = client.post("/api/sessions").json()["session_id"]
    r = client.post(f"/api/sessions/{sid}/tts", json={"message_id": "nope"})
    assert r.status_code == 404
