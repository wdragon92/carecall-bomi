# 똑똑한 돌봄 친구 보미 (돌봄콜 AI)

**독거 어르신 곁에 늘 함께 있는 상시 돌봄 AI.** 집집마다 AI 스피커를 놓듯 '보미'가 어르신의 말동무가 되어
안부를 살피고, 대화에서 **건강·정서·사기·복지 신호를 실시간 관찰**하며, 필요할 때 **공식 복지 자료를 검색(RAG)해
근거 있는 안내**를 합니다. 판정이 필요하면 룰엔진이 셈하고, 위급하면 사람(119·109·보호자)에게 연결합니다.

> 한국 복지는 '신청주의' — 알아야 받습니다. 보미는 정보 비대칭과 고립을 메우되, **지어내지 않습니다**:
> 금액·자격은 LLM이 아닌 코드가 공식 자료 필드에서 그대로 옮기고(수치 환각 차단), 자료 밖 질문엔 모른다고 답합니다.
> 모든 AI 처리는 국내 리전 **NCP CLOVA(소버린 AI)**, 검색 인덱스는 서버 내 FAISS(데이터 외부 유출 없음).

## 핵심 기능

| 기능 | 요약 |
|---|---|
| 🗣️ 상시 말동무 | 시간대별 선인사, 짧은 존댓말 수다, 백채널 이해. 음성 대화(말하는 동안 실시간 자막 미리보기 + CLOVA CSR 확정) |
| 📚 RAG 복지 검색 | **공식 자료 557건**(중앙부처 전국 + 대구·경북 지자체 + 수기 핵심 12)을 하이브리드 검색(FAISS 벡터+BM25 RRF). 근거 카드에 **출처·정보 기준일** 표시 |
| 🎯 정확도 3계층 | T1 제도·신청법은 확정 안내 / **T2 수치는 코드가 카드로 삽입** / T3 개인 확정은 129·주민센터 연계 |
| 🚫 거부 게이트 | 2단 판정(의미 점수 + 어휘 증거)으로 자료 밖 질문 거부. **적대 방어**: "전 국민 100만원?" 같은 소문성 질문에 무관 카드가 붙지 않음 |
| 🧮 기초연금 사전판정 | 대화로 나이·가구 수집 → **결정론적 룰엔진**이 "가능성" 판정(2026 고시값: 단독 247만/부부 395.2만, 출처 주석 필수) + 신청 준비물 패키지 |
| 🩺 위험신호 안전망 | LLM과 별개로 규칙 기반 즉시 탐지(뇌졸중·심장·호흡·자살 신호, 노인 비전형 증상 포함). **연계 분리**: 몸 응급→119, 심리→자살예방 109 |
| 📄 문서 인식 | 서류·문자 사진 → OCR → 문서 종류 분류 + 쉬운 설명 + 큰 글씨 요약 카드(**금액·날짜는 OCR 원문 그대로** — 코드 삽입) + 스미싱 경고 |
| 📋 돌봄 현황·리포트 | 특이사항을 카테고리·심각도(위험/주의/참고)로 한눈에, 종료 시 보호자용 리포트(안내 복지+신청 패키지 포함) |

## 검색 품질 (실측, 재현 가능)

구어체 평가셋(서비스명 없는 어르신 말투 12문 + 문서 밖 6문), 557청크 실 인덱스 기준:

| 지표 | 결과 |
|---|---|
| Hit@1 / Hit@4 | **83% / 100%** |
| 인도메인 오거부 | **0 / 12** |
| 문서 밖 거부율 | **100% (6/6)** |

```bash
python scripts/eval_rag.py --json   # data/eval_results.json 재생성
```
거부 게이트: `top≥0.55 OR (top≥0.47 AND bm25≥12)` — 벡터 점수 분포가 겹쳐 단일 임계값이 불가능함을
실측으로 확인하고 2단으로 설계(근거: `app/rag/search.py` 주석, `scripts/eval_rag.py`).

## 아키텍처

```
[배치 — build_index.py]                        [런타임 — FastAPI + WebSocket]
공공데이터포털 2종 API ─┐                       어르신 발화 ─→ 게이트(벡터+BM25)
  중앙부처복지서비스     ├→ 복지카드 → 해시 증분 ──→ 통과: [복지 자료] 접지 → LLM 답변
  지자체(대구·경북 필터) ┘   (변경분만 재임베딩)        └→ 📌 T2 정보카드(코드 삽입) 말풍선
knowledge/welfare.json ──→ CLOVA 임베딩(bge-m3)      미달: 일반 수다
                          → FAISS + BM25          판정 의도 → 슬롯 수집 → 룰엔진 → 📝 신청 패키지
                                                  (별도 비동기) 특이사항 추출 + 안전망 → 패널/배너
```

- 인덱스는 로컬(ms 검색) — 시연이 외부 API 장애와 무관. 신선도는 주기 재빌드(변경분만 임베딩 ≈ 0원) + "정보 기준일" 표시.
- 무중단 갱신: `python build_index.py --source all` → `POST /api/rag/reload`

## 기술 스택

- **AI (NCP CLOVA)**: 채팅 HCX-005 · 분석/추출 HCX-007(reasoning) · 임베딩 v2(bge-m3, 1024d) · CSR(STT) · Voice Premium(TTS) · OCR
- **검색**: FAISS(IndexFlatIP, 서버 내 운영) + BM25(kiwipiepy 형태소) + RRF 융합
- **데이터**: 공공데이터포털 중앙부처복지서비스(15090532)·지자체복지서비스(15108347), XML
- **백엔드**: Python·FastAPI·WebSocket / **프론트**: 단일 HTML + 순수 JS + Tailwind(CDN), 빌드 도구 없음
- **원칙**: real 실패 시 기능별 mock 자동 폴백 — 시연이 끊기지 않음 (`GET /health`로 real/mock 확인, 화면 헤더에도 표시)

## 빠른 실행

```powershell
# 1) 가상환경 + 의존성
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2) 키 설정 (아래 '키 발급'). 키 없이 보려면 .env의 MOCK_MODE=true
Copy-Item .env.example .env    # 값 채우기

# 3) RAG 인덱스 생성 (공공데이터 키 없으면 --source fixtures 로 수기 12종만)
python build_index.py --source all

# 4) 실행 → http://127.0.0.1:8080
python run.py
```

- **모바일(같은 와이파이)**: `APP_HOST=0.0.0.0` 후 `http://<PC IP>:8080` — 첫 실행 때 Windows 방화벽 허용.
  단, **마이크(STT)는 localhost/https에서만** 동작(모바일 http는 채팅·음성재생·OCR만).
- **마이크**: 🎤 눌러 말하기 → 다시 눌러 전송(최대 30초) / **음성 속도**: `CLOVA_TTS_SPEED`(-5 빠름 ~ 10 느림)

## 키 발급

> 서브 계정 권한에 따라 일부 메뉴가 안 보일 수 있음 → 그 서비스는 mock으로 진행.

1. **CLOVA Studio**(LLM+임베딩 공용): 콘솔 → API 키(`nv-…`) → `CLOVA_STUDIO_API_KEY`
2. **AI·NAVER API**(STT+TTS): Application 등록(**CSR** + **CLOVA Voice Premium**) → `NCP_APIGW_CLIENT_ID/SECRET`
3. **CLOVA OCR**: 도메인 생성(**General**) → API Gateway **자동 연동** 실행 → Secret Key + Invoke URL → `CLOVA_OCR_SECRET`, `CLOVA_OCR_INVOKE_URL`
4. **공공데이터포털**(RAG 데이터): [중앙부처 15090532](https://www.data.go.kr/data/15090532/openapi.do) · [지자체 15108347](https://www.data.go.kr/data/15108347/openapi.do) 활용신청 → **Decoding 키** → `WELFARE_CENTRAL_API_KEY` / `WELFARE_LOCAL_API_KEY` (엔드포인트 4종은 코드 기본값 내장)

항목별 설명은 [.env.example](.env.example) 참고. 승인 직후 401은 게이트웨이 반영 대기(1~2시간), 대량 수집 429는 자동 백오프(일 트래픽 한도 유의).

## 실연동 상태 (2026-07-07)

| 서비스 | 상태 | 비고 |
|--------|------|------|
| CLOVA Studio (LLM) | ✅ | 채팅 HCX-005 / 분석 HCX-007 |
| CLOVA 임베딩 v2 | ✅ | bge-m3 1024d, 실 인덱스 557청크 |
| CLOVA Speech (STT) | ✅ | TTS→STT 왕복 전사 정확 + 브라우저 실시간 자막 미리보기 |
| CLOVA Voice (TTS) | ✅ | vgoeun, 속도 -2, "잘 안 ~" 낭독 페이싱 보정 |
| CLOVA OCR | ✅ | General 도메인 신규 APIGW 연동(2026-07-07). 실 이미지→한글 추출→문서 카드 전 구간 검증 |
| 공공데이터포털 2종 | ✅ | 목록·상세 수집(XML), 429 백오프 |

## 테스트

```powershell
.venv\Scripts\python -m pytest    # MOCK_MODE 강제, 네트워크 불필요 (53개)
```
세션 격리 · 특이사항 · 안전 연계(119/109 분리) · RAG(빌드/증분/게이트/카드/적대 방어) · 룰엔진 · OCR 문서카드 · 리포트 커버.

## 가드레일

진단 금지(관찰만) · 긴급은 사람에게(몸→119, 심리→자살예방 109, 번호 조작 금지) · **수치는 코드만**(T2, LLM 발화 금지) ·
자료 밖 질문 거부(+소문성 질문에 무관 카드 방지) · 고시 상수는 사람이 원문 확인 후 기입(출처 주석 필수) ·
프라이버시(대화·이미지 비영구, 세션 종료 시 폐기) · 위로하되 치료 아님 · 어르신 말투(존댓말·짧게·하나씩).
구현 위치: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §9, `app/core/safety.py`, `app/rag/rules.py`.

## 배포 (NCP 서버)

시연용 공개 URL: **https://101.79.26.62.sslip.io** (시연 기간 한정, systemd + Caddy 자동 HTTPS)

- 런북: [deploy/DEPLOY.md](deploy/DEPLOY.md) / 셋업 스크립트: [deploy/server_setup.sh](deploy/server_setup.sh)
- 배포 흐름: NCP 서버 생성 → 공인IP → `git clone` + `.env` 전송 → `server_setup.sh` → `https://<IP>.sslip.io`
- 코드 갱신: `git pull && pip install -r requirements.txt -q && systemctl restart carecall`
- 인덱스만 갱신(무중단): `build_index.py --source all` → `POST /api/rag/reload`

> ⚠️ 이 서브계정은 **서버 작동 시간제한(평일 09–22시 / 주말 09–18시)** 이 있어 그 밖 시간엔 생성·기동이 막히거나
> 자동 정지될 수 있다. 낮 시간 시연용으로 운용.

## 문서

- [docs/돌봄콜_RAG_파이프라인_계획_v2.md](docs/돌봄콜_RAG_파이프라인_계획_v2.md) — RAG 설계 스펙(정확도 3계층·갱신 전략·룰엔진)
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 실행 설계서(전체 구조·결정)
- [docs/DEMO_SCENARIO.md](docs/DEMO_SCENARIO.md) — 발표용 시연 대본
- [deploy/DEPLOY.md](deploy/DEPLOY.md) — 공인 배포 런북 / [TEARDOWN.md](TEARDOWN.md) — 철수 체크리스트
- [care-call-ai-claude-code-prompt.md](care-call-ai-claude-code-prompt.md) — 원본 요구사항
