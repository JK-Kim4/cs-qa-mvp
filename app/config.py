"""환경변수 설정. 키는 코드나 저장소에 두지 않고 실행 시 환경변수(또는 gitignore 된 .env)로만 받는다."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# 이미 export 된 환경변수가 .env 보다 우선한다.
load_dotenv(ROOT / ".env", override=False)


def _float(name, default):
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


def _int(name, default):
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    wiki_dir: Path
    # Claude
    anthropic_api_key: str | None
    llm_model: str
    llm_effort: str
    rewrite_model: str
    # Microsoft-Decision-1 (Microsoft Foundry)
    decision_endpoint: str | None
    decision_deployment: str | None
    decision_api_key: str | None
    # 검색/라우팅
    top_k: int
    min_confidence: float
    direct_threshold: float | None
    second_page_min: float

    @property
    def llm_enabled(self):
        return bool(self.anthropic_api_key)

    @property
    def decision_enabled(self):
        return bool(self.decision_endpoint and self.decision_deployment and self.decision_api_key)


def load_settings():
    direct = os.getenv("DIRECT_ANSWER_THRESHOLD")
    llm_model = os.getenv("LLM_MODEL", "claude-opus-5-5")
    return Settings(
        wiki_dir=Path(os.getenv("WIKI_DIR", ROOT / "wiki")),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
        llm_model=llm_model,
        llm_effort=os.getenv("LLM_EFFORT", "medium"),
        rewrite_model=os.getenv("REWRITE_MODEL", llm_model),
        decision_endpoint=(os.getenv("DECISION_ENDPOINT") or "").rstrip("/") or None,
        decision_deployment=os.getenv("DECISION_DEPLOYMENT") or None,
        decision_api_key=os.getenv("DECISION_API_KEY") or None,
        top_k=_int("SEARCH_TOP_K", 8),
        min_confidence=_float("MIN_CONFIDENCE", 0.5),
        direct_threshold=float(direct) if direct else None,
        second_page_min=_float("SECOND_PAGE_MIN", 0.2),
    )
