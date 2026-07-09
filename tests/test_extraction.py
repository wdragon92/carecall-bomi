def test_findings_preserved_when_llm_fails():
    """LLM 추출이 실패해도 기존 특이사항이 사라지지 않아야 한다(누적 보존)."""
    import asyncio

    from app.core import extraction
    from app.models import Finding
    from app.session import Session

    class _FailLLM:
        async def extract_json(self, messages, schema):
            raise RuntimeError("boom")

    class _P:
        llm = _FailLLM()
        mllm = _FailLLM()

    sess = Session("t")
    sess.add_message("user", "그냥 이런저런 얘기예요")  # 안전망 미매칭 발화
    sess.findings = [Finding(id="x", category="정서", content="외로움 표현", severity="보통")]

    asyncio.run(extraction._run_once(sess, _P()))

    cats = {f.category for f in sess.findings}
    assert "정서" in cats  # 기존 findings 보존


def _drain_until(ws, pred, max_msgs=90):
    seen = []
    for _ in range(max_msgs):
        m = ws.receive_json()
        seen.append(m)
        if pred(m):
            return m, seen
    return None, seen


def test_extraction_updates_findings(client):
    sid = client.post("/api/sessions").json()["session_id"]
    with client.websocket_connect(f"/ws/{sid}") as ws:
        ws.send_json({"type": "user_message", "text": "요즘 밤에 잠을 통 못 자요"})
        found, _ = _drain_until(ws, lambda m: m.get("type") == "findings_update")
        assert found is not None
        cats = {f["category"] for f in found["findings"]}
        assert "건강" in cats


def test_urgent_triggers_alert(client):
    sid = client.post("/api/sessions").json()["session_id"]
    with client.websocket_connect(f"/ws/{sid}") as ws:
        ws.send_json({"type": "user_message", "text": "이제 다 그만두고 죽고 싶어요"})
        alert, seen = _drain_until(ws, lambda m: m.get("type") == "urgent_alert")
        assert alert is not None
        # 긴급 finding도 함께 있어야 함
        fu = [m for m in seen if m.get("type") == "findings_update"][-1]
        assert any(f["category"] == "긴급" for f in fu["findings"])


def test_two_sessions_isolated(client):
    s1 = client.post("/api/sessions").json()["session_id"]
    s2 = client.post("/api/sessions").json()["session_id"]
    with client.websocket_connect(f"/ws/{s1}") as w1, client.websocket_connect(f"/ws/{s2}") as w2:
        w1.send_json({"type": "user_message", "text": "모르는 번호로 보이스피싱 문자를 받았어요"})
        f1, _ = _drain_until(w1, lambda m: m.get("type") == "findings_update")
        w2.send_json({"type": "user_message", "text": "무릎이 아파서 걷기가 힘들어요"})
        f2, _ = _drain_until(w2, lambda m: m.get("type") == "findings_update")

        cats1 = {f["category"] for f in f1["findings"]}
        cats2 = {f["category"] for f in f2["findings"]}
        assert "사기_노출" in cats1 and "사기_노출" not in cats2
        assert "건강" in cats2 and "건강" not in cats1


class _RecWS:
    """전송 payload를 모으는 최소 WebSocket 스텁."""

    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


def test_findings_accumulate_on_llm_success():
    """S6: LLM 추출이 성공한 턴도 이전 턴 관찰(안전망 백스톱 없는 인지 등)을 보존해야 한다."""
    import asyncio

    from app.core import extraction
    from app.models import Finding
    from app.session import Session

    class _OkLLM:
        async def extract_json(self, messages, schema):
            # 이번 턴 LLM은 '건강'만 보고, 과거 '인지' 관찰은 다시 내지 않음
            return {"findings": [{"카테고리": "건강", "내용": "무릎 통증 언급", "심각도": "보통"}],
                    "welfare_signals": []}

    class _P:
        llm = _OkLLM()
        mllm = _OkLLM()

    sess = Session("t")
    sess.add_message("user", "무릎이 아파요")  # 안전망 미매칭 발화
    sess.findings = [Finding(id="cog-1", category="인지", content="약 복용을 자주 잊음", severity="보통")]

    asyncio.run(extraction._run_once(sess, _P()))

    cats = {f.category for f in sess.findings}
    assert "인지" in cats  # 성공 턴에도 과거 관찰 보존
    assert "건강" in cats  # 이번 턴 LLM 결과도 반영


def test_second_alert_not_gated_by_same_level():
    """S11: medical_soon(경보1)과 LLM 사기(경보2)가 같은 warning 등급이어도
    사기 112/1332 경보가 가려지지 않고 함께 전송돼야 한다."""
    import asyncio

    from app.core import extraction
    from app.session import Session

    class _FraudLLM:
        async def extract_json(self, messages, schema):
            return {"findings": [{"카테고리": "사기_노출", "내용": "보이스피싱 정황",
                                  "심각도": "높음", "사람_개입_필요": True}],
                    "welfare_signals": []}

    class _P:
        llm = _FraudLLM()
        mllm = _FraudLLM()

    sess = Session("t")
    sess.ws = _RecWS()
    sess.add_message("user", "요즘 자꾸 어지러워요")  # medical_soon(어지러) → warning

    asyncio.run(extraction._run_once(sess, _P()))

    msgs = [m["message"] for m in sess.ws.sent if m.get("type") == "urgent_alert"]
    assert any("1332" in m for m in msgs)  # 사기 경보 전송됨(동급이어도 안 가려짐)
    assert any("진료" in m for m in msgs)  # medical_soon 경보도 함께 유지


def test_welfare_matched_cleared_when_no_match():
    """welfare_matched stale: 빈 매칭 턴에 과거 항목이 영구 잔존하지 않아야 한다."""
    import asyncio

    from app.core import extraction
    from app.session import Session

    class _EmptyLLM:
        async def extract_json(self, messages, schema):
            return {"findings": [], "welfare_signals": []}

    class _P:
        llm = _EmptyLLM()
        mllm = _EmptyLLM()

    sess = Session("t")
    sess.add_message("user", "오늘 날씨가 좋네요")  # 복지 키워드 없음
    sess.welfare_matched = ["telecom-discount"]  # 과거 턴 잔재

    asyncio.run(extraction._run_once(sess, _P()))

    assert sess.welfare_matched == []  # 현재 턴 기준으로 비워짐


def test_welfare_match_negation_and_boundary():
    """C8: welfare.match 의 부정어·경계 처리."""
    from app.core import welfare

    if not welfare.load_items():
        import pytest
        pytest.skip("welfare.json 없음")

    # 부정: '치매 아니에요'는 치매치료관리비와 매칭되지 않아야
    neg = welfare.match([], "병원에서 치매 아니에요 라고 했어요")
    assert all(m["id"] != "dementia-care" for m in neg)
    # 대비: '치매가 있대요'는 매칭
    pos = welfare.match([], "치매가 있대요")
    assert any(m["id"] == "dementia-care" for m in pos)

    # 경계: '전기요금'은 통신 '요금'(telecom)이 아니라 에너지바우처로
    energy_ids = [m["id"] for m in welfare.match([], "전기요금이 너무 부담돼요")]
    assert "energy-voucher" in energy_ids
    assert "telecom-discount" not in energy_ids
    # 경계 통과: 띄어 쓴 '휴대폰 요금'은 telecom 매칭 유지
    tel = welfare.match([], "휴대폰 요금이 부담돼요")
    assert any(m["id"] == "telecom-discount" for m in tel)
