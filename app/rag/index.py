"""인덱스 빌드/저장/로드 (v2 §4-1~§4-2, 가이드 3-1/3-2 포팅).
- 증분: text_hash가 같은 카드는 기존 벡터 재사용(변경분만 임베딩, 삭제분은 행 제거)
- 저장: welfare.faiss + welfare.pkl(dict만) + hash.json, 임시파일→os.replace 원자 교체
- 이력: data/rag_meta.db (stdlib sqlite3) 빌드 로그
- faiss 미설치 환경에서도 numpy 내적으로 동일 검색 결과."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pickle
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from app.rag.schema import DocChunk, chunk_from_dict, chunk_to_dict, text_hash

log = logging.getLogger("rag.index")
REPO_ROOT = Path(__file__).resolve().parents[2]

try:
    import faiss  # type: ignore

    HAS_FAISS = True
except ImportError:  # 개발 환경 등에서 faiss 미설치여도 앱은 동작
    faiss = None
    HAS_FAISS = False


def resolve_data_dir(settings) -> Path:
    p = Path(settings.rag_data_dir)
    return p if p.is_absolute() else REPO_ROOT / p


def _normalize(mat: np.ndarray) -> np.ndarray:
    mat = np.asarray(mat, dtype="float32")
    if mat.ndim == 1:
        mat = mat.reshape(1, -1)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (mat / norms).astype("float32")


class VectorIndex:
    """IndexFlatIP(정규화 벡터 → 코사인) 래퍼. faiss 없으면 numpy로 동일 계산."""

    def __init__(self, embeddings: np.ndarray) -> None:
        self.embeddings = _normalize(embeddings) if len(embeddings) else np.zeros((0, 1), "float32")
        self._faiss = None
        if HAS_FAISS and len(self.embeddings):
            ix = faiss.IndexFlatIP(int(self.embeddings.shape[1]))
            ix.add(self.embeddings)
            self._faiss = ix

    def search(self, qvec, k: int) -> tuple[list[float], list[int]]:
        n = len(self.embeddings)
        k = min(int(k), n)
        if k <= 0 or qvec is None:
            return [], []
        q = _normalize(np.asarray(qvec, dtype="float32"))
        if self._faiss is not None:
            scores, idxs = self._faiss.search(q, k)
            return scores[0].tolist(), [int(i) for i in idxs[0]]
        sims = self.embeddings @ q[0]
        order = np.argsort(-sims)[:k]
        return sims[order].tolist(), [int(i) for i in order]


@dataclass
class LoadedIndex:
    chunks: list[DocChunk]
    embeddings: np.ndarray  # 정규화 float32 (N, D)
    meta: dict              # {embed_mode, dim, built_at, count}
    hashes: dict            # {serv_id: text_hash} 변경감지 산출물


async def build_index(
    chunks: list[DocChunk], embed_fn, prev: LoadedIndex | None, embed_mode: str,
    sleep_s: float = 0.1,
) -> tuple[LoadedIndex, dict]:
    """전체 카드에 대해 prev와 text_hash가 같은 벡터는 재사용, 나머지만 embed_fn 호출.
    반환: (인덱스, {"embedded", "reused", "deleted"})."""
    prev_map: dict[str, np.ndarray] = {}
    if prev is not None and prev.meta.get("embed_mode") == embed_mode and len(prev.embeddings):
        for c, v in zip(prev.chunks, prev.embeddings):
            prev_map[text_hash(c.text)] = v

    new_hashes = [text_hash(c.text) for c in chunks]
    vecs: list[np.ndarray | None] = [prev_map.get(h) for h in new_hashes]
    to_embed = [i for i, v in enumerate(vecs) if v is None]

    for n, i in enumerate(to_embed):
        emb = await embed_fn([chunks[i].text])
        vecs[i] = np.asarray(emb[0], dtype="float32")
        if sleep_s and n < len(to_embed) - 1:
            await asyncio.sleep(sleep_s)  # 임베딩 rate limit 완화 (v2 §8)

    if vecs:
        mat = _normalize(np.vstack([v for v in vecs]))
    else:
        mat = np.zeros((0, 1), "float32")

    stats = {
        "embedded": len(to_embed),
        "reused": len(chunks) - len(to_embed),
        "deleted": len(set(prev_map) - set(new_hashes)),
    }
    loaded = LoadedIndex(
        chunks=list(chunks),
        embeddings=mat,
        meta={
            "embed_mode": embed_mode,
            "dim": int(mat.shape[1]) if len(mat) else 0,
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "count": len(chunks),
        },
        hashes={c.serv_id or f"row-{i}": h for i, (c, h) in enumerate(zip(chunks, new_hashes))},
    )
    return loaded, stats


def _atomic_write(path: Path, write_fn) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    write_fn(tmp)
    os.replace(tmp, path)


def save_index(loaded: LoadedIndex, data_dir: Path, stats: dict | None = None) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)

    def _write_pkl(p: Path) -> None:
        with open(p, "wb") as f:
            pickle.dump(
                {"chunks": [chunk_to_dict(c) for c in loaded.chunks],
                 "embeddings": loaded.embeddings, "meta": loaded.meta},
                f,
            )

    _atomic_write(data_dir / "welfare.pkl", _write_pkl)
    _atomic_write(
        data_dir / "hash.json",
        lambda p: p.write_text(json.dumps(loaded.hashes, ensure_ascii=False, indent=1), encoding="utf-8"),
    )
    if HAS_FAISS and len(loaded.embeddings):
        vindex = VectorIndex(loaded.embeddings)
        _atomic_write(data_dir / "welfare.faiss", lambda p: faiss.write_index(vindex._faiss, str(p)))

    con = sqlite3.connect(data_dir / "rag_meta.db")
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS builds "
            "(built_at TEXT, embed_mode TEXT, chunks INT, dim INT, embedded INT, reused INT, deleted INT)"
        )
        s = stats or {}
        con.execute(
            "INSERT INTO builds VALUES (?,?,?,?,?,?,?)",
            (loaded.meta["built_at"], loaded.meta["embed_mode"], loaded.meta["count"],
             loaded.meta["dim"], s.get("embedded", -1), s.get("reused", -1), s.get("deleted", -1)),
        )
        con.commit()
    finally:
        con.close()


def load_index(data_dir: Path) -> LoadedIndex | None:
    pkl = data_dir / "welfare.pkl"
    if not pkl.exists():
        return None
    try:
        with open(pkl, "rb") as f:
            data = pickle.load(f)
        hashes = {}
        hj = data_dir / "hash.json"
        if hj.exists():
            hashes = json.loads(hj.read_text(encoding="utf-8"))
        return LoadedIndex(
            chunks=[chunk_from_dict(d) for d in data["chunks"]],
            embeddings=np.asarray(data["embeddings"], dtype="float32"),
            meta=dict(data.get("meta", {})),
            hashes=hashes,
        )
    except Exception as exc:  # noqa: BLE001 — 손상 인덱스는 미로드로 처리(앱은 뜬다)
        log.warning("RAG index load failed (%s): %s", pkl, exc)
        return None
