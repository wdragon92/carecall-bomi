"""종료 요약 리포트 생성 (report §2, §13-6). real 실패/누락 시 결정적 폴백."""
from __future__ import annotations

import logging

from app.core import prompts_analysis, welfare
from app.models import Report

log = logging.getLogger("report")

# 리포트 findings 정렬 우선순위: 긴급(카테고리) > 높음 > 보통 > 낮음.
# sorted()는 안정 정렬이라 동순위는 삽입순을 유지한다.
_SEVERITY_RANK = {"높음": 1, "보통": 2, "낮음": 3}


def _finding_sort_key(f) -> int:
    """긴급 카테고리를 최상단(0)으로, 그 외는 심각도 순으로."""
    if f.category == "긴급":
        return 0
    return _SEVERITY_RANK.get(f.severity, 2)


def _safety_recs(sess) -> list[str]:
    """안전 카테고리(긴급·건강 높음·사기_노출) 결정적 권고.
    LLM 출력 유무와 무관하게 항상 리포트에 포함되어야 하는 최소 안전망이다."""
    recs: list[str] = []
    if any(f.category == "긴급" for f in sess.findings):
        recs.append("위급 신호가 있었습니다. 자살예방상담 109 또는 119, 보호자·담당자에게 즉시 연결해 주세요.")
    if any(f.category == "건강" and f.severity == "높음" for f in sess.findings):
        recs.append("건강 위험신호가 관찰되었습니다. 가까운 시일 안에 진료(응급 의심 시 119)를 권합니다.")
    if any(f.category == "사기_노출" for f in sess.findings):
        recs.append(
            "의심 전화·문자에 유의하시도록 다시 한번 안내가 필요합니다. "
            "피해가 의심되면 112(경찰)나 1332(금융감독원)로 신고를 권합니다."
        )
    return recs


def _fallback_recs(sess) -> list[str]:
    """결정적 폴백 권고 전체(안전 권고 + 정서·인지·복지 + 마무리)."""
    recs = _safety_recs(sess)  # 긴급·건강높음·사기_노출 (안전 우선)
    if any(f.category == "정서" and f.severity == "높음" for f in sess.findings):
        recs.append("정서적으로 많이 지치신 상태가 관찰됩니다. 자살예방상담 109(24시간) 연계와 잦은 안부 연락을 권합니다.")
    elif any(f.category == "정서" for f in sess.findings):
        recs.append("정기적인 안부 연락이 정서적 안정에 도움이 됩니다.")
    if any(f.category == "인지" for f in sess.findings):
        recs.append("기억력 저하 등 인지 관련 변화가 관찰되었습니다. 가까운 보건소 치매안심센터에 상담을 권합니다.")
    if any(f.category == "복지_니즈" for f in sess.findings):
        recs.append("복지로(129)나 주민센터에서 받을 수 있는 복지 상담을 권합니다.")
    if not recs:
        # 어떤 분기도 못 잡았을 때. findings가 있으면 '없었지만' 문구는 모순이므로 분기한다.
        if sess.findings:
            recs.append("상담 중 나눈 이야기를 바탕으로 정기적인 안부 확인을 권합니다.")
        else:
            recs.append("특이사항은 없었지만, 정기적인 안부 확인을 권합니다.")
    return recs


def _fallback_summary(sess) -> str:
    cats = sorted({f.category for f in sess.findings})
    if sess.findings:
        return (
            "이번 안부 상담에서 "
            + ", ".join(cats)
            + " 관련 이야기를 나눴습니다. 자세한 내용은 특이사항 목록을 참고해 주세요."
        )
    return "이번 상담에서는 특별한 특이사항이 관찰되지 않았습니다. 편안하게 안부를 나눴습니다."


def _fallback_report(sess):
    """요약+권고 결정적 폴백 (real 전체 실패 시)."""
    return _fallback_summary(sess), _fallback_recs(sess)


def _merge_safety_recs(recs: list, sess) -> list[str]:
    """LLM이 준 권고에 누락된 안전 권고를 앞에 보강(중복 제외).
    LLM이 recommendations를 채웠더라도 안전 카테고리 권고는 항상 보장한다."""
    merged = list(recs)
    missing = [r for r in _safety_recs(sess) if r not in merged]
    return missing + merged


async def generate_report(sess, providers) -> dict:
    transcript = sess.transcript_text()
    findings_txt = (
        "\n".join(f"- [{f.category}/{f.severity}] {f.content}" for f in sess.findings)
        or "관찰된 특이사항 없음"
    )
    messages = [
        {"role": "system", "content": prompts_analysis.REPORT_SYSTEM},
        {"role": "user", "content": f"[대화]\n{transcript}\n\n[관찰된 특이사항]\n{findings_txt}"},
    ]

    summary = ""
    recs: list = []
    try:
        data = await providers.llm.extract_json(messages, {})
        if isinstance(data, dict):
            summary = (data.get("summary") or "").strip()
            recs = data.get("recommendations") or []
    except Exception as exc:  # noqa: BLE001 — 어떤 실패든 결정적 폴백 사용
        log.warning("report real failed (%s) → fallback", exc)

    if not summary:
        summary = _fallback_summary(sess)
    # 권고 게이팅은 summary와 독립적으로 처리한다(S9). 비어 있으면(=LLM이 summary만 주거나
    # 전부 실패) 결정적 폴백으로 채우고, 있으면 안전 카테고리 권고 누락분을 병합해 항상 보장한다.
    if not recs:
        recs = _fallback_recs(sess)
    else:
        recs = _merge_safety_recs(recs, sess)

    report = Report(
        summary=summary,
        findings=sorted(sess.findings, key=_finding_sort_key),  # 긴급>높음>보통>낮음
        recommendations=list(recs),
        welfare=welfare.merged_for_report(sess),  # RAG로 안내한 카드(기준일 포함) + 정적 매칭
        apply_packages=list(sess.apply_packages.values()),
    )
    return report.model_dump()
