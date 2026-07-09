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

# 어르신 표지 — (하드 배제가 아니면) 하나라도 있으면 유지.
# ⚠️ '재가'는 넣지 않는다: '재가장애인 밑반찬지원' 같은 장애인 전용 사업이 딸려온 실사례.
# ⚠️ '조손·조부모'도 넣지 않는다: '온가족보듬사업'(한부모·다문화·임신갈등 상담)이
#    대상 나열의 '조손가족' 한 단어로 통과한 실사례 — 가족상담 도메인은 어르신 돌봄이 아님.
_SENIOR = re.compile(
    r"노인|어르신|노년|고령|경로|장수|치매|독거|노령|양로|틀니|보청기|기초연금"
)
# 만 60세 이상류 표기 ("만 65세 이상", "70세부터")
_SENIOR_AGE = re.compile(r"(?:만\s*)?(?:6[0-9]|[7-9]\d|1[0-4]\d)\s*세\s*(?:이상|부터)")

# 비어르신 전용 표지 — 서비스명에 있으면 배제, 대상·태그에만 있으면 어르신 표지 부재 시 배제.
# '근로'는 근로자·근로장려금·자활근로·근로취약계층을 묶어 커버(65세 이상은 근로능력 판정 제외가 원칙).
# 대출·융자·보증·서민금융·저축·적금·기금은 금융상품(복지 급여가 아님). 장애인·임산부·다문화·외국인 등은
# '노인' 표지 없이 그 대상 전용이면 타 도메인. (금융어라도 노인 표지가 함께 있으면 senior 우선으로 유지)
_YOUTH = re.compile(
    r"청년|영유아|아동|어린이|청소년|임산|임신|출산|신혼|보육|육아|학생|대학|병사|장병|군인"
    r"|근로|사업주|직장인|취업|취준|한부모|소상공인|창업|워킹|맞벌이|대출|융자|보증|서민금융|저축|적금|기금"
    r"|장애인|장애아|다문화|외국인|북한이탈|새터민|결혼이민|난민|수업료|학자금|학습보조"
)

# 대상특성 태그(XML trgterIndvdlNmArray·lifeNmArray) 전용 배제 카테고리.
# ⚠️ _YOUTH를 그대로 쓰면 안 됨 — 픽스처 관련어(검색 키워드)의 '보증금' 같은 일반어가
# 금융상품 패턴(보증)에 걸려 주거급여가 통째로 배제된 실사례.
_TAG_EXCLUDE = re.compile(
    r"장애인|임신|출산|다문화|탈북|외국인|난민|영유아|아동|어린이|청소년|청년|학생|한부모|조손"
)
# 상한 연령이 60세 미만인 대상 표기 ("만 19세~34세", "39세 이하")
_AGE_CAP = re.compile(r"(?:만\s*)?(\d{1,2})\s*세\s*(?:이하|미만)")
_AGE_RANGE = re.compile(r"(?:만\s*)?\d{1,2}\s*세?\s*[~∼～-]\s*(?:만\s*)?(\d{1,2})\s*세")


def senior_relevant(name: str, target: str = "", tags: str = "") -> bool:
    """서비스명·지원대상 텍스트·대상특성 태그(XML trgterIndvdlNmArray 등)로 어르신 적합성 판정.
    tags는 '장애인' '임산부·출산' '다문화·탈북민' 같은 공식 대상 카테고리 — 노인 표지 없이
    이런 전용 카테고리만 달린 서비스는 타 도메인으로 본다."""
    name, target, tags = name or "", target or "", tags or ""
    blob = f"{name} {target}"
    if _HARD_EXCLUDE.search(f"{blob} {tags}"):
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
    # 대상특성 태그: 노년 표지가 태그에도 없고 전용 카테고리(장애인·임산부·다문화 등)만 있으면 배제
    if tags and not _SENIOR.search(tags) and "노년" not in tags and _TAG_EXCLUDE.search(tags):
        return False
    return True  # 전연령·미표기(긴급복지 등)는 유지


_TAG_LINE = re.compile(r"관련어:\s*(.+)")


def chunk_senior_relevant(chunk) -> bool:
    """DocChunk 판정 — 구조화 필드 + 카드 텍스트의 관련어(대상특성·생애주기 태그)로 판정."""
    f = chunk.fields or {}
    if not f:
        return True
    target = " ".join(
        str(f.get(k, "")) for k in ("지원대상", "조건", "선정기준") if f.get(k)
    )
    m = _TAG_LINE.search(chunk.text or "")
    tags = m.group(1) if m else ""
    return senior_relevant(f.get("서비스명", ""), target, tags)
