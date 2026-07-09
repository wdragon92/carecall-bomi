# 돌봄콜 AI — 실행 설계서 (Architecture & Execution Blueprint)

> **이 문서의 역할**: `docs/claude-code-prompt.md`(이하 "요구사항 문서")가 *무엇을* 만들지 정의하고,
> 본 문서는 *어떻게* 만들지의 결정을 전부 내려둔 실행 설계서다.
> **충돌 시**: 요구사항(무엇)은 요구사항 문서가 우선, 구현 결정(어떻게)은 본 문서가 우선.
>
> **구현 세션 지침**:
> - §13 단계별 실행 계획을 0단계부터 순서대로, 자율적으로 진행한다 (질문·중단 없이).
> - `[문서확인]` 표시가 붙은 항목은 **구현 직전 WebFetch/WebSearch로 공식 NCP 문서를 검증**하고 구현한다.
>   검증 결과가 본 문서와 다르면 **공식 문서가 정답** — 단, 설계 의도(역할 분리·mock 폴백·가드레일)는 유지한다.
> - 매 단계: 서버 기동 + pytest + (UI 단계는) 브라우저 수동 확인 → git commit → 다음 단계.

---

## 1. 시스템 개요

```
[브라우저 (index.html + app.js)]
   │  HTTP (세션생성/STT/OCR/TTS/종료)  +  WebSocket (채팅/패널 실시간)
   ▼
[FastAPI 단일 앱 (로컬 실행, uvicorn)]
   ├─ SessionStore (in-memory, 세션별 완전 격리)
   ├─ Conversation Orchestrator ──→ LLMProvider ──→ CLOVA Studio (HyperCLOVA X)
   ├─ Extraction Pipeline (비동기) ─→ LLMProvider (structured/JSON)
   ├─ STTProvider ──→ CLOVA Speech Recognition (CSR)
   ├─ TTSProvider ──→ CLOVA Voice Premium
   ├─ OCRProvider ──→ CLOVA OCR (General)
   └─ knowledge/welfare.json (복지 grounding, 정적 파일)
```

- **기본 실행 = 로컬 데모.** 단, 공개 시연용 NCP 서버가 별도 프로비저닝돼 있다(서버 142948660 carecall-bomi·VPC 142283·서브넷 309427·ACG 365174·공인IP 101.79.26.62 — `deploy/DEPLOY.md`, 철수는 `TEARDOWN.md`). 로컬만 쓰면 프로비저닝 불필요.
- NCP 의존 = 외부 API 호출 4종뿐. 키가 없거나 실패하면 provider 단위로 mock 폴백.
- 세션(대화·특이사항)은 **인메모리**에만 두고 영구 저장하지 않는다(요구사항 가드레일 4). 유일한 SQLite는 RAG 빌드 로그 `data/rag_meta.db`(stdlib sqlite3, 인덱스 빌드 이력만 — 개인정보·세션 비저장).

## 2. 디렉토리 구조

```
carecall-bomi/          # (설계 시점 발췌 — 실제 트리엔 app/rag/, app/core/safety.py·prompts_analysis.py, scripts/, deploy/ 등이 추가됨)
├── app/
│   ├── main.py              # FastAPI 앱 생성, lifespan(세션 TTL 스위퍼), 라우터/정적 마운트
│   ├── config.py            # pydantic-settings로 .env 로딩, MOCK_MODE·provider별 키 존재 판정
│   ├── models.py            # Pydantic 모델: Message, Finding, WelfareItem, Report, WS 페이로드
│   ├── session.py           # Session, SessionStore (dict + TTL + LRU cap)
│   ├── routes/
│   │   ├── http.py          # REST 엔드포인트 (§6.1)
│   │   └── ws.py            # WebSocket 핸들러 (§6.2)
│   ├── services/
│   │   ├── base.py          # Provider 추상 인터페이스 4종 + ProviderError
│   │   ├── factory.py       # 키 유무·MOCK_MODE 보고 provider 조립, 기동 시 모드 로그
│   │   ├── clova_llm.py     # CLOVA Studio 실구현 (스트리밍 + structured) [문서확인]
│   │   ├── clova_stt.py     # CSR 실구현 [문서확인]
│   │   ├── clova_tts.py     # CLOVA Voice Premium 실구현 [문서확인]
│   │   ├── clova_ocr.py     # CLOVA OCR General 실구현 [문서확인]
│   │   └── mock.py          # 4종 mock (§7.3 — 데모 가능한 수준의 시나리오 mock)
│   ├── core/
│   │   ├── prompts.py       # 시스템 프롬프트 전부 (가드레일 6종 반영, §9)
│   │   ├── conversation.py  # 채팅 오케스트레이션: 히스토리 관리, 응답 스트리밍, 추출 트리거
│   │   ├── extraction.py    # 특이사항 추출 파이프라인 (비동기·코얼레싱, §8.3)
│   │   ├── welfare.py       # welfare.json 로드, 시그널→복지 매칭 (§10)
│   │   └── report.py        # 종료 요약 리포트 생성
│   └── static/
│       ├── index.html       # 단일 페이지 (Tailwind CDN)
│       └── app.js           # 순수 JS: WS, 녹음(WAV 인코딩), TTS 재생, 렌더링
├── knowledge/welfare.json   # 복지 12항목 (§10)
├── tests/                   # pytest (MOCK_MODE 강제, §12)
├── docs/
│   ├── ARCHITECTURE.md      # 본 문서
│   ├── DEMO_SCENARIO.md     # 7단계에서 작성 (발표용 시나리오 대본)
│   └── claude-code-prompt.md  # 요구사항 문서 (원본 유지)
├── requirements.txt
├── run.py                   # uvicorn 실행 편의 스크립트 (host/port는 .env)
├── .env.example / .env(미커밋) / .gitignore / README.md / TEARDOWN.md
```

## 3. 핵심 기술 결정 (전부 확정 — 재논의 불필요)

| 항목 | 결정 | 이유 / 폴백 |
|---|---|---|
| Python 실행 | `python -m venv .venv` + `requirements.txt`(하한 핀 `>=`) | Python 3.14라 최신 휠 필요. 설치 실패 시 해당 패키지만 버전 조정 |
| 의존성 | fastapi, uvicorn[standard], httpx, pydantic-settings, python-multipart, pytest, pytest-asyncio | 최소 구성. 외부 DB/ORM/프론트 빌드도구 없음(RAG 빌드 로그만 stdlib sqlite3) |
| LLM 채팅 | **HCX-005 확정** 스트리밍 (설계 초안은 HCX-007 후보였으나 지연·빈응답으로 채팅은 005 확정) | 분석/추출은 HCX-007 유지 |
| LLM 추출 | Structured Output(JSON Schema) 시도 → 모델 제약으로 불가 시 "JSON만 출력" 프롬프트 + 견고 파서(코드펜스 제거→json.loads→1회 재시도) | 요구사항 문서가 경고한 기능조합 제약 대응 |
| STT | **CSR(짧은 문장 인식)** — 푸시투토크 턴 단위에 적합 | 장문 CLOVA Speech(도메인 빌더)는 대안. 계정에서 CSR 불가 시에만 전환 |
| 오디오 포맷 | **브라우저에서 PCM 캡처 → JS로 WAV(16kHz mono 16bit) 인코딩 → 업로드** | MediaRecorder webm은 CSR 미지원 위험. WAV면 서버 트랜스코딩(ffmpeg) 불필요 → 로컬 의존성 zero |
| TTS | CLOVA Voice **프리미엄** 보이스, **확정 `vgoeun`·속도 -2** mp3(청감 튜닝; `.env`/persona §2가 기준. 설계 초안은 vmikyung) | 노인 친화 톤 |
| OCR | CLOVA OCR **General** 도메인, base64 JSON 방식 | 고지서·안내문·문자캡처 범용 |
| 프론트 | 단일 index.html + app.js + Tailwind CDN. 빌드 없음 | 요구사항 그대로. CDN 실패 대비 최소 fallback CSS 몇 줄 인라인 |
| 실시간 | WS 1본(세션당): 채팅 + 패널 갱신 + 알림 모두 이 채널 | STT/OCR/TTS는 REST(파일·바이너리 처리에 적합) |
| 세션 | in-memory dict, TTL 120분, 최대 200세션 LRU | 데모 스케일. 수평확장 필요 시 store만 교체 가능한 인터페이스 |
| 스트리밍 응답 | CLOVA SSE → WS delta 중계. 스트리밍 실패 시 통짜 응답 폴백 플래그 | 항상 동작 우선 |
| 재시도 | provider 호출 실패 시 1회 재시도(지수 백오프 1s), 그래도 실패면 친절한 에러 말풍선 | 데모 중 크래시 방지. 세션은 절대 죽이지 않음 |
| 로깅 | std logging INFO. provider 지연(ms)·모드(mock/real) 로그. **키·대화 전문은 로그 금지**(앞 30자 truncate) | 프라이버시 가드레일과 일관 |

## 4. 설정과 시크릿

`.env` → `config.py`(pydantic-settings). 변수는 `.env.example`이 단일 원천(주석 포함).

**Provider별 mock 폴백 규칙 (중요)**
- `MOCK_MODE=true` → 4종 전부 mock.
- `MOCK_MODE=false` → provider **개별로** 자기 키가 있으면 real, 없으면 mock (부분 연동 가능).
- 기동 시 콘솔에 모드 표 출력 + `GET /health`가 `providers: {llm: "real|mock", ...}` 반환 + UI 푸터에 배지 표시(데모 투명성).

## 5. 세션 모델

```python
Session:
  id: str                     # secrets.token_urlsafe(16)
  created_at / last_active
  messages: list[Message]     # role, text, ts, via("text"|"voice"|"system"), attachment_meta?
  findings: list[Finding]     # 최신 추출 결과로 전체 교체
  welfare_matched: list[str]  # welfare item id
  ocr_texts: list[OcrDoc]     # 추출 텍스트만 보관 (이미지 바이트는 요청 처리 후 폐기)
  tts_cache: dict[msg_id, bytes]  # LRU 최대 20개
  extract_lock: asyncio.Lock + dirty flag   # §8.3 코얼레싱
  ws: WebSocket | None
```

- **격리 원칙**: 전역 상태는 SessionStore와 무상태 provider 싱글턴뿐. 모든 데이터는 session id 키로만 접근.
- TTL 스위퍼: lifespan에서 10분 주기 태스크, 120분 무활동 세션 폐기.
- `Finding.id = sha1(category + content[:20])[:8]` → 프론트가 새 카드 하이라이트 diff에 사용.

## 6. API 명세

### 6.1 REST

| Method/Path | 요청 | 응답 | 비고 |
|---|---|---|---|
| `POST /api/sessions` | – | `{session_id}` | 세션 생성 |
| `POST /api/sessions/{sid}/audio` | multipart WAV | `{text}` | STT만 수행. 클라이언트가 결과를 WS `user_message(via:"voice")`로 재전송 → 채팅 진입 경로 단일화 |
| `POST /api/sessions/{sid}/image` | multipart 이미지(≤5MB, 클라에서 장변 1600px 다운스케일) | `202 {upload_id}` | OCR→설명→추출은 비동기, 결과는 WS로 push |
| `POST /api/sessions/{sid}/tts` | `{message_id}` | `audio/mpeg` | 서버가 해당 AI 메시지 텍스트로 TTS(마크다운·이모지 제거 후). 세션 캐시 |
| `POST /api/sessions/{sid}/end` | – | `{report}` (JSON) | 종료 전 추출 1회 flush 후 리포트 생성 |
| `GET /health` | – | `{status, providers:{...}}` | 모드 배지용 |
| `GET /` | – | index.html | 정적 서빙 (동일 출처 → CORS 불필요) |

### 6.2 WebSocket `/ws/{session_id}` — JSON 메시지

| 방향 | type | payload | 비고 |
|---|---|---|---|
| C→S | `user_message` | `{text, via}` | 텍스트 입력·STT 결과 공용 |
| S→C | `session_ready` | `{session_id, providers}` | 연결 직후 |
| S→C | `ai_message_start` | `{id}` | 스트리밍 시작 |
| S→C | `ai_message_delta` | `{id, text}` | 토큰 청크 |
| S→C | `ai_message_end` | `{id, full_text}` | 이때 TTS 트리거(토글 on이면) |
| S→C | `findings_update` | `{findings: [...]}` | 전체 교체 방식 |
| S→C | `welfare_update` | `{items: [...]}` | 매칭된 복지 (id·이름·한줄·심볼) |
| S→C | `urgent_alert` | `{message}` | §9 긴급 코드 강제 규칙 |
| S→C | `ocr_status` | `{upload_id, status}` | processing/done/error |
| S→C | `error` | `{code, message}` | 친절한 한국어 |

**연결 직후 서버가 선인사 push**: `ai_message_*` 시퀀스로 인사말 전송. 인사말은 **LLM 호출 없이 시간대 인지 템플릿 풀**에서 선택(예: 낮 "어르신, 안녕하세요. 점심은 잡수셨어요?") → 첫 화면 즉시성·안정성 확보. 템플릿 인사도 히스토리에 assistant로 기록.

### 6.3 플로우 요약
- **텍스트 턴**: user_message → 히스토리 append → LLM 스트리밍 → end 후 추출 트리거(비동기).
- **음성 턴**: 녹음 → POST audio → `{text}` → 클라가 user_message(via:voice) 전송 → 이후 동일.
- **이미지 턴**: POST image → (서버) OCR → OCR_EXPLAIN 프롬프트로 LLM 설명 → ai_message로 push + ocr_texts 저장 + 추출 트리거. 이미지 바이트는 응답 후 즉시 폐기.
- **종료**: POST end → 추출 flush → REPORT 프롬프트로 리포트 JSON 생성(mock은 조립식) → 모달 렌더.

## 7. Provider 레이어

### 7.1 인터페이스 (`services/base.py`)

```python
class LLMProvider(Protocol):
    def chat_stream(self, messages: list[dict], **opts) -> AsyncIterator[str]: ...
    async def chat(self, messages: list[dict], **opts) -> str: ...
    async def extract_json(self, messages: list[dict], schema: dict) -> dict: ...

class STTProvider(Protocol):
    async def transcribe(self, wav_bytes: bytes) -> str: ...

class TTSProvider(Protocol):
    async def synthesize(self, text: str) -> bytes: ...   # mp3

class OCRProvider(Protocol):
    async def extract_text(self, image_bytes: bytes, fmt: str) -> str: ...
```

### 7.2 실구현 스펙 — 전부 [문서확인] 후 구현 (§14의 URL 후보 사용)

| Provider | 예상 사양 (검증 대상) |
|---|---|
| CLOVA Studio | base `https://clovastudio.stream.ntruss.com`, Chat Completions **v3** `POST /v3/chat-completions/{model}`, 인증 `Authorization: Bearer {CLOVA_STUDIO_API_KEY}`(`nv-`로 시작), 스트리밍은 `Accept: text/event-stream` SSE. 파라미터명(maxTokens vs maxCompletionTokens, thinking effort)이 모델별로 다를 수 있음. OpenAI 호환 엔드포인트가 있으면 구현 단순화 옵션으로 검토 |
| CSR | `POST https://naveropenapi.apigw.ntruss.com/recog/v1/stt?lang=Kor`, 헤더 `X-NCP-APIGW-API-KEY-ID`/`X-NCP-APIGW-API-KEY`, body=음성 바이너리(octet-stream), 응답 `{"text"}`. 길이 제한(~60초)·지원 포맷 목록 확인 |
| CLOVA Voice Premium | `POST https://naveropenapi.apigw.ntruss.com/tts-premium/v1/tts`, 동일 헤더 2종, form-urlencoded `speaker/text/format=mp3/speed`, 응답 mp3 바이너리. 보이스 목록에서 노인 친화 보이스 확정 |
| CLOVA OCR | `POST {CLOVA_OCR_INVOKE_URL}`, 헤더 `X-OCR-SECRET`, body JSON `{version:"V2", requestId, timestamp, images:[{format, name, data: base64}]}`, 응답 `images[].fields[].inferText` 조합 |

공통: httpx.AsyncClient 싱글턴(타임아웃 일반 15s/스트림 30s), 실패 1회 재시도, ProviderError로 래핑. QPM 보호는 임베딩 배치의 호출 간격(sleep)+429 백오프(build_index.py)와 추출의 세션별 코얼레싱 락(§8.3)으로 — 전역 세마포어는 두지 않는다.

### 7.3 Mock 사양 (키 없이도 데모가 "살아있게")

| Mock | 동작 |
|---|---|
| LLM 채팅 | 키워드 규칙 + 공감 템플릿 순환. 단어 단위로 yield해 스트리밍 UX 재현 |
| LLM 추출 | 결정적 키워드 매핑: "잠/아프/약"→건강, "외로/우울/혼자"→정서, "돈/생활비"→복지_니즈, "보이스피싱/문자/계좌"→사기_노출, "죽고"→긴급(높음). 누적 반영 |
| STT | 데모용 문장 순환 반환("요즘 잠을 통 못 자요" 등 — 추출 데모와 연결되는 문장들) |
| TTS | stdlib `wave`로 생성한 0.5초 차임 톤 반환 → 재생 경로 검증 가능 |
| OCR | 파일명에 "문자/sms" 포함 시 스미싱 문자 샘플, 아니면 전기요금 고지서 샘플 텍스트 |

## 8. 대화 오케스트레이션

### 8.1 히스토리·컨텍스트
- 채팅 호출: system(CHAT) + 최근 40개 메시지(초과 시 오래된 것 절단). OCR 텍스트는 발생 턴에 system note로 삽입.
- 응답 스타일 강제: 존댓말, 2~4문장, 질문은 한 번에 하나 (프롬프트 + maxTokens ~300).

### 8.2 채팅 파라미터 (초기값, 튜닝 가능)
- 채팅: temperature 0.5, topP 0.8, repetitionPenalty 완만, thinking 최소(지원 시 off/low — 지연 절감).
- 추출: temperature 0.1, JSON 전용.

### 8.3 추출 파이프라인 (비동기·코얼레싱)
1. 트리거: AI 응답 완료 후 / OCR 반영 후 / 세션 종료 시(flush).
2. 세션별 `extract_lock`: 실행 중이면 `dirty=true`만 세팅 → 종료 시 dirty면 1회 더 실행 (중복 태스크 없음).
3. 입력: 누적 대화 compact(뒤에서 ~6,000자) + OCR 텍스트. 출력: Finding 전체 목록 + `welfare_signals[]` + `matched_welfare_ids[]`.
4. 결과: findings 전체 교체 → `findings_update` push. welfare 매칭(§10) 갱신 시 `welfare_update` push.
5. 실패: 기존 findings 유지, WARN 로그만. UI는 조용히 유지 (데모 안정성).

## 9. 프롬프트 & 가드레일 매핑

> **현행화(2026-07-07):** AI 페르소나는 **"보미"** 로 리브랜딩되었고, 정체성·말투·행동의 단일 기준(SSoT)은
> **`docs/bomi-persona.md`(현행 v1.3)**이다 — 본 절과 그 문서가 다르면 문서가 우선한다.
> 프롬프트 파일은 둘로 분리: `core/prompts.py`(대화·보미) / `core/prompts_analysis.py`(추출·분류·리포트 — HCX-007).
> 또한 §3·§6의 "SSE 스트리밍 중계"는 **통짜 응답 + ai_turn 말풍선 페이싱**(TTS 싱크)으로 대체되었고,
> 추출 결과는 패널뿐 아니라 **[어르신 상황 메모]로 다음 턴 대화 프롬프트에 환류**된다(페르소나 §9).

| 가드레일 (요구사항 §6) | 반영 위치 |
|---|---|
| 1. 진단 금지 | CHAT+EXTRACT 프롬프트("관찰만, '~로 보입니다' 단정 금지") + 리포트 헤더에 "참고용, 진단 아님" 고정 문구(코드) |
| 2. 긴급은 사람에게 | EXTRACT가 긴급 분류 → **코드 강제 규칙**: `category=="긴급" or (심각도=="높음" and 사람_개입_필요)` → `urgent_alert` push + UI 상단 고정 배너 "보호자/담당자/119 연결을 권고합니다" (LLM 판단과 무관하게 코드가 발동). 프롬프트에 "실제 연락처 생성 금지" |
| 3. 복지 정확성 | CHAT 프롬프트에 welfare.json 다이제스트 주입 + "금액·자격은 아래 자료에만 근거, 없으면 '복지로(129)·주민센터 확인' 안내" |
| 4. 프라이버시 | 코드: 이미지 바이트 즉시 폐기, 세션 인메모리·TTL 폐기(영구 저장 없음; SQLite는 RAG 빌드 로그 rag_meta.db뿐), 로그 truncate. README에 소버린 AI 한 줄 |
| 5. 위로하되 치료 아님 | CHAT 프롬프트 + 정서 높음 시 사람 연결 플래그(코드) |
| 6. 말투 | CHAT 프롬프트(존댓말·짧은 문장·쉬운 단어·질문 하나씩) + maxTokens 제한 |

프롬프트 4종: `CHAT_SYSTEM`(페르소나+가드레일+복지 다이제스트+응답 스타일), `EXTRACT_SYSTEM`(스키마+분류 기준+관찰 원칙), `OCR_EXPLAIN`(쉬운 말 설명→잘못 기재·주의점→사기 의심 판단 순서로), `REPORT_SYSTEM`(요약+후속 권고+안내 복지 목록).

## 10. welfare.json

```json
{ "기준연도": "2026", "items": [ {
    "id": "basic-pension", "이름": "기초연금",
    "대상": "...", "조건": "...", "금액": "...", "신청처": "...",
    "한줄": "만 65세 이상 소득 하위 어르신께 매달 연금 지급",
    "signals": ["저소득", "고령"], "키워드": ["연금", "생활비", "돈"]
} ] }
```

**수록 12항목**: 기초연금 / 생계급여(기초생활보장) / 의료급여 / 주거급여 / 긴급복지지원 / 노인맞춤돌봄서비스 / 에너지바우처 / 응급안전안심서비스(독거) / 노인 일자리·사회활동 지원 / 치매치료관리비 지원 / 문화누리카드 / 이동통신요금 감면.

- 작성 규칙: 실제 제도 기준으로 작성하되 **연도 명시** + 금액은 "약 OO만 원 수준" 보수적 표현 + 모든 항목 신청처에 "복지로(129)·주민센터" 포함. 6단계에서 작성.
- 매칭 로직(`welfare.py`): 추출 결과 `welfare_signals`/`matched_welfare_ids` ∪ 사용자 발화 키워드 스캔(결정적 규칙) → 패널 갱신. LLM 없이도 동작하는 폴백 확보.

## 11. 프론트엔드 설계

```
데스크톱                                          모바일
┌──────────────────────────────────────────────┐  ┌────────────────┐
│ 🌼 돌봄콜 AI   [🔊 음성 ON]      [상담 마치기] │  │ 헤더 (동일)     │
├───────────────────────────┬──────────────────┤  ├────────────────┤
│ 채팅 영역                  │ [특이사항|복지] 탭│  │ [채팅|기록] 탭  │
│  AI 말풍선(좌) 나(우)      │ 카드: 카테고리·   │  │  (탭 전환)      │
│  스트리밍 커서 표시        │ 심각도 색상       │  ├────────────────┤
│  이미지 첨부 미리보기      │ (초록/노랑/빨강)  │  │ 입력바 (동일)   │
├───────────────────────────┤ 새 카드 하이라이트│  └────────────────┘
│ [입력창___________] 🎤 📎 ➤│ 긴급 배너(빨강)   │
└───────────────────────────┴──────────────────┘
```

- **접근성**: 기본 폰트 18px+(말풍선 20px), 버튼 최소 48px, 고대비, `aria-live="polite"` 채팅 영역, 포커스 링 유지.
- **마이크 (푸시투토크)**: pointerdown 녹음 시작 → pointerup 종료. Web Audio(AudioWorklet)로 PCM 수집 → JS WAV 인코더(~40줄, 의존성 없음) → POST. 녹음 중 버튼 시각 피드백. localhost는 마이크 권한 OK(타 기기 시연 시 https 필요 — README 주의사항).
- **TTS**: `ai_message_end` 시 토글 on이면 `/tts` fetch → `Audio` 재생. 재생 중 새 응답 오면 이전 것 중지. 토글 상태 localStorage 유지.
- **첨부**: canvas로 장변 1600px 다운스케일 후 업로드(용량·프라이버시). 채팅에 썸네일 말풍선 즉시 표시.
- **WS 재연결**: 끊기면 2s 백오프 3회 재시도 + "연결이 잠시 불안정해요" 배너. 세션 id는 sessionStorage.
- **상태 관리**: 전역 `state` 객체 + 렌더 함수(프레임워크 없음). findings는 id 기반 diff로 새 카드 강조 애니메이션.

## 12. 테스트 전략

pytest (전부 MOCK_MODE 강제, `tests/conftest.py`에서 env 오버라이드):
(설계 시점 시드 목록 — 실제 스위트는 매트릭스·기능계약·안전·RAG 테스트까지 확장되어 현재 **184개**):
`test_health` / `test_session_create` / `test_two_sessions_isolated`(세션 2개 동시 대화, 교차 오염 검증 — 완료 기준 3) / `test_extraction_updates_findings` / `test_urgent_triggers_alert` / `test_ocr_flow` / `test_stt_endpoint` / `test_tts_returns_audio` / `test_welfare_matching` / `test_report_generated`.

수동 데모 체크(7단계에서 DEMO_SCENARIO.md로 정리): 선인사→텍스트 티키타카→"잠을 못 자요"(건강 카드)→"혼자 살아서 외로워"(정서+복지 매칭)→스미싱 캡처 첨부(사기 카드)→마이크 발화→음성 토글→상담 마치기(리포트 모달). 브라우저 2개(일반+시크릿) 동시 진행해 격리 확인.

## 13. 단계별 실행 계획 (각 단계 = 구현→검증→commit)

| 단계 | 내용 | 완료 기준 |
|---|---|---|
| **0. 골격** | venv·requirements·app 트리·config·SessionStore(TTL)·/health·정적 placeholder·pytest 스캐폴드·run.py | uvicorn 기동, /health OK, pytest 2개 green |
| **1. 채팅 루프** | WS·세션 생성·선인사 템플릿·mock LLM 스트리밍·[문서확인] 후 clova_llm 실구현·프론트 채팅 UI | mock으로 브라우저 대화 성립. 키 있으면 실 LLM 검증 |
| **2. 추출+패널** | extraction 파이프라인·findings WS·사이드 패널(심각도 색·긴급 배너)·mock 추출 | "잠 못 자요" 입력 → 건강 카드 실시간 표시, 긴급 배너 코드 규칙 동작 |
| **3. OCR** | 업로드→OCR→설명 응답→추출 반영. 이미지 즉시 폐기 | mock: 고지서 설명 답변+카드 반영. 실키 시 실OCR 검증 |
| **4. STT** | audio 엔드포인트·프론트 녹음(WAV 인코딩)·mock STT | 마이크 홀드→말풍선 등록→AI 응답 |
| **5. TTS** | tts 엔드포인트·캐시·자동재생·토글 | 응답마다 음성 재생, 토글 동작 |
| **6. 복지+리포트** | welfare.json 12항목 작성·매칭·복지 패널·종료 리포트 모달 | 자격 신호 발화→복지 패널 갱신, 종료→리포트 |
| **7. 폴리시** | 접근성·모바일 탭·에러 UX·README 완성·DEMO_SCENARIO.md·2브라우저 격리 최종 확인 | 요구사항 §8 완료 기준 5개 전부 충족 |
| 스트레치 | 스트리밍 STT, 임베딩 RAG, Papago, 인지 스크리닝 리포트 | 시간 남을 때만 |

커밋 메시지: `stage N: <요약>` 형식.

## 14. NCP 문서 검증 목록 (구현 직전 fetch)

| 대상 | URL 후보 (404면 해당 키워드로 검색) |
|---|---|
| CLOVA Studio Chat Completions v3 / 모델·파라미터 | `https://api.ncloud-docs.com/docs/clovastudio-chatcompletionsv3` · guide.ncloud-docs.com "CLOVA Studio" |
| CLOVA Studio Structured Output·thinking 제약 | guide.ncloud-docs.com CLOVA Studio 사용 가이드 내 해당 항목 |
| CSR (STT) | `https://api.ncloud-docs.com/docs/ai-naver-clovaspeechrecognition-stt` |
| CLOVA Voice Premium (TTS) | `https://api.ncloud-docs.com/docs/ai-naver-clovavoice-ttspremium` |
| CLOVA OCR General | `https://api.ncloud-docs.com/docs/ocr-ocr` · guide "CLOVA OCR API" |

확인 항목: 엔드포인트·인증 헤더·요청/응답 스키마·모델/보이스 목록·스트리밍 방식·요금제 무관 제한(QPM·파일 크기·오디오 길이).

## 15. 리스크와 대응

| 리스크 | 대응 |
|---|---|
| 서브 계정에 특정 서비스 권한 없음 | 해당 provider mock 유지 + README에 표기. 사용자에게 관리자 문의 안내 |
| CLOVA Studio 이용신청 승인 지연 | mock으로 전 단계 진행 (설계상 무손실) |
| Structured Output×thinking 동시 제약 | §3 폴백 체인 (JSON 프롬프트+파서) |
| HCX-007 응답 지연 | thinking 최소화 → 그래도 느리면 채팅만 하위 모델, 추출은 상위 유지 |
| CSR 포맷/60초 제한 | WAV 16k 인코딩 설계로 회피 + 녹음 최대 30초 컷 |
| Python 3.14 휠 미지원 패키지 | 최신 버전 설치. 실패 시 uvicorn[standard]→uvicorn 등 경량 대체 |
| Tailwind CDN 오프라인 | 핵심 레이아웃만 유지되는 인라인 fallback CSS 소량 |
| 시연 기기에서 마이크 권한 | localhost OK. 원격 시연 필요 시 별도 논의(범위 외) |
