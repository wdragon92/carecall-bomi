"""RAG 파이프라인 단위 테스트 — 전부 목 임베더, 네트워크·실 키 불필요 (tests §12)."""
import asyncio
import types

from app.rag import cards
from app.rag.index import build_index, load_index, save_index
from app.rag.search import RagRuntime, augment_query, hybrid_retrieve, load_runtime
from app.services.mock import MockEmbed


def test_fixture_cards():
    cs = cards.fixture_cards()
    assert len(cs) == 12
    care = next(c for c in cs if c.serv_id == "fixture-care-service")
    assert care.fields["서비스명"] == "노인맞춤돌봄서비스"
    assert "장보기" in care.text  # 데모 킬러 쿼리 어휘 보강 유지
    assert care.collected_at  # 기준일 표시용


def test_build_save_load_incremental(tmp_path):
    embed = MockEmbed()
    calls = {"n": 0}

    async def counting(texts):
        calls["n"] += len(texts)
        return await embed.embed(texts)

    cs = cards.fixture_cards()
    loaded, st = asyncio.run(build_index(cs, counting, None, "mock", sleep_s=0))
    assert st == {"embedded": 12, "reused": 0, "deleted": 0}
    assert loaded.meta["dim"] == MockEmbed.DIM
    save_index(loaded, tmp_path, st)
    assert (tmp_path / "welfare.pkl").exists() and (tmp_path / "hash.json").exists()

    prev = load_index(tmp_path)
    assert prev is not None and len(prev.chunks) == 12
    assert prev.meta["embed_mode"] == "mock"

    # 증분: 카드 1건 변경 → 그 1건만 임베딩, 옛 텍스트 1건은 삭제 처리
    calls["n"] = 0
    cs2 = cards.fixture_cards()
    cs2[0].text += "\n(개정)"
    _, st2 = asyncio.run(build_index(cs2, counting, prev, "mock", sleep_s=0))
    assert calls["n"] == 1
    assert st2 == {"embedded": 1, "reused": 11, "deleted": 1}


def test_hybrid_retrieve_ranks_and_scores():
    embed = MockEmbed()
    cs = cards.fixture_cards()
    loaded, _ = asyncio.run(build_index(cs, embed.embed, None, "mock", sleep_s=0))
    rt = RagRuntime.from_loaded(loaded)

    # BM25 레그 단독 검증 — 어휘 매칭('월세')이 주거급여를 최상위로 올려야 함
    # (목 n-gram 벡터는 의미를 몰라 융합 순위는 실 임베딩과 다름 — 실측은 P4 평가로)
    import numpy as np

    from app.rag.search import tokenize

    q = "월세가 부담돼서 걱정이야"
    bscores = rt.bm25.get_scores(tokenize(q))
    bm25_top2 = [rt.chunks[int(i)].fields["서비스명"] for i in np.argsort(-bscores)[:2]]
    assert "주거급여" in bm25_top2

    qv = asyncio.run(embed.embed([q]))[0]
    r = hybrid_retrieve(rt, qv, q, k=4)
    assert len(r.items) == 4 and r.top_score > 0.0
    assert r.bm25_top > 0.0  # '월세' 어휘 증거

    # 카드 고유 어휘('치매')는 목 융합에서도 최상위 진입
    q2 = "치매 약값이 걱정이에요"
    qv2 = asyncio.run(embed.embed([q2]))[0]
    r2 = hybrid_retrieve(rt, qv2, q2, k=4)
    names2 = [c.fields["서비스명"] for c, _ in r2.items]
    assert "치매치료관리비 지원" in names2[:2]


def test_embed_mode_guard(tmp_path):
    """목으로 빌드한 인덱스는 real 런타임에서 로드 거부(사일런트 오염 방지)."""
    embed = MockEmbed()
    cs = cards.fixture_cards()
    loaded, st = asyncio.run(build_index(cs, embed.embed, None, "mock", sleep_s=0))
    save_index(loaded, tmp_path, st)
    s = types.SimpleNamespace(rag_data_dir=str(tmp_path))
    assert load_runtime(s, "real") is None
    rt = load_runtime(s, "mock")
    assert rt is not None and len(rt.chunks) == 12


def test_pick_card_spacing_variants():
    """LLM이 서비스명을 띄어 써도 카드-답변 일관성 유지."""
    from app.rag.answer import pick_card
    from app.rag.schema import DocChunk

    care = DocChunk(text="", source="", fields={"서비스명": "노인맞춤돌봄서비스"})
    med = DocChunk(text="", source="", fields={"서비스명": "의료급여"})
    retrieved = [(med, 0.9), (care, 0.8)]  # RRF 1위는 의료급여

    picked = pick_card(retrieved, "어르신께는 노인 맞춤 돌봄 서비스가 도움이 될 것 같아요.")
    assert picked is care  # 띄어쓰기 변형도 매칭
    assert pick_card(retrieved, "식사는 잘 챙겨 드시고 계신가요?") is med  # 부정 아님 → 폴백 유지


def test_pick_card_adversarial_negation():
    """적대적 케이스: 존재하지 않는 정책 질문 → LLM이 '없습니다'로 답하면
    무관한 검색 1위 카드를 붙이지 않는다 (답변-카드 모순 방지)."""
    from app.rag.answer import pick_card
    from app.rag.schema import DocChunk

    grant = DocChunk(text="", source="", fields={"서비스명": "장애인고용장려금"})
    pension = DocChunk(text="", source="", fields={"서비스명": "기초연금"})
    retrieved = [(grant, 0.9), (pension, 0.8)]

    # '모든 국민 100만원' 류 → 부정 답변 → 카드 없음
    neg = "정부에서 모든 국민에게 100만 원을 지원하는 정책은 현재 없습니다. 주민센터에 문의해 보세요."
    assert pick_card(retrieved, neg) is None
    assert pick_card(retrieved, "그런 제도는 확인되지 않아요.") is None

    # 부정 답변이라도 서비스명을 실제로 언급했으면 그 카드는 유효
    mixed = "그런 정책은 없습니다. 대신 기초연금은 검토해보실 만해요."
    assert pick_card(retrieved, mixed) is pension


def test_augment_query():
    assert augment_query("그거 어떻게 신청해요?", "기초연금").startswith("기초연금 ")
    assert augment_query("월세 지원이 궁금해요", "기초연금") == "월세 지원이 궁금해요"
    assert augment_query("신청 서류 뭐 필요해?", None) == "신청 서류 뭐 필요해?"
