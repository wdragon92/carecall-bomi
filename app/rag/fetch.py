"""공공데이터포털 수집 (v2 §4-1, §4-5) — P0 대기 스텁.
실 API 필드명을 아는 코드는 이 파일(parse_items_*)과 cards.service_to_card 뿐이다.
P0 완료 시(.env WELFARE_API_KEY + knowledge/samples/*.json) 여기만 채우면 --source api 활성화."""
from __future__ import annotations

import asyncio
import logging

import httpx

from app.rag.schema import DocChunk

log = logging.getLogger("rag.fetch")

_P0_MSG = (
    "공공데이터포털 수집은 P0 완료 후 활성화됩니다: "
    "1) 활용신청(중앙부처 15090532 / 지자체 15108347) 후 Decoding 키를 .env WELFARE_API_KEY에, "
    "2) 샘플 응답을 knowledge/samples/central.json·local.json 으로 저장, "
    "3) fetch.parse_items_* / cards.service_to_card 매핑 작성. "
    "지금은 `python build_index.py --source fixtures` 를 사용하세요."
)


async def api_cards(settings) -> list[DocChunk]:
    """중앙부처+지자체 복지서비스 전체 수집 → 복지카드 목록.
    URL은 config 기본값(실경로 검증 완료), 키만 .env에 있으면 됨."""
    if not settings.welfare_api_key.strip():
        raise RuntimeError(_P0_MSG)
    raise NotImplementedError(_P0_MSG)  # TODO(P0): 샘플 응답 확보 후 parse_items_* + service_to_card


async def fetch_all(client: httpx.AsyncClient, list_url: str, service_key: str,
                    parse_items, num_rows: int = 100, sleep_s: float = 0.2) -> list[dict]:
    """목록조회 페이지네이션 (v2 §4-1). serviceKey는 반드시 Decoding 키."""
    out: list[dict] = []
    page = 1
    while True:
        resp = await client.get(list_url, params={
            "serviceKey": service_key, "pageNo": page, "numOfRows": num_rows,
        }, timeout=10.0)
        resp.raise_for_status()
        items = parse_items(resp)
        if not items:
            break
        out += items
        page += 1
        await asyncio.sleep(sleep_s)
    return out


def parse_items_central(resp) -> list[dict]:
    """중앙부처복지서비스 목록 응답 → 서비스 dict 목록. TODO(P0): 샘플 보고 JSON/XML·필드 확정."""
    raise NotImplementedError(_P0_MSG)


def parse_items_local(resp) -> list[dict]:
    """지자체복지서비스 목록 응답(대구/경북 필터) → 서비스 dict 목록. TODO(P0)."""
    raise NotImplementedError(_P0_MSG)


async def fetch_detail(settings, serv_id: str, timeout: float = 3.0) -> dict | None:
    """상세조회 1건 (실시간 이중화, v2 §4-5). 실패 시 None → 호출부가 캐시로 폴백.
    TODO(P0): detail URL·파라미터명·응답 파싱 확정."""
    if not settings.welfare_api_key.strip() or not settings.welfare_central_detail_url.strip():
        return None
    return None
