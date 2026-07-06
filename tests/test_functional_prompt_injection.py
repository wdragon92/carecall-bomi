"""[프롬프트 주입 적재적소] 계약 4~7 — LLM에 '실제로 주입된' 시스템 프롬프트/히스토리 검증.

기법: providers.llm(=mock 모드에선 mllm 동일 객체)을 SpyLLM으로 교체해
chat()에 들어간 messages를 그대로 캡처한다. 멘트 파싱이 아니라 배선 검증.

계약 4: 추출이 findings를 만든 '다음 턴'의 시스템 프롬프트에만 [어르신 상황 메모]
        블록 + 해당 관찰 내용이 실린다 (첫 턴, 추출 전엔 없음).
계약 5: 위험 발화의 '같은 턴' 프롬프트에 [방금 발화의 위험신호] 블록.
        무해 발화·백채널 턴엔 없음 (백채널엔 [방금 상황] 블록).
계약 6: 접지 턴엔 [복지 자료]+검색 원문, 비접지 턴엔 '검색된 자료 없음' 문구 —
        두 경우 모두 [연결처] 고정 번호 줄은 항상 존재.
계약 7: 카드가 나간 다음 턴의 히스토리에서 카드 본문(📌 원문)이
        '(화면 정보 카드로 …' 요약으로 대체된다 (T2 수치 재발화 차단).
"""
from test_functional_helpers import (
    ALIEN_Q,
    GROUNDING_Q,
    drain_until,
    handshake,
    install_llm_spy,
    rag_statuses,
    user_turn,
)

from app.core import prompts


# ---- 계약 4(개정): 메모 블록은 항상 존재하되(안내 이력 '없음' 앵커 — 과거 날조 방지),
# 추출 산출(관찰 내용)은 '이후 턴'부터 실린다 ----
def test_memo_block_appears_only_after_extraction(rag_client):
    spy = install_llm_spy(rag_client)
    sid = rag_client.post("/api/sessions").json()["session_id"]
    with rag_client.websocket_connect(f"/ws/{sid}") as ws:
        handshake(ws)

        # 첫 턴 — 추출 전: 관찰 내용은 없고 '안내한 복지 없음' 앵커만 실린다
        user_turn(ws, "요즘 잠을 통 못 자요")
        sys1 = spy.chat_system()
        assert "안내해 드린 복지: 없음" in sys1
        assert "관찰됨(" not in sys1

        # 턴 끝 추출이 findings를 만들 때까지 대기 (수면 관찰이 세션에 적재됨)
        drain_until(
            ws,
            lambda m: m.get("type") == "findings_update"
            and any("수면" in f["content"] for f in m["findings"]),
        )

        # 다음 턴 — 같은 관찰 내용이 메모 블록으로 실제 주입된다
        user_turn(ws, "그냥 그런 하루였어")
        sys2 = spy.chat_system()
        assert "[어르신 상황 메모" in sys2
        assert "수면의 어려움을 언급함" in sys2  # HCX-007 산출이 HCX-005 컨텍스트로 환류
        assert "관찰됨(건강)" in sys2


# ---- 계약 5: [방금 발화의 위험신호]는 위험 발화의 같은 턴에만 ----
def test_danger_signal_block_same_turn_only(rag_client):
    spy = install_llm_spy(rag_client)
    sid = rag_client.post("/api/sessions").json()["session_id"]
    with rag_client.websocket_connect(f"/ws/{sid}") as ws:
        handshake(ws)

        # 무해 발화 → 신호 블록 없음
        user_turn(ws, "오늘 날씨가 참 좋네요")
        s1 = spy.chat_system()
        assert "[방금 발화의 위험신호" not in s1

        # 위험 발화(어지럼) → 같은 턴 프롬프트에 결정적 안전망 신호가 주입됨
        user_turn(ws, "어지럽고 핑 돌아")
        s2 = spy.chat_system()
        assert "[방금 발화의 위험신호" in s2
        assert "어지럼" in s2  # safety.scan이 잡은 내용 문자열 그대로

        # 백채널 턴 → 신호 스캔 자체를 건너뛰고 [방금 상황] 블록만
        user_turn(ws, "그러게")
        s3 = spy.chat_system()
        assert "[방금 발화의 위험신호" not in s3
        assert "[방금 상황]" in s3 and "짧게" in s3


# ---- 계약 6: [복지 자료] 접지/비접지 분기 + [연결처]는 상시 ----
def test_welfare_block_grounded_vs_ungrounded_contacts_always(rag_client):
    spy = install_llm_spy(rag_client)
    sid = rag_client.post("/api/sessions").json()["session_id"]
    with rag_client.websocket_connect(f"/ws/{sid}") as ws:
        handshake(ws)

        # 접지 턴: [복지 자료] 블록 + 검색된 원문(출처 헤더·구조화 텍스트)이 실림
        _, seen = user_turn(ws, GROUNDING_Q)
        s1 = spy.chat_system()
        assert "[복지 자료" in s1
        assert "반드시 아래 내용에만 근거해 안내" in s1
        assert "서비스명:" in s1  # 검색 청크 원문
        src = rag_statuses(seen)[0]["sources"][0]
        assert src in s1  # rag_status로 알린 출처가 실제 프롬프트 블록의 출처와 일치

        # 비접지 턴: '검색된 자료 없음' 명시 (수치 생성 차단 지침)
        user_turn(ws, ALIEN_Q)
        s2 = spy.chat_system()
        assert "이번 턴에 검색된 자료 없음" in s2
        assert "반드시 아래 내용에만 근거해 안내" not in s2

        # 두 경우 모두 연결처 고정 번호 줄은 항상 존재 (LLM 번호 생성 금지 가드)
        for s in (s1, s2):
            assert "[연결처" in s
            assert prompts.CONTACTS_LINE in s


# ---- 계약 7: 카드 뒤 히스토리 격리 — 📌 원문 대신 요약 한 줄 ----
def test_card_body_isolated_from_next_turn_history(rag_client):
    spy = install_llm_spy(rag_client)
    sid = rag_client.post("/api/sessions").json()["session_id"]
    with rag_client.websocket_connect(f"/ws/{sid}") as ws:
        handshake(ws)

        bubbles, _ = user_turn(ws, GROUNDING_Q)
        card = bubbles[-1]
        assert card.get("kind") == "card" and card["text"].startswith("📌")
        title = card["card"]["title"]

        user_turn(ws, "알겠습니다 고맙네요")
        msgs = spy.chat_calls[-1]  # 이 턴에 LLM으로 주입된 messages 전체

        # 카드 본문(📌 원문·연락처 줄)은 어디에도 없다
        assert all("📌" not in m["content"] for m in msgs)
        assert all("· 문의:" not in m["content"] for m in msgs)

        # 대신 '(화면 정보 카드로 …' 요약이 assistant 히스토리로 들어간다
        summaries = [
            m["content"] for m in msgs
            if m["role"] == "assistant" and "(화면 정보 카드로" in m["content"]
        ]
        assert len(summaries) == 1
        assert title in summaries[0]  # 무엇을 안내했는지는 기억
        assert "말로 반복하지 않기" in summaries[0]  # 수치 재발화 차단 지침 포함
