"""어르신 적합성 정책 (단일 원천) — '이 복지를 어르신께 안내해도 되는가'의 결정론 판정.
수집(fetch._keep_age)과 인덱스 로드(search.load_runtime) 양쪽이 같은 함수를 쓴다.
로드 단계에도 거는 이유: 필터 이전에 만든 구버전 인덱스가 배포돼 있어도
코드만 갱신되면 청년·근로자 제도가 검색에 오르지 않게(소급 방역).

원칙: 과잉 배제 금지 — 전연령·미표기 제도(긴급복지 등)는 유지하고,
'대상이 명백히 어르신이 아닌' 것만 걸러낸다."""
from __future__ import annotations

import re

# 무조건 배제 — 어르신 표지보다 우선 (산재보험 '요양급여'가 요양 표지에 걸려 살아남는 것 방지.
# 업무상 재해 보상은 근로자 전용이라 돌봄 문맥과 항상 미스매치: "허리 삐끗" → 산재 카드 사고)
_HARD_EXCLUDE = re.compile(r"산재|산업재해")

# 어르신 표지 — (하드 배제가 아니면) 하나라도 있으면 유지
_SENIOR = re.compile(
    r"노인|어르신|노년|고령|경로|장수|치매|독거|조손|조부모|노령|양로|요양|재가|틀니|보청기|기초연금"
)
# 만 60세 이상류 표기 ("만 65세 이상", "70세부터")
_SENIOR_AGE = re.compile(r"(?:만\s*)?(?:6[0-9]|[7-9]\d|1[0-4]\d)\s*세\s*(?:이상|부터)")

# 비어르신 전용 표지 — 서비스명에 있으면 배제, 대상에만 있으면 어르신 표지 부재 시 배제.
# '근로'는 근로자·근로장려금·자활근로·근로취약계층을 묶어 커버(65세 이상은 근로능력 판정 제외가 원칙).
# 대출·융자·보증은 금융상품 — 돌봄 비서의 안내 대상(급여·서비스)이 아님.
_YOUTH = re.compile(
    r"청년|영유아|아동|어린이|청소년|임산|임신|출산|신혼|보육|육아|학생|대학|병사|장병|군인"
    r"|근로|사업주|직장인|취업|취준|한부모|소상공인|창업|워킹|맞벌이|대출|융자|보증"
)
# 상한 연령이 60세 미만인 대상 표기 ("만 19세~34세", "39세 이하")
_AGE_CAP = re.compile(r"(?:만\s*)?(\d{1,2})\s*세\s*(?:이하|미만)")
_AGE_RANGE = re.compile(r"(?:만\s*)?\d{1,2}\s*세?\s*[~∼～-]\s*(?:만\s*)?(\d{1,2})\s*세")


def senior_relevant(name: str, target: str = "") -> bool:
    """서비스명·지원대상 텍스트로 어르신 적합성 판정."""
    name, target = name or "", target or ""
    blob = f"{name} {target}"
    if _HARD_EXCLUDE.search(blob):
        return False
    if _SENIOR.search(blob) or _SENIOR_AGE.search(blob):
        return True
    if _YOUTH.search(name):
        return False
    for m in _AGE_CAP.finditer(target):
        if int(m.group(1)) < 60:
            return False
    for m in _AGE_RANGE.finditer(target):
        if int(m.group(1)) < 60:
            return False
    if _YOUTH.search(target):
        return False
    return True  # 전연령·미표기(긴급복지 등)는 유지


def chunk_senior_relevant(chunk) -> bool:
    """DocChunk 판정 — 구조화 필드가 있으면 그걸로, 없으면(구 PDF 등) 유지."""
    f = chunk.fields or {}
    if not f:
        return True
    target = " ".join(
        str(f.get(k, "")) for k in ("지원대상", "조건", "선정기준") if f.get(k)
    )
    # 관련어(생애주기 태그)는 다중 태그 나열이라 판정에 쓰지 않는다 — 서비스명·대상 본문만
    return senior_relevant(f.get("서비스명", ""), target)
