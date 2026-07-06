"""신청 지원 패키지 (v2 §4-7) — '전화하세요'가 아니라 신청까지의 거리 최소화.
보호자에게 문자/카톡으로 그대로 전달 가능한 텍스트도 만든다."""
from __future__ import annotations

DEFAULT_CHECKLIST = ["신분증", "본인 명의 통장 사본"]


def build_apply_package(fields: dict, collected_at: str = "", url: str = "") -> dict:
    checklist = list(DEFAULT_CHECKLIST)
    if fields.get("구비서류"):
        checklist.append(fields["구비서류"])
    return {
        "서비스명": fields.get("서비스명", ""),
        "신청처": fields.get("신청방법", "") or "주민센터",
        "온라인신청": url or "https://www.bokjiro.go.kr",
        "필요서류": checklist,
        "문의": fields.get("문의처", "") or "보건복지상담센터 129",
        "기준일": collected_at,
    }


def package_to_text(pkg: dict) -> str:
    """보호자 전송용/카드 표시용 텍스트."""
    lines = [f"📝 {pkg['서비스명']} 신청 준비물"]
    lines.append("· 서류: " + ", ".join(pkg["필요서류"]))
    lines.append(f"· 신청: {pkg['신청처']}")
    lines.append(f"· 온라인: {pkg['온라인신청']}")
    lines.append(f"· 문의: {pkg['문의']}")
    if pkg.get("기준일"):
        lines.append(f"· 정보 기준일: {pkg['기준일']}")
    return "\n".join(lines)
