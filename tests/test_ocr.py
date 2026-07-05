def _drain_until(ws, pred, max_msgs=90):
    seen = []
    for _ in range(max_msgs):
        m = ws.receive_json()
        seen.append(m)
        if pred(m):
            return m, seen
    return None, seen


def test_ocr_flow_smishing(client):
    sid = client.post("/api/sessions").json()["session_id"]
    with client.websocket_connect(f"/ws/{sid}") as ws:
        files = {"file": ("의심문자.png", b"\x89PNG\r\n\x1a\nfake-bytes", "image/png")}
        r = client.post(f"/api/sessions/{sid}/image", files=files)
        assert r.status_code == 202
        assert r.json()["upload_id"]

        done, _ = _drain_until(ws, lambda m: m.get("type") == "ocr_status" and m.get("status") == "done")
        assert done is not None

        expl, _ = _drain_until(ws, lambda m: m.get("type") == "ai_message_end")
        assert expl is not None and expl["full_text"]

        fu, _ = _drain_until(ws, lambda m: m.get("type") == "findings_update")
        assert fu is not None
        assert any(f["category"] == "사기_노출" for f in fu["findings"])


def test_image_requires_session(client):
    r = client.post("/api/sessions/nonexistent/image",
                    files={"file": ("x.png", b"data", "image/png")})
    assert r.status_code == 404
