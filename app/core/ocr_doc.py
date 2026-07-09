"""OCR 문서 인식 — 종류 분류 + 어르신용 요약 카드 조립.
분류는 real LLM(extract_json) → 목 모드·실패 시 결정적 키워드 룰 폴백.
T2 원칙: 카드에 들어가는 수치·날짜·연락처는 OCR 원문에 있는 문자열만 —
LLM 출력의 숫자 토큰을 코드가 원문과 대조해 지어낸 값은 버린다."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from app.core import prompts_analysis

log = logging.getLogger("ocr_doc")


@dataclass
class DocInfo:
    종류: str = "기타"
    보낸곳: str = ""
    한줄요약: str = ""
    해야할일: list[str] = field(default_factory=list)
    주의: str = ""
    사기_의심: bool = False
    사기_이유: str = ""


# ---- 결정적 룰 폴백 (목 모드·LLM 실패 시에도 데모가 살아있게) ----
_SCAM_LURE = ("http", "bit.ly", "링크", "클릭", "눌러", "접속")
_SCAM_BAIT = ("반송", "폐기", "당첨", "환급", "계좌", "송금", "이체", "벌금", "검찰", "출석", "인증번호", "미납")
_TYPE_RULES = [
    ("고지서·청구서", ("청구서", "고지서", "청구금액", "납기", "납부", "요금")),
    ("복지·관공서 안내문", ("기초연금", "연금", "복지", "수급", "바우처", "지원금", "안내문", "주민센터", "구청", "시청", "군청", "공단")),
    ("병원·약국 서류", ("처방", "복용", "약국", "병원", "진료", "검진", "투약")),
    ("광고·전단", ("할인", "세일", "특가", "개업", "광고")),
]
_RULE_SUMMARY = {
    "고지서·청구서": "요금을 안내하는 고지서로 보여요",
    "복지·관공서 안내문": "복지 혜택을 안내하는 우편으로 보여요",
    "병원·약국 서류": "병원이나 약국에서 온 서류로 보여요",
    "광고·전단": "광고 전단으로 보여요",
    "문자·메시지": "휴대전화로 온 문자 내용이에요",
}
_ORG = re.compile(
    r"([가-힣A-Za-z0-9()]+(?:공사|공단|구청|시청|군청|센터|은행|카드|보험|병원|의원|약국|우체국|복지과|복지관))"
)
_AMOUNT = re.compile(r"\d[\d,]*\s*원")
# 날짜 오매칭 방지: 월 1~12·일 1~31로 제한하고, 애매한 'NN-NN/NN.NN'(전화·고객번호 조각)은
# 4자리 연도(ISO형)나 한글 '월/일' 표기가 있을 때만 날짜로 인정한다.
_MON = r"(?:1[0-2]|0?[1-9])"
_DAY = r"(?:3[01]|[12]\d|0?[1-9])"
_DATE = re.compile(
    rf"\d{{4}}\s*[.\-/]\s*{_MON}\s*[.\-/]\s*{_DAY}(?:\s*까지)?"
    rf"|(?:\d{{4}}\s*년\s*)?{_MON}\s*월(?:\s*{_DAY}\s*일)?(?:\s*까지)?"
)


def classify_by_rules(text: str) -> DocInfo:
    t = text or ""
    doc = DocInfo()
    lure = any(k in t for k in _SCAM_LURE)
    bait = any(k in t for k in _SCAM_BAIT)
    if "web발신" in t.lower() or (lure and bait):
        doc.종류 = "문자·메시지"
        if lure and bait:
            doc.사기_의심 = True
            doc.사기_이유 = "출처가 불분명한 링크를 누르도록 유도하고 있어요"
    else:
        for name, keys in _TYPE_RULES:
            if any(k in t for k in keys):
                doc.종류 = name
                break
    doc.한줄요약 = "사기로 의심되는 문자예요" if doc.사기_의심 else _RULE_SUMMARY.get(doc.종류, "")

    m = _ORG.search(t)
    if m:
        doc.보낸곳 = m.group(1)

    amount = _AMOUNT.search(t)
    runs = _src_runs(t)
    dates = [d.strip() for d in _DATE.findall(t)]
    due = next((d for d in dates if "까지" in d), "")
    if not due and dates:  # 기한 없으면 '연도·일'이 든 완성형 날짜 우선 — 막연한 마지막 매칭 채택 완화
        strong = [d for d in dates if "일" in d or re.search(r"\d{4}", d)]
        due = (strong or dates)[-1]
    until = "" if due.endswith("까지") else "까지"

    def _t2(line: str) -> str:  # 조립 문구의 숫자가 원문에 없으면 버림(룰 폴백에도 원문 대조 가드)
        return "" if _fabricated(line, runs) else line

    if doc.종류 == "고지서·청구서" and amount:
        base = f"{amount.group(0)} 납부"
        doc.해야할일 = [(_t2(f"{due}{until} {base}") or base) if due else base]
    elif doc.종류 == "복지·관공서 안내문" and "신청" in t:
        where = "주민센터에서 " if "주민센터" in t else ("복지로에서 " if "복지로" in t else "")
        default = f"{where}신청 방법 확인해 보기".strip()
        doc.해야할일 = [(_t2(f"{due}{until} {where}신청".strip()) or default) if due else default]
    return doc


# ---- LLM 분류 결과 검증 (T2: 원문에 없는 숫자는 버린다) ----
def _src_runs(src: str) -> list[str]:
    return re.findall(r"\d+", re.sub(r"[,\s.]", "", src))


def _fabricated(line: str, src_runs: list[str]) -> bool:
    """3자리 이상 숫자 토큰이 원문 숫자열과 '통째로' 일치하지 않으면 지어낸 것으로 판정.
    부분열 매칭(digits in run)은 조작된 짧은 숫자가 원문의 긴 숫자(고객번호·계좌)에
    묻혀 통과("456"⊂"4567")하므로, 토큰 단위 정확 일치(경계 기반)로 조인다."""
    runs = set(src_runs)
    for tok in re.findall(r"\d[\d,.\s]*\d|\d", line):
        digits = re.sub(r"\D", "", tok)
        if len(digits) >= 3 and digits not in runs:
            return True
    return False


def _from_llm(data, src: str) -> DocInfo | None:
    if not isinstance(data, dict) or "종류" not in data:
        return None
    runs = _src_runs(src)

    def clean(v, n: int) -> str:
        return str(v or "").strip()[:n]

    doc = DocInfo(
        종류=data.get("종류") if data.get("종류") in prompts_analysis.DOC_TYPES else "기타",
        보낸곳=clean(data.get("보낸곳"), 40),
        주의=clean(data.get("주의"), 80),
        사기_의심=bool(data.get("사기_의심")),
        사기_이유=clean(data.get("사기_이유"), 80),
    )
    summary = clean(data.get("한줄요약"), 90)
    doc.한줄요약 = "" if _fabricated(summary, runs) else summary
    items = data.get("해야할일") or []
    if isinstance(items, list):
        for it in items[:3]:
            s = clean(it, 70)
            if s and not _fabricated(s, runs):
                doc.해야할일.append(s)
    return doc


async def classify_document(providers, ocr_text: str) -> DocInfo:
    """real LLM이면 구조화 분류, 아니면(또는 실패하면) 룰 폴백. 예외를 밖으로 내지 않는다."""
    text = (ocr_text or "").strip()
    if not text:
        return DocInfo()
    if providers.modes.get("llm") == "real":
        try:
            data = await providers.llm.extract_json(
                [
                    {"role": "system", "content": prompts_analysis.DOC_CLASSIFY_SYSTEM},
                    {"role": "user", "content": text[:2000]},
                ],
                prompts_analysis.DOC_CLASSIFY_SCHEMA,
            )
            doc = _from_llm(data, text)
            if doc is not None:
                return doc
            log.warning("doc classify: 형식 이상 → rules")
        except Exception as exc:  # noqa: BLE001
            log.warning("doc classify llm failed (%s) → rules", exc)
    return classify_by_rules(text)


def compose_doc_card(doc: DocInfo) -> tuple[str, str]:
    """카드 텍스트 + 짧은 TTS 문구. 알맹이가 없으면 ("","") — 카드 생략."""
    if doc.종류 == "기타" and not (doc.한줄요약 or doc.해야할일 or doc.사기_의심):
        return "", ""
    icon = "🚨" if doc.사기_의심 else "📄"
    if doc.종류 == "기타":
        lines = [f"{icon} 사진 속 내용을 정리해 드렸어요"]
    else:
        what = "문자" if doc.종류 == "문자·메시지" else "문서"
        lines = [f"{icon} 이 {what}는 「{doc.종류}」예요"]
    if doc.보낸곳:
        lines.append(f"보낸 곳: {doc.보낸곳}")
    if doc.한줄요약:
        lines.append(f"내용: {doc.한줄요약}")
    if doc.해야할일:
        lines.append("하실 일:")
        lines += [f"· {it}" for it in doc.해야할일]
    if doc.사기_의심:
        lines.append(f"⚠️ 사기 의심: {doc.사기_이유 or '조심하세요'}")
        lines.append("· 링크를 누르지 마세요")
        lines.append("· 개인정보·돈을 보내지 마세요")
    elif doc.주의:
        lines.append(f"⚠️ 주의: {doc.주의}")
    lines.append("(사진 속 글자를 읽은 거예요 · 정확한 내용은 보낸 곳에 확인해 주세요)")

    if doc.사기_의심:
        tts = "이 문자는 사기로 의심돼요. 링크는 누르지 마시고, 화면에 정리해 드린 카드를 한번 봐 주세요."
    elif doc.종류 == "기타":
        tts = "사진 내용을 화면에 카드로 정리해 드렸어요."
    else:
        tts = f"{doc.종류} 같아요. 중요한 내용은 화면에 카드로 크게 정리해 드렸어요."
    return "\n".join(lines), tts
