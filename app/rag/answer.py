"""응답 조립 (v2 §4-4, §4-5): 역할 분리 — LLM은 말투와 흐름, 수치는 코드가 카드로.
카드(📌)는 별도 말풍선(kind:card)으로 나가고, TTS는 짧은 안내문(tts_text)만 읽는다."""
from __future__ import annotations

import logging
import re
from datetime import date

from app.rag.cards import card_url
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

        fresh = await fetch.fetch_detail(settings, chunk.serv_id, scope=fields.get("_scope", "central"))
    except Exception as exc:  # noqa: BLE001 — 상세조회 실패로 턴을 깨지 않는다
        log.warning("refresh_detail failed (%s) -> cache", exc)
        fresh = None
    if fresh:
        fields.update(fresh)
        # 실시간 조회 성공 → 표시 기준일을 오늘로 갱신 (수집본 캐시 날짜가 아니라 방금 확인한 값).
        # compose_card가 이 사설 키를 우선 사용한다. 실패·픽스처 경로에는 추가하지 않는다.
        fields["_collected_at"] = date.today().isoformat()
        return fields, True
    return fields, False


def _prefix_dup(a: str, b: str) -> bool:
    """두 필드가 사실상 같은 문구인지 — 공백 무시 후 완전 일치이거나 한쪽이 다른 쪽의 접두면 True.
    (대상/지원이 같은 요약을 서로 다른 길이로 자른 접두 중복 카드를 막는다.)"""
    na = re.sub(r"\s+", "", a or "")
    nb = re.sub(r"\s+", "", b or "")
    if not na or not nb:
        return False
    return na.startswith(nb) or nb.startswith(na)


def compose_card(chunk: DocChunk, fields: dict, live: bool) -> tuple[str, str]:
    """T2 정보 카드 — 금액·신청처·기준일은 여기(구조화 필드)에서만 나온다.
    반환: (카드 텍스트, TTS 대체 안내문)."""
    name = fields.get("서비스명", "").strip() or "복지 서비스"
    lines = [f"📌 {name}"]
    if fields.get("지역"):
        lines.append(f"· 지역: {fields['지역']}")
    target = fields.get("지원대상") or ""
    benefit = fields.get("지원내용") or ""
    if target:
        lines.append(f"· 대상: {target}")
    # 대상과 동일 문구 복제 방지 — 완전 일치뿐 아니라 한쪽이 다른 쪽의 접두인 경우도 걸러낸다.
    # (service_to_card가 대상 180자 / 지원 220자로 잘라 둘 다 summary 폴백이면 접두 중복 발생)
    if benefit and not _prefix_dup(benefit, target):
        lines.append(f"· 지원: {benefit}")
    if fields.get("신청방법"):
        lines.append(f"· 신청: {fields['신청방법']}")
    if fields.get("구비서류"):
        lines.append(f"· 서류: {fields['구비서류']}")
    lines.append(f"· 문의: {fields.get('문의처') or '보건복지상담센터 129'}")
    # 표시 기준일: live 갱신본(refresh_detail가 오늘로 세팅) 우선, 없으면 수집본 캐시 날짜.
    base_date = fields.get("_collected_at") or chunk.collected_at
    if base_date:
        lines.append(f"· 정보 기준일: {base_date}" + (" · 방금 확인" if live else ""))
    lines.append(f"· 복지로: {card_url(chunk)}")  # 링크는 폴백 체인으로 항상 부착
    tts = f"{name}의 지원 내용과 신청 방법은 화면에 정보 카드로 정리해 드렸어요."
    return "\n".join(lines), tts


# 부정 답변 신호 — "그런 정책은 없습니다" 류. 이때 무관한 카드를 붙이면 답변과 모순(적대적 케이스).
_NEGATION = re.compile(r"없습니다|없어요|없네요|않습니다|않아요|찾지 못|확인되지 않|해당하지 않|존재하지 않")


def pick_card(
    retrieved: list[tuple[DocChunk, float]], llm_text: str, strict: bool = False,
) -> DocChunk | None:
    """카드로 보여줄 1건 (카드 = '답변'의 시각화, 검색 결과의 시각화가 아님).
    1) AI가 실제 언급한 서비스명 매칭(공백 무시)만 신뢰.
    2) 매칭 실패 시:
       - strict(실 LLM): 카드 생략 — 적대적 질문('전 국민 100만원?')에서 검색 1위
         (어휘 우연 일치한 무관 서비스)가 붙는 모순을 원천 차단. 프롬프트가 서비스명을
         그대로 부르게 하므로 정상 안내에서는 이름이 매칭된다.
       - lenient(목 LLM, 정형 응답): 부정('없습니다' 등)만 아니면 검색 1위 폴백.
    PDF 청크(fields 없음)는 카드 불가."""
    withf = [(c, s) for c, s in retrieved if c.fields]
    if not withf:
        return None
    text_n = re.sub(r"\s+", "", llm_text or "")
    for c, _ in withf:
        name_n = re.sub(r"\s+", "", c.fields.get("서비스명", ""))
        if name_n and name_n in text_n:
            return c
    for c, _ in withf:
        base_n = re.sub(r"\s+", "", c.fields.get("서비스명", "").split("(")[0])
        if base_n and base_n in text_n:
            return c
    if strict or _NEGATION.search(llm_text or ""):
        return None
    return withf[0][0]


async def retrieve_for(providers, settings, question: str) -> tuple[Retrieval, bool]:
    """질의 임베딩 → 하이브리드 검색 + 게이트 판정. 반환: (Retrieval, 통과 여부)."""
    rt: RagRuntime | None = providers.rag
    if rt is None:
        return Retrieval(), False
    qvec = (await providers.embed.embed([question]))[0]
    emode = providers.modes.get("embed", "mock")
    r = hybrid_retrieve(rt, qvec, question, k=settings.rag_top_k, pool=settings.rag_pool,
                        min_vec=settings.rag_item_threshold(emode),
                        region=settings.rag_default_region)
    return r, passes_gate(r, settings, emode)
