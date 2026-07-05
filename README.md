# 돌봄콜 AI — 노인 안부 상담 데모

독거 어르신 대상 **AI 안부 상담** 웹 데모. AI 상담원 **'봄이'**가 먼저 안부를 여쭙고 채팅하듯 대화하며,
대화 속 **특이사항(건강·정서·인지·사기 노출·복지 니즈·긴급)**을 실시간으로 추출해 옆 패널에 정리합니다.
자격이 맞는 **복지 정보**를 자연스럽게 안내하고, 서류·문자 사진을 올리면 **OCR로 읽어 쉬운 말로 설명 + 사기 판별**,
종료 시 **요약 리포트**를 제공합니다.

> 한국 복지는 '신청주의'라 알아야 받습니다. AI가 먼저 안부를 물으며 자격 맞는 복지를 알려주고 사기 예방까지 도와,
> 어르신의 정보 비대칭과 고립을 메웁니다. **모든 처리는 국내 리전 CLOVA(소버린 AI)** 로 이뤄집니다.

## 기술 스택
- **백엔드**: Python + FastAPI + WebSocket (실시간 스트리밍/패널)
- **프론트**: 단일 HTML + 순수 JS + Tailwind(CDN), 빌드 도구 없음
- **AI (NCP CLOVA)**: CLOVA Studio(HyperCLOVA X) · CLOVA Speech(STT) · CLOVA Voice(TTS) · CLOVA OCR
  - 채팅 = **HCX-005**(빠른 스트리밍), 분석(특이사항 추출·리포트) = **HCX-007**(reasoning)

## 빠른 실행
```powershell
# 1) 가상환경 + 의존성
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2) 키 설정 (아래 '키 발급' 참고). 키 없이 데모하려면 .env의 MOCK_MODE=true
Copy-Item .env.example .env   # 값 채우기

# 3) 실행
python run.py                 # → http://127.0.0.1:8080
```
브라우저로 http://127.0.0.1:8080 접속.

**모바일(같은 와이파이) 접속**: `.env`의 `APP_HOST=0.0.0.0` 상태에서 서버 실행 → 휴대폰 브라우저에서
`http://<PC의 IP>:8080` (PC IP는 `ipconfig`의 IPv4 주소). 첫 실행 때 Windows 방화벽 "허용"을 눌러 주세요.
단, **마이크(STT)는 브라우저 보안정책상 localhost/https에서만 동작** → 모바일(http)에서는 채팅·음성재생·OCR만 가능.

**음성 속도**: `.env`의 `CLOVA_TTS_SPEED` (-5 빠름 ~ 10 느림, 0 기본).
**마이크 사용법**: 🎤 한 번 눌러 말하기 시작 → 다시 눌러 전송 (최대 30초).

## 동작 모드 (MOCK_MODE)
`.env`의 `MOCK_MODE`로 제어합니다. **비용과 무관** — 키 없이도 데모가 돌아가게 하고, 테스트를 안정화하기 위한 스위치입니다.
- `MOCK_MODE=true` : 4종(LLM/STT/TTS/OCR) 전부 가짜 응답으로 전체 UX 시연.
- `MOCK_MODE=false` : 키가 있는 provider는 실제 CLOVA 연동, **개별 호출 실패 시 그 기능만 자동 mock 폴백**(데모가 끊기지 않음).

`GET /health` 로 현재 각 provider가 `real`인지 `mock`인지 확인할 수 있고, 화면 헤더에도 상태가 표시됩니다.

## 키 발급 (NCP 콘솔)
> 서브 계정 권한에 따라 일부 메뉴가 안 보일 수 있음 → 그 서비스는 mock으로 진행.

1. **CLOVA Studio**(LLM): 이용 신청 → CLOVA Studio 콘솔 → **API 키**(`nv-…`) → `CLOVA_STUDIO_API_KEY`
2. **AI·NAVER API**(STT+TTS): Application 등록 → **CSR** + **CLOVA Voice Premium** 선택 → Client ID/Secret → `NCP_APIGW_CLIENT_ID/SECRET`
3. **CLOVA OCR**: 도메인 생성(**General**) → Secret Key + **APIGW Invoke URL** → `CLOVA_OCR_SECRET`, `CLOVA_OCR_INVOKE_URL`

`.env` 항목별 설명은 [.env.example](.env.example) 참고.

## 실연동 검증 상태 (2026-07-05)
| 서비스 | 상태 | 비고 |
|--------|------|------|
| CLOVA Studio (LLM) | ✅ 실연동 확인 | 채팅 HCX-005 / 분석 HCX-007 |
| CLOVA Speech (STT) | ✅ 실연동 확인 | TTS→STT 왕복 전사 정확 |
| CLOVA Voice (TTS) | ✅ 실연동 확인 | vmikyung 프리미엄 보이스 |
| CLOVA OCR | ⚠️ endpoint 404 | invoke URL 형식은 정상 → **도메인 배포 상태/URL 재확인 필요**. 현재 mock 폴백으로 동작 |

## 테스트
```powershell
.venv\Scripts\python -m pytest      # MOCK_MODE 강제, 네트워크 불필요 (13개)
```
동시 접속 세션 격리, 특이사항 추출, 긴급 코드강제, OCR/STT/TTS/복지/리포트 플로우 커버.

## 가드레일 (필수)
진단 금지(관찰만) · 긴급은 사람에게(119/보호자 권고, 번호 조작 금지) · 복지 정확성(welfare.json 근거) ·
프라이버시(대화/이미지 비영구, 세션 종료 시 폐기) · 위로하되 치료 아님 · 어르신 말투(존댓말·짧게·하나씩).
구현 위치는 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §9 참고.

## 문서
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 실행 설계서(전체 구조·결정)
- [docs/DEMO_SCENARIO.md](docs/DEMO_SCENARIO.md) — 발표용 시연 대본
- [TEARDOWN.md](TEARDOWN.md) — 프로젝트 종료 시 철수 체크리스트
- [care-call-ai-claude-code-prompt.md](care-call-ai-claude-code-prompt.md) — 원본 요구사항
