# 돌봄콜 RAG 파이프라인 계획 v2

> v1 대비 변경: 정확도 3계층 정책 신설, 갱신 전략 교정(연 1회 아님), 수치 템플릿 슬롯,
> 실시간 상세조회 이중화, 사전판정 룰엔진, 신청 지원 패키지, API 발급 체크리스트 추가.
> 실습 코드(RAG_Gradio1.ipynb)와 확장 가이드의 부품을 재사용하는 원칙은 동일.
>
> **⚠️ 이 문서는 설계 시점 계획서다.** 최종 구현과 다음이 다르니 참고 시 주의:
> - **LLM은 CLOVA HCX만** — 채팅 HCX-005 / 분석·추출 HCX-007. 계획의 Gemini A/B 비교는 **최종 미채택**.
> - **세션은 인메모리** — SQLite는 세션·리포트를 담지 않고 RAG 빌드 로그(`data/rag_meta.db`)만. 인덱스·해시는 로컬 파일(`welfare.faiss`·`welfare.pkl`·`hash.json`).
> - **평가셋은 구어체 12문 + 문서밖 6문(총 18)** — 계획의 "15개"가 아님(`scripts/eval_rag.py`).
> - 실 코퍼스: 수집 557 → 어르신 필터 후 **347청크**(지자체 대구 60·경북 134).

---

## 0. 서비스 원칙 — "확정"이 아니라 "신청 성공까지의 거리 최소화"

개인별 최종 수급 여부·금액은 신청 후 지자체의 소득·재산 조사로 확정된다 (정부의 모의계산도 참고용).
따라서 이 서비스의 목표는 **발견 → 판정(가능성) → 서류 → 신청**까지의 모든 거리를 좁히는 것이다.

**정확도 3계층 (Tier) 정책 — 모든 응답의 기준:**

| Tier | 정보 종류 | 응답 방식 |
|---|---|---|
| **T1 확정** | 제도의 존재, 신청 방법·채널, 필요 서류 | 단정적으로 안내 (공식 자료 필드 그대로) |
| **T2 기준 명시** | 금액·선정기준·신청기간 | 수치는 LLM이 아닌 **코드가 필드에서 삽입** + 기준일 표시 + 룰엔진 "가능성" 판정 |
| **T3 연계** | 개인별 최종 수급액·확정 판정 | "신청하시면 구청 조사 후 확정" + **상담 요약서** 들려 warm handoff |

기존의 "129에 문의하세요"는 전 응답의 면책 문구가 아니라 T3에서만 등장하는 연계 절차가 된다.

---

## 1. 루브릭 매핑

| 평가 항목 | 이 설계가 채우는 것 |
|---|---|
| 기획·주제 적합성 (20) | 복지부가 2026년 복지행정 AI·생성형 AI 상담을 시범 적용 → 정부 방향과 일치. 차별점: 케어콜(안부 중심) 대비 "근거 있는 복지 정보 + 사전판정 + 신청 지원 + 보호자 리포트" |
| 핵심 기능·기술 구현도 (35) | AI(CLOVA HCX-005/007 — 최종은 Gemini 미채택) / API(공공데이터포털 2종 + CLOVA) / DB(SQLite: RAG 빌드 로그 rag_meta.db — 세션은 인메모리) / 서버(NCP·FastAPI) — 명시된 4요소 전부 |
| 데이터·AI 활용도 (25) | 외부 데이터 수집(API 배치+실시간) + RAG + LLM + OCR + 룰엔진(결정론적 판정) |
| 발표 준비도 (20) | 로컬 인덱스 = 외부 장애 무관 데모 / "지어내지 않는 AI + 가능성 판정 + 신청 연결" 킬러 장면 |

---

## 2. 데이터 소스와 갱신 전략

### 트랙 A — 공공데이터포털 API (정형)

| API | 용도 | 링크 |
|---|---|---|
| 한국사회보장정보원_**중앙부처복지서비스** | 핵심. 전 부처 복지서비스 목록·상세 | https://www.data.go.kr/data/15090532/openapi.do |
| 한국사회보장정보원_**지자체복지서비스** | 대구/경북 필터 → 지역 서비스 추천(개인화) | https://www.data.go.kr/data/15108347/openapi.do |
| (선택) 사회서비스 제공기관 정보 검색 | 근처 제공기관 안내 (P3 이후 여유 시) | https://www.data.go.kr/data/15057683/openapi.do |

- 수집 방식: **배치** (build_index.py) — 서비스 1건 = 복지카드 1장 = 청크 1개
- 배치 근거: ① 자연어 발화 ↔ 키워드 API 검색은 매칭 불가 → 의미 검색(임베딩) 필요 ② 데모가 외부 API 상태와 무관 ③ 신선도는 아래 갱신 전략으로 확보

### 트랙 B — 보건복지부 공식 PDF (비정형)

- 2026년 긴급복지지원사업 안내, 2026년도 국민기초생활보장사업 안내 (복지부 홈페이지 > 정보 > 사업)
- 기초연금·노인맞춤돌봄·노인장기요양 안내 자료 2026년판
- (P1 우선순위 낮음) 금융감독원 보이스피싱 사례 자료 → 사기 예방 레이어
- **어르신 관련 챕터만 발췌**해 인덱싱 (수백 페이지 전체 금지 — 임베딩 비용)

### 갱신 전략 (v2 교정)

복지 제도의 **골격은 연 단위**(기준중위소득·급여단가, 12월 말~1월 초 연도전환)지만,
**개별 사업은 수시 변경**된다 (신규·시범 확대, 지침 중간 개정, 지자체 사업 순차 반영, 신청기간 개폐).

| 장치 | 구현 |
|---|---|
| 주기 배치 | build_index.py를 cron 주 1회 (발표 전날 수동 1회 필수) |
| 변경분 감지 | 카드 해시 비교 → **바뀐 카드만 재임베딩** (비용 ≈ 0으로 일 단위 신선도 가능) |
| 기준일 표시 | 모든 카드에 수집일 저장 → 답변·리포트에 "YYYY-MM-DD 기준" 명시 |
| 시간민감 필터 | 신청기간 필드가 있는 카드: 수집일 기준 마감 지난 사업은 검색 제외 또는 "기간 확인 필요" 태그 |
| 실시간 이중화 | §4-5 — 매칭된 서비스 1건은 답변 직전 상세조회 API로 최신화 |

### 공통 스키마

```python
@dataclass
class DocChunk:
    text: str                 # 임베딩 대상 텍스트 (복지카드 또는 PDF 청크)
    source: str               # "복지로/중앙부처 WLF-000123" | "긴급복지안내_p12"
    source_type: str = "pdf"  # "pdf" | "api"
    serv_id: str = ""         # API 카드만: 상세조회·해시 비교 키
    url: str = ""             # 복지로 상세/신청 페이지 (딥링크)
    fields: dict | None = None  # API 카드만: 구조화 필드 원본 (템플릿 슬롯용)
    collected_at: str = ""    # 수집일 (기준일 표시용)
```

두 트랙 모두 **하나의 FAISS 인덱스**. `fields`가 있는 카드(=API)는 T2 응답에서 슬롯 소스로 사용.

---

## 3. 아키텍처 v2

```
[배치 — build_index.py, cron 주 1회 + 발표 전날]
공공데이터 API(중앙+지자체) → JSON → 해시 비교(변경분만) → 복지카드 ─┐
복지부 PDF(발췌) → 문장 청킹(+오버랩) ─────────────────────────────┼→ CLOVA 임베딩 → FAISS
                                                                     ┘        ↓
                            welfare.faiss / welfare.pkl / hash.json (Object Storage 백업)

[런타임 — FastAPI on NCP]
어르신 발화 → CLOVA STT → 대화 LLM
  ├ (A) 복지 질문 ─→ RAG 검색 ─→ [매칭 서비스 실시간 상세조회] ─→ 응답 조립(슬롯) → TTS
  ├ (B) 니즈 신호 ─→ RAG 검색 ─→ 관찰 패널 추천 카드
  ├ (C) 판정 의도 ─→ 슬롯 추출(LLM) ─→ 룰엔진(코드) ─→ 가능성 안내 + 신청 패키지
  └ (D) 세션 종료 ─→ 리포트: 추천 서비스 + 근거 + 신청 패키지 / (필요 시) 상담 요약서

RAG 검색 = 질문 재작성(멀티턴) → 하이브리드(벡터+BM25, RRF) → 임계값 거부 판정
```

---

## 4. 구현 상세

### 4-1. 수집 → 복지카드 + 변경 감지

```python
import hashlib, json, requests, time

def card_hash(svc: dict) -> str:
    return hashlib.md5(
        json.dumps(svc, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()

def fetch_all(base_url: str) -> list[dict]:
    """목록조회 페이지네이션. 필드 파싱은 샘플 응답 확보 후 매핑."""
    out, page = [], 1
    while True:
        r = requests.get(base_url, params={
            "serviceKey": SERVICE_KEY,   # 반드시 Decoding 키
            "pageNo": page, "numOfRows": 100}, timeout=10)
        r.raise_for_status()
        items = parse_items(r)           # JSON/XML 여부는 샘플 보고 결정
        if not items:
            break
        out += items; page += 1; time.sleep(0.2)
    return out

def diff_and_embed(services, prev_hashes: dict) -> list[DocChunk]:
    """바뀐 서비스만 카드 생성 → 임베딩 대상 반환. 나머지는 기존 벡터 재사용."""
    changed = [s for s in services
               if prev_hashes.get(s["서비스ID"]) != card_hash(s)]
    return [service_to_card(s) for s in changed]
```

복지카드 텍스트 = 서비스명/지원대상/지원내용/신청방법/문의 (v1과 동일).
`fields`에 원본 dict 저장, `collected_at`에 수집일.

### 4-2. 인덱스 빌드

확장 가이드 부품 재사용: `build_rag_index_from_chunks`(3-2) + `save_index`(3-1) + PDF 문장 청킹(1-3).
해시 맵(hash.json)과 인덱스 파일을 함께 저장, Object Storage에 백업.

### 4-3. 검색 계층

1. 질문 재작성 (가이드 3-3) — "그거 어떻게 신청해요?" → 독립 질문
2. 하이브리드 검색 (가이드 2-3) — 서비스명이 전부 고유명사인 도메인이라 BM25 효과 큼
3. 거부 처리 (가이드 1-1) — 벡터 top_score 기준, 복지 질문셋으로 임계값 재튜닝

### 4-4. 응답 조립 — 수치는 LLM이 만들지 않는다

역할 분리: **LLM = 말투와 흐름 / 코드 = 사실과 수치.**

```python
SYS_PROMPT = """당신은 어르신을 돕는 복지 안내 도우미입니다.
- 주어진 [복지 자료]의 내용만으로 답합니다. 자료에 없으면 모른다고 합니다.
- 금액·기준·날짜·전화번호 등 수치는 직접 말하지 않습니다.
  (수치는 시스템이 정보 카드로 별도 안내합니다)
- 쉬운 말과 존댓말, 한 번에 1~2개 서비스만.
- 건강 상태에 대한 진단·단정 금지."""

def compose_answer(llm_text: str, svc_fields: dict) -> str:
    """T2: 수치·연락처·기준일은 구조화 필드에서 코드가 삽입"""
    card = (f"\n\n📌 {svc_fields['서비스명']}"
            f"\n· 지원: {svc_fields['지원내용']}"
            f"\n· 신청: {svc_fields['신청방법']}"
            f"\n· 문의: {svc_fields.get('문의처', '보건복지상담센터 129')}"
            f"\n· 정보 기준일: {svc_fields['collected_at']} (복지로 제공)")
    return llm_text + card
```

음성(TTS) 경로에서도 수치 문장은 고정 템플릿("지원 내용은 ○○입니다")으로 필드값을 읽는다.
→ 수치 환각 원천 차단 + 기준일 투명성.

### 4-5. 실시간 상세조회 이중화 (배치 + 라이브)

인덱스는 "찾기" 담당. **매칭된 서비스 1건**은 답변 직전 상세조회 API를 실시간 호출해
신청기간·문의처를 최신값으로 갱신한다. 호출 실패 시 인덱스 캐시값으로 폴백.

```python
def refresh_detail(serv_id: str, cached: dict) -> dict:
    try:
        r = requests.get(DETAIL_URL, params={
            "serviceKey": SERVICE_KEY, "서비스ID파라미터": serv_id}, timeout=3)
        r.raise_for_status()
        fresh = parse_detail(r)
        fresh["_live"] = True          # 답변에 "방금 확인한 정보" 표시 가능
        return fresh
    except Exception:
        cached["_live"] = False        # graceful degradation
        return cached
```

의미 검색의 장점(배치) + 신선도(라이브)를 동시에. 호출량도 세션당 1~2회라 트래픽 부담 없음.

### 4-6. 사전판정 룰엔진 — 기초연금 1종 (T2의 핵심)

역할 분리: **LLM = 대화에서 슬롯 추출 / 코드 = 결정론적 판정.**

```python
# P0에서 복지부 2026년 고시 확인 후 기입 (LLM 생성값 금지, 사람이 확인한 상수만)
BASIC_PENSION_2026 = {
    "age_min": 65,
    "income_threshold_single": None,  # 선정기준액(단독) — 고시에서 확인
    "income_threshold_couple": None,  # 선정기준액(부부)
}

def extract_slots(history) -> dict:
    """LLM 호출: 대화에서 {나이, 가구형태, 소득 힌트} JSON 추출. 없으면 되묻기."""
    ...

def check_basic_pension(age, household, income_hint) -> tuple[str, str]:
    if age < BASIC_PENSION_2026["age_min"]:
        return "해당없음", "만 65세부터 신청하실 수 있어요."
    if income_hint is None:
        return "확인필요", "소득·재산에 따라 달라져서, 간단히 몇 가지만 더 여쭤볼게요."
    th = BASIC_PENSION_2026[f"income_threshold_{household}"]
    return ("가능성높음" if income_hint <= th else "가능성낮음"), ""
```

- 판정 어휘는 항상 **"가능성"** — "확정"은 T3(신청 후 조사) 영역임을 응답에 명시
- 소득인정액은 복잡(소득+재산 환산) → 과제 범위: 간단 문답 1차 스크리닝 + 복지로 모의계산 링크 연계
- 확장 여지: 같은 구조로 노인맞춤돌봄 등 추가 가능 (발표에서 "룰 플러그인 구조"로 어필)

### 4-7. 신청 지원 패키지 — "신청까지"의 실체

```python
def build_apply_package(svc_fields: dict) -> dict:
    return {
        "서비스명": svc_fields["서비스명"],
        "온라인신청": svc_fields.get("url"),          # 복지로 딥링크 (온라인 신청 지원 서비스)
        "필요서류": svc_fields.get("구비서류", "주민센터에서 확인"),
        "체크리스트": ["신분증", "본인 명의 통장사본", "..."],  # 필드 기반 생성
        "기준일": svc_fields["collected_at"],
    }
```

- 보호자(따님/아드님)에게 카톡/문자용 텍스트로 전송 가능한 형태
- OCR 연계: 어르신이 받은 안내문 촬영 → 서비스 식별 → 해당 패키지 자동 제시
- 오프라인 신청/복잡 케이스: **상담 요약서** 자동 생성 (파악된 상황, 관심 서비스, 담당자 확인 질문
  목록) — 리포트 파이프라인 재활용. "전화하세요"가 아니라 warm handoff.

### 4-8. 서비스 통합 (FastAPI)

```
POST /rag/answer   {session_id, question, history} → {answer, sources, top_score, live}
POST /rag/screen   {session_id, slots}             → {판정, 근거, apply_package}
```

트리거: (A) 직접 질문 → /rag/answer / (B) 니즈 태그 → 패널 카드 / (C) 판정 의도 → /rag/screen
/ (D) 세션 종료 → 리포트(추천+패키지+요약서).

---

## 5. NCP 스택 (FAISS 정당화 포함)

| 계층 | 사용 기술 | 비고 |
|---|---|---|
| AI | CLOVA Studio (STT·TTS·OCR·챗·임베딩) | 과정 핵심 (계획의 Gemini A/B 비교는 최종 미채택) |
| 검색 | **FAISS on NCP 서버** | 라이브러리(≠외부 SaaS). 강사 교안 조합 그대로. 데이터가 서버 밖으로 안 나감 = 소버린 부합 |
| 저장 | 인덱스·해시는 로컬 파일(welfare.faiss·welfare.pkl·hash.json) / SQLite는 RAG 빌드 로그(rag_meta.db)만 — 세션·리포트는 인메모리(영구 저장 없음) | 루브릭 "DB" 명시 대응 |
| 서버 | NCP 서버 + FastAPI | |

방어 멘트: "수천 건 규모엔 인메모리 검색이 최적이고, 서버 내 자체 운영으로 데이터가 외부로
나가지 않습니다. 스케일업 시 NCP Cloud DB for PostgreSQL의 pgvector로 이전 가능한 구조입니다
(검색·생성이 분리된 교체 가능 블록 — 교안의 설계 원칙 그대로)."

---

## 6. Phase 계획 v2

| Phase | 작업 | 담당 | 소요 |
|---|---|---|---|
| **P0** | API 활용신청 3종 + 샘플 JSON 확보 / PDF 발췌 선정 / 기초연금 2026 고시값 확인 | **승용** | 반나절 |
| **P1** | build_index.py: 수집→해시 변경감지→카드→임베딩→저장(+Object Storage) | Claude Code | 1일 |
| **P2** | 검색 계층 + /rag/answer + 응답 조립(슬롯) + 실시간 상세조회 | Claude Code | 1일 |
| **P3** | 룰엔진(기초연금) + /rag/screen + 신청 패키지 + 트리거 B/C/D | Claude Code | 1~1.5일 |
| **P4** | 평가셋(구어체 12문 + 문서밖 6문 = 18) + Hit@4·거부율 + 데모 리허설 | 승용+CC | 반나절 |

Claude Code에 넘길 것: 이 문서 + 확장 가이드 + 샘플 JSON 3종 + `.env`(키는 코드에 하드코딩 금지).

---

## 7. 데모 시나리오 (킬러 3+1)

1. **의미로 찾는다**: "무릎이 아파서 장 보러 가기가 힘들어요" → 노인맞춤돌봄 카드. *"키워드가 아니라 의미"*
2. **판정하고 신청까지 (신규 킬러)**: "기초연금 나도 받을 수 있나?" → 대화로 슬롯 수집 → "가능성이 높으세요" → 서류 체크리스트+복지로 링크를 따님께 전송. *"안내에서 판정·신청 지원으로"*
3. **지어내지 않는다**: 문서 밖 질문 → 정중한 거부 + Hit@4/거부율 슬라이드
4. (여유 시) **OCR**: 안내문 촬영 → 서비스 식별 → 신청 패키지

각 답변 하단의 "정보 기준일 + 실시간 확인 표시"를 카메라에 잡히게 — 정확도 정책의 시각적 증거.

---

## 8. 리스크 v2

| 리스크 | 대응 |
|---|---|
| API 필드명·응답구조 미확정 | P0 샘플 응답 선확보 → 매핑은 Claude Code |
| **활용신청 직후 키 미반영** | 자동승인이어도 반영에 1~2시간 걸릴 수 있음 → `SERVICE ACCESS DENIED` 나오면 기다렸다 재시도 (포털 단골 이슈) |
| 인증키 이중 인코딩 | Decoding 키를 params로. 인코딩 키 사용 시 `SERVICE_KEY_IS_NOT_REGISTERED` |
| 임베딩 rate limit | 배치 sleep+재시도. 변경분만 재임베딩이라 2회차부터는 호출 급감 |
| 고시값 오입력 (룰엔진) | 상수는 사람이 고시 원문 확인 후 기입 + 출처 주석. LLM 생성값 금지 |
| PDF 표 추출 깨짐 | 깨지는 페이지 제외, 동일 정보는 API 카드로 대체 |
| 발표 중 외부 API 지연 | 검색은 로컬(ms). 상세조회는 timeout 3초+캐시 폴백. CLOVA 장애는 MOCK_MODE |

---

## 9. API 키 발급 체크리스트 (승용 직접, 계정·본인인증 필요)

발급은 전부 자동승인 계열이라 건당 10분 내외. **발급 → 샘플 호출 1회 → 응답 JSON 저장 → Claude Code 전달**까지가 한 세트.

- [ ] **공공데이터포털 회원가입/로그인** — https://www.data.go.kr
- [ ] **중앙부처복지서비스 활용신청** — https://www.data.go.kr/data/15090532/openapi.do
- [ ] **지자체복지서비스 활용신청** — https://www.data.go.kr/data/15108347/openapi.do
- [ ] (선택, P3 이후) 사회서비스 제공기관 정보 검색 — https://www.data.go.kr/data/15057683/openapi.do
- [ ] 발급 페이지에서 **Decoding 인증키** 복사 (인코딩 키 아님!)
- [ ] 샘플 호출 1회 → 응답 JSON 저장 (키 미반영 에러 시 1~2시간 후 재시도)
- [x] CLOVA Studio 키 — 실습에서 발급 완료. 프로젝트용 호출 한도만 확인
- [x] Gemini 키 (aistudio.google.com) — 과정에서 발급 완료. (백엔드 A/B 비교용 — **최종 구현엔 미채택**, CLOVA HCX만 사용)
- [ ] 모든 키는 `.env`로 관리 — 코드·깃 커밋에 하드코딩 금지 (Claude Code 지시문에 명시)

복지로 자체는 별도 키 불필요 — 신청 딥링크는 API 응답의 상세 URL 필드를 그대로 사용.
