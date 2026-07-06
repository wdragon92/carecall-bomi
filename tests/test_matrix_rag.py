"""RAG 매트릭스 (RG·OF·BC 계열) — 게이트 수치표·소급 필터·카드 조립·패널 병합·
제안 수락/백채널 경로. 전부 목 임베더 + 결정적 입력."""
import asyncio
from collections import OrderedDict
from types import SimpleNamespace

import numpy as np
import pytest

from app.config import Settings
from app.core import conversation, welfare
from app.core.conversation import _accepts_offer, _is_backchannel
from app.core.prompts import chat_system
from app.rag import cards
from app.rag.answer import compose_card, pick_card, refresh_detail
from app.rag.cards import BOKJIRO_HOME
from app.rag.index import LoadedIndex, build_index
from app.rag.schema import DocChunk
from app.rag.search import (
    RagRuntime,
    Retrieval,
    _senior_only,
    augment_query,
    hybrid_retrieve,
    passes_gate,
)
from app.services.mock import MockEmbed


def _gate_settings() -> Settings:
    """게이트 수치표 검증용 — .env·환경변수에 흔들리지 않게 임계값을 명시 고정."""
    return Settings(
        _env_file=None,
        rag_score_threshold=0.47, rag_score_threshold_high=0.55,
        rag_score_threshold_mock=0.06, rag_score_threshold_mock_high=0.12,
        rag_bm25_evidence=12.0, rag_bm25_evidence_mock=4.0,
    )


# ---- RG-01: 2단 거부 게이트 수치표 — top≥high OR (top≥low AND bm25≥evidence) ----
def test_gate_threshold_table():
    s = _gate_settings()
    item = [(DocChunk(text="x", source="s"), 0.5)]

    for top, bm, want in [(0.121, 0.0, True), (0.07, 4.0, True),
                          (0.07, 3.9, False), (0.119, 3.9, False)]:
        assert passes_gate(Retrieval(item, top, bm), s, "mock") is want, ("mock", top, bm)

    for top, bm, want in [(0.551, 0.0, True), (0.471, 12.0, True),
                          (0.469, 99.0, False), (0.539, 11.9, False)]:
        assert passes_gate(Retrieval(item, top, bm), s, "real") is want, ("real", top, bm)

    assert passes_gate(Retrieval([], 0.9, 99.0), s, "mock") is False  # 결과 자체가 비면 거부


# ---- RG-08: 로드 시점 어르신 소급 가드 — 청크·임베딩 행 동기 제거 ----
def test_senior_guard_filters_chunks_and_embeddings_in_sync():
    youth = DocChunk(text="서비스명: 청년 월세 한시 특별지원", source="s",
                     fields={"서비스명": "청년 월세 한시 특별지원", "지원대상": "만 19세~34세 청년"})
    senior = DocChunk(text="서비스명: 기초연금", source="s",
                      fields={"서비스명": "기초연금", "지원대상": "만 65세 이상"})
    loaded = LoadedIndex(
        chunks=[youth, senior],
        embeddings=np.array([[1.0, 0.0], [0.0, 1.0]], dtype="float32"),
        meta={"embed_mode": "mock"}, hashes={},
    )
    out = _senior_only(loaded)
    assert [c.fields["서비스명"] for c in out.chunks] == ["기초연금"]
    assert out.embeddings.shape == (1, 2)
    assert out.embeddings.tolist() == [[0.0, 1.0]]  # 남은 청크의 벡터 행이 함께 따라옴


# ---- RG-10: 질의 보강 경계 — 28자 이하 + 후속 신호 어휘 + 이름 미포함일 때만 ----
# (상한 28: "근데 아까 그거 그래도 한번 알려줘 봐"=21자 같은 자연 대용어 문장 수용 — D5 실측)
def test_augment_query_boundaries():
    t28 = "그거 신청" + "요" * 23
    t29 = "그거 신청" + "요" * 24
    assert len(t28) == 28 and len(t29) == 29
    assert augment_query(t28, "기초연금").startswith("기초연금 ")
    assert augment_query(t29, "기초연금") == t29  # 29자 초과 → 미보강
    assert augment_query("근데 아까 그거 그래도 한번 알려줘 봐", "의료급여").startswith("의료급여 ")

    assert augment_query("기초연금 신청 어떻게 해요", "기초연금") == "기초연금 신청 어떻게 해요"  # 이름 포함
    assert augment_query("알려줘", "기초연금") == "알려줘"  # '알려줘' 단독은 새 주제일 수 있어 미보강
    assert augment_query("자세히 알려줘", "기초연금") == "기초연금 자세히 알려줘"


# ---- RG-12: strict(실 LLM) 모드에서 무언급이면 카드 생략 ----
def test_pick_card_strict_requires_mention():
    retrieved = [(DocChunk(text="", source="", fields={"서비스명": "의료급여"}), 0.9)]
    assert pick_card(retrieved, "식사는 잘 챙겨 드시고 계세요?", strict=True) is None
    assert pick_card(retrieved, "의료급여를 살펴보세요.", strict=True) is not None  # 언급 시엔 유지


# ---- RG-13: 구조화 필드 없는 청크만 있으면 카드 불가 ----
def test_pick_card_no_structured_fields():
    retrieved = [(DocChunk(text="PDF 원문 조각", source="", fields=None), 0.9)]
    assert pick_card(retrieved, "아무 답변", strict=False) is None


# ---- RG-14: 카드 조립 — 문의처 폴백·live 접미·복지로 링크 마지막 줄·고정 TTS ----
def test_compose_card_fallbacks_and_layout():
    chunk = DocChunk(text="", source="복지자료 2026·기초연금", source_type="fixture",
                     serv_id="fixture-x", url="",  # 딥링크 없음 → 복지로 홈 폴백
                     fields={"서비스명": "기초연금", "지원대상": "만 65세 이상"},
                     collected_at="2026-07-01")
    text, tts = compose_card(chunk, chunk.fields, live=True)
    lines = text.split("\n")
    assert lines[0] == "📌 기초연금"
    assert "· 문의: 보건복지상담센터 129" in lines  # 문의처 없음 → 129 폴백
    assert "· 정보 기준일: 2026-07-01 · 방금 확인" in lines  # live 접미
    assert lines[-1] == f"· 복지로: {BOKJIRO_HOME}"  # 링크는 항상 마지막 줄
    assert tts == "기초연금의 지원 내용과 신청 방법은 화면에 정보 카드로 정리해 드렸어요."

    text2, _ = compose_card(chunk, chunk.fields, live=False)
    assert "방금 확인" not in text2


# ---- RG-15: 픽스처 카드는 실시간 상세조회 없이 수집본 그대로 ----
async def test_refresh_detail_fixture_uses_cache():
    chunk = DocChunk(text="", source="s", source_type="fixture", serv_id="fixture-x",
                     fields={"서비스명": "기초연금"})
    fields, live = await refresh_detail(None, chunk)  # settings 미사용 경로(네트워크 미접근)
    assert fields == {"서비스명": "기초연금"} and live is False
    assert fields is not chunk.fields  # 원본 오염 방지용 복사본


# ---- 목 인덱스 공유 (RG-16/17) ----
@pytest.fixture(scope="module")
def mock_rt():
    embed = MockEmbed()
    loaded, _ = asyncio.run(build_index(cards.fixture_cards(), embed.embed, None, "mock", sleep_s=0))
    return RagRuntime.from_loaded(loaded), embed


# ---- RG-16: 항목별 벡터 하한(min_vec)이 전부 걸러내면 결과 빈 + 게이트 거부 ----
def test_item_threshold_filters_all_and_gate_rejects(mock_rt):
    rt, embed = mock_rt
    q = "치매 약값이 걱정이에요"
    qv = asyncio.run(embed.embed([q]))[0]
    r = hybrid_retrieve(rt, qv, q, k=4, min_vec=0.99)
    assert r.items == []
    assert r.top_score > 0.0  # 게이트 신호는 살아 있으나
    assert passes_gate(r, _gate_settings(), "mock") is False  # 항목이 없으면 무조건 거부


# ---- RG-17: 코퍼스와 토큰 교집합이 없는 질의는 BM25 증거 0 ----
def test_alien_tokens_zero_bm25(mock_rt):
    rt, embed = mock_rt
    q = "zzz9 xqx7 wvw"
    qv = asyncio.run(embed.embed([q]))[0]
    r = hybrid_retrieve(rt, qv, q, k=4)
    assert r.bm25_top == 0.0


# ---- RG-18: 임베딩 장애 → 수다 경로로 조용히 폴백 (칩 소음 없음) ----
class _RecSess(SimpleNamespace):
    async def send(self, payload):
        self.sent.append(payload)
        return True


async def test_rag_lookup_embed_failure_falls_back_quietly():
    class _BoomEmbed:
        async def embed(self, texts):
            raise RuntimeError("embed down")

    sess = _RecSess(sent=[], messages=[], last_rag=None)
    providers = SimpleNamespace(rag=object(), embed=_BoomEmbed(), modes={"embed": "mock"})
    settings = SimpleNamespace(rag_enabled=True)

    out = await conversation._rag_lookup(sess, providers, settings, "치매 약값이 걱정이에요")
    assert out is None
    # 칩은 근거 발견(found) 시에만 — 실패·미달 턴엔 검색 UI 소음을 내지 않는다
    assert all(m.get("type") != "rag_status" for m in sess.sent)


# ---- RG-21: 복지 패널 병합 — RAG 카드 우선 + 이름 dedupe + 4개 절단 ----
async def test_push_welfare_merges_rag_first_dedup_and_caps():
    rag_card = {"id": "fixture-basic-pension", "이름": "기초연금",
                "한줄": "만 65세 이상 연금", "신청처": "주민센터", "기준일": "2026-07-01",
                "url": BOKJIRO_HOME}
    sess = _RecSess(
        sent=[],
        welfare_cards=OrderedDict([("fixture-basic-pension", rag_card)]),
        welfare_matched=["basic-pension", "care-service", "medical-aid",
                         "housing-benefit", "energy-voucher"],
    )
    await welfare.push_welfare(sess)

    assert len(sess.sent) == 1 and sess.sent[0]["type"] == "welfare_update"
    items = sess.sent[0]["items"]
    names = [it["이름"] for it in items]
    assert items[0]["id"] == "fixture-basic-pension"  # RAG 카드(근거 보유)가 맨 앞
    assert names.count("기초연금") == 1  # 정적 basic-pension은 이름 중복으로 제외
    assert len(items) == 4  # 패널 절단
    assert "에너지바우처" not in names  # 5번째 후보는 잘림


# ---- RG-22: 정적 복지 매칭은 키워드 직접 일치가 필수 ----
def test_welfare_match_requires_keyword_hit():
    assert welfare.match([], "오늘 날씨가 좋네요") == []
    assert welfare.match(["저소득"], "별 얘기 없었어요") == []  # 신호만으로는 미노출

    got = welfare.match([], "휴대폰 요금이 너무 나와")
    assert got and got[0]["id"] == "telecom-discount"  # '휴대폰'+'요금' 직접 일치


# ---- OF-03: 제안 거절 배터리 ----
def test_offer_decline_battery():
    for utter in ["아니", "됐어", "나중에", "괜찮아, 말고"]:
        assert not _accepts_offer(utter), utter


# ---- OF-04: 수락 인정 길이 경계 (정규화 10자) ----
def test_offer_accept_length_boundary():
    t10 = "궁금해" + "요" * 7
    t11 = "궁금해" + "요" * 8
    assert len(t10) == 10 and len(t11) == 11
    assert _accepts_offer(t10)  # 정보요청 어휘('궁금') + 10자 이하
    assert not _accepts_offer(t11)  # 11자 — 긴 발화는 새 화제로 본다


# ---- OF-05: 거절 어휘가 있으면 정보요청 어휘보다 우선 ----
def test_decline_beats_info_request():
    assert not _accepts_offer("아니 알려줘")


# ---- OF-06 [mock-e2e]: 직전 제안이 없는 "응"은 RAG 미시도 ----
def _next_ai_turn_seen(ws, tries: int = 25):
    seen = []
    for _ in range(tries):
        m = ws.receive_json()
        seen.append(m)
        if m["type"] == "ai_turn":
            return m["bubbles"], seen
    raise AssertionError("ai_turn not received")


def test_assent_without_offer_skips_rag(rag_client):
    sid = rag_client.post("/api/sessions").json()["session_id"]
    with rag_client.websocket_connect(f"/ws/{sid}") as ws:
        assert ws.receive_json()["type"] == "session_ready"
        _next_ai_turn_seen(ws)  # 선인사 (제안 문형 아님)

        ws.send_json({"type": "user_message", "text": "응"})
        _, seen = _next_ai_turn_seen(ws)
        assert all(m.get("type") != "rag_status" for m in seen)  # 검색 자체를 안 탐


# ---- BC-01: 백채널 판정 + [mock-e2e] rag_status 미전송 ----
def test_backchannel_variants():
    for utter in ["그러게", "네~", "알겠어요", "고마워"]:
        assert _is_backchannel(utter), utter


def test_backchannel_turn_skips_rag(rag_client):
    sid = rag_client.post("/api/sessions").json()["session_id"]
    with rag_client.websocket_connect(f"/ws/{sid}") as ws:
        assert ws.receive_json()["type"] == "session_ready"
        _next_ai_turn_seen(ws)  # 선인사

        ws.send_json({"type": "user_message", "text": "그러게"})
        _, seen = _next_ai_turn_seen(ws)
        assert all(m.get("type") != "rag_status" for m in seen)


# ---- BC-03: 문장부호·공백 정규화 후 백채널 판정 ----
def test_backchannel_normalization():
    for utter in ["네!!", "그래~", "응 응", "괜찮아요…"]:
        assert _is_backchannel(utter), utter
    assert not _is_backchannel("네 그게 말이죠 사실은")  # 정규화해도 5자 초과


# ---- BC-04: 백채널 턴 시스템 프롬프트 — 메모·무자료·백채널 블록 동시 포함 ----
def test_chat_system_backchannel_blocks_coexist():
    s = chat_system("", memo="- 관찰됨(건강): 무릎 통증 언급", backchannel=True, rag=False)
    assert "[어르신 상황 메모" in s
    assert "이번 턴에 검색된 자료 없음" in s
    assert "[방금 상황]" in s and "짧게 호응만" in s
