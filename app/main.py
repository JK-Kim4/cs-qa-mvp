import logging
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import ROOT, load_settings
from .pipeline import Pipeline
from .ticket import NotionClient, NotionError, close_ticket, sync_ticket

settings = load_settings()
pipeline = Pipeline(settings)
notion = NotionClient(settings.notion_token, settings.notion_data_source_id) if settings.notion_enabled else None
app = FastAPI(title="CS QA MVP")


# Cache-Control 이 없으면 브라우저가 CSS/JS 를 임의로 캐시해, 배포 후 새 HTML 과 옛 CSS/JS 가 섞여 화면이 깨진다.
# no-cache 는 매번 서버에 확인(ETag)하게 하고, 바뀌지 않았으면 304 로 본문 없이 끝난다.
@app.middleware("http")
async def revalidate_ui(request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response

# 시작할 때 어떤 구현체로 떴는지 서버 로그에 남긴다.
_st = pipeline.status()
log = logging.getLogger("uvicorn.error")
log.info("LLM: %s", f"{settings.llm_provider} / {settings.llm_model}" if _st["llm"]["enabled"] else f"미설정 ({settings.llm_key_name} 없음)")
log.info("판단 모델: %s (%s)", _st["decision"]["label"] if _st["decision"]["enabled"] else "미설정", _st["decision"]["reason"])
log.info("노션 티켓: %s", f"사용 (보드 {settings.notion_data_source_id})" if notion else "미설정 (NOTION_TOKEN, NOTION_DATA_SOURCE_ID 필요)")


class Message(BaseModel):
    role: Literal["user", "bot"]
    text: str = Field(min_length=1, max_length=20000)
    route: str | None = None  # 답변 메시지의 라우팅 결과 (generate | direct | escalate)


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    compare: bool = False  # 비교 모드: 판단 모델 경로와 바이패스 경로를 함께 실행
    # 이번 질문 앞의 대화. 파이프라인은 이 중 최근 메시지만 쓴다 (pipeline.HISTORY_MAX_MESSAGES).
    history: list[Message] = Field(default_factory=list, max_length=50)


PAGE_ID = r"^[A-Za-z0-9-]{1,64}$"  # 노션 페이지 ID. 경로에 그대로 들어가므로 형식을 제한한다.


class TicketSync(BaseModel):
    page_id: str | None = Field(default=None, pattern=PAGE_ID)  # 없으면 '상담 중' 카드를 새로 만든다
    messages: list[Message] = Field(min_length=1, max_length=200)  # 아직 카드에 기록하지 않은 메시지


class TicketClose(BaseModel):
    page_id: str | None = Field(default=None, pattern=PAGE_ID)
    messages: list[Message] = Field(min_length=1, max_length=500)  # 상담 전체 대화 (요약·분류용)
    unsynced: list[Message] = Field(default_factory=list, max_length=200)  # 그중 카드에 아직 없는 메시지
    reason: Literal["end", "escalate", "handoff"]


@app.get("/api/status")
def status():
    return {**pipeline.status(), "notion": {"enabled": notion is not None}}


# def(동기)로 두면 FastAPI 가 스레드풀에서 돌려서 느린 API 호출이 서버 전체를 막지 않는다.
@app.post("/api/chat")
def chat(req: ChatRequest):
    question = req.question.strip()
    if not question:
        raise HTTPException(400, "질문이 비어 있습니다.")
    history = [m.model_dump() for m in req.history]
    return pipeline.compare(question, history) if req.compare else pipeline.run(question, history=history)


def _require_notion():
    if notion is None:
        raise HTTPException(503, "노션 티켓이 설정되지 않았습니다. .env 에 NOTION_TOKEN, NOTION_DATA_SOURCE_ID 를 넣고 서버를 재시작하세요.")


def _require_customer(messages):
    if not any(m["role"] == "user" for m in messages):
        raise HTTPException(422, "고객 메시지가 없는 대화로는 티켓을 만들 수 없습니다.")


@app.post("/api/tickets/sync")
def sync(req: TicketSync):
    """답변이 끝날 때마다 호출: 첫 호출이면 '상담 중' 카드를 만들고, 이후에는 새 메시지를 덧붙인다."""
    _require_notion()
    messages = [m.model_dump() for m in req.messages]
    if req.page_id is None:
        _require_customer(messages)
    try:
        return sync_ticket(notion, req.page_id, messages)
    except NotionError as e:
        raise HTTPException(502, str(e)) from e


@app.post("/api/tickets/close")
def close(req: TicketClose):
    """상담 종료 / 상담원 연결 / 담당자 처리 요청: 요약·분류해 카드를 '등록' 으로 넘긴다."""
    _require_notion()
    messages = [m.model_dump() for m in req.messages]
    _require_customer(messages)
    try:
        return close_ticket(notion, pipeline.llm, req.page_id, messages,
                            [m.model_dump() for m in req.unsynced], req.reason)
    except NotionError as e:
        raise HTTPException(502, str(e)) from e


@app.get("/api/guide")
def guide():
    """데이터셋/주제 가이드. 위키는 실행 중 바뀌지 않으므로 처음 한 번만 만든다."""
    global _guide
    if _guide is None:
        _guide = pipeline.wiki.guide()
    return _guide


_guide = None


@app.get("/api/wiki/{page_id:path}")
def wiki_page(page_id: str):
    page = pipeline.wiki.by_id.get(page_id)
    if not page:
        raise HTTPException(404, "페이지가 없습니다.")
    return {"id": page.id, "path": f"wiki/{page.path}", "title": page.title, "body": page.body(pipeline.wiki.dir)}


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")
