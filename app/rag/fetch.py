"""공공데이터포털 수집 (v2 §4-1, §4-5) — 실 구현 (샘플: knowledge/samples/*.xml).
중앙부처복지서비스(15090532)·지자체복지서비스(15108347), 응답 XML.
실 필드명을 아는 코드는 이 파일과 cards.service_to_card 뿐이다."""
from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from datetime import date

import httpx

from app.rag.schema import DocChunk

log = logging.getLogger("rag.fetch")

_P0_MSG = (
    "공공데이터포털 키가 없습니다: .env의 WELFARE_CENTRAL_API_KEY / WELFARE_LOCAL_API_KEY에 "
    "Decoding 키를 넣으세요. 키 없이는 `python build_index.py --source fixtures` 를 사용하세요."
)

# 어르신 서비스 필터: 생애주기 배열에 '노년' 포함 또는 생애주기 미표기(전 연령성) 항목 유지
AGE_TOKEN = "노년"
REGIONS = ("대구광역시", "경상북도")  # v2 §2 트랙 A — 지역 개인화


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def _clean(s: str, cap: int = 0) -> str:
    out = " ".join((s or "").split())
    return out[:cap].rstrip() if cap and len(out) > cap else out


# data.go.kr은 인증 실패·트래픽 초과·서비스 오류를 HTTP 200 + 에러 XML로 돌려준다(소프트 200).
# 성공 응답은 resultCode=0(SUCCESS). 에러바디를 '정상 0건'으로 흡수하면 텅 빈 인덱스가 만들어지므로
# 파싱 진입점에서 감지해 예외로 승격한다(호출측이 빌드를 중단 → 기존 정상 인덱스 보존).
_OK_CODES = {"", "0", "00"}


def _raise_for_error(root: ET.Element) -> None:
    """공공데이터포털 에러바디 감지 — 에러면 RuntimeError, 정상이면 통과.
    ⚠️ 예외 메시지에 serviceKey가 담기지 않도록 코드/사유만 싣는다."""
    tag = root.tag.rsplit("}", 1)[-1]  # 네임스페이스 접두 제거
    if tag == "OpenAPI_ServiceResponse" or root.find(".//cmmMsgHeader") is not None:
        msg = (_text(root.find(".//returnAuthMsg")) or _text(root.find(".//errMsg"))
               or _text(root.find(".//returnReasonCode")) or "OpenAPI_ServiceResponse")
        raise RuntimeError(f"data.go.kr 오류 응답: {msg}")
    for path in ("resultCode", ".//header/resultCode", ".//cmmMsgHeader/returnReasonCode"):
        el = root.find(path)
        if el is not None and _text(el) not in _OK_CODES:
            reason = _text(root.find("resultMessage")) or _text(root.find(".//resultMsg"))
            raise RuntimeError(f"data.go.kr resultCode={_text(el)} {reason}".strip())


def _rows_to_dicts(xml_text: str) -> tuple[list[dict], int]:
    """wantedList/servList → [{tag: text}], totalCount."""
    root = ET.fromstring(xml_text)
    _raise_for_error(root)  # 소프트 200 에러바디를 정상 0건으로 흡수하지 않는다
    total = int(_text(root.find("totalCount")) or 0)
    rows = []
    for item in root.findall("servList"):
        rows.append({child.tag: _text(child) for child in item})
    return rows, total


def parse_items_central(xml_text: str) -> tuple[list[dict], int]:
    return _rows_to_dicts(xml_text)


def parse_items_local(xml_text: str) -> tuple[list[dict], int]:
    return _rows_to_dicts(xml_text)


def _parse_detail(xml_text: str) -> dict:
    """wantedDtl → 평면 dict (+ 신청방법/문의처 리스트 요약)."""
    root = ET.fromstring(xml_text)
    _raise_for_error(root)  # 에러바디 상세는 카드에 병합하지 않는다(호출측이 폴백 처리)
    out: dict = {}
    for child in root:
        if len(child) == 0:
            out[child.tag] = _text(child)
    # 중앙부처: 신청 절차(applmetList)와 문의(inqplCtadrList)는 리스트 → 요약 문자열로
    apply_steps = [
        _text(el.find("servSeDetailLink"))
        for el in root.findall("applmetList")
        if _text(el.find("servSeDetailNm")).startswith("신청")
    ]
    if apply_steps:
        out["_apply"] = " / ".join(dict.fromkeys(filter(None, apply_steps)))
    contacts = []
    for el in root.findall("inqplCtadrList"):
        nm = _text(el.find("servSeDetailNm")) or _text(el.find("wlfareInfoReldNm"))
        no = _text(el.find("servSeDetailLink")) or _text(el.find("wlfareInfoReldCn"))
        if nm or no:
            contacts.append(f"{nm} {no}".strip())
    if contacts:
        out["_contact"] = " / ".join(contacts[:2])
    return out


async def _get_xml(client: httpx.AsyncClient, url: str, key: str, params: dict,
                   timeout: float = 20.0, tries: int = 4) -> str:
    """GET + 재시도(429 및 일시 네트워크 오류 지수 백오프 — 포털이 간헐적으로 연결을 끊는다).
    예외 메시지에 URL(=serviceKey)을 절대 담지 않는다(로그 유출 방지)."""
    delay = 2.0
    last = "HTTP 429"
    for _ in range(tries):
        try:
            r = await client.get(url, params={"serviceKey": key, **params}, timeout=timeout)
        except httpx.HTTPError as exc:
            last = f"net:{type(exc).__name__}"
            await asyncio.sleep(delay)
            delay *= 2
            continue
        if r.status_code == 429:  # 데이터포털 과속 제한 — 기다렸다 재시도
            last = "HTTP 429"
            await asyncio.sleep(delay)
            delay *= 2
            continue
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}") from None
        return r.text
    raise RuntimeError(f"{last} (retries exhausted)")


async def fetch_all(client: httpx.AsyncClient, list_url: str, key: str,
                    parse, num_rows: int = 100, sleep_s: float = 0.15,
                    extra: dict | None = None) -> list[dict]:
    """목록조회 페이지네이션 (v2 §4-1). extra: 서비스별 필수 파라미터
    (중앙부처는 callTp=L·srchKeyCode=001 없이는 빈 목록을 반환한다 — 실측)."""
    out: list[dict] = []
    page, total = 1, None
    while True:
        params = {"pageNo": page, "numOfRows": num_rows, **(extra or {})}
        rows, tc = parse(await _get_xml(client, list_url, key, params))
        total = tc if total is None else total
        if not rows:
            break
        out += rows
        if len(out) >= (total or 0):
            break
        page += 1
        await asyncio.sleep(sleep_s)
    log.info("list fetched: %d/%s rows (%s)", len(out), total, list_url.rsplit("/", 1)[-1])
    return out


def _keep_age(row: dict, life_key: str) -> bool:
    """어르신 대상 필터 v3 (정책은 senior.senior_relevant 단일 원천):
    - 생애주기에 '노년'이 없으면 제외.
    - '노년'이 있어도 다중 태그 나열(청년,중장년,노년…)일 수 있으므로
      서비스명·대상 본문으로 어르신 적합성을 한 번 더 판정.
    - 생애주기 미표기(전 연령성)는 본문 판정만으로 결정.
    ⚠️ C4: 대상특성·관심주제 배열(trgterIndvdl·intrsThema)은 servDgst(본문)와 성격이 달라
    load(cards.service_to_card '관련어' → chunk_senior_relevant tags)와 동일하게 tags 인자로 넘긴다.
    (본문 target은 넓은 _YOUTH, 카테고리 tags는 좁은 _TAG_EXCLUDE로 판정 — 수집=로드 통일)."""
    from app.rag.senior import senior_relevant

    life = row.get(life_key, "")
    if life and AGE_TOKEN not in life:
        return False
    target = row.get("servDgst", "")
    tags = " ".join(filter(None, [
        life,
        row.get("trgterIndvdlArray") or row.get("trgterIndvdlNmArray", ""),
        row.get("intrsThemaArray") or row.get("intrsThemaNmArray", ""),
    ]))
    return senior_relevant(row.get("servNm", ""), target, tags)


async def api_cards(settings, age_filter: bool = True, progress=None) -> list[DocChunk]:
    """중앙부처(전국) + 지자체(대구·경북) 수집 → 상세조회 병합 → 복지카드.
    시행기간이 끝난 지자체 사업은 제외(시간민감 필터, v2 §2)."""
    from app.rag.cards import service_to_card

    ck, lk = settings.welfare_key("central"), settings.welfare_key("local")
    if not ck and not lk:
        raise RuntimeError(_P0_MSG)

    today = date.today().isoformat()
    ymd_today = today.replace("-", "")
    cards: list[DocChunk] = []

    async with httpx.AsyncClient() as client:
        if ck:
            rows = await fetch_all(client, settings.welfare_central_list_url, ck, parse_items_central,
                                   extra={"callTp": "L", "srchKeyCode": "001"})
            keep = [r for r in rows if not age_filter or _keep_age(r, "lifeArray")]
            if progress:
                progress(f"central: {len(rows)} rows -> {len(keep)} after age filter")
            for i, row in enumerate(keep):
                try:
                    detail = _parse_detail(await _get_xml(
                        client, settings.welfare_central_detail_url, ck,
                        {"callTp": "D", "servId": row["servId"]}))
                except Exception as exc:  # noqa: BLE001 — 상세 실패는 목록 정보만으로 카드
                    log.warning("central detail %s failed: %s", row.get("servId"), exc)
                    detail = {}
                cards.append(service_to_card({**row, **detail}, "central", today))
                if progress and (i + 1) % 50 == 0:
                    progress(f"central detail: {i + 1}/{len(keep)}")
                await asyncio.sleep(0.25)  # 포털 과속(429) 예방

        if lk:
            rows = await fetch_all(client, settings.welfare_local_list_url, lk, parse_items_local)
            keep = [r for r in rows if r.get("ctpvNm") in REGIONS
                    and (not age_filter or _keep_age(r, "lifeNmArray"))]
            if progress:
                progress(f"local: {len(rows)} rows -> {len(keep)} after region({'/'.join(REGIONS)})+age filter")
            for i, row in enumerate(keep):
                try:
                    detail = _parse_detail(await _get_xml(
                        client, settings.welfare_local_detail_url, lk, {"servId": row["servId"]}))
                except Exception as exc:  # noqa: BLE001
                    log.warning("local detail %s failed: %s", row.get("servId"), exc)
                    detail = {}
                merged = {**row, **detail}
                end = merged.get("enfcEndYmd", "")
                if end and end < ymd_today:  # 종료된 사업 제외
                    continue
                cards.append(service_to_card(merged, "local", today))
                if progress and (i + 1) % 50 == 0:
                    progress(f"local detail: {i + 1}/{len(keep)}")
                await asyncio.sleep(0.25)  # 포털 과속(429) 예방

    # C1: 키를 들고도 카드 0건이면 에러 응답을 정상 0건으로 흡수한 정황 — 예외로 승격(빈 인덱스 방지).
    # (실 API는 461+ 목록을 반환하므로 필터 후에도 0건은 사실상 장애·인증오류 신호)
    if (ck or lk) and not cards:
        raise RuntimeError(
            "공공데이터포털 카드 0건(키 보유). 에러 응답을 정상 0건으로 흡수하지 않도록 빌드를 중단합니다."
        )
    if progress:
        progress(f"api cards total: {len(cards)}")
    return cards


async def fetch_detail(settings, serv_id: str, scope: str = "central", timeout: float = 3.0) -> dict | None:
    """상세조회 1건 (런타임 실시간 이중화, v2 §4-5). 실패 시 None → 캐시 폴백."""
    key = settings.welfare_key(scope)
    if not key or not serv_id.startswith("WLF"):
        return None
    url = settings.welfare_central_detail_url if scope == "central" else settings.welfare_local_detail_url
    params = {"callTp": "D", "servId": serv_id} if scope == "central" else {"servId": serv_id}
    try:
        async with httpx.AsyncClient() as client:
            detail = _parse_detail(await _get_xml(client, url, key, params, timeout=timeout))
    except Exception as exc:  # noqa: BLE001
        log.warning("fetch_detail %s failed: %s", serv_id, exc)
        return None
    if not detail.get("servNm"):
        return None
    fresh: dict = {}
    tgt = detail.get("tgtrDtlCn") or detail.get("sprtTrgtCn")
    alw = detail.get("alwServCn")
    apply_ = detail.get("_apply") or detail.get("aplyMtdCn") or detail.get("aplyMtdNm")
    contact = detail.get("_contact") or detail.get("rprsCtadr")
    if tgt:
        fresh["지원대상"] = _clean(tgt, 180)
    if alw:
        fresh["지원내용"] = _clean(alw, 220)
    if apply_:
        fresh["신청방법"] = _clean(apply_, 180)
    if contact:
        fresh["문의처"] = _clean(contact, 80)
    return fresh or None
