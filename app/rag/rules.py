"""사전판정 룰엔진 (v2 §4-6) — 기초연금 1종, 룰 플러그인 구조.
역할 분리: LLM은 대화에서 슬롯 추출만, 판정은 이 파일의 결정론적 코드가 한다.
판정 어휘는 항상 '가능성' — 확정은 신청 후 지자체 조사(T3)의 몫."""
from __future__ import annotations

import re

# ⚠️ 고시 상수 — 사람이 원문 확인 후 기입 (LLM 생성값 금지, v2 §8)
# 출처: 보건복지부 보도자료 "2026년 노인 단독가구, 소득인정액 월 247만 원 이하면 기초연금 받는다"
#       (2026-01-01, mohw.go.kr board.es list_no=1488478) — 사용자+웹 교차 확인(2026-07-06).
#       부부가구는 단독의 1.6배(247×1.6=395.2만 원) — 검산 일치.
BASIC_PENSION_2026 = {
    "age_min": 65,                          # 만 65세 이상 (기초연금법 §3)
    "income_threshold_single": 2_470_000,   # 선정기준액(단독가구, 월 소득인정액·원)
    "income_threshold_couple": 3_952_000,   # 선정기준액(부부가구)
}

MOCK_CALC_GUIDE = "복지로 홈페이지의 '복지서비스 모의계산'에서 미리 셈해볼 수 있어요."


def check_basic_pension(
    age: int | None, household: str | None, income_hint: int | None,
) -> tuple[str, str]:
    """기초연금 1차 스크리닝. 반환: (판정, 보미 멘트).
    판정: 해당없음 | 확인필요 | 가능성높음 | 가능성낮음"""
    if age is None:
        return "확인필요", "기초연금은 연세 기준이 있어서요. 실례지만 어르신, 올해 연세가 어떻게 되세요?"
    if age < BASIC_PENSION_2026["age_min"]:
        return "해당없음", (
            f"기초연금은 만 {BASIC_PENSION_2026['age_min']}세부터 신청하실 수 있어요. "
            "아직은 조금 이르지만, 때가 되면 제가 꼭 다시 챙겨드릴게요."
        )
    if household is None:
        return "확인필요", "혼자 지내세요, 아니면 배우자분과 함께 지내세요? 가구에 따라 기준이 달라서요."

    th = BASIC_PENSION_2026.get(f"income_threshold_{household}")
    if income_hint is None or th is None:
        # 소득인정액은 소득+재산 환산이라 복잡 — 1차 스크리닝은 여기까지, 모의계산·신청 연계(T3)
        crit = (
            f"{'혼자 사시는' if household == 'single' else '부부'} 가구는 "
            f"한 달 소득인정액 {th / 10_000:g}만 원 이하면 받으실 수 있어요(2026년 기준). "
            if th else "연세는 되시니까 소득·재산에 따라 받으실 수 있어요. "
        )
        return "확인필요", (
            crit + f"정확한 건 신청하시면 구청에서 셈해주고요, {MOCK_CALC_GUIDE} "
            "신청에 필요한 것들은 제가 카드로 정리해 드릴게요."
        )
    if income_hint * 10_000 <= th:  # 슬롯 income은 만원 단위, 고시값은 원
        # income 힌트는 원천소득(연금·근로 등)이라 소득인정액(=소득+재산 환산)보다 낮게 잡히기 쉽다.
        # 자산 보유 저소득 어르신의 과잉확신(false-positive)을 낮추려 재산 환산 캐비엇을 덧붙인다.
        return "가능성높음", (
            f"기준이 한 달 소득인정액 {th / 10_000:g}만 원 이하인데, 말씀해주신 걸로 보면 "
            "받으실 가능성이 높아 보여요. 다만 소득인정액은 소득에 집·자동차 같은 재산을 환산해 "
            "더한 금액이라, 재산이 있으시면 달라질 수 있어요. "
            "최종 확정은 신청 후 구청 조사로 정해지니 꼭 신청해 보세요. 신청 준비물은 카드로 정리해 드릴게요."
        )
    return "가능성낮음", (
        f"기준(월 {th / 10_000:g}만 원)을 조금 넘을 수도 있어요. "
        f"그래도 재산 환산에 따라 달라지니 {MOCK_CALC_GUIDE}"
    )


# ---- 판정 의도 감지 (트리거 C) ----
_SCREEN_TARGET = re.compile(r"기초\s*연금|노령\s*연금")
_SCREEN_ASK = re.compile(r"받을\s*수|받나|받겠|되나|될까|해당|자격|대상|가능|신청할\s*수|나도|저도")


def detect_screen_intent(text: str) -> str | None:
    """'기초연금 나도 받을 수 있나?' 류 → 'basic_pension'. 아니면 None."""
    t = text or ""
    if _SCREEN_TARGET.search(t) and _SCREEN_ASK.search(t):
        return "basic_pension"
    return None


# ---- 슬롯 추출 (LLM extract_json 용) ----
SLOT_SYSTEM = """대화에서 기초연금 판정에 필요한 정보만 뽑는 분석기입니다.
어르신 발화에서 아래 3가지를 찾아 JSON으로만 출력하세요. 대화에 없으면 null.
- age: 만 나이 숫자 ("일흔둘"→72, "예순다섯"→65). 애매하면 null.
- household: "single"(혼자 삶) | "couple"(배우자와 삶) | null
- income: 월 소득 힌트(만원 단위 숫자, 예: "연금 30만원 받아"→30) | null
출력 형식: {"age": 72, "household": "single", "income": null}"""

SLOT_SCHEMA = {
    "type": "object",
    "properties": {
        "age": {"type": ["integer", "null"]},
        "household": {"type": ["string", "null"], "enum": ["single", "couple", None]},
        "income": {"type": ["integer", "null"]},
    },
}

# 정규식 폴백 (mock 모드/LLM 실패 시) — 흔한 표현만 커버
_TENS = {"예순": 60, "일흔": 70, "여든": 80, "아흔": 90, "쉰": 50}
_ONES = {"하나": 1, "한": 1, "둘": 2, "두": 2, "셋": 3, "세": 3, "넷": 4, "네": 4,
         "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9}


def slots_from_text(text: str) -> dict:
    """결정적 슬롯 추출 폴백. LLM 없이도 데모가 동작하게.
    같은 슬롯이 여러 번 언급되면 **마지막 언급**을 채택한다 — "일흔둘이야 … 아니 예순넷이야"
    같은 정정 발화에서 최신 값이 이겨야 merge_slots(정정 우선)와 아귀가 맞는다."""
    t = text or ""
    out: dict = {"age": None, "household": None, "income": None}

    age_pos = -1
    for m in re.finditer(r"(?:만\s*)?(\d{2,3})\s*(?:살|세)", t):
        age = int(m.group(1))
        if 40 <= age <= 119:  # "만 100세" 오파싱(00세→0)·전화번호 오탐 방지
            out["age"], age_pos = age, m.start()
    for tens, tv in _TENS.items():
        for m in re.finditer(tens, t):
            if m.start() <= age_pos:
                continue
            age = tv
            tail = t[m.end(): m.end() + 4]
            for ones, ov in _ONES.items():
                if re.match(r"\s*" + ones, tail):
                    age = tv + ov
                    break
            out["age"], age_pos = age, m.start()

    single = [m.start() for m in re.finditer(r"혼자|독거|홀로", t)]
    couple = [m.start() for m in re.finditer(r"부부|배우자|영감|할멈|같이 살|둘이 살", t)]
    if single or couple:
        out["household"] = "single" if max(single or [-1]) > max(couple or [-1]) else "couple"
    return out


def merge_slots(base: dict, new: dict) -> dict:
    """새로 확인된 값이 우선(정정 반영: "아니 예순넷이야"), 새 정보가 없으면 기존 유지.
    슬롯 추출은 사용자 발화 전체를 다시 보므로 최신 추출값이 곧 최신 사실이다."""
    out = dict(base or {})
    for k in ("age", "household", "income"):
        if new.get(k) is not None:
            out[k] = new[k]
    return out
