"""안전망 매트릭스 (SF·EX 계열) — 결정적 규칙 스캔·경보 분기·추출 파이프라인.
스캔은 코드가 반드시 잡아야 하는 안전망이라 실측 동작을 그대로 고정한다.
부정 판정은 '구절을 직접 부정하는 형태'(지/진 않·못, 앞의 안/못, 명사화 없/안)만
본다 — 무관한 뒷절의 '없'이 참-양성을 억누르지 않도록 정밀화된 동작을 고정."""
import asyncio
from types import SimpleNamespace

from app.core import extraction, safety
from app.session import Session, finding_id


def _kinds(text):
    return {d["_kind"] for d in safety.scan(text)}


# ---- SF-01: 의식·출혈 응급 표현 → medical_emergency + 119 전용 배너 ----
def test_consciousness_and_bleeding_are_emergency():
    """정신을 잃음·피를 토함 → 즉시 119. 배너에 자살예방 109는 미노출(연계 분리).
    ※ '변이 새까만 게 짜장 같아'(어순·활용 변형)는 현행 구문("새까만 변"/"짜장 같은")에
    안 걸린다 — 안전망 공백으로 보고됨(테스트로 고정하지 않음)."""
    for utter in ["아까 잠깐 정신을 잃었어", "피를 토했어"]:
        kinds = _kinds(utter)
        assert "medical_emergency" in kinds, utter
        level, msg = safety.alert(kinds)
        assert level == "emergency"
        assert "119" in msg and "109" not in msg


# ---- SF-02: 같은 규칙의 복수 구문 매칭은 1건으로 dedupe ----
def test_same_rule_multiple_phrases_dedupe():
    # '식은땀'은 이제 심장/호흡 문맥과 결합해야 발동 — 아래 두 구문 모두 같은 규칙이라 1건
    hits = safety.scan("가슴을 쥐어짜는 것 같고 식은땀이 나고 가슴이 조여")
    assert [h["_kind"] for h in hits] == ["medical_emergency"]  # '가슴을 쥐어'+'식은땀이 나고 가슴' → 1건


# ---- SF-03: 매칭 직후 부정 표현은 오탐 방지 ----
def test_negation_right_after_phrase_no_match():
    assert _kinds("입이 돌아가진 않았어") == set()
    assert _kinds("쓰러졌다는 건 아니고") == set()


# ---- SF-04: 낙상 후 통증 → 빠른 진료 권고 (응급 아님) ----
def test_fall_then_groin_pain_is_medical_soon():
    hits = safety.scan("어제 넘어진 뒤로 사타구니가 아파")
    assert len(hits) == 1  # '넘어진 뒤로'+'사타구니가 아파' 같은 규칙 → dedupe
    h = hits[0]
    assert h["_kind"] == "medical_soon" and h["심각도"] == "높음"
    assert h["사람_개입_필요"] is False
    level, msg = safety.alert({h["_kind"]})
    assert level == "warning"
    assert "진료" in msg and "보호자" in msg
    assert "109" not in msg and "119" not in msg


# ---- SF-06: 열 없는 기력·식욕 저하(노인 폐렴 의심) ----
def test_appetite_drop_and_lethargy_is_medical_soon():
    assert "medical_soon" in _kinds("요즘 밥맛이 뚝 떨어지고 종일 멍하고 잠만 자려 해")


# ---- SF-07: 소변 변화(탈수·요로감염 의심)는 '보통' 심각도 ----
def test_urine_change_is_medical_soon_moderate():
    hits = safety.scan("소변이 진하고 냄새가 나")
    assert [(h["_kind"], h["심각도"]) for h in hits] == [("medical_soon", "보통")]


# ---- SF-08: 어지럼 매칭 vs 부정 ----
def test_dizziness_match_and_negation():
    assert "medical_soon" in _kinds("자꾸 어지럽고 핑 돌아")
    assert _kinds("어지럽지는 않아") == set()


# ---- SF-09: 명사화 부정은 정확히 미매칭 (S4 정밀화) ----
def test_nominalized_negation_excluded_precisely():
    """'죽고 싶다는 생각은 없어/안 해요'는 구절을 직접 부정하는 '명사화 부정' —
    좁은 창에서 표지('다는')와 부정어('없/안')의 동시 출현으로 정확히 걸러낸다.
    (과거 7자 창 한계로 인한 '안전측 오탐'을 제거함.)"""
    assert "suicide_acute" not in _kinds("죽고 싶다는 생각은 없어")
    assert "suicide_acute" not in _kinds("죽고 싶다는 생각은 없어요")
    assert "suicide_acute" not in _kinds("죽고 싶다는 생각은 안 해요")
    # 참-양성은 그대로: 소망/의도가 살아 있는 발화는 계속 잡는다
    assert "suicide_acute" in _kinds("죽고 싶다는 생각이 자꾸 들어요")
    # 무관한 뒷절의 '없'은 참-양성을 억누르지 않는다
    assert "medical_soon" in _kinds("숨이 차고 기운이 없어")
    assert "medical_soon" in _kinds("가슴이 뻐근하고 힘이 없어")


# ---- SF-10: 앞에서 부정된 구문이 뒤에서 재출현하면 매칭 ----
def test_phrase_reappearance_after_negation_matches():
    assert "medical_soon" in _kinds("가슴이 답답하진 않았는데 오늘은 가슴이 답답해")


# ---- SF-11: 완곡한 죽음 언급 배터리 ----
def test_euphemistic_death_battery():
    """완곡 표현은 최소 suicide_warning + 109 연계. '자는 듯이 갔으면'은 현행 규칙상
    suicide_acute(안전측 상향)로 분류된다 — 어느 쪽이든 109가 배너에 나온다.
    ※ '먼저 간 양반이 부럽네'는 현행 구문("먼저 간 사람이 부")에 안 걸린다 —
    안전망 공백으로 보고됨(테스트로 고정하지 않음)."""
    for utter in ["눈 안 떴으면 싶어", "늙으면 죽어야지"]:
        kinds = _kinds(utter)
        assert kinds == {"suicide_warning"}, utter
        level, msg = safety.alert(kinds)
        assert level == "warning" and "109" in msg

    kinds = _kinds("자는 듯이 갔으면 좋겠어")
    assert "suicide_acute" in kinds  # acute 구문 목록에 포함 — 안전측 상향(현행)
    level, msg = safety.alert(kinds)
    assert level == "emergency" and "109" in msg


# ---- SF-12: 직접 표현 + 완곡 표현 동시 → acute 우선 emergency ----
def test_direct_plus_passive_acute_wins():
    kinds = _kinds("죽고 싶어. 내가 짐만 되는 것 같아")
    assert {"suicide_acute", "suicide_warning"} <= kinds
    level, msg = safety.alert(kinds)
    assert level == "emergency" and "109" in msg and "119" in msg


# ---- 추출 파이프라인용 페이크 ----
class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


class _StubLLM:
    """extract_json이 고정 응답을 내는 페이크 (안전망·경보 분기만 검증)."""

    def __init__(self, data=None):
        self.data = data if data is not None else {"findings": [], "welfare_signals": []}

    async def extract_json(self, messages, schema):
        return self.data


# ---- SF-16: AI 발화는 스캔 대상이 아님 (user_transcript 경유) ----
async def test_assistant_utterance_not_scanned():
    """추출은 어르신 발화만 본다 — 보미가 되물으며 언급한 위험 어휘가 오탐되면 안 됨."""
    sess = Session("t")
    sess.ws = _FakeWS()
    sess.add_message("assistant", "많이 힘드시면 '죽고 싶다'는 생각이 들 수도 있어요.")
    sess.add_message("user", "고마워, 요즘은 뭐 그냥 지내")

    assert "죽고 싶" not in sess.user_transcript()  # AI 발화 자체가 추출 입력에서 제외

    providers = SimpleNamespace(llm=_StubLLM(), mllm=_StubLLM())
    await extraction._run_once(sess, providers)
    assert all(m["type"] != "urgent_alert" for m in sess.ws.sent)
    assert all(f.category != "긴급" for f in sess.findings)


# ---- SF-17 [mock-e2e]: 무해한 일상 발화 — 경보·복지 패널 미전송 ----
def _drain_until(ws, pred, max_msgs=90):
    seen = []
    for _ in range(max_msgs):
        m = ws.receive_json()
        seen.append(m)
        if pred(m):
            return m, seen
    return None, seen


def test_benign_smalltalk_no_alert_no_welfare(norag_client):
    """일상 발화 → findings 빈 배열, urgent_alert·welfare_update 미전송.
    두 번째 턴을 펜스로 써서 첫 턴 추출의 후행 전송까지 부재를 확정한다."""
    sid = norag_client.post("/api/sessions").json()["session_id"]
    with norag_client.websocket_connect(f"/ws/{sid}") as ws:
        ws.send_json({"type": "user_message", "text": "오늘 화단에 물을 줬어"})
        fu1, seen1 = _drain_until(ws, lambda m: m.get("type") == "findings_update")
        assert fu1 is not None and fu1["findings"] == []

        ws.send_json({"type": "user_message", "text": "그러게"})  # 펜스 턴 (역시 무해)
        fu2, seen2 = _drain_until(ws, lambda m: m.get("type") == "findings_update")
        assert fu2 is not None and fu2["findings"] == []

        bad = [m for m in seen1 + seen2 if m.get("type") in ("urgent_alert", "welfare_update")]
        assert bad == []


# ---- EX-01: 안전망·LLM이 같은 특이사항을 내면 finding_id로 dedupe ----
def test_safety_and_llm_findings_dedupe_by_id():
    raw_safety = [{"카테고리": "건강", "내용": "낙상 후 통증 — 진료 권고",
                   "심각도": "높음", "사람_개입_필요": True, "_kind": "medical_soon"}]
    raw_llm = [{"카테고리": "건강", "내용": "낙상 후 통증 — 진료 권고",
                "심각도": "보통", "사람_개입_필요": False}]
    sf = extraction._parse_findings(raw_safety)
    lf = extraction._parse_findings(raw_llm)
    assert sf[0].id == lf[0].id == finding_id("건강", "낙상 후 통증 — 진료 권고")

    merged = extraction._merge(sf, lf)
    assert len(merged) == 1
    assert merged[0].severity == "높음"  # 안전망(먼저 온 것)이 이긴다


# ---- EX-02: 비허용 카테고리·형식 이상 finding은 skip ----
def test_parse_findings_drops_invalid_items():
    raw = [
        {"카테고리": "잡담", "내용": "티비 얘기", "심각도": "보통"},  # 허용 외 카테고리
        {"카테고리": "건강", "내용": "무릎 통증 언급", "심각도": "보통"},
        "그냥 문자열",  # dict 아님
        {"카테고리": "건강"},  # 필수 필드(내용) 누락
    ]
    out = extraction._parse_findings(raw)
    assert [f.category for f in out] == ["건강"]
    assert out[0].content == "무릎 통증 언급" and out[0].id


# ---- EX-03: trigger_extract 코얼레싱 — lock 점유 중 요청은 dirty 1회로 합침 ----
async def test_trigger_extract_coalesces_while_locked():
    sess = Session("t")
    sess.add_message("user", "오늘 화단에 물을 줬어요")

    gate = asyncio.Event()
    calls = []

    class _BlockingLLM:
        async def extract_json(self, messages, schema):
            calls.append(1)
            await gate.wait()
            return {"findings": [], "welfare_signals": []}

    providers = SimpleNamespace(llm=_BlockingLLM(), mllm=_BlockingLLM())
    t1 = asyncio.create_task(extraction.trigger_extract(sess, providers))
    for _ in range(50):  # t1이 lock을 잡고 LLM 호출에 들어갈 때까지 양보
        if sess.extract_lock.locked():
            break
        await asyncio.sleep(0)
    assert sess.extract_lock.locked()

    await extraction.trigger_extract(sess, providers)  # 점유 중 → dirty 마킹 후 즉시 반환
    assert sess.extract_dirty is True

    gate.set()
    await t1
    assert len(calls) == 2  # 최초 1회 + dirty 재실행 1회 (요청 2건이 2회로 코얼레싱)
    assert sess.extract_dirty is False


# ---- EX-05: LLM이 건강/높음/개입필요를 내면 의료 응급 경보(119)로 상향 ----
async def test_llm_medical_high_flag_escalates_to_medical_emergency():
    sess = Session("t")
    sess.ws = _FakeWS()
    sess.add_message("user", "오늘 다녀온 얘기 좀 하려고")  # 안전망 미매칭 발화

    data = {"findings": [{"카테고리": "건강", "내용": "급성 흉통 의심 정황",
                          "심각도": "높음", "사람_개입_필요": True}],
            "welfare_signals": []}
    providers = SimpleNamespace(llm=_StubLLM(data), mllm=_StubLLM(data))
    await extraction._run_once(sess, providers)

    alerts = [m for m in sess.ws.sent if m["type"] == "urgent_alert"]
    assert len(alerts) == 1
    assert alerts[0]["level"] == "emergency"
    assert "119" in alerts[0]["message"] and "109" not in alerts[0]["message"]
    fu = [m for m in sess.ws.sent if m["type"] == "findings_update"][-1]
    assert any(f["category"] == "건강" and f["severity"] == "높음" for f in fu["findings"])
