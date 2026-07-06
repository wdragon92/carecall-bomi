"""어르신 적합성 필터(senior)와 카드 링크 폴백 체인, 제안 수락 흐름의 결정적 검증."""
from types import SimpleNamespace

from app.core import welfare
from app.core.conversation import _accepts_offer, _offered_service, _situation_memo
from app.rag.cards import BOKJIRO_HOME, card_url, fixture_cards
from app.rag.schema import DocChunk
from app.rag.senior import chunk_senior_relevant, senior_relevant


# ---- 연령 필터 ----
def test_senior_keeps_elderly_and_universal():
    assert senior_relevant("기초연금", "만 65세 이상 소득 하위 70%")
    assert senior_relevant("노인맞춤돌봄서비스", "")
    assert senior_relevant("긴급복지 생계지원", "갑작스러운 위기로 생계가 곤란한 가구")  # 전연령 유지
    # 가족상담 도메인은 '조손' 한 단어로 통과 금지 (온가족보듬사업 실사례)
    assert not senior_relevant("온가족보듬사업", "한부모‧조손가족, 1인가구, 다문화가족 등 위기가족")
    assert not senior_relevant("조손가족 지원", "조손가족의 아동 양육 지원")  # 가족 도메인으로 분류


def test_senior_drops_youth_and_worker():
    assert not senior_relevant("청년 월세 한시 특별지원", "만 19세~34세 청년")
    assert not senior_relevant("주거안정 월세대출", "무주택 세대주")  # 금융상품(대출)
    assert not senior_relevant("근로·자녀장려금", "일하는 저소득 가구")
    assert not senior_relevant("자활근로(기초, 차상위)", "근로능력 있는 수급자")


def test_senior_hard_excludes_sanjae_over_yoyang():
    # '요양급여'의 요양 표지에 걸려 살아남던 산재 제도 — 하드 배제가 이긴다
    assert not senior_relevant("요양급여(보조기)-산재보험급여", "산재근로자를 지원합니다")
    assert not senior_relevant("산재근로자 사회심리재활지원", "요양하고 있는 산재노동자")


def test_senior_age_cap_in_target():
    assert not senior_relevant("어떤 지원", "만 39세 이하 대상")
    assert senior_relevant("어떤 지원", "만 65세 이상 대상")


def test_chunk_filter_defaults_keep():
    assert chunk_senior_relevant(DocChunk(text="x", source="s", fields=None))


def test_senior_drops_other_target_domains():
    """실사례: '밥을 잘 못 먹어' → 문경시 재가장애인 밑반찬지원 매칭 사고.
    장애인·임산부·다문화 등 전용 대상 도메인은 노인 표지 없으면 배제."""
    assert not senior_relevant("재가장애인 밑반찬지원사업", "재가장애인을 지원합니다")
    assert not senior_relevant("임산부 영양제 지원", "")
    assert not senior_relevant("다문화가족 정착 지원", "결혼이민자")
    # 노인·장애인 겸용은 유지(노인 표지 우선)
    assert senior_relevant("독거노인·장애인 응급안전안심서비스", "")
    # 전연령 저소득은 유지
    assert senior_relevant("저소득층 밑반찬 지원사업", "저소득 가구")


def test_senior_tag_category_exclusion():
    """XML 대상특성 태그(관련어)에 전용 카테고리만 있으면 배제 — 단 검색 키워드의
    일반어('보증금')는 태그 배제에 걸리면 안 됨(주거급여 오배제 실사례)."""
    disabled = DocChunk(
        text="서비스명: 이동지원 바우처\n지원대상: 등록자\n관련어: 장애인",
        source="s", fields={"서비스명": "이동지원 바우처", "지원대상": "등록자"},
    )
    assert not chunk_senior_relevant(disabled)
    housing = DocChunk(
        text="서비스명: 주거급여\n지원대상: 중위소득 48% 이하 가구\n관련어: 월세 집세 전세 주거 수리 보증금",
        source="s", fields={"서비스명": "주거급여", "지원대상": "중위소득 48% 이하 가구"},
    )
    assert chunk_senior_relevant(housing)
    tagged_senior = DocChunk(
        text="서비스명: 무릎수술 지원\n지원대상: 등록자\n관련어: 노년 장애인",
        source="s", fields={"서비스명": "무릎수술 지원", "지원대상": "등록자"},
    )
    assert chunk_senior_relevant(tagged_senior)  # 노년 공존 태그는 유지


def test_region_gate_default_daegu():
    from app.rag.search import region_ok

    mun_gyeong = DocChunk(text="", source="s", fields={"지역": "경상북도 문경시"})
    daegu = DocChunk(text="", source="s", fields={"지역": "대구광역시 달서구"})
    national = DocChunk(text="", source="s", fields={})
    q = "요즘 밥을 잘 못 먹어"
    assert not region_ok(mun_gyeong, "대구", q)  # 타 지역 지자체 — 기본 차단
    assert region_ok(daegu, "대구", q)
    assert region_ok(national, "대구", q)  # 중앙부처(전국)
    assert region_ok(mun_gyeong, "대구", "문경 사는 동생 얘긴데")  # 지역 직접 언급 시 허용
    assert region_ok(mun_gyeong, "대구", "경북에도 이런 게 있나")  # 광역 약칭


# ---- 링크 폴백 체인 ----
def test_card_url_chain():
    assert card_url(DocChunk(text="", source="", url="https://a.b/c")) == "https://a.b/c"
    wlf = card_url(DocChunk(text="", source="", serv_id="WLF00000102"))
    assert wlf.startswith("https://www.bokjiro.go.kr") and "WLF00000102" in wlf
    assert card_url(DocChunk(text="", source="", serv_id="fixture-x")) == BOKJIRO_HOME


def test_fixture_cards_carry_links():
    for c in fixture_cards():
        assert c.url.startswith("https://www.bokjiro.go.kr"), c.serv_id


def test_panel_items_carry_links():
    items = welfare.by_ids(["basic-pension", "care-service"])
    assert len(items) == 2
    for it in items:
        assert it["url"].startswith("https://www.bokjiro.go.kr")
    matched = welfare.match([], "무릎이 아파서 장 보러 가기가 힘들어요 돌봄")
    assert all(m["url"] for m in matched)


# ---- 제안 수락 흐름 (HCX-007 감지 → 보미 제안 → "응" → 근거 안내) ----
def _sess(ai_text: str, matched=None, cards=None):
    return SimpleNamespace(
        messages=[SimpleNamespace(role="assistant", text=ai_text)],
        welfare_matched=list(matched or []),
        welfare_cards=dict(cards or {}),
        slots={},
        findings=[],
    )


def test_accepts_offer():
    assert _accepts_offer("응")
    assert _accepts_offer("그래 알려줘")
    assert _accepts_offer("궁금하네")
    assert not _accepts_offer("아니 괜찮아")
    assert not _accepts_offer("우리 손주가 어제 왔다 갔어")  # 길이 초과·무관 발화


def test_offered_service_from_last_ai_message():
    sess = _sess("어르신, 혹시 '노인맞춤돌봄서비스'라고 들어보셨어요? 도움되는 제도가 있는데 알려드릴까요?")
    assert _offered_service(sess) == "노인맞춤돌봄서비스"
    # LLM 표현 변주: "~ 알려드릴게요"도 제안으로 인식 (실측)
    sess_b = _sess("'의료급여'라는 제도가 있어요. 필요하시다면 더 자세히 알려드릴게요.")
    assert _offered_service(sess_b) == "의료급여"
    # 카드 말풍선이 마지막이어도 직전 발화의 제안을 찾는다
    sess_c = _sess("'의료급여'라는 제도가 있어요. 자세히 알려드릴까요?")
    sess_c.messages.append(SimpleNamespace(role="assistant", text="📌 의료급여\n· 대상: ...", kind="card"))
    assert _offered_service(sess_c) == "의료급여"
    # 제안 문형이 아니면 None
    assert _offered_service(_sess("오늘 날씨가 참 좋네요.")) is None


def test_fraud_sent_action_card_via_ws(rag_client):
    """송금 완료 사기 정황 → 같은 턴에 결정적 행동 카드(112·지급정지·1332) 보장."""
    import json as _json

    sid = rag_client.post("/api/sessions").json()["session_id"]
    with rag_client.websocket_connect(f"/ws/{sid}") as ws:
        assert ws.receive_json()["type"] == "session_ready"
        for _ in range(10):
            if ws.receive_json()["type"] == "ai_turn":
                break
        ws.send_text(_json.dumps({"type": "user_message", "text": "보이스피싱인 것 같은데 아까 돈을 보냈어", "via": "text"}))
        bubbles = []
        for _ in range(20):
            m = ws.receive_json()
            if m.get("type") == "ai_turn":
                bubbles = m["bubbles"]
                break
        cards = [b for b in bubbles if b.get("kind") == "card"]
        assert cards, bubbles
        joined = " ".join(b["text"] for b in cards)
        assert "112" in joined and "지급정지" in joined and "1332" in joined


def test_offer_window_survives_decline_turn():
    """"아니 됐어" 거절 후 "아까 그거 알려줘" — 제안 참조가 사용자 2턴까지 유효 (D5 실측)."""
    from app.core.conversation import _last_offer_text, _normalize_utterance

    sess = SimpleNamespace(messages=[
        SimpleNamespace(role="user", text="병원비가 부담스러워", kind="text"),
        SimpleNamespace(role="assistant", text="'의료급여'라는 제도가 있어요. 자세히 알려드릴까요?", kind="text"),
        SimpleNamespace(role="user", text="아니 됐어, 괜찮아", kind="text"),
        SimpleNamespace(role="assistant", text="알겠어요, 언제든 말씀해 주세요.", kind="text"),
        SimpleNamespace(role="user", text="근데 아까 그거 그래도 한번 알려줘 봐", kind="text"),
    ], welfare_matched=[], welfare_cards={}, slots={}, findings=[])
    assert _last_offer_text(sess) is not None and "의료급여" in _last_offer_text(sess)

    # 사용자 3턴 이상 지나면 만료
    sess.messages += [
        SimpleNamespace(role="assistant", text="네.", kind="text"),
        SimpleNamespace(role="user", text="날씨 얘기나 하자", kind="text"),
        SimpleNamespace(role="assistant", text="좋아요.", kind="text"),
        SimpleNamespace(role="user", text="응", kind="text"),
    ]
    assert _last_offer_text(sess) is None

    # STT 오전사 정규화
    assert "기초연금" in _normalize_utterance("기소연금 나도 받을 수 있나?")


def test_situation_memo_mentions_offer_hint():
    sess = _sess("네, 듣고 있어요.", matched=["care-service"])
    memo = _situation_memo(sess)
    assert "노인맞춤돌봄서비스" in memo and "알려드릴까요" in memo
    # 이미 안내한 복지는 제안 힌트에서 빠진다
    sess2 = _sess("네.", matched=["care-service"],
                  cards={"x": {"이름": "노인맞춤돌봄서비스"}})
    assert "제안 힌트" not in _situation_memo(sess2)
