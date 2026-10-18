"""상담 종료 시 대화를 노션 CS 처리 보드의 카드 한 장으로 만든다.

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

STAGE_REGISTERED = "등록"
CHANNEL = "서비스 채팅"  # 보드에 없던 선택지. 첫 카드를 만들 때 노션이 자동으로 추가한다.
NOTION_TEXT_LIMIT = 2000  # rich_text 항목 하나의 최대 글자 수
NOTION_MAX_BLOCKS = 100  # 페이지 생성 요청 한 번에 넣을 수 있는 블록 수
ROUTE_LABEL = {"generate": "위키 근거 답변", "direct": "위키 대표 답변", "escalate": "상담원 연결 안내"}
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


def _message_block(m):
    return _block("paragraph", _texts(_label(m), bold=True) + _texts(m["text"]))


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

    def create_page(self, page):
        """페이지를 만들고 {cs_id, url, page_id} 를 돌려준다. CS 번호는 노션이 매긴 값을 읽는다."""
        try:
            resp = self.http.post("/v1/pages", json=page, headers=self._headers())
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
        uid = data["properties"]["CS 번호"]["unique_id"]
        return {"cs_id": f"{uid['prefix']}-{uid['number']}", "url": data["url"], "page_id": data["id"]}


def build_page(data_source_id, messages, info):
    """대화(messages)와 분류 결과(info)로 노션 페이지 생성 요청 본문을 만든다."""
    head = [
        _block("heading_2", _texts("상담 정리")),
        _block("paragraph", _texts(info["detail"])),
        _block("heading_2", _texts("대화 기록")),
    ]
    body = [_message_block(m) for m in messages]
    if len(head) + len(body) > NOTION_MAX_BLOCKS:
        keep = NOTION_MAX_BLOCKS - len(head) - 1
        note = f"(대화가 길어 이후 {len(body) - keep}개 메시지는 생략했습니다.)"
        body = body[:keep] + [_block("paragraph", _texts(note))]
    return {
        "parent": {"type": "data_source_id", "data_source_id": data_source_id},
        "properties": {
            "CS 요약": {"title": [{"text": {"content": info["summary"]}}]},
            "단계": {"status": {"name": STAGE_REGISTERED}},
            "우선순위": {"select": {"name": info["priority"]}},
            "카테고리": {"select": {"name": info["category"]}},
            "접수 채널": {"select": {"name": CHANNEL}},
        },
        "children": head + body,
    }
