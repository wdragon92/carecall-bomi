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


def test_augment_query():
    assert augment_query("그거 어떻게 신청해요?", "기초연금").startswith("기초연금 ")
    assert augment_query("월세 지원이 궁금해요", "기초연금") == "월세 지원이 궁금해요"
    assert augment_query("신청 서류 뭐 필요해?", None) == "신청 서류 뭐 필요해?"
