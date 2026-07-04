"""키 없이도 데모가 '살아있게' 하는 mock provider 4종 (services §7.3).
결정적(deterministic) 규칙 기반이라 시나리오 데모/테스트에 사용."""
from __future__ import annotations

import asyncio
import io
import math
import struct
import wave
from typing import AsyncIterator


def _last_user(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return m.get("content", "")
    return ""


def _all_user_text(messages: list[dict]) -> str:
    # 사용자 발화만 스캔 (system=지시 프롬프트라 키워드 오탐 유발)
    parts = [m.get("content", "") for m in messages if m.get("role") == "user"]
    return " ".join(parts)


# 키워드 → 공감 응답 (존댓말·짧은 문장·질문 하나)
_CHAT_RULES: list[tuple[tuple[str, ...], str]] = [
    (("잠", "수면", "못 자", "불면"),
     "밤에 편히 못 주무시니 많이 힘드시겠어요. 잠자리에 드시는 시간은 대체로 일정하신가요?"),
    (("아파", "아프", "통증", "결려", "쑤"),
     "어디가 편찮으시다니 걱정이 됩니다. 그 증상이 언제부터 있으셨어요?"),
    (("낙상", "넘어", "미끄러"),
     "넘어지셨다니 놀라셨겠어요. 다치신 곳은 없는지 살펴봐야겠어요. 지금 걷는 건 괜찮으세요?"),
    (("외로", "혼자", "쓸쓸", "우울", "적적"),
     "혼자 계시면 적적하고 마음이 가라앉을 때가 있으시죠. 오늘은 어떤 하루를 보내셨어요?"),
    (("돈", "생활비", "형편", "힘들어", "부담"),
     "생활이 빠듯하시면 마음도 무거우시죠. 혹시 받고 계신 복지 혜택이 있으신가요?"),
    (("전화", "문자", "보이스피싱", "계좌", "송금", "이체", "링크"),
     "그런 연락은 조심하셔야 해요. 절대 계좌번호나 비밀번호를 알려주지 마세요. 어떤 내용이었는지 말씀해 주시겠어요?"),
    (("밥", "식사", "드셨", "끼니"),
     "끼니 거르지 않고 챙겨 드시는 게 제일 중요해요. 오늘은 뭘 드셨어요?"),
    (("약", "복용", "혈압", "당뇨"),
     "약은 시간 맞춰 잘 챙겨 드시는 게 중요해요. 요즘 거르지 않고 드시고 계세요?"),
]

_CHAT_DEFAULTS = [
    "네, 말씀 잘 들었어요. 요즘 몸은 좀 어떠세요?",
    "그러셨군요. 오늘 하루는 어떻게 보내고 계세요?",
    "말씀해 주셔서 고맙습니다. 더 하고 싶으신 이야기가 있으세요?",
    "그랬군요. 식사는 잘 챙겨 드시고 계신가요?",
]


class MockLLM:
    def __init__(self, settings=None) -> None:
        self._turn = 0

    def _reply(self, messages: list[dict]) -> str:
        text = _last_user(messages)
        for keys, resp in _CHAT_RULES:
            if any(k in text for k in keys):
                return resp
        self._turn += 1
        return _CHAT_DEFAULTS[self._turn % len(_CHAT_DEFAULTS)]

    async def chat_stream(self, messages: list[dict], **opts) -> AsyncIterator[str]:
        reply = self._reply(messages)
        for token in reply.split(" "):
            await asyncio.sleep(0.02)
            yield token + " "

    async def chat(self, messages: list[dict], **opts) -> str:
        return self._reply(messages)

    async def extract_json(self, messages: list[dict], schema: dict) -> dict:
        text = _all_user_text(messages)
        findings: list[dict] = []
        signals: set[str] = set()

        def add(cat: str, content: str, sev: str, human: bool = False) -> None:
            findings.append({"카테고리": cat, "내용": content, "심각도": sev, "사람_개입_필요": human})

        if any(k in text for k in ("죽고", "죽었으면", "살기 싫", "자해", "죽어")):
            add("긴급", "삶에 대한 부정적 표현이 관찰됨", "높음", True)
        if any(k in text for k in ("잠", "불면", "못 자")):
            add("건강", "수면의 어려움을 언급함", "보통")
        if any(k in text for k in ("아파", "아프", "통증", "결려")):
            add("건강", "신체적 통증/불편을 언급함", "보통")
        if any(k in text for k in ("낙상", "넘어", "미끄러")):
            add("건강", "낙상/넘어짐 관련 언급이 있음", "높음", True)
        if any(k in text for k in ("외로", "혼자", "쓸쓸", "우울", "적적")):
            sev = "높음" if "우울" in text else "보통"
            add("정서", "외로움/우울감으로 보이는 정서 표현이 관찰됨", sev, sev == "높음")
            signals.update(["독거"])
        if any(k in text for k in ("보이스피싱", "문자", "계좌", "송금", "이체", "링크")):
            add("사기_노출", "의심스러운 연락/금전 요구 정황이 언급됨", "높음", True)
        if any(k in text for k in ("돈", "생활비", "형편", "부담", "힘들")):
            add("복지_니즈", "경제적 어려움 신호가 관찰됨", "보통")
            signals.update(["저소득"])

        return {
            "findings": findings,
            "welfare_signals": sorted(signals),
            "matched_welfare_ids": [],
        }


_STT_SAMPLES = [
    "요즘 밤에 잠을 통 못 자요.",
    "혼자 지내다 보니 좀 외롭네요.",
    "어제 모르는 번호로 문자가 왔는데 좀 이상했어요.",
    "생활비가 빠듯해서 걱정이에요.",
    "무릎이 아파서 병원에 가야 하나 싶어요.",
]


class MockSTT:
    def __init__(self, settings=None) -> None:
        self._i = 0

    async def transcribe(self, wav_bytes: bytes) -> str:
        s = _STT_SAMPLES[self._i % len(_STT_SAMPLES)]
        self._i += 1
        return s


class MockTTS:
    """실제 음성 대신 0.5초 차임 톤(WAV)을 생성해 재생 경로를 검증."""

    def __init__(self, settings=None) -> None:
        pass

    async def synthesize(self, text: str) -> bytes:
        rate, dur, freq = 16000, 0.5, 660.0
        n = int(rate * dur)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            frames = bytearray()
            for i in range(n):
                env = min(1.0, i / (rate * 0.05), (n - i) / (rate * 0.05))
                val = int(0.3 * env * 32767 * math.sin(2 * math.pi * freq * i / rate))
                frames += struct.pack("<h", val)
            w.writeframes(bytes(frames))
        return buf.getvalue()


_OCR_BILL = (
    "한국전력공사 전기요금 청구서\n"
    "고객번호: 0123-4567\n청구월: 2026년 6월\n"
    "청구금액: 38,200원\n납기일: 2026-07-25\n"
    "문의: 국번없이 123"
)
_OCR_SMS = (
    "[Web발신] 고객님 택배가 주소불명으로 반송되었습니다.\n"
    "아래 링크를 눌러 주소를 확인해 주세요. http://bit.ly/xxə9\n"
    "미확인 시 자동 폐기됩니다."
)


class MockOCR:
    def __init__(self, settings=None) -> None:
        pass

    async def extract_text(self, image_bytes: bytes, fmt: str, name: str = "") -> str:
        low = name.lower()
        if any(k in low for k in ("문자", "sms", "smish", "link", "택배")):
            return _OCR_SMS
        return _OCR_BILL
