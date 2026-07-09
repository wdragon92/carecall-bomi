"""P0: 실 API 샘플(XML) 파싱·카드 매핑 검증 — knowledge/samples/* 는 실제 응답 저장본.
+ C1(에러바디 승격·0건 가드·하한 가드) / C4(수집 태그 판정 통일) 회귀."""
import asyncio
from pathlib import Path

import numpy as np
import pytest

from app.rag import fetch as _fetch
from app.rag.cards import service_to_card
from app.rag.fetch import (_keep_age, _parse_detail, api_cards,
                           parse_items_central, parse_items_local)
from app.rag.index import LoadedIndex, guard_min_count

SAMPLES = Path(__file__).resolve().parents[1] / "knowledge" / "samples"


def _read(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


def test_parse_central_list():
    rows, total = parse_items_central(_read("central_list.xml"))
    assert total == 461 and len(rows) == 3
    r = rows[0]
    assert r["servId"].startswith("WLF") and r["servNm"]
    assert "bokjiro.go.kr" in r["servDtlLink"]


def test_parse_local_list_region_fields():
    rows, total = parse_items_local(_read("local_list.xml"))
    assert total > 4000 and rows[0]["ctpvNm"]  # 시도명 → 대구·경북 필터 근거
    assert rows[0]["servId"].startswith("WLF")


def test_central_detail_to_card():
    rows, _ = parse_items_central(_read("central_list.xml"))
    detail = _parse_detail(_read("central_detail.xml"))
    merged = {**rows[0], **detail}
    card = service_to_card(merged, "central", "2026-07-06")
    assert card.serv_id == "WLF00000024" and card.source_type == "api"
    assert "서비스명: 아이돌봄서비스" in card.text
    assert "지원대상:" in card.text and "지원내용:" in card.text
    assert card.fields["_scope"] == "central"
    assert card.fields["문의처"]  # 대표문의 or 문의 리스트
    assert card.url.startswith("https://www.bokjiro.go.kr")
    assert len(card.text) < 2000  # 상세 전문(5KB+)이 카드에 통째로 들어가지 않음


def test_local_detail_to_card():
    detail = _parse_detail(_read("local_detail.xml"))
    card = service_to_card(detail, "local", "2026-07-06")
    assert card.fields["지역"].startswith("부산광역시")
    assert card.fields["_scope"] == "local"
    assert "신청방법:" in card.text  # aplyMtdCn 전문 요약
    assert card.fields["신청방법"]


# ---- C1: data.go.kr 소프트 200 에러바디 → 예외 승격 (정상 0건 흡수 금지) ----
def test_error_body_gateway_raises():
    err = ("<OpenAPI_ServiceResponse><cmmMsgHeader>"
           "<returnAuthMsg>LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR</returnAuthMsg>"
           "<returnReasonCode>22</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>")
    with pytest.raises(RuntimeError):
        parse_items_central(err)


def test_error_body_resultcode_raises():
    nodata = ("<response><header><resultCode>03</resultCode>"
              "<resultMsg>NODATA_ERROR</resultMsg></header><body/></response>")
    with pytest.raises(RuntimeError):
        parse_items_local(nodata)


def test_success_body_not_flagged():
    # resultCode=0(SUCCESS) 정상 응답은 통과해야 한다(에러 감지 오탐 방지)
    ok = ("<wantedList><totalCount>1</totalCount><resultCode>0</resultCode>"
          "<resultMessage>SUCCESS</resultMessage>"
          "<servList><servId>WLF1</servId><servNm>x</servNm></servList></wantedList>")
    rows, total = parse_items_central(ok)
    assert total == 1 and rows[0]["servId"] == "WLF1"


def test_api_cards_zero_with_key_raises(monkeypatch):
    """키 보유 상태에서 수집 0건 → 예외 승격(에러 응답을 정상 0건으로 흡수 금지)."""
    class _S:
        welfare_central_list_url = "http://x/cl"
        welfare_local_list_url = "http://x/ll"
        welfare_central_detail_url = "http://x/cd"
        welfare_local_detail_url = "http://x/ld"

        def welfare_key(self, scope):
            return "KEY" if scope == "central" else ""

    async def _empty(*a, **k):
        return []

    monkeypatch.setattr(_fetch, "fetch_all", _empty)
    with pytest.raises(RuntimeError):
        asyncio.run(api_cards(_S()))


# ---- C1: 자동 재빌드 하한 가드 ----
def _idx(count):
    return LoadedIndex(chunks=[], embeddings=np.zeros((0, 1), "float32"),
                       meta={"count": count}, hashes={})


def test_min_count_guard():
    assert guard_min_count(400, None) is None            # 첫 빌드 — 비교 대상 없음
    assert guard_min_count(400, _idx(0)) is None         # 이전 0건 — 비교 대상 없음
    assert guard_min_count(410, _idx(400)) is None       # 증가/유지 통과
    assert guard_min_count(205, _idx(410)) is None       # 정확히 50%는 통과(>=)
    assert guard_min_count(204, _idx(410)) is not None   # 50% 미만 급감 → 거부 사유
    assert guard_min_count(0, _idx(12)) is not None      # 완전 소실 → 거부


# ---- C4: 수집(_keep_age) 대상특성 태그를 로드와 동일하게 tags로 판정 ----
def test_keep_age_tag_consistency():
    # 금융상품(서민금융 테마 + 저축 명): 노인 표지 없으면 배제 (실 API '농어가목돈마련저축')
    fin = {"servNm": "농어가목돈마련저축 저축장려금 지급", "servDgst": "만기 시 저축장려금 지급",
           "intrsThemaArray": "서민금융"}
    assert not _keep_age(fin, "lifeArray")
    # 장애인 전용 대상특성 태그: 노인 표지 없으면 배제(tags 인자 경로 — 좁은 _TAG_EXCLUDE)
    disabled = {"servNm": "이동지원 바우처", "servDgst": "등록 대상자 지원",
                "trgterIndvdlArray": "장애인"}
    assert not _keep_age(disabled, "lifeArray")
    # 노년 생애주기 + 장애인 공존 태그는 유지 — 구(舊) fetch는 target 경로로 오배제하던 케이스
    senior = {"servNm": "무릎수술 지원", "servDgst": "등록 대상자", "lifeArray": "노년",
              "trgterIndvdlArray": "장애인"}
    assert _keep_age(senior, "lifeArray")
