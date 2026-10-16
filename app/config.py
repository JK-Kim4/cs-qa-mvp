"""환경변수 설정. 키는 코드나 저장소에 두지 않고 실행 시 환경변수(또는 gitignore 된 .env)로만 받는다."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# 이미 export 된 환경변수가 .env 보다 우선한다.
load_dotenv(ROOT / ".env", override=False)


DEFAULT_MODELS = {"openai": "gpt-5-mini", "anthropic": "claude-opus-5-5"}


def _float(name, default):
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


def _int(name, default):
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    wiki_dir: Path
    # LLM (openai | anthropic)
    llm_provider: str
    openai_api_key: str | None
    anthropic_api_key: str | None
    llm_model: str
    llm_effort: str
    rewrite_model: str
    # 판단 모델 (openrouter: TypeSafe Jev | azure: Microsoft-Decision-1)
    decision_provider: str
    decision_model: str
    openrouter_api_key: str | None
    decision_endpoint: str | None
    decision_deployment: str | None
    decision_api_key: str | None
    # 검색/라우팅
    top_k: int
    min_confidence: float
    direct_threshold: float | None
    second_page_min: float

    @property
    def llm_key_name(self):
        return "OPENAI_API_KEY" if self.llm_provider == "openai" else "ANTHROPIC_API_KEY"

    @property
    def llm_api_key(self):
        return self.openai_api_key if self.llm_provider == "openai" else self.anthropic_api_key

    @property
    def llm_enabled(self):
        return bool(self.llm_api_key)

    @property
    def decision_enabled(self):
        if self.decision_provider == "openrouter":
            return bool(self.openrouter_api_key)
        return bool(self.decision_endpoint and self.decision_deployment and self.decision_api_key)

    @property
    def decision_label(self):
        return f"Jev ({self.decision_model})" if self.decision_provider == "openrouter" else "Decision-1"


def load_settings():
    direct = os.getenv("DIRECT_ANSWER_THRESHOLD")
    provider = (os.getenv("LLM_PROVIDER") or "openai").lower()
    if provider not in DEFAULT_MODELS:
        raise ValueError(f"LLM_PROVIDER 는 {' 또는 '.join(DEFAULT_MODELS)} 여야 합니다: {provider}")
    llm_model = os.getenv("LLM_MODEL") or DEFAULT_MODELS[provider]
    openrouter_key = os.getenv("OPENROUTER_API_KEY") or None
    # 명시하지 않으면 OpenRouter 키가 있을 때 Jev, 없으면 Azure Decision-1 을 쓴다.
    decision_provider = (os.getenv("DECISION_PROVIDER") or ("openrouter" if openrouter_key else "azure")).lower()
    if decision_provider not in ("openrouter", "azure"):
        raise ValueError(f"DECISION_PROVIDER 는 openrouter 또는 azure 여야 합니다: {decision_provider}")
    return Settings(
        wiki_dir=Path(os.getenv("WIKI_DIR", ROOT / "wiki")),
        llm_provider=provider,
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
        llm_model=llm_model,
        llm_effort=os.getenv("LLM_EFFORT", "medium"),
        rewrite_model=os.getenv("REWRITE_MODEL") or llm_model,
        decision_provider=decision_provider,
        decision_model=os.getenv("DECISION_MODEL") or "typesafe/jev-1.13",
        openrouter_api_key=openrouter_key,
        decision_endpoint=(os.getenv("DECISION_ENDPOINT") or "").rstrip("/") or None,
        decision_deployment=os.getenv("DECISION_DEPLOYMENT") or None,
        decision_api_key=os.getenv("DECISION_API_KEY") or None,
        top_k=_int("SEARCH_TOP_K", 8),
        min_confidence=_float("MIN_CONFIDENCE", 0.5),
        direct_threshold=float(direct) if direct else None,
        second_page_min=_float("SECOND_PAGE_MIN", 0.2),
    )
