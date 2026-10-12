"""외부 API 없이 파이프라인 라우팅을 검증한다. Claude/Decision-1 은 가짜 객체로 바꾼다."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.decision import NONE_KEY, DecisionError
from app.llm import LLMError
from app.pipeline import ESCALATE_MESSAGE, Pipeline


class FakeClaude:
    def __init__(self, fail=False):
        self.fail = fail
        self.answered_with = None

    def _r(self, text):
        return {"text": text, "model": "fake", "stop_reason": "end_turn",
                "usage": {"input_tokens": 100, "output_tokens": 10}, "cost_usd": 0.001, "latency_ms": 1}

    def rewrite_query(self, question):
        return self._r("cancel order")

    def answer(self, question, pages):
        if self.fail:
            raise LLMError("boom")
        self.answered_with = [path for path, _ in pages]
        return {**self._r("생성된 답변"), "prompt_chars": 1000}


class FakeDecision:
    """probs 를 후보 키(p1..pK, none) 기준으로 돌려준다."""

    def __init__(self, probs=None, error=False):
        self.probs = probs
        self.error = error
        self.criteria = None

    def choose(self, state, instructions, criteria):
        if self.error:
            raise DecisionError("down")
        self.criteria = criteria
        probs = {k: 0.0 for k in criteria} | self.probs
        choice = max(probs, key=probs.get)
        return choice, probs, {"state": state}, {"answers": {}, "usage": {"prompt_tokens": 1000}}


@pytest.fixture
def settings(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "DECISION_ENDPOINT", "DECISION_DEPLOYMENT", "DECISION_API_KEY", "DIRECT_ANSWER_THRESHOLD"):
        monkeypatch.setenv(k, "")
    return load_settings()


def test_no_keys_uses_bm25_top_and_canned_answer(settings):
    out = Pipeline(settings).run("I want to cancel my order")
    t = out["trace"]
    assert out["route"] == "generate"
    assert t["selected_pages"][0]["id"] == "bitext/order/cancel-order"
    assert [s["status"] for s in t["steps"]] == ["skipped", "ok", "skipped", "skipped"]
    assert out["answer_mode"] == "canned" and "{{" in out["answer"]  # 영어 위키 대표 답변 그대로


def test_korean_without_llm_warns(settings):
    out = Pipeline(settings).run("주문을 취소하고 싶어요")
    assert out["trace"]["warnings"]
    assert out["route"] == "escalate"  # 한국어 원문은 영어 위키와 매칭되지 않음


def test_korean_is_rewritten_then_generated(settings):
    claude = FakeClaude()
    out = Pipeline(settings, claude=claude).run("주문을 취소하고 싶어요")
    rewrite = out["trace"]["steps"][0]
    assert rewrite["detail"]["query"] == "cancel order"
    assert out["answer"] == "생성된 답변"
    assert claude.answered_with[0] == "wiki/bitext/order/cancel-order.md"


def test_decision_none_escalates_without_llm_call(settings):
    claude = FakeClaude()
    out = Pipeline(settings, claude=claude, decision=FakeDecision({NONE_KEY: 0.9})).run("what is the weather today")
    assert out["route"] == "escalate"
    assert out["answer"] == ESCALATE_MESSAGE
    assert claude.answered_with is None


def test_low_confidence_escalates(settings):
    out = Pipeline(settings, claude=FakeClaude(), decision=FakeDecision({"p1": 0.4, "p2": 0.35})).run("cancel my order")
    assert out["route"] == "escalate"


def test_decision_choice_and_second_page(settings):
    claude = FakeClaude()
    out = Pipeline(settings, claude=claude, decision=FakeDecision({"p2": 0.7, "p1": 0.25})).run("cancel my order")
    t = out["trace"]
    cands = t["steps"][1]["detail"]["candidates"]
    assert out["route"] == "generate"
    assert [p["id"] for p in t["selected_pages"]] == [cands[1]["id"], cands[0]["id"]]
    assert len(claude.answered_with) == 2


def test_direct_route_skips_llm(settings):
    s = replace(settings, direct_threshold=0.9)
    claude = FakeClaude()
    out = Pipeline(s, claude=claude, decision=FakeDecision({"p1": 0.95})).run("cancel my order")
    assert out["route"] == "direct"
    assert claude.answered_with is None
    assert "## " not in out["answer"]


def test_failures_fall_back(settings):
    out = Pipeline(settings, claude=FakeClaude(fail=True), decision=FakeDecision(error=True)).run("cancel my order")
    assert out["route"] == "generate"
    assert len(out["trace"]["warnings"]) == 2
    assert {s["status"] for s in out["trace"]["steps"][2:]} == {"error"}


def test_http_api(settings):
    from app.main import app

    client = TestClient(app)
    assert client.get("/api/status").json()["pages"] == 87
    r = client.post("/api/chat", json={"question": "refund please"})
    assert r.status_code == 200 and r.json()["trace"]["steps"]
    page_id = r.json()["trace"]["selected_pages"][0]["id"]
    assert client.get(f"/api/wiki/{page_id}").status_code == 200
    assert client.get("/").status_code == 200
    assert client.post("/api/chat", json={"question": ""}).status_code == 422
