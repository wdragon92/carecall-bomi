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
    # LLM이 심리 위급 → 109 포함 문구
    level, msg = safety.alert(set(), {"psych"})
    assert level == "emergency" and "109" in msg
    # 둘 다면 심리(109 포함) 우선 — 사람 연결이 가장 급한 케이스
    level, msg = safety.alert(set(), {"medical", "psych"})
    assert "109" in msg


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
    # 심리 위급이 동시면 심리(emergency)가 우선
    level, msg = safety.alert(set(), {"fraud", "psych"})
    assert level == "emergency" and "109" in msg


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


def test_phrase_variants_from_matrix_audit():
    """테스트 카탈로그 감사에서 발견된 어순·활용 변형 공백 보강."""
    kinds = {d["_kind"] for d in safety.scan("변이 새까만 게 짜장 같아")}
    assert "medical_emergency" in kinds  # 위장관 출혈 신호 (어순 변형)
    kinds = {d["_kind"] for d in safety.scan("먼저 간 양반이 부럽네")}
    assert "suicide_warning" in kinds  # 완곡 자살 신호 ('양반' 변형)
    kinds = {d["_kind"] for d in safety.scan("요즘 자꾸 어지럽네")}
    assert "medical_soon" in kinds  # 'ㅂ' 활용형
