"""검색 계층 (v2 §4-3, 가이드 2-3/3-3 포팅).
- 하이브리드: 벡터(FAISS) + BM25(kiwipiepy 토큰) → RRF 융합
- 거부 게이트는 RRF 점수가 아니라(유사도 아님 — 가이드 경고) 벡터 top_score + BM25 증거의
  2단 판정: top ≥ high(고신뢰) OR (top ≥ low AND bm25 ≥ evidence). 구어체 질의는 벡터
  분포가 겹쳐(실측: in 0.413~ / out ~0.479) 단일 임계값으로 분리 불가하기 때문.
- 질의 보강: 짧은 지시어 후속 질문에 직전 서비스명을 결정적으로 덧붙임(LLM 재작성 대체)"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

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


def _senior_only(loaded: LoadedIndex) -> LoadedIndex:
    """어르신 적합성 소급 가드 — 필터 이전에 빌드된 인덱스가 배포돼 있어도
    로드 시점에 청년·근로자 제도를 걷어낸다 (정책: app/rag/senior.py 단일 원천)."""
    from app.rag.senior import chunk_senior_relevant

    keep = [i for i, c in enumerate(loaded.chunks) if chunk_senior_relevant(c)]
    if len(keep) == len(loaded.chunks):
        return loaded
    log.info("senior guard: %d -> %d chunks (비어르신 제도 %d건 제외)",
             len(loaded.chunks), len(keep), len(loaded.chunks) - len(keep))
    return LoadedIndex(
        chunks=[loaded.chunks[i] for i in keep],
        embeddings=loaded.embeddings[keep] if len(loaded.embeddings) else loaded.embeddings,
        meta=dict(loaded.meta),
        hashes=loaded.hashes,
    )


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
    rt = RagRuntime.from_loaded(_senior_only(loaded))
    log.info("RAG index loaded: %d chunks (embed=%s, built %s)",
             len(rt.chunks), built_mode, loaded.meta.get("built_at", "?"))
    return rt


@dataclass
class Retrieval:
    """하이브리드 검색 결과 + 거부 게이트용 신호."""

    items: list[tuple[DocChunk, float]] = field(default_factory=list)  # RRF 상위 k
    top_score: float = 0.0  # 벡터 top1 코사인 (전역 의미 신호 — 관측·리포트용, 필터 이전)
    bm25_top: float = 0.0   # BM25 최고점 (전역 어휘 증거 신호, 필터 이전)
    # 게이트용 신호 — 필터(min_vec·region) 생존 후보 기준 최고값. None이면 전역값으로 폴백.
    # (지역가드로 걸러진 고점 청크가 접지 승인에 기여하는 것을 막는다 — passes_gate)
    gate_top: float | None = None
    gate_bm25: float | None = None


# 광역 지자체 약칭 — 질의의 "경북" 발화가 "경상북도" 카드를 허용하게
_REGION_ALIASES = {"경상북도": ("경북",), "대구광역시": ("대구",), "경상남도": ("경남",),
                   "전라북도": ("전북",), "전라남도": ("전남",), "충청북도": ("충북",), "충청남도": ("충남",)}


def region_ok(chunk: DocChunk, region: str, qtext: str) -> bool:
    """지자체 카드 지역 게이트 — 중앙부처(지역 없음)는 통과, 지자체는 기본 지역(대구) 또는
    질의에 그 지역명이 직접 등장할 때만. ('밥을 못 먹어' → 문경시 사업 매칭 방지)"""
    area = (chunk.fields or {}).get("지역", "")
    if not area:
        return True
    if region and region in area:
        return True
    for tok in area.split():
        if tok in qtext:
            return True
        base = tok.rstrip("시군구도")
        if base and base in qtext:
            return True
        for alias in _REGION_ALIASES.get(tok, ()):
            if alias in qtext:
                return True
    return False


def hybrid_retrieve(
    rt: RagRuntime, qvec, qtext: str, k: int = 4, pool: int = 20, rrf_k: int = 60,
    min_vec: float = 0.0, region: str = "",
) -> Retrieval:
    """벡터+BM25 RRF 융합 상위 k + 게이트 신호(벡터 top1, BM25 top).
    min_vec: 항목별 벡터 유사도 하한 — 'top4 고집' 대신 기준 미달 항목은 결과에서 제외
    (컨텍스트·카드·패널에 관련 낮은 자료가 끼는 것 방지).
    region: 지자체 카드 기본 지역 게이트 (region_ok)."""
    n = len(rt.chunks)
    if n == 0 or qvec is None:
        return Retrieval()
    pool = min(pool, n)

    vscores, vidx = rt.vindex.search(qvec, pool)
    top_score = float(vscores[0]) if vscores else 0.0
    vec_of = {int(i): float(s) for s, i in zip(vscores, vidx)}

    fused: dict[int, float] = {}
    for r, i in enumerate(vidx):
        fused[int(i)] = fused.get(int(i), 0.0) + 1.0 / (rrf_k + r + 1)
    bscores = None
    bm25_top = 0.0
    if rt.bm25 is not None:
        bscores = rt.bm25.get_scores(tokenize(qtext))
        bm25_top = float(np.max(bscores)) if len(bscores) else 0.0
        for r, i in enumerate(np.argsort(-bscores)[:pool]):
            if bscores[i] <= 0:  # BM25 0점(토큰 미교집합)은 순위 기여 없음
                break
            fused[int(i)] = fused.get(int(i), 0.0) + 1.0 / (rrf_k + r + 1)

    # 항목별 벡터 유사도: 벡터 풀(top-pool) 밖의 BM25 단독 청크는 실제 코사인을 계산한다.
    # (0.0으로 단정하면 min_vec>0에서 어휘로 걸린 청크가 항상 탈락 — C3)
    emb = rt.vindex.embeddings
    qn = np.asarray(qvec, dtype="float32").reshape(-1)
    qnorm = float(np.linalg.norm(qn))
    if qnorm:
        qn = qn / qnorm

    def _vsim(i: int) -> float:
        s = vec_of.get(i)  # 풀에 있으면 검색이 준 실측 코사인 (0.0/음수도 유효)
        if s is not None:
            return s
        if 0 <= i < len(emb) and emb.ndim == 2 and emb.shape[1] == qn.shape[0]:
            return float(emb[i] @ qn)  # emb는 정규화 저장 → 내적 = 코사인
        return 0.0

    # 필터(min_vec·region)를 먼저 적용하고 그 다음 RRF 상위 k로 절단한다 (C2).
    # 게이트 신호(gate_top/gate_bm25)는 **관련성(min_vec) 통과 후보** 기준으로 산출한다.
    # 지역(region_ok)은 '어느 카드를 보여줄지'(표시 선택)이지 '답할 수 있는가'(접지 가능)가
    # 아니므로 게이트 신호엔 반영하지 않는다 — 반영하면 대구 사용자가 물은 병원비 질의에서
    # 상위 경북 카드가 표시 제외되며 게이트가 무너져 중앙 의료급여 카드(정답)까지 과대거부된다.
    # 대신 표시할 카드가 하나도 없으면(items 빔) passes_gate가 거부하므로 안전. (min_vec 미달
    # 잡음은 여전히 게이트에서 제외 — 접지 근거가 되지 못한다.)
    order = sorted(fused.items(), key=lambda x: -x[1])
    items: list[tuple[DocChunk, float]] = []
    gate_top = 0.0
    gate_bm25 = 0.0
    for i, s in order:
        vs = _vsim(i)
        if vs < min_vec:
            continue
        if vs > gate_top:
            gate_top = vs
        if bscores is not None and float(bscores[i]) > gate_bm25:
            gate_bm25 = float(bscores[i])
        if region_ok(rt.chunks[i], region, qtext) and len(items) < k:
            items.append((rt.chunks[i], s))
    return Retrieval(items, top_score, bm25_top, gate_top, gate_bm25)


def passes_gate(r: Retrieval, settings, embed_mode: str) -> bool:
    """복지 접지 여부 2단 판정 (실측 근거: scripts/eval_rag.py).
    고신뢰 의미 매칭이거나, 중간 의미 + 뚜렷한 어휘 증거일 때만 통과."""
    if not r.items:
        return False
    low = settings.rag_threshold(embed_mode)
    high = settings.rag_threshold_high(embed_mode)
    # 필터 생존 후보 기준 신호로 판정 (C5). 미설정(None)이면 전역값 폴백 — 직접 구성된
    # Retrieval(수치표 테스트 등)과의 호환. 필터가 아무 것도 걸러내지 않으면 두 값은 동일.
    top = r.top_score if r.gate_top is None else r.gate_top
    bm = r.bm25_top if r.gate_bm25 is None else r.gate_bm25
    return top >= high or (top >= low and bm >= settings.rag_bm25_min(embed_mode))


# '알려줘' 단독은 새 주제 질문("로또 번호 알려줘")에도 흔해 오증강 위험 — '자세히'만 후속 신호로 인정
_FOLLOWUP = re.compile(r"그거|그건|그게|저거|거기|어디서|어떻게 해|신청|서류|얼마|자세히")


def augment_query(text: str, last_service: str | None) -> str:
    """멀티턴 후속 질문 보강 (가이드 3-3의 결정적 대체).
    '그거 어떻게 신청해요?' → '기초연금 그거 어떻게 신청해요?'
    상한 28자: "근데 아까 그거 그래도 한번 알려줘 봐"(21자) 같은 자연 대용어 문장 수용."""
    t = (text or "").strip()
    if last_service and len(t) <= 28 and _FOLLOWUP.search(t) and last_service not in t:
        return f"{last_service} {t}"
    return t
