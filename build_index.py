"""RAG 인덱스 배치 빌드 CLI (v2 §4-1~§4-2). cron 주 1회 + 발표 전날 수동 1회.

  python build_index.py --source fixtures          # welfare.json 12종 (P0 전 기본)
  python build_index.py --source api               # 공공데이터포털 (P0 완료 후)
  python build_index.py --source pdf               # knowledge/pdf/*.pdf (트랙 B)
  python build_index.py --source all --force       # 전체 재임베딩

증분: 텍스트가 같은 카드는 기존 벡터 재사용 → 2회차부터 임베딩 호출 급감.
출력은 ASCII(Windows cp949 콘솔 안전)."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path


def _pick_embedder(s):
    """factory와 동일 기준: MOCK_MODE 아님 + CLOVA 키 있으면 real."""
    if not s.mock_mode and s.llm_available():
        from app.services.clova_embed import ClovaEmbed

        return ClovaEmbed(s), "real"
    from app.services.mock import MockEmbed

    return MockEmbed(s), "mock"


async def _main() -> int:
    ap = argparse.ArgumentParser(description="build welfare RAG index")
    ap.add_argument("--source", choices=["fixtures", "api", "pdf", "all"], default="fixtures")
    ap.add_argument("--data-dir", default=None, help="default: settings.rag_data_dir")
    ap.add_argument("--force", action="store_true", help="ignore previous index (full re-embed)")
    args = ap.parse_args()

    from app.config import get_settings
    from app.rag import cards
    from app.rag.index import build_index, load_index, resolve_data_dir, save_index

    s = get_settings()
    data_dir = Path(args.data_dir).resolve() if args.data_dir else resolve_data_dir(s)

    chunks = []
    if args.source in ("fixtures", "all"):
        fx = cards.fixture_cards()
        print(f"[build_index] fixtures: {len(fx)} cards (knowledge/welfare.json)")
        chunks += fx
    if args.source in ("api", "all"):
        from app.rag import fetch

        api = await fetch.api_cards(s)  # P0 전에는 명확한 안내와 함께 실패
        print(f"[build_index] api: {len(api)} cards")
        chunks += api
    if args.source in ("pdf", "all"):
        from app.rag import pdf

        pc = pdf.pdf_cards()
        print(f"[build_index] pdf: {len(pc)} chunks (knowledge/pdf)")
        chunks += pc

    if not chunks:
        print("[build_index] no chunks to index - nothing to do")
        return 1

    embedder, mode = _pick_embedder(s)
    prev = None if args.force else load_index(data_dir)
    loaded, st = await build_index(chunks, embedder.embed, prev, mode)
    save_index(loaded, data_dir, st)
    print(
        f"[build_index] mode={mode} chunks={len(chunks)} dim={loaded.meta['dim']} | "
        f"embedded={st['embedded']} reused={st['reused']} deleted={st['deleted']} -> {data_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
