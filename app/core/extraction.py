"""특이사항 추출 파이프라인 (extraction §8.3). 비동기·코얼레싱.
결과는 findings_update / urgent_alert / welfare_update 로 push."""
from __future__ import annotations

import logging

from app.core import prompts, welfare
from app.models import Finding
from app.services.base import ProviderError
from app.session import finding_id

log = logging.getLogger("extract")


def _dump(f: Finding) -> dict:
    return {
        "id": f.id,
        "category": f.category,
        "content": f.content,
        "severity": f.severity,
        "needs_human": f.needs_human,
    }


def _parse_findings(data) -> list[Finding]:
    raw = data.get("findings") if isinstance(data, dict) else data
    out: list[Finding] = []
    for item in raw or []:
        try:
            f = Finding.model_validate(item)  # 한글 키(alias) 흡수
        except Exception:  # noqa: BLE001 — 형식 안 맞는 항목은 건너뜀
            continue
        f.id = finding_id(f.category, f.content)
        out.append(f)
    return out


def _is_urgent(f: Finding) -> bool:
    # 가드레일 2: 긴급이거나, 높음+사람개입필요면 코드가 강제로 경고
    return f.category == "긴급" or (f.severity == "높음" and f.needs_human)


async def trigger_extract(sess, providers) -> None:
    """코얼레싱: 실행 중이면 dirty만 세팅. 종료 시 dirty면 1회 더."""
    if sess.extract_lock.locked():
        sess.extract_dirty = True
        return
    async with sess.extract_lock:
        await _run_once(sess, providers)
        while sess.extract_dirty:
            sess.extract_dirty = False
            await _run_once(sess, providers)


async def _run_once(sess, providers) -> None:
    transcript = sess.user_transcript()
    if not transcript.strip():
        return
    messages = [
        {"role": "system", "content": prompts.EXTRACT_SYSTEM},
        {"role": "user", "content": transcript},
    ]
    try:
        data = await providers.llm.extract_json(messages, prompts.EXTRACT_SCHEMA)
    except ProviderError as exc:
        log.warning("extract real failed (%s) → mock", exc)
        try:
            data = await providers.mllm.extract_json(messages, prompts.EXTRACT_SCHEMA)
        except Exception as exc2:  # noqa: BLE001 — 실패해도 기존 findings 유지
            log.error("extract mock failed: %s", exc2)
            return

    findings = _parse_findings(data)
    sess.findings = findings
    await sess.send({"type": "findings_update", "findings": [_dump(f) for f in findings]})

    if any(_is_urgent(f) for f in findings):
        await sess.send(
            {
                "type": "urgent_alert",
                "message": "위급 신호가 감지되었어요. 보호자·담당자 또는 119 연결을 권고합니다.",
            }
        )

    # 복지 매칭 (welfare.json 있으면; stage 6 전까지는 빈 목록)
    signals = data.get("welfare_signals") if isinstance(data, dict) else None
    matched = welfare.match(signals or [], transcript)
    if matched:
        sess.welfare_matched = [m["id"] for m in matched]
        await sess.send({"type": "welfare_update", "items": matched})
