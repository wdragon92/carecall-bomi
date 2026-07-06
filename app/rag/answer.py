"""응답 조립 (v2 §4-4, §4-5): 역할 분리 — LLM은 말투와 흐름, 수치는 코드가 카드로.
카드(📌)는 별도 말풍선(kind:card)으로 나가고, TTS는 짧은 안내문(tts_text)만 읽는다."""
from __future__ import annotations

import logging

from app.rag.schema import DocChunk
from app.rag.search import RagRuntime, Retrieval, hybrid_retrieve, passes_gate

log = logging.getLogger("rag.answer")

REJECT_ANSWER = (
    "제가 가진 복지 자료에서는 정확한 답을 찾지 못했어요. "
    "보건복지상담센터 129나 가까운 주민센터에 문의해 보시는 게 좋겠어요."
)


def rag_prompt_block(retrieved: list[tuple[DocChunk, float]]) -> str:
    """검색된 카드 → 시스템 프롬프트 [복지 자료] 블록 (출처·기준일 포함)."""
    parts = []
    for c, _ in retrieved:
        head = f"### {c.source}" + (f" (기준일 {c.collected_at})" if c.collected_at else "")
        parts.append(f"{head}\n{c.text}")
    return "\n\n".join(parts)


async def refresh_detail(settings, chunk: DocChunk) -> tuple[dict, bool]:
    """매칭 서비스 1건 실시간 상세조회 이중화 (§4-5).
    api 카드만 시도, 실패·키없음·픽스처는 캐시(수집본) 폴백. 반환: (fields, live)."""
    fields = dict(chunk.fields or {})
    if chunk.source_type != "api" or not chunk.serv_id:
        return fields, False
    try:
        from app.rag import fetch

        fresh = await fetch.fetch_detail(settings, chunk.serv_id)
    except Exception as exc:  # noqa: BLE001 — 상세조회 실패로 턴을 깨지 않는다
        log.warning("refresh_detail failed (%s) -> cache", exc)
        fresh = None
    if fresh:
        fields.update(fresh)
        return fields, True
    return fields, False


def compose_card(chunk: DocChunk, fields: dict, live: bool) -> tuple[str, str]:
    """T2 정보 카드 — 금액·신청처·기준일은 여기(구조화 필드)에서만 나온다.
    반환: (카드 텍스트, TTS 대체 안내문)."""
    name = fields.get("서비스명", "").strip() or "복지 서비스"
    lines = [f"📌 {name}"]
    if fields.get("지원대상"):
        lines.append(f"· 대상: {fields['지원대상']}")
    if fields.get("지원내용"):
        lines.append(f"· 지원: {fields['지원내용']}")
    if fields.get("신청방법"):
        lines.append(f"· 신청: {fields['신청방법']}")
    if fields.get("구비서류"):
        lines.append(f"· 서류: {fields['구비서류']}")
    lines.append(f"· 문의: {fields.get('문의처') or '보건복지상담센터 129'}")
    if chunk.collected_at:
        lines.append(f"· 정보 기준일: {chunk.collected_at}" + (" · 방금 확인" if live else ""))
    if chunk.url:
        lines.append(f"· 복지로: {chunk.url}")
    tts = f"{name}의 지원 내용과 신청 방법은 화면에 정보 카드로 정리해 드렸어요."
    return "\n".join(lines), tts


def pick_card(retrieved: list[tuple[DocChunk, float]], llm_text: str) -> DocChunk | None:
    """카드로 보여줄 1건: AI가 실제 언급한 서비스 우선, 없으면 검색 1위.
    fields 없는 청크(PDF)는 카드 불가."""
    withf = [(c, s) for c, s in retrieved if c.fields]
    if not withf:
        return None
    for c, _ in withf:
        name = c.fields.get("서비스명", "")
        if name and name in llm_text:
            return c
    for c, _ in withf:
        base = c.fields.get("서비스명", "").split("(")[0].strip()
        if base and base in llm_text:
            return c
    return withf[0][0]


async def retrieve_for(providers, settings, question: str) -> tuple[Retrieval, bool]:
    """질의 임베딩 → 하이브리드 검색 + 게이트 판정. 반환: (Retrieval, 통과 여부)."""
    rt: RagRuntime | None = providers.rag
    if rt is None:
        return Retrieval(), False
    qvec = (await providers.embed.embed([question]))[0]
    r = hybrid_retrieve(rt, qvec, question, k=settings.rag_top_k, pool=settings.rag_pool)
    return r, passes_gate(r, settings, providers.modes.get("embed", "mock"))
