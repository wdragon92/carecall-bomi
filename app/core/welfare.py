"""복지 지식 로딩 + 매칭 (welfare §10). welfare.json이 없으면 우아하게 빈 값 반환
(stage 6에서 welfare.json 작성 시 자동 활성화)."""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

from app.models import WelfareItem

log = logging.getLogger("welfare")
_PATH = Path(__file__).resolve().parent.parent.parent / "knowledge" / "welfare.json"


@lru_cache
def load_items() -> list[WelfareItem]:
    if not _PATH.exists():
        return []
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
        return [WelfareItem(**it) for it in data.get("items", [])]
    except Exception as exc:
        log.warning("welfare.json load failed: %s", exc)
        return []


def get_digest(max_items: int = 12) -> str:
    items = load_items()
    if not items:
        return ""
    lines = [f"- {it.이름}: {it.한줄} (대상 {it.대상}; 금액 {it.금액}; 신청 {it.신청처})" for it in items[:max_items]]
    return "\n".join(lines)


def by_ids(ids: list[str]) -> list[dict]:
    index = {it.id: it for it in load_items()}
    out: list[dict] = []
    for i in ids:
        it = index.get(i)
        if it:
            out.append({"id": it.id, "이름": it.이름, "한줄": it.한줄, "신청처": it.신청처})
    return out


def match(signals: list[str], text: str) -> list[dict]:
    """추출 신호 + 사용자 발화 키워드로 복지 항목 매칭. LLM 없이 동작하는 결정적 규칙."""
    items = load_items()
    if not items:
        return []
    sigset = {s for s in signals}
    matched: list[dict] = []
    for it in items:
        hit_signal = bool(sigset & set(it.signals))
        hit_kw = any(kw and kw in text for kw in it.키워드)
        if hit_signal or hit_kw:
            matched.append({"id": it.id, "이름": it.이름, "한줄": it.한줄, "신청처": it.신청처})
    return matched
