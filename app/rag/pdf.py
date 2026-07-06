"""복지부 PDF 발췌 → 문장 청킹 카드 (v2 트랙 B, 가이드 1-3 포팅).
kss 대신 kiwipiepy의 split_into_sents 사용(의존성 절감). PDF가 없으면 빈 목록."""
from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

from app.rag.schema import DocChunk

log = logging.getLogger("rag.pdf")
PDF_DIR = Path(__file__).resolve().parents[2] / "knowledge" / "pdf"


def _split_sentences(text: str) -> list[str]:
    from app.rag.search import get_kiwi

    kiwi = get_kiwi()
    if kiwi is not None:
        try:
            return [s.text.strip() for s in kiwi.split_into_sents(text) if s.text.strip()]
        except Exception as exc:  # noqa: BLE001
            log.warning("split_into_sents failed (%s) -> regex", exc)
    parts = re.split(r"(?<=[.!?다요])\s+", text)
    return [p.strip() for p in parts if p.strip()]


def load_pdf_to_chunks_sentence(pdf_path: Path, max_chars: int = 500) -> list[DocChunk]:
    """문장 경계를 지키며 max_chars 이하로 청킹 (가이드 1-3b). 페이지 단위 출처 표기."""
    from pypdf import PdfReader

    today = date.today().isoformat()
    reader = PdfReader(str(pdf_path))
    out: list[DocChunk] = []
    for pno, page in enumerate(reader.pages, start=1):
        text = re.sub(r"\s+", " ", (page.extract_text() or "")).strip()
        if not text:
            continue
        buf = ""
        for sent in _split_sentences(text):
            if buf and len(buf) + len(sent) + 1 > max_chars:
                out.append(DocChunk(
                    text=buf, source=f"{pdf_path.stem} p{pno}", source_type="pdf",
                    collected_at=today,
                ))
                buf = sent
            else:
                buf = f"{buf} {sent}".strip()
        if buf:
            out.append(DocChunk(
                text=buf, source=f"{pdf_path.stem} p{pno}", source_type="pdf",
                collected_at=today,
            ))
    return out


def pdf_cards(pdf_dir: Path | None = None, max_chars: int = 500) -> list[DocChunk]:
    """knowledge/pdf/*.pdf 전체 → 청크. 표가 깨지는 페이지는 P0에서 발췌본으로 정리(v2 §8)."""
    d = pdf_dir or PDF_DIR
    if not d.exists():
        log.info("no pdf dir (%s) -> skip track B", d)
        return []
    out: list[DocChunk] = []
    for p in sorted(d.glob("*.pdf")):
        chunks = load_pdf_to_chunks_sentence(p, max_chars=max_chars)
        log.info("pdf %s -> %d chunks", p.name, len(chunks))
        out += chunks
    return out
