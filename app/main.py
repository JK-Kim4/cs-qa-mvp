import logging

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import ROOT, load_settings
from .pipeline import Pipeline

settings = load_settings()
pipeline = Pipeline(settings)
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


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    compare: bool = False  # 비교 모드: 판단 모델 경로와 바이패스 경로를 함께 실행


@app.get("/api/status")
def status():
    return pipeline.status()


# def(동기)로 두면 FastAPI 가 스레드풀에서 돌려서 느린 API 호출이 서버 전체를 막지 않는다.
@app.post("/api/chat")
def chat(req: ChatRequest):
    question = req.question.strip()
    if not question:
        raise HTTPException(400, "질문이 비어 있습니다.")
    return pipeline.compare(question) if req.compare else pipeline.run(question)


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
