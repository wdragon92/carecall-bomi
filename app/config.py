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
    rag_score_threshold: float = 0.41       # 실 임베딩(bge-m3 코사인) 거부 임계값 — 실측 in 0.42~/out ~0.40, P4에서 재튜닝
    rag_score_threshold_mock: float = 0.15  # 목 n-gram 벡터는 분포가 달라 별도 임계값
    rag_rewrite: bool = False               # LLM 질문 재작성(실모드 전용, 기본 off — 지연 1콜 추가)

    # 공공데이터포털 (P0 후 채움 — 발급은 사용자, Decoding 키만)
    welfare_api_key: str = ""
    welfare_central_list_url: str = ""
    welfare_central_detail_url: str = ""
    welfare_local_list_url: str = ""

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
        """거부 판정 임계값 — 임베딩 종류(real/mock)에 따라 분포가 달라 분리."""
        return self.rag_score_threshold if embed_mode == "real" else self.rag_score_threshold_mock


@lru_cache
def get_settings() -> Settings:
    return Settings()
