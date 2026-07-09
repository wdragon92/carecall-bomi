"""특이사항 추출 파이프라인 (extraction §8.3). 비동기·코얼레싱.
LLM 추출 결과에 결정적 안전망(safety)을 병합해, 위험신호를 놓치지 않는다.
결과는 findings_update / urgent_alert(level) / welfare_update 로 push."""
from __future__ import annotations

import logging

from app.core import prompts_analysis, safety, welfare
from app.models import Finding
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


def _parse_findings(raw_list) -> list[Finding]:
    out: list[Finding] = []
    for item in raw_list or []:
        try:
            f = Finding.model_validate(item)  # 한글 키(alias) 흡수, 여분 키(_kind)는 무시
        except Exception:  # noqa: BLE001
            continue
        f.id = finding_id(f.category, f.content)
        out.append(f)
    return out


def _merge(*groups: list[Finding]) -> list[Finding]:
    # 앞선 그룹 우선(안전망 → LLM → 기존 findings). id 기준 중복 제거로 무한 누적 방지.
    merged: list[Finding] = []
    seen: set[str] = set()
    for group in groups:
        for f in group:
            if f.id in seen:
                continue
            seen.add(f.id)
            merged.append(f)
    return merged


async def _send_alert(sess, level: str, message: str) -> None:
    """경보 전송 + 동일 경보 재전송 억제 — 추출이 누적 대화 전체를 매번 스캔하므로
    위험 발화 '이후 모든 턴'에 같은 배너가 재전송되던 문제(오경보 피로) 방지.
    내용·수위가 달라진 경보는 정상 전송(프론트는 emergency→warning 강등만 무시)."""
    cur = (level, message)
    if getattr(sess, "last_alert", None) == cur:
        return
    sess.last_alert = cur
    await sess.send({"type": "urgent_alert", "level": level, "message": message})


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


async def flush_extract(sess, providers) -> None:
    """리포트 직전 '기다리는' flush — trigger_extract는 실행 중이면 dirty만 걸고
    즉시 반환하므로(코얼레싱), 종료 시점에 쓰면 진행 중 추출 결과가 리포트를
    놓치는 경합이 생긴다(실측: 마지막 턴의 사기_노출이 리포트에서 유실).
    dirty 루프까지 소화해 flush 도중 끼어든 턴도 반영한다."""
    async with sess.extract_lock:  # 진행 중 추출이 있으면 끝날 때까지 대기
        await _run_once(sess, providers)
        while sess.extract_dirty:
            sess.extract_dirty = False
            await _run_once(sess, providers)


async def _run_once(sess, providers) -> None:
    transcript = sess.user_transcript()
    if not transcript.strip():
        return

    # 1) 즉시: 결정적 안전망 먼저 (느린 LLM보다 앞서 위험신호를 바로 표시)
    safety_raw = safety.scan(transcript)
    kinds = {d["_kind"] for d in safety_raw}
    safety_findings = _parse_findings(safety_raw)
    if safety_findings:
        sess.findings = _merge(safety_findings, sess.findings)
        await sess.send({"type": "findings_update", "findings": [_dump(f) for f in sess.findings]})
    level, message = safety.alert(kinds)
    if level:
        await _send_alert(sess, level, message)

    # 2) LLM 추출 (느림) → 안전망과 병합해 갱신
    messages = [
        {"role": "system", "content": prompts_analysis.EXTRACT_SYSTEM},
        {"role": "user", "content": transcript},
    ]
    data: dict = {}
    llm_ok = False
    try:
        data = await providers.llm.extract_json(messages, prompts_analysis.EXTRACT_SCHEMA)
        llm_ok = True
    except Exception as exc:  # noqa: BLE001
        log.warning("extract real failed (%s) → mock", exc)
        try:
            data = await providers.mllm.extract_json(messages, prompts_analysis.EXTRACT_SCHEMA)
            llm_ok = True
        except Exception as exc2:  # noqa: BLE001
            log.error("extract mock failed: %s", exc2)
            data = {}

    llm_findings = _parse_findings(data.get("findings") if isinstance(data, dict) else None)
    # LLM 성공/실패 모두 기존 findings를 누적 보존한다 — 성공 턴이 이전 관찰(안전망 백스톱이
    # 없는 인지·복지_니즈 등)을 덮어써 지우던 문제(S6) 방지. 우선순위 안전망 > 이번 LLM > 기존,
    # id 기준 dedup 으로 무한 누적은 막는다. 실패 경로(안전망+기존)와 대칭.
    findings = (
        _merge(safety_findings, llm_findings, sess.findings) if llm_ok
        else _merge(safety_findings, sess.findings)
    )
    sess.findings = findings
    await sess.send({"type": "findings_update", "findings": [_dump(f) for f in findings]})

    # 경보 재평가 — LLM이 새 위험을 잡았으면 상향(하향은 안 함).
    # 연계 분리: 건강 위급 → 119 / 심리(긴급·정서) 위급 → 109 / 사기 → 112·1332
    # (사기를 psych로 묶으면 보이스피싱 정황에 자살예방 109 배너가 뜨는 오연계가 된다)
    llm_flags: set[str] = set()
    for f in llm_findings:
        serious = f.category == "긴급" or (f.severity == "높음" and f.needs_human)
        if not serious:
            continue
        if f.category == "건강":
            llm_flags.add("medical")
        elif f.category == "사기_노출":
            llm_flags.add("fraud")
        else:
            llm_flags.add("psych")
    level2, message2 = safety.alert(kinds, llm_flags)
    # 등급이 같아도 문구가 다르면 전송한다 — medical_soon warning과 사기 warning이 공존할 때
    # 이전 `level2 != level`이 동급 사기 112/1332 경보를 가리던 문제(S11) 방지.
    # 동일 경보의 재전송 억제는 _send_alert 가 계속 담당.
    if level2 and (level2, message2) != (level, message):
        await _send_alert(sess, level2, message2)

    # 복지 매칭 — 패널 전송은 push_welfare 단일 지점(RAG 카드와 병합)
    signals = data.get("welfare_signals") if isinstance(data, dict) else None
    matched = welfare.match(signals or [], transcript)
    # 현재 턴 기준으로 갱신 — 빈 매칭 턴에 갱신을 건너뛰어 과거 항목이 영구 잔존하던 문제 방지
    # (누적 transcript가 잘려 키워드가 사라진 턴 등). 패널 stale 완화.
    sess.welfare_matched = [m["id"] for m in matched]
    if matched or sess.welfare_cards:
        await welfare.push_welfare(sess)
