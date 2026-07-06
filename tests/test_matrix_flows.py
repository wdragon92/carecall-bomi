"""흐름 매트릭스 (OC·SS·PS·RS 계열) — OCR 문서 규칙, 세션 수명/격리,
페르소나 프롬프트 고정값, REST 방어선. 전부 목·결정적 입력."""
import time
from types import SimpleNamespace

from app.core import conversation
from app.core.ocr_doc import DocInfo, _from_llm, classify_by_rules, compose_doc_card
from app.core.prompts import CONTACTS_LINE, GREETINGS, chat_system, greeting, period_of_hour
from app.routes.http import _clean_for_tts, _fmt_from
from app.services.mock import MockSTT
from app.session import Session, SessionStore


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


# ======== OC: OCR 문서 흐름 ========

# ---- OC-02: 빈 OCR → 재촬영 안내 한 덩어리, ocr_texts 미적재 ----
async def test_empty_ocr_asks_retake_without_storing():
    class _EmptyOCR:
        async def extract_text(self, image_bytes, fmt, name=""):
            return "   "  # 공백뿐 → 알맹이 없음

    class _EchoLLM:
        async def chat(self, messages, **opts):
            return "사진이 흐려서 글자를 못 읽었어요.\n\n밝은 곳에서 다시 한 번 찍어 주시겠어요?"

    sess = Session("t")
    sess.ws = _FakeWS()
    providers = SimpleNamespace(ocr=_EmptyOCR(), mocr=_EmptyOCR(),
                                llm=_EchoLLM(), mllm=_EchoLLM(), modes={"llm": "mock"})
    await conversation.handle_image(sess, providers, b"fake-bytes", "png", "doc.png", "up1")

    statuses = [m["status"] for m in sess.ws.sent if m["type"] == "ocr_status"]
    assert statuses == ["processing", "done"]  # 'error' 아님 — 읽긴 했으나 빈 내용
    turns = [m for m in sess.ws.sent if m["type"] == "ai_turn"]
    assert len(turns) == 1
    assert len(turns[0]["bubbles"]) == 1  # single=True — 안내가 여러 풍선으로 안 쪼개짐
    assert sess.ocr_texts == []  # 빈 인식 결과는 추출 문맥에 남기지 않음


# ---- OC-05: 사기 의심 카드의 고정 행동 수칙 줄 ----
def test_scam_card_fixed_action_lines():
    doc = DocInfo(종류="문자·메시지", 사기_의심=True, 사기_이유="출처 불명 링크로 유도")
    text, tts = compose_doc_card(doc)
    lines = text.split("\n")
    assert lines[0].startswith("🚨")
    assert "⚠️ 사기 의심: 출처 불명 링크로 유도" in lines
    assert "· 링크를 누르지 마세요" in lines
    assert "· 개인정보·돈을 보내지 마세요" in lines
    assert tts.startswith("이 문자는 사기로 의심돼요")


# ---- OC-06: Web발신 표지는 문자·메시지로 분류하되 미끼 없으면 사기 아님 ----
def test_web_sms_without_bait_not_scam():
    doc = classify_by_rules("[Web발신] 내일 경로당 모임 안내입니다")
    assert doc.종류 == "문자·메시지"
    assert doc.사기_의심 is False
    assert doc.한줄요약 == "휴대전화로 온 문자 내용이에요"


# ---- OC-07: 알맹이 없는 문서는 카드 생략 ----
def test_empty_docinfo_no_card():
    assert compose_doc_card(DocInfo()) == ("", "")


# ---- OC-08: LLM이 지어낸 숫자(3자리+)는 드랍, 사기 플래그는 보존 ----
def test_llm_fabricated_number_dropped_scam_flag_kept():
    src = "[Web발신] 미납 통행료 3,500원이 있습니다. 링크 확인 http://pay.example"
    data = {
        "종류": "문자·메시지",
        "한줄요약": "통행료 3,500원 미납이라는 문자예요",  # 원문 숫자 → 유지
        "해야할일": ["385,000원 즉시 송금"],  # 원문에 없는 숫자 → 드랍
        "사기_의심": True,
        "사기_이유": "출처 불명 링크로 결제 유도",
    }
    doc = _from_llm(data, src)
    assert doc is not None
    assert "3,500원" in doc.한줄요약
    assert doc.해야할일 == []  # 조작 숫자 항목 제거
    assert doc.사기_의심 is True and doc.사기_이유  # 판단 플래그는 검증과 무관하게 보존


# ---- OC-11: 업로드 포맷 판별 4케이스 ----
def test_fmt_from_variants():
    assert _fmt_from("사진.jpeg", None) == "jpg"  # jpeg → jpg 정규화
    assert _fmt_from("scan.PDF", None) == "pdf"  # 확장자 대소문자 무시
    assert _fmt_from(None, "image/png") == "png"  # 파일명 없으면 content-type
    assert _fmt_from("noext", "application/octet-stream") == "jpg"  # 최후 기본값


# ======== SS: 세션 수명 ========

def _next_ai_turn(ws, tries: int = 25) -> list[dict]:
    for _ in range(tries):
        m = ws.receive_json()
        if m["type"] == "ai_turn":
            return m["bubbles"]
    raise AssertionError("ai_turn not received")


# ---- SS-01 [mock-e2e]: 재연결 시 인사는 1회뿐 ----
def test_reconnect_greets_only_once(app, client):
    sid = client.post("/api/sessions").json()["session_id"]
    with client.websocket_connect(f"/ws/{sid}") as ws:
        assert ws.receive_json()["type"] == "session_ready"
        _next_ai_turn(ws)  # 최초 접속 선인사

    with client.websocket_connect(f"/ws/{sid}") as ws2:  # 재연결
        assert ws2.receive_json()["type"] == "session_ready"
        ws2.send_json({"type": "user_message", "text": "무릎이 아파서 걷기가 힘들어"})
        reply = _next_ai_turn(ws2)
        assert "편찮으시다니" in reply[0]["text"]  # 첫 ai_turn이 재인사가 아니라 내 발화의 응답

    sess = app.state.store.get(sid)
    greets = [m for m in sess.messages if m.role == "assistant" and m.via == "system"]
    assert len(greets) == 1  # 선인사 누적 없음


# ---- SS-02 [mock-e2e]: 없는 세션 WS → no_session 에러 ----
def test_ws_unknown_session_error(client):
    with client.websocket_connect("/ws/no-such-session") as ws:
        m = ws.receive_json()
        assert m["type"] == "error" and m["code"] == "no_session"


# ---- SS-03: TTL 초과 세션만 sweep ----
async def test_ttl_sweep_removes_expired_only():
    store = SessionStore(ttl_min=1)
    stale = await store.create()
    fresh = await store.create()
    stale.last_active = time.monotonic() - 61  # TTL(60초) 경과로 조작

    assert await store.sweep() == 1
    assert store.get(stale.id) is None
    assert store.get(fresh.id) is fresh
    assert store.count() == 1


# ---- SS-04: 용량 초과 시 가장 오래 활동 없는 세션 축출 (LRU) ----
async def test_lru_eviction_respects_recent_touch():
    store = SessionStore(max_sessions=2)
    a = await store.create()
    b = await store.create()
    assert store.get(a.id) is a  # a를 touch → b가 최고령이 됨
    c = await store.create()

    assert store.count() == 2
    assert store.get(b.id) is None  # b 축출
    assert store.get(a.id) is a and store.get(c.id) is c


# ---- SS-05 [mock-e2e]: 빈 발화는 무시, 다음 정상 턴은 그대로 진행 ----
def test_blank_utterance_ignored_then_normal_turn(app, client):
    sid = client.post("/api/sessions").json()["session_id"]
    with client.websocket_connect(f"/ws/{sid}") as ws:
        assert ws.receive_json()["type"] == "session_ready"
        _next_ai_turn(ws)  # 선인사

        ws.send_json({"type": "user_message", "text": "   "})  # 공백뿐 → 무응답
        ws.send_json({"type": "user_message", "text": "요즘 밤에 잠을 통 못 자요"})
        reply = _next_ai_turn(ws)
        assert "주무시" in reply[0]["text"]  # 빈 발화가 아니라 정상 발화에 대한 응답

    sess = app.state.store.get(sid)
    assert len([m for m in sess.messages if m.role == "user"]) == 1  # 빈 발화 미기록


# ---- SS-06 [mock-e2e]: 모르는 메시지 타입은 무시하고 세션 유지 ----
def test_unknown_ws_type_ignored(client):
    sid = client.post("/api/sessions").json()["session_id"]
    with client.websocket_connect(f"/ws/{sid}") as ws:
        assert ws.receive_json()["type"] == "session_ready"
        _next_ai_turn(ws)

        ws.send_json({"type": "future_feature", "payload": 1})  # 미지 타입
        ws.send_json({"type": "user_message", "text": "생활비가 부담돼요"})
        reply = _next_ai_turn(ws)
        assert "빠듯" in reply[0]["text"]  # 크래시·에러 없이 다음 턴 정상


# ---- SS-07: TTS 캐시 상한(기본 20) — 오래된 것부터 밀어냄 ----
def test_tts_cache_cap():
    sess = Session("x")
    for i in range(25):
        sess.cache_tts(f"m{i}", b"audio")
    assert len(sess.tts_cache) == 20
    assert "m4" not in sess.tts_cache and "m5" in sess.tts_cache
    assert "m24" in sess.tts_cache


# ======== PS: 페르소나 프롬프트 고정값 ========

# ---- PS-01: 선인사 8종 — 'AI 상담원' 미노출, 호칭 일관, 시간대별 '보미' 자기소개 ----
def test_greetings_persona_rules():
    all_greets = [g for pool in GREETINGS.values() for g in pool]
    assert len(all_greets) == 8
    for g in all_greets:
        assert "AI 상담원" not in g, g  # 먼저 딱딱하게 내세우지 않음 (페르소나 v1.2)
        assert "어르신" in g, g
    for period, pool in GREETINGS.items():
        assert any("보미" in g for g in pool), period  # 시간대마다 자기소개 변형 보유
    assert greeting("이상한값") in GREETINGS["afternoon"]  # 미지 시간대는 낮 인사 폴백


# ---- PS-02: 시간대 경계 8값 ----
def test_period_of_hour_boundaries():
    assert [period_of_hour(h) for h in (4, 5, 10, 11, 16, 17, 21, 22)] == [
        "night", "morning", "morning", "afternoon",
        "afternoon", "evening", "evening", "night",
    ]


# ---- PS-03: 연결처는 고정 문자열로 프롬프트에 주입 (번호 생성 금지) ----
def test_chat_system_contacts_fixed():
    s = chat_system()
    assert CONTACTS_LINE in s
    for num in ("109", "119", "112", "1332", "1577-1389", "129"):
        assert num in CONTACTS_LINE


# ---- PS-04: 접지(RAG) 블록 스위치 ----
def test_chat_system_grounding_switch():
    s_on = chat_system("### 검색자료\n서비스명: 기초연금", rag=True)
    assert "[복지 자료 — 방금 검색된 공식 자료" in s_on
    assert "### 검색자료" in s_on
    assert "이번 턴에 검색된 자료 없음" not in s_on

    s_off = chat_system("", rag=False)
    assert "이번 턴에 검색된 자료 없음" in s_off

    s_blank = chat_system("   ", rag=True)  # rag 플래그가 있어도 자료가 비면 무자료 블록
    assert "이번 턴에 검색된 자료 없음" in s_blank


# ---- PS-06: 상황 메모는 참고만 — 분석 티 금지 지시 포함 ----
def test_chat_system_memo_nondisclosure():
    s = chat_system(memo="- 어르신 기본 정보: 만 72세")
    assert "[어르신 상황 메모" in s
    assert "'기록했다', '분석했다', '메모를 보니' 같은 말은 절대 하지 않습니다" in s
    assert "[어르신 상황 메모" not in chat_system(memo="")  # 메모 없으면 블록 자체가 없음


# ---- PS-07: 대화 프롬프트에 추출(분석) 어휘 미혼입 (페르소나 §9 분리) ----
def test_chat_system_free_of_extraction_vocab():
    s = chat_system("자료", memo="- 관찰됨(건강): x", backchannel=True, rag=True, signal="낙상 신호")
    for token in ("JSON", "카테고리", "심각도", "사람_개입_필요", "welfare_signals", "findings"):
        assert token not in s, token


# ======== RS: REST 방어선 ========

# ---- RS-01/RS-02: answer 질문 누락 400, 인덱스 없으면 503 ----
def test_rag_answer_validation_and_no_index(norag_client):
    assert norag_client.post("/api/rag/answer", json={"question": ""}).status_code == 400
    assert norag_client.post("/api/rag/answer", json={}).status_code == 400
    r = norag_client.post("/api/rag/answer", json={"question": "치매 약값이 걱정이에요"})
    assert r.status_code == 503  # 인덱스 미로드 → 명시적 서비스 불가


# ---- RS-03: UTF-8 원시 바이트 질의 라운드트립 ----
def test_rag_answer_utf8_bytes_roundtrip(rag_client):
    payload = '{"question": "치매 약값이 걱정이에요"}'.encode("utf-8")
    r = rag_client.post("/api/rag/answer", content=payload,
                        headers={"Content-Type": "application/json"})
    assert r.status_code == 200
    d = r.json()
    assert d["rejected"] is False
    assert d["card"] and d["card"].startswith("📌")  # 한글·이모지 손상 없이 왕복
    assert "치매" in d["card"]


# ---- RS-04: 이미지 업로드 — 빈 파일 400, 5MB 초과 413 ----
def test_image_upload_size_guards(client):
    sid = client.post("/api/sessions").json()["session_id"]
    r = client.post(f"/api/sessions/{sid}/image",
                    files={"file": ("x.png", b"", "image/png")})
    assert r.status_code == 400
    big = b"\x00" * (5 * 1024 * 1024 + 1)
    r = client.post(f"/api/sessions/{sid}/image",
                    files={"file": ("big.png", big, "image/png")})
    assert r.status_code == 413


# ---- RS-05: 오디오 — 무세션 404, 빈 바이트 400 ----
def test_audio_upload_guards(client):
    r = client.post("/api/sessions/no-such/audio",
                    files={"file": ("a.wav", b"\x00" * 16, "audio/wav")})
    assert r.status_code == 404
    sid = client.post("/api/sessions").json()["session_id"]
    r = client.post(f"/api/sessions/{sid}/audio",
                    files={"file": ("a.wav", b"", "audio/wav")})
    assert r.status_code == 400


# ---- RS-06: TTS — 무세션 404, message_id null 400, 사용자 발화 id 404 ----
def test_tts_guards(app, client):
    r = client.post("/api/sessions/no-such/tts", json={"message_id": "m1"})
    assert r.status_code == 404

    sid = client.post("/api/sessions").json()["session_id"]
    r = client.post(f"/api/sessions/{sid}/tts", json={"message_id": None})
    assert r.status_code == 400

    sess = app.state.store.get(sid)
    mid = sess.add_message("user", "안녕하세요").id  # 어르신 발화는 합성 대상이 아님
    r = client.post(f"/api/sessions/{sid}/tts", json={"message_id": mid})
    assert r.status_code == 404


# ---- RS-07: TTS 전처리 — 이모지·마크다운 제거, 낭독 쉼표, 핫라인 자릿수 낭독 ----
def test_clean_for_tts():
    got = _clean_for_tts("📌 **기초연금** [카드] 자살예방 109, 응급 119, 1577-1389")
    assert got == "기초연금 카드 자살예방 일공구, 응급 일일구, 일오칠칠에 일삼팔구"
    # "잘 안 ~"은 붙여 읽으면 어색 — 쉼표로 숨 고르기 (표시는 원문 그대로, 낭독만)
    assert _clean_for_tts("숨이 잘 안 쉬어지면 바로 연락하세요") == "숨이 잘, 안 쉬어지면 바로 연락하세요"


# ---- RS-08: 인덱스 없는 /health — 앱은 ok, rag.loaded만 false ----
def test_health_reports_rag_off(norag_client):
    body = norag_client.get("/health").json()
    assert body["status"] == "ok"
    assert body["rag"] == {"loaded": False}


# ---- RS-09: 없는 세션 종료 404 ----
def test_end_unknown_session_404(client):
    assert client.post("/api/sessions/no-such/end").status_code == 404


# ---- RS-11: mock STT는 5개 샘플 문장을 순환 ----
async def test_mock_stt_cycles_five_samples():
    stt = MockSTT()
    outs = [await stt.transcribe(b"\x00") for _ in range(6)]
    assert len(set(outs[:5])) == 5  # 서로 다른 5문장
    assert outs[5] == outs[0]  # 6번째부터 순환
