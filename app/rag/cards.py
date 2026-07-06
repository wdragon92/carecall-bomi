"""복지카드 생성 (v2 §4-1): 서비스 1건 = 카드 1장 = 청크 1개.
픽스처(knowledge/welfare.json)로 파이프라인을 먼저 완성하고,
실 API 매핑(service_to_card)은 P0 샘플 확보 후 이 파일만 채운다."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from app.rag.schema import DocChunk

KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "knowledge"

BOKJIRO_HOME = "https://www.bokjiro.go.kr"
_BOKJIRO_DETAIL = (
    "https://www.bokjiro.go.kr/ssis-tbu/twataa/wlfareInfo/moveTWAT52011M.do"
    "?wlfareInfoId={sid}&wlfareInfoReldBztpCd=01"
)


def card_url(chunk: DocChunk) -> str:
    """카드 링크 폴백 체인 — 모든 카드에 링크가 '항상' 달리게 하는 단일 지점.
    수집된 딥링크 → servId 기반 복지로 상세 → 복지로 홈."""
    if (chunk.url or "").startswith("http"):
        return chunk.url
    sid = chunk.serv_id or ""
    if sid.startswith("WLF"):
        return _BOKJIRO_DETAIL.format(sid=sid)
    return BOKJIRO_HOME


def fixture_cards(path: Path | None = None, collected_at: str | None = None) -> list[DocChunk]:
    """수기 정리본 welfare.json 12종 → 복지카드. P0(공공데이터 키) 없이도 전체 파이프라인 구동용."""
    p = path or (KNOWLEDGE_DIR / "welfare.json")
    data = json.loads(p.read_text(encoding="utf-8"))
    today = collected_at or date.today().isoformat()
    year = str(data.get("기준연도", ""))
    out: list[DocChunk] = []
    for it in data.get("items", []):
        text = "\n".join(
            [
                f"서비스명: {it['이름']}",
                f"지원대상: {it.get('대상', '')}",
                f"조건: {it.get('조건', '')}",
                f"지원내용: {it.get('금액', '')}",
                f"신청방법: {it.get('신청처', '')}",
                f"요약: {it.get('한줄', '')}",
                "관련어: " + " ".join(it.get("키워드", [])),  # 구어 매칭(BM25) 보강
            ]
        )
        fields = {
            "서비스명": it["이름"],
            "지원대상": it.get("대상", ""),
            "조건": it.get("조건", ""),
            "지원내용": it.get("금액", ""),
            "신청방법": it.get("신청처", ""),
            "문의처": "보건복지상담센터 129",
            "구비서류": "",
            "기준연도": year,
        }
        out.append(
            DocChunk(
                text=text,
                source=f"복지자료 {year}·{it['이름']}",
                source_type="fixture",
                serv_id=f"fixture-{it['id']}",
                url=it.get("링크", ""),
                fields=fields,
                collected_at=today,
            )
        )
    return out


def _clean(s: str, cap: int = 0) -> str:
    out = " ".join((s or "").split())
    return out[:cap].rstrip() if cap and len(out) > cap else out


def service_to_card(svc: dict, scope: str, collected_at: str) -> DocChunk:
    """실 API 응답(목록+상세 병합 dict) 1건 → 복지카드 (샘플: knowledge/samples/*.xml).
    scope: 'central'(중앙부처) | 'local'(지자체). 필드명 출처는 이 함수와 fetch.py뿐."""
    name = svc.get("servNm", "")
    summary = svc.get("wlfareInfoOutlCn") or svc.get("servDgst", "")
    target = svc.get("tgtrDtlCn") or svc.get("sprtTrgtCn", "")
    crit = svc.get("slctCritCn", "")
    benefit = svc.get("alwServCn", "")
    apply_ = svc.get("_apply") or svc.get("aplyMtdCn") or svc.get("aplyMtdNm", "")
    contact = svc.get("_contact") or svc.get("rprsCtadr", "")
    region = " ".join(filter(None, [svc.get("ctpvNm", ""), svc.get("sggNm", "")]))
    related = " ".join(filter(None, [
        svc.get("lifeArray") or svc.get("lifeNmArray", ""),
        svc.get("trgterIndvdlArray") or svc.get("trgterIndvdlNmArray", ""),
        svc.get("intrsThemaArray") or svc.get("intrsThemaNmArray", ""),
    ]))

    lines = [f"서비스명: {name}"]
    if region:
        lines.append(f"지역: {region}")
    if summary:
        lines.append(f"요약: {_clean(summary, 200)}")
    if target:
        lines.append(f"지원대상: {_clean(target, 300)}")
    if crit:
        lines.append(f"선정기준: {_clean(crit, 200)}")
    if benefit:
        lines.append(f"지원내용: {_clean(benefit, 300)}")
    if apply_:
        lines.append(f"신청방법: {_clean(apply_, 200)}")
    if related:
        lines.append(f"관련어: {related}")

    # 대상·내용이 둘 다 summary 폴백이면 카드에 같은 문장이 두 줄 복제됨(실측: 무릎인공관절)
    field_target = _clean(target or summary, 180)
    field_benefit = _clean(benefit or summary, 220)
    if field_benefit == field_target:
        field_benefit = _clean(benefit, 220)  # 원본이 비면 빈 값 — compose_card가 줄 생략
    fields = {
        "서비스명": name,
        "지원대상": field_target,
        "지원내용": field_benefit,
        "신청방법": _clean(apply_, 180) or "주민센터·복지로에서 확인",
        "문의처": _clean(contact, 80) or "보건복지상담센터 129",
        "구비서류": "",
        "기준연도": svc.get("crtrYr", ""),
        "지역": region,
        "소관": _clean(svc.get("jurMnofNm") or svc.get("bizChrDeptNm", ""), 60),
        "_scope": scope,
    }
    org = "복지로/중앙부처" if scope == "central" else f"복지로/지자체 {region}".rstrip()
    return DocChunk(
        text="\n".join(lines),
        source=f"{org} {svc.get('servId', '')}".strip(),
        source_type="api",
        serv_id=svc.get("servId", ""),
        url=svc.get("servDtlLink", ""),
        fields=fields,
        collected_at=collected_at,
    )
