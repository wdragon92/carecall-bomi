"""검색 계층 (v2 §4-3, 가이드 2-3/3-3 포팅).
- 하이브리드: 벡터(FAISS) + BM25(kiwipiepy 토큰) → RRF 융합
- 거부 판정은 반드시 '벡터 top_score'로만 한다 — RRF 점수는 유사도가 아님(가이드 경고)
- 질의 보강: 짧은 지시어 후속 질문에 직전 서비스명을 결정적으로 덧붙임(LLM 재작성 대체)"""
from __future__ import annotations

import logging
import re

import numpy as np

from app.rag.index import LoadedIndex, VectorIndex, load_index, resolve_data_dir
from app.rag.schema import DocChunk

log = logging.getLogger("rag.search")

try:
    from rank_bm25 import BM25Okapi  # type: ignore
except ImportError:
    BM25Okapi = None

_kiwi = None
_kiwi_failed = False


def get_kiwi():
    """kiwipiepy 지연 싱글턴(초기화 ~1초). 실패 시 None → 정규식 폴백."""
    global _kiwi, _kiwi_failed
    if _kiwi is None and not _kiwi_failed:
        try:
            from kiwipiepy import Kiwi

            _kiwi = Kiwi()
        except Exception as exc:  # noqa: BLE001
            log.warning("kiwipiepy unavailable (%s) -> regex tokenizer", exc)
            _kiwi_failed = True
    return _kiwi


def tokenize(text: str) -> list[str]:
    kiwi = get_kiwi()
    if kiwi is not None:
        return [t.form for t in kiwi.tokenize(text or "")]
    return re.findall(r"[가-힣a-zA-Z0-9]+", text or "")


class RagRuntime:
    """로드된 인덱스 + 검색 준비물(BM25 프리빌드). providers.rag에 부착된다."""

    def __init__(self, chunks: list[DocChunk], vindex: VectorIndex, bm25, meta: dict) -> None:
        self.chunks = chunks
        self.vindex = vindex
        self.bm25 = bm25
        self.meta = meta

    @classmethod
    def from_loaded(cls, loaded: LoadedIndex) -> "RagRuntime":
        bm25 = None
        if BM25Okapi is not None and loaded.chunks:
            bm25 = BM25Okapi([tokenize(c.text) for c in loaded.chunks])
        elif BM25Okapi is None:
            log.warning("rank_bm25 unavailable -> vector-only retrieval")
        return cls(loaded.chunks, VectorIndex(loaded.embeddings), bm25, dict(loaded.meta))


def load_runtime(settings, embed_mode: str) -> RagRuntime | None:
    """인덱스 로드 + embed_mode 가드. 목으로 빌드한 인덱스를 실 벡터로 검색하면
    조용히 엉터리 결과가 나오므로 모드 불일치는 미로드 처리한다."""
    loaded = load_index(resolve_data_dir(settings))
    if loaded is None:
        log.info("RAG index not found -> RAG off (python build_index.py --source fixtures)")
        return None
    built_mode = loaded.meta.get("embed_mode", "")
    if built_mode != embed_mode:
        log.error(
            "RAG index embed_mode=%s but runtime embed=%s -> index ignored. "
            "rebuild: python build_index.py", built_mode, embed_mode,
        )
        return None
    rt = RagRuntime.from_loaded(loaded)
    log.info("RAG index loaded: %d chunks (embed=%s, built %s)",
             len(rt.chunks), built_mode, loaded.meta.get("built_at", "?"))
    return rt


def hybrid_retrieve(
    rt: RagRuntime, qvec, qtext: str, k: int = 4, pool: int = 20, rrf_k: int = 60,
) -> tuple[list[tuple[DocChunk, float]], float]:
    """벡터+BM25 RRF 융합 상위 k와, 거부 판정용 '벡터 top_score'(코사인)를 함께 반환."""
    n = len(rt.chunks)
    if n == 0 or qvec is None:
        return [], 0.0
    pool = min(pool, n)

    vscores, vidx = rt.vindex.search(qvec, pool)
    top_score = float(vscores[0]) if vscores else 0.0

    fused: dict[int, float] = {}
    for r, i in enumerate(vidx):
        fused[i] = fused.get(i, 0.0) + 1.0 / (rrf_k + r + 1)
    if rt.bm25 is not None:
        bscores = rt.bm25.get_scores(tokenize(qtext))
        for r, i in enumerate(np.argsort(-bscores)[:pool]):
            if bscores[i] <= 0:  # BM25 0점(토큰 미교집합)은 순위 기여 없음
                break
            fused[int(i)] = fused.get(int(i), 0.0) + 1.0 / (rrf_k + r + 1)

    order = sorted(fused.items(), key=lambda x: -x[1])[:k]
    return [(rt.chunks[i], s) for i, s in order], top_score


_FOLLOWUP = re.compile(r"그거|그건|그게|저거|거기|어디서|어떻게 해|신청|서류|얼마")


def augment_query(text: str, last_service: str | None) -> str:
    """멀티턴 후속 질문 보강 (가이드 3-3의 결정적 대체).
    '그거 어떻게 신청해요?' → '기초연금 그거 어떻게 신청해요?'"""
    t = (text or "").strip()
    if last_service and len(t) <= 20 and _FOLLOWUP.search(t) and last_service not in t:
        return f"{last_service} {t}"
    return t
