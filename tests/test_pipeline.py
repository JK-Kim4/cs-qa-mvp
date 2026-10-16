"""외부 API 없이 파이프라인 라우팅을 검증한다. LLM/Decision-1 은 가짜 객체로 바꾼다."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.decision import NONE_KEY, OPENROUTER_URL, DecisionError, _parse_answer, make_decision
from app.llm import Claude, LLMError, OpenAIChat, make_llm
from app.pipeline import ESCALATE_MESSAGE, Pipeline


class FakeLLM:
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
    for k in ("LLM_PROVIDER", "LLM_MODEL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "DECISION_PROVIDER", "DECISION_MODEL", "DECISION_ENDPOINT", "DECISION_DEPLOYMENT", "DECISION_API_KEY", "DIRECT_ANSWER_THRESHOLD"):
        monkeypatch.setenv(k, "")
    return load_settings()


def test_provider_selection(settings, monkeypatch):
    assert settings.llm_provider == "openai" and settings.llm_model == "gpt-5-mini"
    assert make_llm(settings) is None
    assert isinstance(make_llm(replace(settings, openai_api_key="sk-test")), OpenAIChat)
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    s = load_settings()
    assert s.llm_model == "claude-opus-5-5" and s.llm_key_name == "ANTHROPIC_API_KEY"
    assert isinstance(make_llm(s), Claude)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    with pytest.raises(ValueError):
        load_settings()


def test_openai_response_parsing():
    from types import SimpleNamespace as NS

    def resp(content, status="completed", reason=None):
        return NS(output=[NS(type="reasoning"), NS(type="message", content=content)],
                  output_text="".join(c.text for c in content if c.type == "output_text"),
                  status=status, incomplete_details=NS(reason=reason) if reason else None,
                  model="gpt-5-mini-2025-08-07", usage=NS(input_tokens=1000, output_tokens=100))

    llm = OpenAIChat("sk-test", "gpt-5-mini", "medium", "gpt-5-mini")
    calls = []
    llm.client = NS(responses=NS(create=lambda **kw: calls.append(kw) or queue.pop(0)))
    queue = [resp([NS(type="output_text", text=" cancel order ")]),
             resp([NS(type="refusal", refusal="no")]),
             resp([], status="incomplete", reason="max_output_tokens")]

    r = llm.rewrite_query("주문 취소")
    assert r["text"] == "cancel order" and r["stop_reason"] == "completed"
    assert r["cost_usd"] == round(1000 / 1e6 * 0.25 + 100 / 1e6 * 2.0, 6)
    assert calls[0]["reasoning"] == {"effort": "low"} and calls[0]["model"] == "gpt-5-mini"
    with pytest.raises(LLMError, match="거절"):
        llm.answer("q", [("wiki/a.md", "body")])
    with pytest.raises(LLMError, match="max_output_tokens"):
        llm.answer("q", [("wiki/a.md", "body")])


def test_decision_provider_selection(settings, monkeypatch):
    assert make_decision(settings) is None
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    s = load_settings()
    assert s.decision_provider == "openrouter" and s.decision_model == "typesafe/jev-1.13"
    d = make_decision(s)
    assert d.url == OPENROUTER_URL and d.headers["Authorization"] == "Bearer sk-or-test"
    monkeypatch.setenv("DECISION_PROVIDER", "azure")
    for k, v in {"DECISION_ENDPOINT": "https://x.services.ai.azure.com/", "DECISION_DEPLOYMENT": "Decision-1",
                 "DECISION_API_KEY": "k"}.items():
        monkeypatch.setenv(k, v)
    d = make_decision(load_settings())
    assert d.url == "https://x.services.ai.azure.com/providers/microsoft/v1/systemone" and d.headers["api-key"] == "k"


def test_parse_answer_shapes():
    criteria = {"p1": "", "p2": "", "none": ""}
    assert _parse_answer({"choice": "p1", "probabilities": {"p1": 0.8, "p2": 0.2}}, criteria) == ("p1", {"p1": 0.8, "p2": 0.2})
    assert _parse_answer({"probabilities": {"p1": 0.3, "p2": 0.7}}, criteria)[0] == "p2"
    assert _parse_answer([{"scores": [{"option": "none", "score": 0.9}, {"option": "p1", "score": 0.1}]}], criteria)[0] == "none"


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
    llm = FakeLLM()
    out = Pipeline(settings, llm=llm).run("주문을 취소하고 싶어요")
    rewrite = out["trace"]["steps"][0]
    assert rewrite["detail"]["query"] == "cancel order"
    assert out["answer"] == "생성된 답변"
    assert llm.answered_with[0] == "wiki/bitext/order/cancel-order.md"


def test_decision_none_escalates_without_llm_call(settings):
    llm = FakeLLM()
    out = Pipeline(settings, llm=llm, decision=FakeDecision({NONE_KEY: 0.9})).run("what is the weather today")
    assert out["route"] == "escalate"
    assert out["answer"] == ESCALATE_MESSAGE
    assert llm.answered_with is None


def test_low_confidence_escalates(settings):
    out = Pipeline(settings, llm=FakeLLM(), decision=FakeDecision({"p1": 0.4, "p2": 0.35})).run("cancel my order")
    assert out["route"] == "escalate"


def test_decision_choice_and_second_page(settings):
    llm = FakeLLM()
    out = Pipeline(settings, llm=llm, decision=FakeDecision({"p2": 0.7, "p1": 0.25})).run("cancel my order")
    t = out["trace"]
    cands = t["steps"][1]["detail"]["candidates"]
    assert out["route"] == "generate"
    assert [p["id"] for p in t["selected_pages"]] == [cands[1]["id"], cands[0]["id"]]
    assert len(llm.answered_with) == 2


def test_direct_route_skips_llm(settings):
    s = replace(settings, direct_threshold=0.9)
    llm = FakeLLM()
    out = Pipeline(s, llm=llm, decision=FakeDecision({"p1": 0.95})).run("cancel my order")
    assert out["route"] == "direct"
    assert llm.answered_with is None
    assert "## " not in out["answer"]


def test_failures_fall_back(settings):
    out = Pipeline(settings, llm=FakeLLM(fail=True), decision=FakeDecision(error=True)).run("cancel my order")
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
