"""복지카드 생성 (v2 §4-1): 서비스 1건 = 카드 1장 = 청크 1개.
픽스처(knowledge/welfare.json)로 파이프라인을 먼저 완성하고,
실 API 매핑(service_to_card)은 P0 샘플 확보 후 이 파일만 채운다."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from app.rag.schema import DocChunk

KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "knowledge"


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
                fields=fields,
                collected_at=today,
            )
        )
    return out


def service_to_card(svc: dict, collected_at: str) -> DocChunk:
    """공공데이터포털 서비스 1건 → 복지카드.
    TODO(P0): knowledge/samples/central.json·local.json 확보 후 실제 필드명으로 작성.
    (서비스명/지원대상/지원내용/신청방법/문의처/상세URL/서비스ID 매핑)"""
    raise NotImplementedError(
        "P0 대기: 샘플 응답(knowledge/samples/*.json) 확보 후 필드 매핑을 채우세요."
    )
