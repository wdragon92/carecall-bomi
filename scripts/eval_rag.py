"""RAG 검색 품질 평가 (v2 P4): Hit@1/2/4, 문서 밖 거부율, 임계값 제안.

  python scripts/eval_rag.py            # 현재 설정(실키면 real 임베딩)으로 평가
  python scripts/eval_rag.py --json     # data/eval_results.json 저장(발표 슬라이드용)

평가셋: 카드 12종당 구어체 질문 1개(서비스명 미포함 — 의미 검색 시험) + 문서 밖 6개.
거부 판정은 벡터 top_score 기준(가이드 1-1). 제안 임계값 = (문서밖 최고점 + 인도메인 최저점)/2."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# (serv_id, 구어체 질문 — 어르신 말투, 서비스명 없이 의미로만)
IN_SET = [
    ("fixture-basic-pension", "나라에서 나오는 노인 용돈 같은 거, 나는 언제부터 받을 수 있나"),
    ("fixture-livelihood", "먹고살 돈이 없어서 끼니 걱정을 해"),
    ("fixture-medical-aid", "병원비가 무서워서 아파도 병원엘 못 가겠어"),
    ("fixture-housing-benefit", "월세 내기가 너무 버거워"),
    ("fixture-emergency-aid", "갑자기 일을 못 하게 돼서 당장 살길이 막막해"),
    ("fixture-care-service", "무릎이 아파서 장 보러 가기가 힘들어"),
    ("fixture-energy-voucher", "겨울에 난방비가 무서워서 보일러를 못 틀어"),
    ("fixture-safety-service", "혼자 있다가 쓰러지면 어쩌나 겁이 나"),
    ("fixture-senior-job", "소일거리라도 해서 용돈이라도 벌고 싶은데"),
    ("fixture-dementia-care", "자꾸 깜빡깜빡하는데 치매약 값이 부담돼"),
    ("fixture-culture-card", "영화 구경이라도 가고 싶은데 돈이 아까워서"),
    ("fixture-telecom-discount", "휴대폰 요금이 다달이 아까워"),
]
OUT_SET = [
    "오늘 날씨가 어때",
    "손주가 보고 싶네",
    "저녁에 뭘 먹을까 고민이야",
    "주식으로 돈 버는 법 좀 알려줘",
    "로또 당첨 번호 좀 알려줘",
    "요즘 대통령이 누구더라",
]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="save data/eval_results.json")
    ap.add_argument("--k", type=int, default=4)
    args = ap.parse_args()

    from app.config import get_settings
    from app.rag.search import hybrid_retrieve, load_runtime, passes_gate
    from app.services.mock import MockEmbed

    s = get_settings()
    embed_mode = "real" if (not s.mock_mode and s.llm_available()) else "mock"
    rt = load_runtime(s, embed_mode)
    if rt is None:
        print("[eval] index not loaded - run build_index.py first")
        return 1
    if embed_mode == "real":
        from app.services.clova_embed import ClovaEmbed

        embedder = ClovaEmbed(s)
    else:
        embedder = MockEmbed(s)
    gate_desc = (f"top>={s.rag_threshold_high(embed_mode)} OR "
                 f"(top>={s.rag_threshold(embed_mode)} AND bm25>={s.rag_bm25_evidence})")
    print(f"[eval] chunks={len(rt.chunks)} embed={embed_mode} gate: {gate_desc}")

    hits = {1: 0, 2: 0, 4: 0}
    in_tops: list[float] = []
    false_reject = 0
    rows = []
    print("\n== IN-domain (Hit@k + 게이트 통과 기대) ==")
    for gold, q in IN_SET:
        qv = (await embedder.embed([q]))[0]
        r = hybrid_retrieve(rt, qv, q, k=args.k, pool=s.rag_pool)
        ids = [c.serv_id for c, _ in r.items]
        rank = ids.index(gold) + 1 if gold in ids else 0
        for k in hits:
            if rank and rank <= k:
                hits[k] += 1
        ok = passes_gate(r, s, embed_mode)
        false_reject += not ok
        in_tops.append(r.top_score)
        mark = f"hit@{rank}" if rank else "MISS"
        print(f"  {r.top_score:.3f} b{r.bm25_top:5.1f}  {mark:>6} {'' if ok else ' 게이트거부!'}  {q}")
        rows.append({"set": "in", "q": q, "gold": gold, "rank": rank,
                     "top": round(r.top_score, 4), "bm25": round(r.bm25_top, 2), "gate": ok})

    out_tops: list[float] = []
    rejected = 0
    print("\n== OUT-of-domain (게이트 거부 기대) ==")
    for q in OUT_SET:
        qv = (await embedder.embed([q]))[0]
        r = hybrid_retrieve(rt, qv, q, k=args.k, pool=s.rag_pool)
        rej = not passes_gate(r, s, embed_mode)
        rejected += rej
        out_tops.append(r.top_score)
        print(f"  {r.top_score:.3f} b{r.bm25_top:5.1f}  {'거부 OK' if rej else 'PASS-THRU!'}  {q}")
        rows.append({"set": "out", "q": q, "top": round(r.top_score, 4),
                     "bm25": round(r.bm25_top, 2), "rejected": bool(rej)})

    n = len(IN_SET)
    print("\n== 결과 ==")
    print(f"  Hit@1 {hits[1]}/{n} ({hits[1]/n:.0%})   Hit@2 {hits[2]}/{n} ({hits[2]/n:.0%})   "
          f"Hit@4 {hits[4]}/{n} ({hits[4]/n:.0%})")
    print(f"  게이트: 인도메인 오거부 {false_reject}/{n}  |  문서밖 거부율 {rejected}/{len(OUT_SET)} "
          f"({rejected/len(OUT_SET):.0%})")
    print(f"  vector top — in: {min(in_tops):.3f}~{max(in_tops):.3f} / out: {min(out_tops):.3f}~{max(out_tops):.3f}"
          + ("  (분포 겹침 → 2단 게이트 사용 근거)" if min(in_tops) < max(out_tops) else ""))

    if args.json:
        out = {
            "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "embed_mode": embed_mode, "gate": gate_desc,
            "hit_at": {str(k): f"{v}/{n}" for k, v in hits.items()},
            "in_false_reject": f"{false_reject}/{n}",
            "out_reject_rate": f"{rejected}/{len(OUT_SET)}",
            "rows": rows,
        }
        p = Path(__file__).resolve().parents[1] / "data" / "eval_results.json"
        p.parent.mkdir(exist_ok=True)
        p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  saved -> {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
