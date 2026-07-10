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


# ---- S8: 재접속 시 sent_alerts 초기화 → 위기 배너 재전송 허용 ----
def test_ws_reconnect_resets_last_alert(client, app):
    sid = client.post("/api/sessions").json()["session_id"]
    sess = app.state.store.get(sid)
    sess.add_message("user", "이전 대화가 있었음")          # 이미 진행된 세션(재접속 상황)
    sess.sent_alerts = {("emergency", "119에 연락하세요")}  # 직전 위기 배너 dedup 상태
    with client.websocket_connect(f"/ws/{sid}") as ws:
        assert ws.receive_json()["type"] == "session_ready"
    assert sess.sent_alerts == set()


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


# ---- WS-BUSYLOOP: 끊긴 소켓에서 receive가 WebSocketDisconnect가 아니라 RuntimeError로
#      나는 비정상 종료 경로에서, '잘못된 프레임'으로 오판해 무한 재수신(이벤트 루프 점유)에
#      빠지지 않아야 한다. (프로덕션 재접속/churn 부하에서 실측된 busy-loop 회귀) ----
def test_ws_recv_error_on_disconnected_socket_does_not_busyloop():
    from types import SimpleNamespace

    from starlette.websockets import WebSocketState

    from app.routes.ws import ws_endpoint

    async def scenario():
        store = SessionStore()
        sess = await store.create()
        sess.add_message("user", "이미 진행된 세션")  # 비어있지 않음 → greet(sleep) 스킵
        fake_app = SimpleNamespace(state=SimpleNamespace(
            store=store,
            providers=SimpleNamespace(modes={}),         # session_ready에만 사용
            settings=SimpleNamespace(greet_delay_seconds=0.0),
        ))

        class FakeWS:
            """끊긴 뒤 receive_json이 WebSocketDisconnect가 아니라 RuntimeError를 내는 소켓.
            실측 조건 재현: receive_json이 검사하는 application_state만 DISCONNECTED로 바뀌고
            client_state는 CONNECTED로 남는다(=client_state만 보던 초기 수정이 놓쳤던 경로)."""

            def __init__(self):
                self.app = fake_app
                self.application_state = WebSocketState.CONNECTED
                self.client_state = WebSocketState.CONNECTED
                self.calls = 0

            async def accept(self):
                pass

            async def send_json(self, _payload):
                pass

            async def receive_json(self):
                self.calls += 1
                self.application_state = WebSocketState.DISCONNECTED  # receive_json 가드가 보는 상태
                raise RuntimeError('WebSocket is not connected. Need to call "accept" first.')

            async def close(self):
                pass

        fws = FakeWS()
        # 수정 전이라면 continue로 무한 재수신 → wait_for 타임아웃(TimeoutError)로 실패한다.
        await asyncio.wait_for(ws_endpoint(fws, sess.id), timeout=3.0)
        return sess, fws

    sess, fws = asyncio.run(scenario())
    assert fws.calls == 1        # 1회 시도 후 즉시 종료(무한 재시도 아님)
    assert sess.ws is None       # finally 정리로 stale 소켓 참조 해제


def test_ws_recv_error_fuse_bounds_busyloop_even_if_state_stays_connected():
    """상태가 CONNECTED로 남아도(상태 판정이 빗나가는 최악의 경우) 연속 수신 예외 퓨즈가
    busy-loop을 유한하게 끊는다 — 이중 안전장치."""
    from types import SimpleNamespace

    from starlette.websockets import WebSocketState

    from app.routes.ws import ws_endpoint

    async def scenario():
        store = SessionStore()
        sess = await store.create()
        sess.add_message("user", "이미 진행된 세션")
        fake_app = SimpleNamespace(state=SimpleNamespace(
            store=store,
            providers=SimpleNamespace(modes={}),
            settings=SimpleNamespace(greet_delay_seconds=0.0),
        ))

        class StuckWS:
            def __init__(self):
                self.app = fake_app
                self.application_state = WebSocketState.CONNECTED  # 끝까지 CONNECTED(오판 상황)
                self.client_state = WebSocketState.CONNECTED
                self.calls = 0

            async def accept(self):
                pass

            async def send_json(self, _payload):
                pass

            async def receive_json(self):
                self.calls += 1
                raise RuntimeError("boom")  # 매번 즉시 예외(대기 없음)

            async def close(self):
                pass

        sws = StuckWS()
        await asyncio.wait_for(ws_endpoint(sws, sess.id), timeout=3.0)
        return sws

    sws = asyncio.run(scenario())
    assert sws.calls == 5        # 퓨즈(recv_errors >= 5)에서 종료 — 무한 아님
