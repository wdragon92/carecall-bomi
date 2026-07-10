"""안전망 연계 분리: 건강 응급→119 전용, 자살예방 109는 심리 신호 전용."""
from app.core import safety


def test_breathing_difficulty_detected_as_medical_emergency():
    """'숨이 잘 안 쉬어져' 변형이 안전망에 걸려 즉시 119 배너."""
    for utter in ["숨이 잘 안 쉬어져", "숨쉬기가 힘들어", "숨을 잘 못 쉬겠어", "숨이 막혀"]:
        kinds = {d["_kind"] for d in safety.scan(utter)}
        assert "medical_emergency" in kinds, utter
        level, msg = safety.alert(kinds)
        assert level == "emergency"
        assert "119" in msg and "109" not in msg  # 몸 응급엔 109 미노출


def test_negation_not_matched():
    assert safety.scan("숨이 잘 안 쉬어지진 않아") == [] or all(
        d["_kind"] != "medical_emergency" for d in safety.scan("숨이 잘 안 쉬어지진 않아")
    )


def test_alert_split_by_llm_flags():
    # LLM이 건강 위급만 잡음 → 119 전용 문구
    level, msg = safety.alert(set(), {"medical"})
    assert level == "emergency" and "119" in msg and "109" not in msg
    # LLM 단독 심리 위급 → warning + 109 (환각 오경보가 emergency 신뢰를 깎는 실측 대응)
    level, msg = safety.alert(set(), {"psych"})
    assert level == "warning" and "109" in msg
    # 의료+심리 동시(LLM)면 의료 emergency(119)가 우선 — 결정 우선순위
    level, msg = safety.alert(set(), {"medical", "psych"})
    assert level == "emergency" and "119" in msg


def test_suicide_rules_keep_109():
    kinds = {d["_kind"] for d in safety.scan("요즘은 그만 살고 싶다는 생각이 들어")}
    level, msg = safety.alert(kinds)
    assert level in ("warning", "emergency") and "109" in msg


def test_medical_soon_mentions_guardian_not_109():
    kinds = {d["_kind"] for d in safety.scan("조금만 걸어도 숨이 차")}
    level, msg = safety.alert(kinds)
    assert level == "warning" and "109" not in msg and "보호자" in msg


def test_fraud_flag_routes_to_112_not_109():
    """사기_노출(높음·개입필요)은 112·1332 경고 — 자살예방 109로 오연계 금지."""
    level, msg = safety.alert(set(), {"fraud"})
    assert level == "warning"
    assert "112" in msg and "1332" in msg and "109" not in msg
    # 심리 플래그 동시면 심리 warning(109)이 사기 문구보다 우선
    level, msg = safety.alert(set(), {"fraud", "psych"})
    assert level == "warning" and "109" in msg


def test_code_medical_emergency_beats_llm_psych_flag():
    """코드가 확정한 의료 응급은 LLM의 심리 추정 플래그에 가려지지 않는다."""
    level, msg = safety.alert({"medical_emergency"}, {"psych"})
    assert level == "emergency" and "119" in msg and "109" not in msg


def test_rescue_call_detected():
    """유사 서비스 실사례: '살려줘' 한마디가 잡담 처리되면 치명적."""
    kinds = {d["_kind"] for d in safety.scan("아이고 사람 살려")}
    assert "medical_emergency" in kinds
    level, msg = safety.alert(kinds)
    assert level == "emergency" and "119" in msg


def test_idiom_not_suicide_false_positive():
    """'아파 죽겠다'류 관용구는 자살 신호로 오탐하지 않는다 (오경보 피로 방지)."""
    for utter in ["아이고 무릎 아파 죽겠네", "더워 죽겠어", "우스워 죽겠네"]:
        kinds = {d["_kind"] for d in safety.scan(utter)}
        assert "suicide_acute" not in kinds and "suicide_warning" not in kinds, utter


def test_fraud_deterministic_rule():
    """사기 탐지는 LLM 전용이면 안 된다 — 결정적 규칙 병행 (리포트 유실 실측)."""
    kinds = {d["_kind"] for d in safety.scan("아들인 줄 알고 30만원을 보냈는데 아무래도 사기 같아")}
    assert "fraud_exposure" in kinds
    level, msg = safety.alert(kinds)
    assert level == "warning" and "112" in msg and "1332" in msg
    cats = {d["카테고리"] for d in safety.scan("검찰이라면서 전화가 왔어")}
    assert "사기_노출" in cats
    # 부정문 가드
    assert all(d["_kind"] != "fraud_exposure" for d in safety.scan("사기 같지는 않았어"))


def test_fraud_dialect_variants():
    """사투리·구어 사칭 표현도 결정망이 잡는다 (G 배치 실측 공백)."""
    for utter in ["검찰청이라 카믄서 전화가 왔다", "은행이라 카믄서 링크를 누르라고 문자가 왔데이",
                  "폰이 고장 나가 돈이 급하다 카데"]:
        kinds = {d["_kind"] for d in safety.scan(utter)}
        assert "fraud_exposure" in kinds, utter


def test_fraud_routes_to_112_not_109():
    """사기 노출(지시형·기관사칭)은 112·1332로 — 109 오연계 금지."""
    kinds = {d["_kind"] for d in safety.scan("검찰이라면서 돈을 보내라는 문자가 왔어")}
    assert "fraud_exposure" in kinds
    level, msg = safety.alert(kinds)
    assert level == "warning" and "112" in msg and "1332" in msg and "109" not in msg
    # 완료형 문구(가족송금·카톡링크 오발)는 제거됨 — 발동 안 함
    assert "fraud_exposure" not in {d["_kind"] for d in safety.scan("카톡 링크를 눌렀어")}


def test_cardiac_radiation_routes_to_care_not_emergency():
    """심장 방사통(팔·턱)은 빠른 진료(warning)+보호자 — 109/119 미노출."""
    kinds = {d["_kind"] for d in safety.scan("턱이 아프고 팔이 저려")}
    assert "medical_soon" in kinds and "medical_emergency" not in kinds
    level, msg = safety.alert(kinds)
    assert level == "warning" and "진료" in msg and "보호자" in msg
    assert "109" not in msg and "119" not in msg
    # 단독·양측 저림은 자세성 오발이 커 제외
    assert "medical_soon" not in {d["_kind"] for d in safety.scan("자고 일어났더니 손발이 저려")}


def test_s13_passive_suicide_additions_keep_109():
    """S13 완곡 죽음 소망 보강 문구도 109 연계(warning)로 이어진다."""
    for utter in ["저세상 가고 싶어", "하늘나라 가고 싶어", "죽는 게 나아"]:
        kinds = {d["_kind"] for d in safety.scan(utter)}
        assert "suicide_warning" in kinds, utter
        level, msg = safety.alert(kinds)
        assert level == "warning" and "109" in msg, utter


def test_gated_homonym_no_false_emergency():
    """게이팅 유지 표층(유서 깊은·먼저 간다·식은땀)은 경보 미발동 — 오경보 피로 방지.
    (농약·번개탄 등 method 어휘는 안전-우선으로 bare — 오탐 감수하고 미탐 방지)."""
    for utter in ["이 동네는 유서 깊은 곳이야", "그럼 나 먼저 간다", "긴장돼서 식은땀 났어"]:
        assert safety.alert({d["_kind"] for d in safety.scan(utter)})[0] is None, utter


def test_phrase_variants_from_matrix_audit():
    """테스트 카탈로그 감사에서 발견된 어순·활용 변형 공백 보강."""
    kinds = {d["_kind"] for d in safety.scan("변이 새까만 게 짜장 같아")}
    assert "medical_emergency" in kinds  # 위장관 출혈 신호 (어순 변형)
    kinds = {d["_kind"] for d in safety.scan("먼저 간 양반이 부럽네")}
    assert "suicide_warning" in kinds  # 완곡 자살 신호 ('양반' 변형)
    kinds = {d["_kind"] for d in safety.scan("요즘 자꾸 어지럽네")}
    assert "medical_soon" in kinds  # 'ㅂ' 활용형
