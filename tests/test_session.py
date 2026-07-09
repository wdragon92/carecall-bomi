import asyncio
import unicodedata

from app.session import SessionStore, finding_id


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


# ---- C7: finding_id는 전체 content를 해시 (앞 20자만 쓰면 서로 다른 관찰이 유실) ----
def test_finding_id_uses_full_content_not_prefix():
    prefix = "혈압약을 자주 거르신다고 하시는 어르신의 반복 관찰"  # 20자 초과 공통 접두
    a = prefix + " — 어제는 복용 확인됨"
    b = prefix + " — 오늘도 미복용, 어지럼 호소(에스컬레이션)"
    assert a[:20] == b[:20]  # 기존 버그(content[:20])가 같은 id로 접던 조건
    assert finding_id("건강", a) != finding_id("건강", b)


def test_finding_id_stable_for_identical_content():
    # 동일 content는 여전히 같은 id → 안전망·LLM 교차 dedupe(EX-01) 유지
    c = "낙상 후 통증 — 진료 권고"
    assert finding_id("건강", c) == finding_id("건강", c)


# ---- C6: 활동(bump) 시 LRU 갱신 — 대화 중 활성 세션이 용량 축출되지 않게 ----
def test_store_bump_protects_active_session_from_eviction():
    async def scenario():
        store = SessionStore(max_sessions=2)
        a = await store.create()
        b = await store.create()  # 순서: [a, b]
        store.bump(a.id)          # 활동 → [b, a]
        c = await store.create()  # 용량 초과 → 가장 앞(b) 축출 → [a, c]
        return store, a.id, b.id, c.id

    store, aid, bid, cid = asyncio.run(scenario())
    assert store.count() == 2
    assert store.get(aid) is not None   # bump된 활성 세션 생존
    assert store.get(cid) is not None
    assert store.get(bid) is None       # bump 안 된 오래된 세션이 축출


# ---- S8: 재접속 시 last_alert 초기화 → 위기 배너 재전송 허용 ----
def test_ws_reconnect_resets_last_alert(client, app):
    sid = client.post("/api/sessions").json()["session_id"]
    sess = app.state.store.get(sid)
    sess.add_message("user", "이전 대화가 있었음")          # 이미 진행된 세션(재접속 상황)
    sess.last_alert = ("emergency", "119에 연락하세요")     # 직전 위기 배너 dedup 상태
    with client.websocket_connect(f"/ws/{sid}") as ws:
        assert ws.receive_json()["type"] == "session_ready"
    assert sess.last_alert is None


# ---- R3: 잘못된/비-dict 프레임이 턴 루프를 죽이지 않음 (끊김만 종료) ----
def test_ws_bad_frames_do_not_kill_turn_loop(client, app, monkeypatch):
    from app.core import conversation

    seen: list[str] = []

    async def fake_greet(sess):
        return None

    async def fake_turn(sess, providers, settings):
        seen.append(sess.messages[-1].text)
        await sess.send({"type": "test_ack", "n": len(seen)})

    monkeypatch.setattr(conversation, "greet", fake_greet)
    monkeypatch.setattr(conversation, "handle_turn", fake_turn)

    sid = client.post("/api/sessions").json()["session_id"]
    with client.websocket_connect(f"/ws/{sid}") as ws:
        assert ws.receive_json()["type"] == "session_ready"
        ws.send_text("{ not valid json")               # 비-JSON 프레임 → 무시
        ws.send_json([1, 2, 3])                         # 비-dict 프레임 → 무시
        ws.send_json({"type": "bogus"})                 # 알 수 없는 타입 → 무시
        ws.send_json({"type": "user_message", "text": "안녕하세요"})  # 정상 → 처리
        ack = ws.receive_json()
        assert ack["type"] == "test_ack"
    assert seen == ["안녕하세요"]  # 앞선 잘못된 프레임에도 정상 턴은 처리됨


# ---- R6: ws user_message text를 NFC로 정규화해 세션에 저장 ----
def test_ws_user_message_nfc_normalized(client, app, monkeypatch):
    from app.core import conversation

    async def fake_greet(sess):
        return None

    async def fake_turn(sess, providers, settings):
        await sess.send({"type": "test_ack"})

    monkeypatch.setattr(conversation, "greet", fake_greet)
    monkeypatch.setattr(conversation, "handle_turn", fake_turn)

    sid = client.post("/api/sessions").json()["session_id"]
    nfd = unicodedata.normalize("NFD", "각") + " 통증"      # 조합형(자모 분리) 입력
    assert nfd != unicodedata.normalize("NFC", nfd)         # 정규화 전/후가 실제로 다름
    with client.websocket_connect(f"/ws/{sid}") as ws:
        assert ws.receive_json()["type"] == "session_ready"
        ws.send_json({"type": "user_message", "text": nfd})
        assert ws.receive_json()["type"] == "test_ack"
    sess = app.state.store.get(sid)
    user_texts = [m.text for m in sess.messages if m.role == "user"]
    assert user_texts == [unicodedata.normalize("NFC", nfd)]
