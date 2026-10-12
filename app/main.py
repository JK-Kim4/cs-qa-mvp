from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import ROOT, load_settings
from .pipeline import Pipeline

settings = load_settings()
pipeline = Pipeline(settings)
app = FastAPI(title="CS QA MVP")


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)


@app.get("/api/status")
def status():
    return pipeline.status()


# def(동기)로 두면 FastAPI 가 스레드풀에서 돌려서 느린 API 호출이 서버 전체를 막지 않는다.
@app.post("/api/chat")
def chat(req: ChatRequest):
    question = req.question.strip()
    if not question:
        raise HTTPException(400, "질문이 비어 있습니다.")
    return pipeline.run(question)


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
