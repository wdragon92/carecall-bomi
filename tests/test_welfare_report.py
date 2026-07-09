"""리포트 생성 폴백/정렬 회귀 테스트 + 기존 통합 테스트."""
import asyncio
from types import SimpleNamespace

from app.core import report
from app.models import Finding


def _f(category, severity="보통", content="관찰"):
    return Finding(category=category, content=content, severity=severity)


class _StubLLM:
    """extract_json이 지정한 dict를 그대로 반환하는 가짜 LLM."""

    def __init__(self, data):
        self._data = data

    async def extract_json(self, messages, schema):
        return self._data


def _mk_report_sess(findings):
    return SimpleNamespace(
        findings=findings,
        transcript_text=lambda: "어르신: 안녕하세요\n상담원: 네, 안녕하세요",
        welfare_cards={},
        welfare_matched=[],
        apply_packages={},
    )


def _drain_until(ws, pred, max_msgs=90):
    seen = []
    for _ in range(max_msgs):
        m = ws.receive_json()
        seen.append(m)
        if pred(m):
            return m, seen
    return None, seen


def test_welfare_matching(client):
    sid = client.post("/api/sessions").json()["session_id"]
    with client.websocket_connect(f"/ws/{sid}") as ws:
        ws.send_json({"type": "user_message", "text": "생활비가 너무 부족하고 혼자 살아서 외로워요"})
        wu, _ = _drain_until(ws, lambda m: m.get("type") == "welfare_update")
        assert wu is not None
        assert wu["items"]
        ids = {it["id"] for it in wu["items"]}
        # 저소득/독거 신호 → 기초연금이나 돌봄서비스 등이 매칭되어야 함
        assert ids


def test_report_generated(client):
    sid = client.post("/api/sessions").json()["session_id"]
    with client.websocket_connect(f"/ws/{sid}") as ws:
        ws.send_json({"type": "user_message", "text": "무릎이 아프고 생활비도 부족해요"})
        _drain_until(ws, lambda m: m.get("type") == "findings_update")

    r = client.post(f"/api/sessions/{sid}/end")
    assert r.status_code == 200
    report = r.json()["report"]
    assert report["summary"]
    assert "disclaimer" in report and report["disclaimer"]
    assert isinstance(report["findings"], list)
    assert isinstance(report["recommendations"], list) and report["recommendations"]


# ---- S9: 안전 카테고리 권고는 LLM 출력과 무관하게 항상 보장 ----
def test_safety_recs_covers_all_safety_categories():
    sess = SimpleNamespace(findings=[_f("긴급", "높음"), _f("건강", "높음"), _f("사기_노출", "보통")])
    joined = " ".join(report._safety_recs(sess))
    assert "109" in joined and "119" in joined  # 긴급 위기 연결
    assert "진료" in joined                       # 건강 높음
    assert "112" in joined and "1332" in joined  # 사기 신고처
    # 건강 '보통'은 안전 권고에 포함되지 않음(높음 전용)
    assert report._safety_recs(SimpleNamespace(findings=[_f("건강", "보통")])) == []


def test_merge_safety_recs_dedups_and_prepends():
    sess = SimpleNamespace(findings=[_f("긴급", "높음")])
    safety = report._safety_recs(sess)
    # 이미 있으면 중복 추가하지 않음
    assert report._merge_safety_recs(list(safety), sess) == safety
    # LLM 권고가 안전 권고를 누락하면 앞에 보강
    merged = report._merge_safety_recs(["보호자에게 안부 연락을 권합니다."], sess)
    assert "보호자에게 안부 연락을 권합니다." in merged
    assert "109" in merged[0]  # 안전 권고가 맨 앞


def test_generate_report_summary_only_still_guarantees_safety_recs():
    """LLM이 summary만 주고 recommendations를 비워도 결정적 위기·사기 권고가 보장돼야 함(S9)."""
    sess = _mk_report_sess([_f("긴급", "높음"), _f("사기_노출", "높음")])
    providers = SimpleNamespace(llm=_StubLLM({"summary": "요약입니다.", "recommendations": []}))
    rep = asyncio.run(report.generate_report(sess, providers))
    joined = " ".join(rep["recommendations"])
    assert rep["summary"] == "요약입니다."           # LLM summary는 보존
    assert rep["recommendations"]                    # 권고가 비지 않음
    assert "109" in joined and "119" in joined       # 긴급 위기 권고
    assert "112" in joined and "1332" in joined      # 사기 신고 권고


def test_generate_report_merges_safety_into_llm_recs():
    """LLM이 권고를 채워도 안전 카테고리 권고 누락분은 병합돼야 함(S9)."""
    sess = _mk_report_sess([_f("긴급", "높음")])
    providers = SimpleNamespace(
        llm=_StubLLM({"summary": "요약", "recommendations": ["보호자에게 안부 연락을 권합니다."]})
    )
    rep = asyncio.run(report.generate_report(sess, providers))
    recs = rep["recommendations"]
    assert "보호자에게 안부 연락을 권합니다." in recs  # LLM 권고 보존
    assert "109" in recs[0]                            # 안전 권고가 앞에 보강


# ---- S10: 인지 분기 + findings 존재 시 '없었지만' 모순 금지 ----
def test_fallback_recs_cognitive_branch_no_contradiction():
    sess = SimpleNamespace(findings=[_f("인지", "보통")])
    recs = report._fallback_recs(sess)
    assert any("인지" in r or "치매안심센터" in r for r in recs)   # 인지 권고 존재
    assert not any("특이사항은 없었지만" in r for r in recs)        # 모순 문구 금지


def test_fallback_recs_findings_present_never_says_none():
    """세부 분기를 못 잡는 findings(건강/보통)라도 '없었지만'은 금지(S10 일반화)."""
    recs = report._fallback_recs(SimpleNamespace(findings=[_f("건강", "보통")]))
    assert recs
    assert not any("특이사항은 없었지만" in r for r in recs)


def test_fallback_recs_empty_findings_keeps_catchall():
    """특이사항이 실제로 없을 때만 기존 캐치올 문구 유지."""
    recs = report._fallback_recs(SimpleNamespace(findings=[]))
    assert recs == ["특이사항은 없었지만, 정기적인 안부 확인을 권합니다."]


# ---- 심각도 정렬: 긴급 > 높음 > 보통 > 낮음 ----
def test_generate_report_sorts_findings_by_severity():
    findings = [
        _f("복지_니즈", "낮음"),
        _f("긴급", "높음"),
        _f("건강", "보통"),
        _f("정서", "높음"),
    ]
    sess = _mk_report_sess(findings)
    providers = SimpleNamespace(llm=_StubLLM({"summary": "요약", "recommendations": ["x"]}))
    rep = asyncio.run(report.generate_report(sess, providers))
    assert [f["category"] for f in rep["findings"]] == ["긴급", "정서", "건강", "복지_니즈"]


def test_finding_sort_key_stable_within_same_rank():
    a = _f("건강", "낮음", content="A")
    b = _f("복지_니즈", "낮음", content="B")
    # 동순위(낮음)는 삽입순 유지
    assert sorted([a, b], key=report._finding_sort_key) == [a, b]
    assert sorted([b, a], key=report._finding_sort_key) == [b, a]
