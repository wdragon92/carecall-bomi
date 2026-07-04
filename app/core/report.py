"""종료 요약 리포트 생성 (report §2, §13-6). real 실패/누락 시 결정적 폴백."""
from __future__ import annotations

import logging

from app.core import prompts, welfare
from app.models import Report
from app.services.base import ProviderError

log = logging.getLogger("report")


def _is_urgent(f) -> bool:
    return f.category == "긴급" or (f.severity == "높음" and f.needs_human)


def _fallback_report(sess):
    cats = sorted({f.category for f in sess.findings})
    if sess.findings:
        summary = (
            "이번 안부 상담에서 "
            + ", ".join(cats)
            + " 관련 이야기를 나눴습니다. 자세한 내용은 특이사항 목록을 참고해 주세요."
        )
    else:
        summary = "이번 상담에서는 특별한 특이사항이 관찰되지 않았습니다. 편안하게 안부를 나눴습니다."

    recs: list[str] = []
    if any(_is_urgent(f) for f in sess.findings):
        recs.append("위급 신호가 있었습니다. 보호자·담당자 또는 119에 연락해 주세요.")
    if any(f.category == "복지_니즈" for f in sess.findings):
        recs.append("복지로(129)나 주민센터에서 받을 수 있는 복지 상담을 권합니다.")
    if any(f.category == "사기_노출" for f in sess.findings):
        recs.append("의심 전화·문자에 유의하시도록 다시 한번 안내가 필요합니다.")
    if any(f.category == "정서" for f in sess.findings):
        recs.append("정기적인 안부 연락이 정서적 안정에 도움이 됩니다.")
    if not recs:
        recs.append("특이사항은 없었지만, 정기적인 안부 확인을 권합니다.")
    return summary, recs


async def generate_report(sess, providers) -> dict:
    transcript = sess.transcript_text()
    findings_txt = (
        "\n".join(f"- [{f.category}/{f.severity}] {f.content}" for f in sess.findings)
        or "관찰된 특이사항 없음"
    )
    messages = [
        {"role": "system", "content": prompts.REPORT_SYSTEM},
        {"role": "user", "content": f"[대화]\n{transcript}\n\n[관찰된 특이사항]\n{findings_txt}"},
    ]

    summary = ""
    recs: list = []
    try:
        data = await providers.llm.extract_json(messages, {})
        if isinstance(data, dict):
            summary = (data.get("summary") or "").strip()
            recs = data.get("recommendations") or []
    except ProviderError as exc:
        log.warning("report real failed (%s) → fallback", exc)

    if not summary:
        summary, recs = _fallback_report(sess)

    report = Report(
        summary=summary,
        findings=sess.findings,
        recommendations=list(recs),
        welfare=welfare.by_ids(sess.welfare_matched),
    )
    return report.model_dump()
