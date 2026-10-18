"""상담 종료 -> 노션 티켓 생성을 검증한다. 노션 HTTP 는 httpx.MockTransport, LLM 은 가짜 객체로 바꾼다."""
import json

import httpx
import pytest

from app.llm import LLMError
from app.ticket import NOTION_VERSION, NotionClient, NotionError, build_page, classify

DS_ID = "ds-123"
MESSAGES = [
    {"role": "user", "text": "주문을 취소하고 싶어요"},
    {"role": "bot", "text": "주문 내역에서 취소할 수 있어요.", "route": "generate"},
    {"role": "user", "text": "버튼이 안 보여요"},
    {"role": "bot", "text": "담당 상담원에게 연결해 드릴게요.", "route": "escalate"},
]
INFO = {"summary": "주문 취소 버튼이 보이지 않음", "category": "버그·오류", "priority": "높음",
        "detail": "고객이 주문 취소 버튼을 찾지 못함. 자동 응답 후 상담원 연결."}


def test_page_properties_follow_board_schema():
    page = build_page(DS_ID, MESSAGES, INFO)

    assert page["parent"] == {"type": "data_source_id", "data_source_id": DS_ID}
    assert page["properties"] == {
        "CS 요약": {"title": [{"text": {"content": "주문 취소 버튼이 보이지 않음"}}]},
        "단계": {"status": {"name": "등록"}},
        "우선순위": {"select": {"name": "높음"}},
        "카테고리": {"select": {"name": "버그·오류"}},
        "접수 채널": {"select": {"name": "서비스 채팅"}},
    }


def block_texts(page):
    """children 블록을 (블록 종류, 이어 붙인 본문) 목록으로 바꾼다."""
    out = []
    for b in page["children"]:
        rich = b[b["type"]]["rich_text"]
        out.append((b["type"], "".join(r["text"]["content"] for r in rich)))
    return out


def test_page_body_has_summary_and_labeled_transcript():
    page = build_page(DS_ID, MESSAGES, INFO)

    assert block_texts(page) == [
        ("heading_2", "상담 정리"),
        ("paragraph", "고객이 주문 취소 버튼을 찾지 못함. 자동 응답 후 상담원 연결."),
        ("heading_2", "대화 기록"),
        ("paragraph", "고객: 주문을 취소하고 싶어요"),
        ("paragraph", "답변 (위키 근거 답변): 주문 내역에서 취소할 수 있어요."),
        ("paragraph", "고객: 버튼이 안 보여요"),
        ("paragraph", "답변 (상담원 연결 안내): 담당 상담원에게 연결해 드릴게요."),
    ]


def test_long_text_is_split_and_block_count_capped_for_notion_limits():
    long_text = "가" * 4500
    many = [{"role": "user", "text": long_text}] + [{"role": "user", "text": f"질문 {i}"} for i in range(150)]

    page = build_page(DS_ID, many, INFO)

    # 노션 제한: rich_text 항목 하나당 2000자, 요청 한 번에 블록 100개
    assert all(len(r["text"]["content"]) <= 2000 for b in page["children"] for r in b[b["type"]]["rich_text"])
    assert len(page["children"]) == 100
    texts = block_texts(page)
    assert texts[3] == ("paragraph", "고객: " + long_text)
    assert texts[-1] == ("paragraph", "(대화가 길어 이후 55개 메시지는 생략했습니다.)")


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
    """노션 API 자리에 끼우는 가짜 HTTP. 받은 요청을 seen 에 모은다."""
    def handler(request):
        seen.append(request)
        return httpx.Response(status, json=body)
    return httpx.Client(base_url="https://api.notion.com", transport=httpx.MockTransport(handler))


def test_notion_client_creates_page_and_returns_cs_number_and_url():
    seen = []
    created = {"object": "page", "id": "page-1", "url": "https://www.notion.so/page-1",
               "properties": {"CS 번호": {"type": "unique_id", "unique_id": {"prefix": "CS", "number": 7}}}}
    client = NotionClient("secret-token", DS_ID, http=notion_http(200, created, seen))
    page = build_page(DS_ID, MESSAGES, INFO)

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
        client.create_page(build_page(DS_ID, MESSAGES, INFO))

    assert str(e.value) == expected


def test_notion_client_reports_connection_failure():
    def handler(request):
        raise httpx.ConnectError("down")
    client = NotionClient("t", DS_ID, http=httpx.Client(base_url="https://api.notion.com", transport=httpx.MockTransport(handler)))

    with pytest.raises(NotionError) as e:
        client.create_page(build_page(DS_ID, MESSAGES, INFO))

    assert str(e.value) == "노션 API 에 연결할 수 없습니다: down"


CREATED = {"object": "page", "id": "page-9", "url": "https://www.notion.so/page-9",
           "properties": {"CS 번호": {"type": "unique_id", "unique_id": {"prefix": "CS", "number": 9}}}}


@pytest.fixture
def api(monkeypatch):
    """실제 .env 의 노션 토큰·LLM 키를 쓰지 않도록 앱의 노션 클라이언트와 LLM 을 갈아 끼운다."""
    from fastapi.testclient import TestClient

    from app import main

    seen = []
    monkeypatch.setattr(main, "notion", NotionClient("t", DS_ID, http=notion_http(200, CREATED, seen)))
    reply = {"summary": "주문 취소 버튼 미노출", "category": "버그·오류", "priority": "높음", "detail": "취소 버튼이 안 보임."}
    monkeypatch.setattr(main.pipeline, "llm", FakeLLM(json.dumps(reply, ensure_ascii=False)))
    return TestClient(main.app), seen, main


def test_ticket_api_creates_notion_card_from_conversation(api):
    client, seen, _ = api

    r = client.post("/api/tickets", json={"messages": MESSAGES})

    assert r.status_code == 200
    assert r.json() == {"cs_id": "CS-9", "url": "https://www.notion.so/page-9", "page_id": "page-9",
                        "summary": "주문 취소 버튼 미노출", "category": "버그·오류", "priority": "높음", "warnings": []}
    sent = json.loads(seen[0].content)
    assert sent["parent"]["data_source_id"] == DS_ID
    assert sent["properties"]["CS 요약"]["title"][0]["text"]["content"] == "주문 취소 버튼 미노출"


def test_ticket_api_rejects_conversation_without_customer_message(api):
    client, seen, _ = api

    r = client.post("/api/tickets", json={"messages": [{"role": "bot", "text": "안녕하세요", "route": "generate"}]})

    assert r.status_code == 422
    assert seen == []


def test_ticket_api_returns_503_when_notion_not_configured(api, monkeypatch):
    client, _, main = api
    monkeypatch.setattr(main, "notion", None)

    r = client.post("/api/tickets", json={"messages": MESSAGES})

    assert r.status_code == 503
    assert "NOTION_TOKEN" in r.json()["detail"]
    assert client.get("/api/status").json()["notion"] == {"enabled": False}


def test_ticket_api_returns_502_with_notion_message_on_notion_error(api, monkeypatch):
    client, _, main = api
    error = {"object": "error", "status": 400, "code": "validation_error", "message": "단계 is invalid."}
    monkeypatch.setattr(main, "notion", NotionClient("t", DS_ID, http=notion_http(400, error, [])))

    r = client.post("/api/tickets", json={"messages": MESSAGES})

    assert (r.status_code, r.json()["detail"]) == (502, "노션 API 오류 400: 단계 is invalid.")
