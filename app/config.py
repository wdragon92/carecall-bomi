"""애플리케이션 설정. .env → pydantic-settings 단일 로딩 지점 (config §4)."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # 실행 모드
    mock_mode: bool = True

    # CLOVA Studio (LLM)
    clova_studio_api_key: str = ""

    # AI·NAVER API (STT: CSR / TTS: CLOVA Voice Premium)
    ncp_apigw_client_id: str = ""
    ncp_apigw_client_secret: str = ""

    # CLOVA OCR
    clova_ocr_invoke_url: str = ""
    clova_ocr_secret: str = ""

    # ncloud CLI용 메인 키 (앱 런타임에는 미사용)
    ncp_access_key: str = ""
    ncp_secret_key: str = ""
    ncp_region: str = "KR"

    # RAG — 복지 검색 (docs/돌봄콜_RAG_파이프라인_계획_v2.md)
    rag_enabled: bool = True
    rag_data_dir: str = "data"
    rag_top_k: int = 4
    rag_pool: int = 20
    # 거부 게이트(2단, P4 실측): top ≥ high(고신뢰) OR (top ≥ low AND bm25 ≥ evidence)
    rag_score_threshold: float = 0.40        # low — 실 임베딩 하한 (in-domain 최저 0.413)
    rag_score_threshold_high: float = 0.50   # high — 어휘 증거 없이도 통과하는 고신뢰선
    rag_score_threshold_mock: float = 0.06   # 목 n-gram low (실측 in 0.064~/out ~0.054)
    rag_score_threshold_mock_high: float = 0.12  # 목 high
    rag_bm25_evidence: float = 4.0           # 어휘 증거 하한 (in 4.10~10.96 / out 대부분 <4)
    rag_rewrite: bool = False               # LLM 질문 재작성(실모드 전용, 기본 off — 지연 1콜 추가)

    # 공공데이터포털 — 서비스(중앙부처/지자체)별로 키·엔드포인트 한 벌씩.
    # 계정 공용 키(포털 정책상 두 페이지 키가 같은 값)라면 같은 값을 두 칸에 넣으면 된다.
    # URL은 실경로 검증 완료(2026-07-06, 무키 401 / 오경로 404·500 대조). 응답 포맷 XML.
    welfare_central_api_key: str = ""  # 중앙부처복지서비스(15090532) Decoding 키
    welfare_local_api_key: str = ""    # 지자체복지서비스(15108347) Decoding 키
    welfare_api_key: str = ""          # (공용 폴백 — 서비스별 키가 비어 있으면 이 값 사용)
    welfare_central_list_url: str = (
        "https://apis.data.go.kr/B554287/NationalWelfareInformationsV001/NationalWelfarelistV001"
    )
    welfare_central_detail_url: str = (
        "https://apis.data.go.kr/B554287/NationalWelfareInformationsV001/NationalWelfaredetailedV001"
    )
    welfare_local_list_url: str = (
        "https://apis.data.go.kr/B554287/LocalGovernmentWelfareInformations/LcgvWelfarelist"
    )
    welfare_local_detail_url: str = (
        "https://apis.data.go.kr/B554287/LocalGovernmentWelfareInformations/LcgvWelfaredetailed"
    )

    def welfare_key(self, scope: str) -> str:
        """scope: 'central' | 'local'. 서비스별 키 우선, 없으면 공용 키."""
        specific = self.welfare_central_api_key if scope == "central" else self.welfare_local_api_key
        return (specific or self.welfare_api_key).strip()

    # 앱 파라미터
    app_host: str = "127.0.0.1"
    app_port: int = 8080  # 8000은 Windows 예약대역(7902-8001)에 걸려 바인딩 실패할 수 있음
    clova_llm_model: str = "HCX-007"   # 분석(추출/리포트)용 — reasoning
    clova_chat_model: str = "HCX-005"  # 채팅용 — 빠른 스트리밍
    clova_tts_voice: str = "vmikyung"
    clova_tts_speed: int = 0  # -5(빠름)~10(느림), 0=기본
    max_upload_mb: int = 5
    session_ttl_min: int = 120
    log_level: str = "INFO"

    # ---- provider별 키 존재 판정 (real/mock 결정에 사용) ----
    def llm_available(self) -> bool:
        return bool(self.clova_studio_api_key.strip())

    def stt_available(self) -> bool:
        return bool(self.ncp_apigw_client_id.strip() and self.ncp_apigw_client_secret.strip())

    def tts_available(self) -> bool:
        return bool(self.ncp_apigw_client_id.strip() and self.ncp_apigw_client_secret.strip())

    def ocr_available(self) -> bool:
        return bool(self.clova_ocr_invoke_url.strip() and self.clova_ocr_secret.strip())

    def rag_threshold(self, embed_mode: str) -> float:
        """게이트 하한(low) — 임베딩 종류(real/mock)에 따라 분포가 달라 분리."""
        return self.rag_score_threshold if embed_mode == "real" else self.rag_score_threshold_mock

    def rag_threshold_high(self, embed_mode: str) -> float:
        """게이트 고신뢰선(high) — 어휘 증거 없이도 접지."""
        return (
            self.rag_score_threshold_high if embed_mode == "real"
            else self.rag_score_threshold_mock_high
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
