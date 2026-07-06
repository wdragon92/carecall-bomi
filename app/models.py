"""Pydantic 모델 (models §2). 추출 결과는 LLM이 한글 키로 내므로 alias로 흡수하고,
프론트로는 영문 속성명으로 직렬화한다."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["user", "assistant", "system"]
Via = Literal["text", "voice", "system"]
Category = Literal["건강", "정서", "인지", "사기_노출", "복지_니즈", "긴급"]
Severity = Literal["낮음", "보통", "높음"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Message(BaseModel):
    id: str
    role: Role
    text: str
    ts: datetime = Field(default_factory=_utcnow)
    via: Via = "text"
    tts_text: Optional[str] = None  # 있으면 TTS는 text 대신 이 문장을 읽음(정보 카드용)


class Finding(BaseModel):
    """특이사항. 입력(LLM)은 한글 키(alias), 코드/프론트는 영문 속성."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = ""
    category: Category = Field(alias="카테고리")
    content: str = Field(alias="내용")
    severity: Severity = Field(default="낮음", alias="심각도")
    needs_human: bool = Field(default=False, alias="사람_개입_필요")


class WelfareItem(BaseModel):
    id: str
    이름: str
    한줄: str
    대상: str = ""
    조건: str = ""
    금액: str = ""
    신청처: str = ""
    링크: str = ""  # 복지로 상세 URL (수기 검증본)
    signals: list[str] = Field(default_factory=list)
    키워드: list[str] = Field(default_factory=list)


class Report(BaseModel):
    summary: str
    findings: list[Finding] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    welfare: list[dict] = Field(default_factory=list)
    apply_packages: list[dict] = Field(default_factory=list)  # 안내한 신청 준비물(§4-7)
    disclaimer: str = "본 내용은 참고용이며 의학적 진단이 아닙니다. 정확한 상담은 전문가에게 문의하세요."
