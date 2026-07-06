"""사전판정 룰엔진 (v2 §4-6) — 기초연금 1종, 룰 플러그인 구조.
역할 분리: LLM은 대화에서 슬롯 추출만, 판정은 이 파일의 결정론적 코드가 한다.
판정 어휘는 항상 '가능성' — 확정은 신청 후 지자체 조사(T3)의 몫."""
from __future__ import annotations

import re

# ⚠️ 고시 상수 — 반드시 사람이 보건복지부 고시 원문을 확인해 기입 (LLM 생성값 금지, v2 §8)
# 출처(기입 시 주석으로 남길 것): 보건복지부 고시 「2026년도 기초연금 선정기준액」
# None = 미확인 → 룰엔진은 '확인필요'로 안전하게 응답
BASIC_PENSION_2026 = {
    "age_min": 65,                     # 만 65세 이상 (기초연금법 §3)
    "income_threshold_single": None,   # 선정기준액(단독가구, 월 소득인정액 원) — P0에서 확인
    "income_threshold_couple": None,   # 선정기준액(부부가구) — P0에서 확인
}

MOCK_CALC_GUIDE = "복지로 홈페이지의 '복지서비스 모의계산'에서 미리 셈해볼 수 있어요."


def check_basic_pension(
    age: int | None, household: str | None, income_hint: int | None,
) -> tuple[str, str]:
    """기초연금 1차 스크리닝. 반환: (판정, 보미 멘트).
    판정: 해당없음 | 확인필요 | 가능성높음 | 가능성낮음"""
    if age is None:
        return "확인필요", "실례지만 어르신, 올해 연세가 어떻게 되세요? 만 나이로 알려주시면 제가 셈해볼게요."
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
        return "확인필요", (
            "연세는 되시니까 소득·재산에 따라 받으실 수 있어요. "
            f"정확한 건 신청하시면 구청에서 셈해주고요, {MOCK_CALC_GUIDE} "
            "신청에 필요한 것들은 제가 카드로 정리해 드릴게요."
        )
    if income_hint <= th:
        return "가능성높음", (
            "말씀해주신 걸로 보면 받으실 가능성이 높아 보여요. "
            "최종 확정은 신청 후 구청 조사로 정해지니, 꼭 신청해 보세요. 신청 준비물은 카드로 정리해 드릴게요."
        )
    return "가능성낮음", (
        "말씀해주신 소득으로는 기준을 조금 넘을 수도 있어요. "
        f"그래도 재산 상황에 따라 달라지니 {MOCK_CALC_GUIDE}"
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
    """결정적 슬롯 추출 폴백. LLM 없이도 데모가 동작하게."""
    t = text or ""
    out: dict = {"age": None, "household": None, "income": None}

    m = re.search(r"(?:만\s*)?(\d{2})\s*(?:살|세)", t)
    if m:
        out["age"] = int(m.group(1))
    else:
        for tens, tv in _TENS.items():
            if tens in t:
                out["age"] = tv
                for ones, ov in _ONES.items():
                    if re.search(tens + r"\s*" + ones, t):
                        out["age"] = tv + ov
                        break
                break

    if re.search(r"혼자|독거|나 혼자|홀로", t):
        out["household"] = "single"
    elif re.search(r"부부|배우자|영감|할멈|같이 살|둘이 살", t):
        out["household"] = "couple"
    return out


def merge_slots(base: dict, new: dict) -> dict:
    """이미 아는 값은 유지, 새로 알게 된 값만 채움."""
    out = dict(base or {})
    for k in ("age", "household", "income"):
        if out.get(k) is None and new.get(k) is not None:
            out[k] = new[k]
    return out
