"""RAG 공통 스키마 + 변경감지 해시 (RAG v2 §2, §4-1).
피클에는 DocChunk 인스턴스가 아닌 dict를 저장한다(모듈 경로 결합 방지)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass


@dataclass
class DocChunk:
    text: str                 # 임베딩 대상 텍스트 (복지카드 또는 PDF 청크)
    source: str               # "복지자료 2026·기초연금" | "긴급복지안내 p12" | "복지로/중앙부처 WLF-000123"
    source_type: str = "api"  # "api" | "pdf" | "fixture"
    serv_id: str = ""         # 상세조회·변경감지 키 (api serv id / "fixture-{id}")
    url: str = ""             # 복지로 상세/신청 딥링크
    fields: dict | None = None  # 구조화 필드 원본 — T2 응답의 슬롯 소스 (api/fixture만)
    collected_at: str = ""    # 수집일 YYYY-MM-DD ("정보 기준일" 표시용)


def card_hash(svc: dict) -> str:
    """서비스 원본 dict의 변경감지 해시 — 바뀐 카드만 재임베딩 (v2 §4-1)."""
    return hashlib.md5(json.dumps(svc, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def text_hash(text: str) -> str:
    """임베딩 재사용 키 — 카드 텍스트가 같으면 기존 벡터를 그대로 쓴다."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def chunk_to_dict(c: DocChunk) -> dict:
    return asdict(c)


def chunk_from_dict(d: dict) -> DocChunk:
    known = set(DocChunk.__dataclass_fields__)
    return DocChunk(**{k: v for k, v in d.items() if k in known})
