"""RAG 파이프라인 (docs/돌봄콜_RAG_파이프라인_계획_v2.md).
schema(카드) → cards/fetch(수집) → senior(어르신 필터) → index(임베딩·FAISS·증분)
→ search(하이브리드) → answer(조립)."""
