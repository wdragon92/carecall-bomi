def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert set(body["providers"]) == {"llm", "stt", "tts", "ocr", "embed"}
    assert all(v == "mock" for v in body["providers"].values())
    assert "rag" in body  # 인덱스 유무와 무관하게 상태 노출
