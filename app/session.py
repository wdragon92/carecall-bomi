"""세션 모델 + 인메모리 스토어 (session §5). 세션별 완전 격리, DB 없음, TTL 폐기."""
from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from collections import OrderedDict
from datetime import datetime, timezone

from app.models import Finding, Message


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def finding_id(category: str, content: str) -> str:
    # 전체 content 해시 — content[:20]만 쓰면 앞 20자가 같은 '서로 다른/에스컬레이션된
    # 관찰'이 같은 id로 접혀 하나가 유실된다. 동일 content는 여전히 같은 id →
    # 안전망·LLM 교차 dedupe(extraction._merge, EX-01)는 그대로 유지.
    return hashlib.sha1(f"{category}|{content}".encode("utf-8")).hexdigest()[:8]


class Session:
    def __init__(self, sid: str) -> None:
        self.id = sid
        self.created_at = _utcnow()
        self.last_active = time.monotonic()
        self.messages: list[Message] = []
        self.findings: list[Finding] = []
        self.welfare_matched: list[str] = []
        self.welfare_cards: "OrderedDict[str, dict]" = OrderedDict()  # RAG로 안내한 복지 (패널·리포트 병합용)
        self.last_rag: dict | None = None  # 직전 RAG 매칭 {"서비스명", "serv_id"} — 후속 질문 보강
        self.slots: dict = {}  # 판정용 슬롯(나이·가구형태 등) + _pending 카운터
        self.apply_packages: dict[str, dict] = {}  # 안내한 신청 패키지 (리포트 첨부)
        self.ocr_texts: list[str] = []
        self.tts_cache: "OrderedDict[str, bytes]" = OrderedDict()
        self.extract_lock = asyncio.Lock()
        self.extract_dirty = False
        self.last_alert: tuple | None = None  # 직전 긴급 배너(수위, 문구) — 동일 경보 재전송 억제
        self.crisis_hold: tuple[str, int] | None = None  # (수위, 남은 턴) — 위기 후속 턴 지침 유지
        self.ws = None  # 활성 WebSocket (있으면)
        self.send_lock = asyncio.Lock()  # WS 동시 전송 직렬화
        self.bg_tasks: set = set()  # 백그라운드 태스크(추출 등) 참조 유지
        self._mcount = 0

    def touch(self) -> None:
        self.last_active = time.monotonic()

    async def send(self, payload: dict) -> bool:
        """활성 WS로 안전하게 전송(직렬화). 실패해도 세션은 죽지 않음."""
        ws = self.ws
        if ws is None:
            return False
        async with self.send_lock:
            try:
                await ws.send_json(payload)
                return True
            except Exception:
                return False

    def spawn(self, coro):
        """세션 수명 동안 유지되는 백그라운드 태스크 생성."""
        task = asyncio.create_task(coro)
        self.bg_tasks.add(task)
        task.add_done_callback(self.bg_tasks.discard)
        return task

    def add_message(
        self, role: str, text: str, via: str = "text", id: str | None = None,
        tts_text: str | None = None, kind: str = "text",
    ) -> Message:
        self._mcount += 1
        mid = id or f"m{self._mcount}-{secrets.token_hex(3)}"
        msg = Message(id=mid, role=role, text=text, via=via, tts_text=tts_text, kind=kind)
        self.messages.append(msg)
        self.touch()
        return msg

    def cache_tts(self, message_id: str, audio: bytes, cap: int = 20) -> None:
        self.tts_cache[message_id] = audio
        self.tts_cache.move_to_end(message_id)
        while len(self.tts_cache) > cap:
            self.tts_cache.popitem(last=False)

    def history_for_llm(self, limit: int = 40) -> list[dict]:
        """카드 말풍선은 요약 한 줄로 대체 — 카드 속 금액·기준을 LLM이 히스토리에서 주워
        말로 반복하는 것(T2 위반)을 차단하되, '무엇을 안내했는지'는 기억하게 한다."""
        msgs = [m for m in self.messages if m.role in ("user", "assistant")][-limit:]
        out = []
        for m in msgs:
            text = m.text
            if m.kind == "card":
                title = m.text.split("\n", 1)[0].lstrip("📌📝 ").strip()
                text = f"(화면 정보 카드로 '{title}'을 보여드렸음 — 수치·신청처는 카드에 있으니 말로 반복하지 않기)"
            out.append({"role": m.role, "content": text})
        return out

    def transcript_text(self, max_chars: int = 6000) -> str:
        """전체 대화(사용자+상담원). 리포트 생성용."""
        lines = []
        for m in self.messages:
            if m.role == "system":
                continue
            who = "어르신" if m.role == "user" else "상담원"
            lines.append(f"{who}: {m.text}")
        for t in self.ocr_texts:
            lines.append(f"[첨부문서]: {t}")
        text = "\n".join(lines)
        return text[-max_chars:]

    def user_transcript(self, max_chars: int = 6000) -> str:
        """어르신 발화 + 첨부문서만. 특이사항 추출용 (AI 발화/인사에서 오탐 방지)."""
        lines = [f"어르신: {m.text}" for m in self.messages if m.role == "user"]
        for t in self.ocr_texts:
            lines.append(f"[첨부문서]: {t}")
        return "\n".join(lines)[-max_chars:]


class SessionStore:
    def __init__(self, ttl_min: int = 120, max_sessions: int = 200) -> None:
        self._sessions: "OrderedDict[str, Session]" = OrderedDict()
        self._ttl = ttl_min * 60
        self._max = max_sessions
        self._lock = asyncio.Lock()

    async def create(self) -> Session:
        async with self._lock:
            sid = secrets.token_urlsafe(16)
            sess = Session(sid)
            self._sessions[sid] = sess
            self._sessions.move_to_end(sid)
            # 용량 초과 시 가장 오래 활동 없는 세션 축출
            while len(self._sessions) > self._max:
                self._sessions.popitem(last=False)
            return sess

    def get(self, sid: str) -> Session | None:
        sess = self._sessions.get(sid)
        if sess is not None:
            sess.touch()
            self._sessions.move_to_end(sid)
        return sess

    def bump(self, sid: str) -> None:
        """활동(대화 턴) 시 LRU 순서 갱신 — WS 대화는 sess 참조를 오래 붙들고 store.get()을
        다시 부르지 않아, 대화 중 활성 세션이 create()의 용량 축출(popitem) 대상이 되던
        문제를 막는다. get()과 같은 락-프리 move_to_end 패턴."""
        sess = self._sessions.get(sid)
        if sess is not None:
            sess.touch()
            self._sessions.move_to_end(sid)

    async def drop(self, sid: str) -> None:
        async with self._lock:
            self._sessions.pop(sid, None)

    async def sweep(self) -> int:
        now = time.monotonic()
        async with self._lock:
            expired = [sid for sid, s in self._sessions.items() if now - s.last_active > self._ttl]
            for sid in expired:
                self._sessions.pop(sid, None)
        return len(expired)

    def count(self) -> int:
        return len(self._sessions)
