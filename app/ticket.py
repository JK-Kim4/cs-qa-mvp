"""상담 한 건을 노션 CS 처리 보드의 카드 한 장으로 기록한다.

첫 답변이 끝나면 '상담 중' 카드를 만들고(LLM 없음), 이후 대화는 카드 끝에 덧붙인다.
상담이 끝나면(상담 종료 / 상담원 연결 / 담당자 처리 요청) LLM 으로 요약·분류해 속성을 채우고 '등록'으로 넘긴다.

속성 이름과 선택지는 노션 보드(테스트 칸반 > CS 처리 보드)와 정확히 같아야 한다.
선택형 속성에 보드에 없는 이름을 쓰면 노션이 그 이름으로 새 선택지를 만들어 버린다.
"""
import json
import re

import httpx

from .llm import LLMError

NOTION_API = "https://api.notion.com"
# data_source_id 를 부모로 페이지를 만들려면 데이터 소스를 지원하는 API 버전이어야 한다.
NOTION_VERSION = "2025-09-03"

STAGE_CHATTING = "상담 중"  # 고객과 대화가 진행 중인 카드. 크루가 처리할 대상이 아니다.
STAGE_REGISTERED = "등록"
CHANNEL = "서비스 채팅"  # 보드에 없던 선택지. 첫 카드를 만들 때 노션이 자동으로 추가한다.
NOTION_TEXT_LIMIT = 2000  # rich_text 항목 하나의 최대 글자 수
NOTION_MAX_BLOCKS = 100  # 요청 한 번에 넣을 수 있는 블록 수
ROUTE_LABEL = {"generate": "위키 근거 답변", "direct": "위키 대표 답변", "escalate": "상담원 연결 안내",
               "handoff": "담당자 전달"}
CLOSE_REASON = {"end": "고객이 상담 종료", "escalate": "상담원 연결 (맞는 위키 없음)", "handoff": "담당자 처리가 필요한 요청"}
CATEGORIES = ("사용법 문의", "버그·오류", "원고·데이터", "계정·로그인", "기기·브라우저",
              "AI 리뷰·표지", "커뮤니티·공유·신고", "기능 제안", "기타")
# 기준 문구는 노션 보드 페이지의 '우선순위 기준'을 그대로 옮겼다.
PRIORITIES = {
    "긴급": "원고 유실, 로그인 불가처럼 쓰기 자체가 막힘",
    "높음": "주요 기능 오류지만 우회 방법이 있음",
    "보통": "일반 문의·경미한 오류",
    "낮음": "제안, 문구·디자인 의견",
}
FALLBACK_CATEGORY = "기타"
FALLBACK_PRIORITY = "보통"
SUMMARY_MAX = 80

TICKET_SYSTEM = f"""당신은 고객센터 운영 크루를 돕는 분류 담당자입니다. 고객과 자동 응답 챗봇의 대화를 읽고 CS 티켓 정보를 JSON 하나로만 출력합니다.

출력 형식: {{"summary": "...", "category": "...", "priority": "...", "detail": "..."}}
- summary: 크루가 목록에서 보고 바로 알 수 있는 한국어 한 줄 요약 ({SUMMARY_MAX}자 이내)
- category: 다음 중 정확히 하나. {", ".join(CATEGORIES)}
- priority: 다음 중 정확히 하나.
{chr(10).join(f"  {k}: {v}" for k, v in PRIORITIES.items())}
- detail: 고객이 원하는 것, 챗봇이 안내한 것, 크루가 이어서 확인할 것을 한국어 2~4문장으로
JSON 외의 설명은 쓰지 않습니다."""


def _texts(content, bold=False):
    """노션 글자 수 제한에 맞춰 rich_text 항목 여러 개로 나눈다."""
    items = []
    for i in range(0, max(len(content), 1), NOTION_TEXT_LIMIT):
        item = {"type": "text", "text": {"content": content[i:i + NOTION_TEXT_LIMIT]}}
        if bold:
            item["annotations"] = {"bold": True}
        items.append(item)
    return items


def _block(kind, rich_text):
    return {"object": "block", "type": kind, kind: {"rich_text": rich_text}}


def _label(m):
    if m["role"] == "user":
        return "고객: "
    route = ROUTE_LABEL.get(m.get("route"))
    return f"답변 ({route}): " if route else "답변: "


def message_blocks(messages):
    return [_block("paragraph", _texts(_label(m), bold=True) + _texts(m["text"])) for m in messages]


def transcript_text(messages):
    return "\n".join(_label(m) + m["text"] for m in messages)


def classify(llm, messages):
    """LLM 으로 요약·카테고리·우선순위를 정한다. (info, warnings) 를 돌려준다.

    LLM 이 없거나 실패해도 카드는 만들어야 하므로, 첫 질문을 제목으로 한 기본값으로 대신한다.
    """
    try:
        if llm is None:
            raise LLMError("LLM 미설정")
        result = llm.summarize_ticket(TICKET_SYSTEM, transcript_text(messages))
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", result["text"].strip())
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise LLMError("LLM 응답이 JSON 이 아님") from e
        if not isinstance(data, dict):
            raise LLMError("LLM 응답이 JSON 이 아님")
    except LLMError as e:
        first = next(m["text"] for m in messages if m["role"] == "user")
        return {
            "summary": first.strip()[:SUMMARY_MAX],
            "category": FALLBACK_CATEGORY,
            "priority": FALLBACK_PRIORITY,
            "detail": "자동 분류를 하지 못해 첫 질문을 제목으로 썼습니다. 대화 기록을 확인해 주세요.",
        }, [f"자동 분류 실패: {e}"]
    info = {k: str(data.get(k) or "").strip() for k in ("summary", "category", "priority", "detail")}
    warnings = []
    if info["category"] not in CATEGORIES:
        warnings.append(f"LLM 이 보드에 없는 카테고리 '{info['category']}' 를 골라 '{FALLBACK_CATEGORY}' 로 바꿨습니다.")
        info["category"] = FALLBACK_CATEGORY
    if info["priority"] not in PRIORITIES:
        warnings.append(f"LLM 이 보드에 없는 우선순위 '{info['priority']}' 을 골라 '{FALLBACK_PRIORITY}' 으로 바꿨습니다.")
        info["priority"] = FALLBACK_PRIORITY
    return info, warnings


class NotionError(RuntimeError):
    pass


class NotionClient:
    def __init__(self, token, data_source_id, http=None, timeout=30.0):
        self.token = token
        self.data_source_id = data_source_id  # 카드를 만들 보드(CS 처리 보드의 데이터 소스)
        self.http = http or httpx.Client(base_url=NOTION_API, timeout=timeout)

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}", "Notion-Version": NOTION_VERSION}

    def _request(self, method, path, body=None):
        try:
            resp = self.http.request(method, path, json=body, headers=self._headers())
        except httpx.HTTPError as e:
            raise NotionError(f"노션 API 에 연결할 수 없습니다: {e}") from e
        data = resp.json()
        if resp.status_code >= 400:
            message = data.get("message") or resp.text[:300]
            if resp.status_code == 401:
                raise NotionError("NOTION_TOKEN 이 올바르지 않습니다.")
            if resp.status_code == 404:
                raise NotionError("노션 보드를 찾을 수 없습니다. NOTION_DATA_SOURCE_ID 와, "
                                  f"토큰의 통합이 보드 페이지에 연결됐는지 확인하세요. ({message})")
            raise NotionError(f"노션 API 오류 {resp.status_code}: {message}")
        return data

    @staticmethod
    def _card(data):
        uid = data["properties"]["CS 번호"]["unique_id"]
        return {"page_id": data["id"], "cs_id": f"{uid['prefix']}-{uid['number']}", "url": data["url"]}

    def create_page(self, page):
        """페이지를 만들고 {page_id, cs_id, url} 을 돌려준다. CS 번호는 노션이 매긴 값을 읽는다."""
        return self._card(self._request("POST", "/v1/pages", page))

    def get_board_card(self, page_id):
        """화면이 보낸 페이지 ID 가 이 보드의 살아 있는 카드인지 확인하고 현재 단계를 돌려준다."""
        data = self._request("GET", f"/v1/pages/{page_id}")
        if data.get("parent", {}).get("data_source_id") != self.data_source_id:
            raise NotionError("CS 처리 보드의 카드가 아닙니다")
        if data.get("in_trash") or data.get("archived"):
            raise NotionError("삭제된 카드입니다")
        return {**self._card(data), "stage": data["properties"]["단계"]["status"]["name"]}

    def append_blocks(self, page_id, blocks):
        for i in range(0, len(blocks), NOTION_MAX_BLOCKS):
            self._request("PATCH", f"/v1/blocks/{page_id}/children", {"children": blocks[i:i + NOTION_MAX_BLOCKS]})

    def update_properties(self, page_id, properties):
        self._request("PATCH", f"/v1/pages/{page_id}", {"properties": properties})


def build_open_page(data_source_id, messages):
    """'상담 중' 카드 생성 요청. 제목은 첫 질문이고, 대화는 만든 뒤 append_blocks 로 덧붙인다."""
    first = next(m["text"] for m in messages if m["role"] == "user")
    return {
        "parent": {"type": "data_source_id", "data_source_id": data_source_id},
        "properties": {
            "CS 요약": {"title": [{"text": {"content": first.strip()[:SUMMARY_MAX]}}]},
            "단계": {"status": {"name": STAGE_CHATTING}},
            "접수 채널": {"select": {"name": CHANNEL}},
        },
        "children": [_block("heading_2", _texts("대화 기록"))],
    }


def close_properties(info, move_to_registered):
    """상담 종료 시 덮어쓸 속성. 크루가 이미 단계를 옮긴 카드는 단계를 건드리지 않는다."""
    props = {
        "CS 요약": {"title": [{"text": {"content": info["summary"]}}]},
        "우선순위": {"select": {"name": info["priority"]}},
        "카테고리": {"select": {"name": info["category"]}},
    }
    if move_to_registered:
        props["단계"] = {"status": {"name": STAGE_REGISTERED}}
    return props


def summary_blocks(info, reason):
    return [
        _block("heading_2", _texts("상담 정리")),
        _block("paragraph", _texts(f"종료: {CLOSE_REASON[reason]}")),
        _block("paragraph", _texts(info["detail"])),
    ]


def sync_ticket(notion, page_id, messages):
    """카드가 없으면 '상담 중' 으로 만들고, 아직 기록하지 않은 메시지를 덧붙인다. LLM 을 부르지 않는다."""
    if page_id is None:
        card = notion.create_page(build_open_page(notion.data_source_id, messages))
    else:
        card = notion.get_board_card(page_id)
        card.pop("stage")
    if messages:
        notion.append_blocks(card["page_id"], message_blocks(messages))
    return card


def close_ticket(notion, llm, page_id, messages, unsynced, reason):
    """남은 메시지를 덧붙이고, 대화 전체로 요약·분류해 속성을 채운 뒤 상담 정리를 남긴다."""
    if page_id is None:
        card, stage = sync_ticket(notion, None, unsynced or messages), STAGE_CHATTING
    else:
        card = notion.get_board_card(page_id)
        stage = card.pop("stage")
        if unsynced:
            notion.append_blocks(card["page_id"], message_blocks(unsynced))
    info, warnings = classify(llm, messages)
    notion.update_properties(card["page_id"], close_properties(info, move_to_registered=stage == STAGE_CHATTING))
    notion.append_blocks(card["page_id"], summary_blocks(info, reason))
    return {**card, "summary": info["summary"], "category": info["category"], "priority": info["priority"],
            "warnings": warnings}
