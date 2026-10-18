"""상담 종료 -> 노션 티켓 생성을 검증한다. 노션 HTTP 는 httpx.MockTransport, LLM 은 가짜 객체로 바꾼다."""
import json

import httpx
import pytest

from app.llm import LLMError
from app.ticket import (NOTION_VERSION, NotionClient, NotionError, build_open_page, classify, close_properties,
                        message_blocks, summary_blocks)

DS_ID = "ds-123"
MESSAGES = [
    {"role": "user", "text": "주문을 취소하고 싶어요"},
    {"role": "bot", "text": "주문 내역에서 취소할 수 있어요.", "route": "generate"},
    {"role": "user", "text": "버튼이 안 보여요"},
    {"role": "bot", "text": "담당 상담원에게 연결해 드릴게요.", "route": "escalate"},
]
INFO = {"summary": "주문 취소 버튼이 보이지 않음", "category": "버그·오류", "priority": "높음",
        "detail": "고객이 주문 취소 버튼을 찾지 못함. 자동 응답 후 상담원 연결."}


def test_open_page_starts_card_in_chatting_stage_with_first_question_as_title():
    long_first = [{"role": "user", "text": "주" * 100}] + MESSAGES

    page = build_open_page(DS_ID, long_first)

    assert page["parent"] == {"type": "data_source_id", "data_source_id": DS_ID}
    assert page["properties"] == {
        "CS 요약": {"title": [{"text": {"content": "주" * 80}}]},
        "단계": {"status": {"name": "상담 중"}},
        "접수 채널": {"select": {"name": "서비스 채팅"}},
    }
    assert block_texts(page["children"]) == [("heading_2", "대화 기록")]


def block_texts(blocks):
    """블록을 (블록 종류, 이어 붙인 본문) 목록으로 바꾼다."""
    out = []
    for b in blocks:
        rich = b[b["type"]]["rich_text"]
        out.append((b["type"], "".join(r["text"]["content"] for r in rich)))
    return out


def test_message_blocks_label_each_turn_and_split_long_text():
    long_text = "가" * 4500
    turns = MESSAGES + [{"role": "bot", "text": "담당자에게 전달했어요.", "route": "handoff"},
                        {"role": "user", "text": long_text}]

    blocks = message_blocks(turns)

    assert block_texts(blocks) == [
        ("paragraph", "고객: 주문을 취소하고 싶어요"),
        ("paragraph", "답변 (위키 근거 답변): 주문 내역에서 취소할 수 있어요."),
        ("paragraph", "고객: 버튼이 안 보여요"),
        ("paragraph", "답변 (상담원 연결 안내): 담당 상담원에게 연결해 드릴게요."),
        ("paragraph", "답변 (담당자 전달): 담당자에게 전달했어요."),
        ("paragraph", "고객: " + long_text),
    ]
    # 노션 제한: rich_text 항목 하나당 2000자
    assert all(len(r["text"]["content"]) <= 2000 for b in blocks for r in b[b["type"]]["rich_text"])


def test_close_fills_classification_and_appends_summary_with_reason():
    assert close_properties(INFO, move_to_registered=True) == {
        "CS 요약": {"title": [{"text": {"content": "주문 취소 버튼이 보이지 않음"}}]},
        "우선순위": {"select": {"name": "높음"}},
        "카테고리": {"select": {"name": "버그·오류"}},
        "단계": {"status": {"name": "등록"}},
    }
    assert "단계" not in close_properties(INFO, move_to_registered=False)
    assert block_texts(summary_blocks(INFO, "handoff")) == [
        ("heading_2", "상담 정리"),
        ("paragraph", "종료: 담당자 처리가 필요한 요청"),
        ("paragraph", "고객이 주문 취소 버튼을 찾지 못함. 자동 응답 후 상담원 연결."),
    ]


class FakeLLM:
    """요약·분류 호출만 흉내 낸다. text 로 LLM 응답 문자열을 정하고, error 면 LLMError 를 낸다."""

    def __init__(self, text="", error=False):
        self.text = text
        self.error = error
        self.transcript = None

    def summarize_ticket(self, system, transcript):
        if self.error:
            raise LLMError("boom")
        self.transcript = transcript
        return {"text": self.text, "cost_usd": 0.0001}


def test_classify_reads_llm_json_even_inside_code_fence():
    reply = {"summary": "주문 취소 버튼 미노출", "category": "버그·오류", "priority": "높음", "detail": "취소 버튼이 안 보임."}
    llm = FakeLLM("```json\n" + json.dumps(reply, ensure_ascii=False) + "\n```")

    info, warnings = classify(llm, MESSAGES)

    assert info == reply
    assert warnings == []
    assert "고객: 버튼이 안 보여요" in llm.transcript


def test_classify_maps_off_board_values_to_fallback_so_notion_gets_no_new_option():
    reply = {"summary": "환불 요청", "category": "결제", "priority": "매우 급함", "detail": "환불을 원함."}
    llm = FakeLLM(json.dumps(reply, ensure_ascii=False))

    info, warnings = classify(llm, MESSAGES)

    assert (info["category"], info["priority"]) == ("기타", "보통")
    assert warnings == ["LLM 이 보드에 없는 카테고리 '결제' 를 골라 '기타' 로 바꿨습니다.",
                        "LLM 이 보드에 없는 우선순위 '매우 급함' 을 골라 '보통' 으로 바꿨습니다."]


@pytest.mark.parametrize("llm, reason", [
    (None, "LLM 미설정"),
    (FakeLLM(error=True), "boom"),
    (FakeLLM("요약할 수 없습니다"), "LLM 응답이 JSON 이 아님"),
    (FakeLLM("[1, 2]"), "LLM 응답이 JSON 이 아님"),
])
def test_classify_falls_back_to_first_question_when_llm_unavailable(llm, reason):
    long_first = [{"role": "user", "text": "환" * 100}] + MESSAGES

    info, warnings = classify(llm, long_first)

    assert info == {"summary": "환" * 80, "category": "기타", "priority": "보통",
                    "detail": "자동 분류를 하지 못해 첫 질문을 제목으로 썼습니다. 대화 기록을 확인해 주세요."}
    assert warnings == [f"자동 분류 실패: {reason}"]


def notion_http(status, body, seen):
    """모든 요청에 같은 응답을 주는 가짜 노션 HTTP. 받은 요청을 seen 에 모은다."""
    def handler(request):
        seen.append(request)
        return httpx.Response(status, json=body)
    return httpx.Client(base_url="https://api.notion.com", transport=httpx.MockTransport(handler))


def notion_router(routes, seen):
    """(메서드, 경로) 별로 (상태, 본문) 을 돌려주는 가짜 노션 HTTP."""
    def handler(request):
        seen.append(request)
        status, body = routes[(request.method, request.url.path)]
        return httpx.Response(status, json=body)
    return httpx.Client(base_url="https://api.notion.com", transport=httpx.MockTransport(handler))


def card(page_id="page-9", stage="상담 중", data_source_id=DS_ID, in_trash=False, number=9):
    """노션 페이지 조회 응답. 부모 형태는 실제 보드 카드 응답을 따랐다."""
    return {"object": "page", "id": page_id, "url": f"https://www.notion.so/{page_id}", "in_trash": in_trash,
            "parent": {"type": "data_source_id", "data_source_id": data_source_id, "database_id": "db-1"},
            "properties": {"CS 번호": {"type": "unique_id", "unique_id": {"prefix": "CS", "number": number}},
                           "단계": {"type": "status", "status": {"name": stage}}}}


def test_notion_client_creates_page_and_returns_cs_number_and_url():
    seen = []
    created = {"object": "page", "id": "page-1", "url": "https://www.notion.so/page-1",
               "properties": {"CS 번호": {"type": "unique_id", "unique_id": {"prefix": "CS", "number": 7}}}}
    client = NotionClient("secret-token", DS_ID, http=notion_http(200, created, seen))
    page = build_open_page(DS_ID, MESSAGES)

    result = client.create_page(page)

    assert result == {"cs_id": "CS-7", "url": "https://www.notion.so/page-1", "page_id": "page-1"}
    req = seen[0]
    assert (req.method, str(req.url)) == ("POST", "https://api.notion.com/v1/pages")
    assert req.headers["authorization"] == "Bearer secret-token"
    assert req.headers["notion-version"] == NOTION_VERSION
    assert json.loads(req.content) == page


@pytest.mark.parametrize("status, message, expected", [
    (401, "API token is invalid.", "NOTION_TOKEN 이 올바르지 않습니다."),
    (404, "Could not find data_source with ID: ds-123.",
     "노션 보드를 찾을 수 없습니다. NOTION_DATA_SOURCE_ID 와, 토큰의 통합이 보드 페이지에 연결됐는지 확인하세요. (Could not find data_source with ID: ds-123.)"),
    (400, "body.properties.카테고리 is invalid.", "노션 API 오류 400: body.properties.카테고리 is invalid."),
])
def test_notion_client_turns_error_responses_into_readable_messages(status, message, expected):
    body = {"object": "error", "status": status, "code": "x", "message": message}
    client = NotionClient("t", DS_ID, http=notion_http(status, body, []))

    with pytest.raises(NotionError) as e:
        client.create_page(build_open_page(DS_ID, MESSAGES))

    assert str(e.value) == expected


def test_notion_client_reports_connection_failure():
    def handler(request):
        raise httpx.ConnectError("down")
    client = NotionClient("t", DS_ID, http=httpx.Client(base_url="https://api.notion.com", transport=httpx.MockTransport(handler)))

    with pytest.raises(NotionError) as e:
        client.create_page(build_open_page(DS_ID, MESSAGES))

    assert str(e.value) == "노션 API 에 연결할 수 없습니다: down"




def test_append_blocks_sends_at_most_100_blocks_per_request():
    seen = []
    client = NotionClient("t", DS_ID, http=notion_router({("PATCH", "/v1/blocks/page-9/children"): (200, {})}, seen))
    blocks = message_blocks([{"role": "user", "text": f"질문 {i}"} for i in range(150)])

    client.append_blocks("page-9", blocks)

    assert [len(json.loads(r.content)["children"]) for r in seen] == [100, 50]
    assert json.loads(seen[1].content)["children"] == blocks[100:]


@pytest.mark.parametrize("page, reason", [
    (card(data_source_id="other-ds"), "CS 처리 보드의 카드가 아닙니다"),
    (card(in_trash=True), "삭제된 카드입니다"),
])
def test_board_card_check_rejects_pages_outside_board(page, reason):
    client = NotionClient("t", DS_ID, http=notion_router({("GET", "/v1/pages/page-9"): (200, page)}, []))

    with pytest.raises(NotionError) as e:
        client.get_board_card("page-9")

    assert str(e.value) == reason


def test_board_card_check_returns_stage_and_cs_number():
    client = NotionClient("t", DS_ID, http=notion_router({("GET", "/v1/pages/page-9"): (200, card(stage="할당"))}, []))

    assert client.get_board_card("page-9") == {"page_id": "page-9", "cs_id": "CS-9",
                                               "url": "https://www.notion.so/page-9", "stage": "할당"}


def test_update_properties_patches_page():
    seen = []
    client = NotionClient("t", DS_ID, http=notion_router({("PATCH", "/v1/pages/page-9"): (200, card())}, seen))

    client.update_properties("page-9", close_properties(INFO, move_to_registered=True))

    assert json.loads(seen[0].content) == {"properties": close_properties(INFO, move_to_registered=True)}


REPLY = {"summary": "주문 취소 버튼 미노출", "category": "버그·오류", "priority": "높음", "detail": "취소 버튼이 안 보임."}
CLOSED_INFO = {"summary": "주문 취소 버튼 미노출", "category": "버그·오류", "priority": "높음", "detail": "취소 버튼이 안 보임."}


@pytest.fixture
def api(monkeypatch):
    """실제 .env 의 노션 토큰·LLM 키를 쓰지 않도록 앱의 노션 클라이언트와 LLM 을 갈아 끼운다.

    use(stage=...) 로 카드의 현재 단계를 정하고, 돌려받은 seen 으로 노션에 보낸 요청을 확인한다.
    """
    from fastapi.testclient import TestClient

    from app import main

    llm = FakeLLM(json.dumps(REPLY, ensure_ascii=False))
    monkeypatch.setattr(main.pipeline, "llm", llm)

    def use(stage="상담 중", page=None):
        seen = []
        routes = {
            ("POST", "/v1/pages"): (200, card()),
            ("GET", "/v1/pages/page-9"): (200, page or card(stage=stage)),
            ("PATCH", "/v1/blocks/page-9/children"): (200, {}),
            ("PATCH", "/v1/pages/page-9"): (200, card()),
        }
        monkeypatch.setattr(main, "notion", NotionClient("t", DS_ID, http=notion_router(routes, seen)))
        return seen

    return TestClient(main.app), use, llm, main


def calls(seen):
    return [(r.method, r.url.path, json.loads(r.content) if r.content else None) for r in seen]


def test_first_sync_opens_chatting_card_and_appends_conversation_without_llm(api):
    client, use, llm, _ = api
    seen = use()

    r = client.post("/api/tickets/sync", json={"page_id": None, "messages": MESSAGES[:2]})

    assert r.status_code == 200
    assert r.json() == {"page_id": "page-9", "cs_id": "CS-9", "url": "https://www.notion.so/page-9"}
    assert calls(seen) == [
        ("POST", "/v1/pages", build_open_page(DS_ID, MESSAGES[:2])),
        ("PATCH", "/v1/blocks/page-9/children", {"children": message_blocks(MESSAGES[:2])}),
    ]
    assert llm.transcript is None


def test_later_sync_checks_card_then_appends_only_new_messages(api):
    client, use, _, _ = api
    seen = use()

    r = client.post("/api/tickets/sync", json={"page_id": "page-9", "messages": MESSAGES[2:]})

    assert r.status_code == 200
    assert calls(seen) == [
        ("GET", "/v1/pages/page-9", None),
        ("PATCH", "/v1/blocks/page-9/children", {"children": message_blocks(MESSAGES[2:])}),
    ]


def test_close_appends_rest_classifies_and_moves_chatting_card_to_registered(api):
    client, use, llm, _ = api
    seen = use(stage="상담 중")

    r = client.post("/api/tickets/close", json={"page_id": "page-9", "messages": MESSAGES,
                                                "unsynced": MESSAGES[3:], "reason": "escalate"})

    assert r.status_code == 200
    assert r.json() == {"page_id": "page-9", "cs_id": "CS-9", "url": "https://www.notion.so/page-9",
                        "summary": "주문 취소 버튼 미노출", "category": "버그·오류", "priority": "높음", "warnings": []}
    assert calls(seen) == [
        ("GET", "/v1/pages/page-9", None),
        ("PATCH", "/v1/blocks/page-9/children", {"children": message_blocks(MESSAGES[3:])}),
        ("PATCH", "/v1/pages/page-9", {"properties": close_properties(CLOSED_INFO, move_to_registered=True)}),
        ("PATCH", "/v1/blocks/page-9/children", {"children": summary_blocks(CLOSED_INFO, "escalate")}),
    ]
    assert "고객: 주문을 취소하고 싶어요" in llm.transcript  # 분류는 대화 전체로 한다


def test_close_keeps_stage_when_crew_already_moved_card(api):
    client, use, _, _ = api
    seen = use(stage="할당")

    client.post("/api/tickets/close", json={"page_id": "page-9", "messages": MESSAGES, "reason": "end"})

    assert ("PATCH", "/v1/pages/page-9", {"properties": close_properties(CLOSED_INFO, move_to_registered=False)}) in calls(seen)


def test_close_without_card_opens_one_first(api):
    client, use, _, _ = api
    seen = use()

    r = client.post("/api/tickets/close", json={"page_id": None, "messages": MESSAGES, "unsynced": MESSAGES,
                                                "reason": "handoff"})

    assert r.status_code == 200
    assert [c[:2] for c in calls(seen)] == [("POST", "/v1/pages"), ("PATCH", "/v1/blocks/page-9/children"),
                                            ("PATCH", "/v1/pages/page-9"), ("PATCH", "/v1/blocks/page-9/children")]


def test_ticket_api_refuses_card_outside_board(api):
    client, use, _, _ = api
    seen = use(page=card(data_source_id="other-ds"))

    r = client.post("/api/tickets/sync", json={"page_id": "page-9", "messages": MESSAGES})

    assert (r.status_code, r.json()["detail"]) == (502, "CS 처리 보드의 카드가 아닙니다")
    assert [c[0] for c in calls(seen)] == ["GET"]


def test_ticket_api_rejects_new_card_without_customer_message(api):
    client, use, _, _ = api
    seen = use()

    r = client.post("/api/tickets/sync", json={"page_id": None, "messages": [{"role": "bot", "text": "안녕하세요"}]})

    assert r.status_code == 422
    assert seen == []


def test_ticket_api_returns_503_when_notion_not_configured(api, monkeypatch):
    client, _, _, main = api
    monkeypatch.setattr(main, "notion", None)

    r = client.post("/api/tickets/sync", json={"page_id": None, "messages": MESSAGES})

    assert r.status_code == 503
    assert "NOTION_TOKEN" in r.json()["detail"]
    assert client.get("/api/status").json()["notion"] == {"enabled": False}


def test_ticket_api_returns_502_with_notion_message_on_notion_error(api, monkeypatch):
    client, _, _, main = api
    error = {"object": "error", "status": 400, "code": "validation_error", "message": "단계 is invalid."}
    monkeypatch.setattr(main, "notion", NotionClient("t", DS_ID, http=notion_http(400, error, [])))

    r = client.post("/api/tickets/close", json={"page_id": None, "messages": MESSAGES, "reason": "end"})

    assert (r.status_code, r.json()["detail"]) == (502, "노션 API 오류 400: 단계 is invalid.")
