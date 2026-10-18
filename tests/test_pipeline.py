"""외부 API 없이 파이프라인 라우팅을 검증한다. LLM/Decision-1 은 가짜 객체로 바꾼다."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.decision import NONE_KEY, AzureDecision1, DecisionError, OpenRouterJev, TypeSafeJev, parse_answer, select_decision
from app.llm import Claude, LLMError, OpenAIChat, make_llm
from app.pipeline import ESCALATE_MESSAGE, Pipeline


class FakeLLM:
    def __init__(self, fail=False, answer_text="생성된 답변"):
        self.fail = fail
        self.answer_text = answer_text
        self.answered_with = None
        self.rewrite_previous = None
        self.answer_history = None

    def _r(self, text):
        return {"text": text, "model": "fake", "stop_reason": "end_turn",
                "usage": {"input_tokens": 100, "output_tokens": 10}, "cost_usd": 0.001, "latency_ms": 1}

    def rewrite_query(self, question, previous=()):
        self.rewrite_previous = list(previous)
        return self._r("cancel order")

    def answer(self, question, pages, history=()):
        if self.fail:
            raise LLMError("boom")
        self.answered_with = [path for path, _ in pages]
        self.answer_history = list(history)
        return {**self._r(self.answer_text), "prompt_chars": 1000}


class FakeDecision:
    """probs 를 후보 키(p1..pK, none) 기준으로 돌려준다."""

    name = "fake"
    label = "FakeDecision"

    def cost(self, usage):
        return 0.00004

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
    for k in ("LLM_PROVIDER", "LLM_MODEL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TYPESAFE_API_KEY", "OPENROUTER_API_KEY", "DECISION_PROVIDER", "DECISION_ENDPOINT", "DECISION_DEPLOYMENT", "DECISION_API_KEY", "DIRECT_ANSWER_THRESHOLD", "NOTION_TOKEN", "NOTION_DATA_SOURCE_ID"):
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


def test_llm_prompts_include_earlier_messages_only_when_given():
    from types import SimpleNamespace

    ok = SimpleNamespace(output=[], output_text="q", status="completed", incomplete_details=None,
                         model="gpt-5-mini", usage=SimpleNamespace(input_tokens=1, output_tokens=1))
    llm = OpenAIChat("sk-test", "gpt-5-mini", "medium", "gpt-5-mini")
    calls = []
    llm.client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: calls.append(kw) or ok))

    llm.rewrite_query("발송해주세요")
    llm.rewrite_query("발송해주세요", ["인증 메일이 안 와요"])
    llm.answer("발송해주세요", [("wiki/a.md", "body")], [{"role": "user", "text": "인증 메일이 안 와요"},
                                                        {"role": "bot", "text": "재발송해 드릴까요?"}])

    assert calls[0]["input"] == "발송해주세요"
    assert calls[1]["input"] == ("<earlier_customer_messages>\n- 인증 메일이 안 와요\n</earlier_customer_messages>\n"
                                 "<latest_message>\n발송해주세요\n</latest_message>")
    assert calls[2]["input"].startswith("<conversation>\n고객: 인증 메일이 안 와요\n상담원: 재발송해 드릴까요?\n</conversation>\n\n<wiki>")


def test_decision_provider_selection(settings, monkeypatch):
    d, reason = select_decision(settings)
    assert d is None and "미설정" in reason

    # 키가 하나만 있으면 그 구현체를 자동 선택
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    d, _ = select_decision(load_settings())
    assert isinstance(d, OpenRouterJev) and d.model == "typesafe/jev-1.13"
    assert d.url == "https://openrouter.ai/api/alpha/decisions" and d.headers() == {"Authorization": "Bearer sk-or-test"}

    # 여러 개면 typesafe > openrouter > azure 순
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test")
    for k, v in {"DECISION_ENDPOINT": "https://x.services.ai.azure.com/", "DECISION_DEPLOYMENT": "Decision-1",
                 "DECISION_API_KEY": "k"}.items():
        monkeypatch.setenv(k, v)
    d, _ = select_decision(load_settings())
    assert isinstance(d, TypeSafeJev) and d.url == "https://api.typesafe.ai/v1/systemone" and d.model == "jev-latest"

    # DECISION_PROVIDER 로 명시하면 그 구현체
    monkeypatch.setenv("DECISION_PROVIDER", "azure")
    d, _ = select_decision(load_settings())
    assert isinstance(d, AzureDecision1)
    assert d.url == "https://x.services.ai.azure.com/providers/microsoft/v1/systemone" and d.headers() == {"api-key": "k"}

    # 명시했는데 키가 없으면 비활성 + 이유
    monkeypatch.setenv("DECISION_API_KEY", "")
    d, reason = select_decision(load_settings())
    assert d is None and "DECISION_API_KEY" in reason

    monkeypatch.setenv("DECISION_PROVIDER", "jev")
    with pytest.raises(ValueError):
        load_settings()


def test_decision_cost():
    d = OpenRouterJev("k", "typesafe/jev-1.13")
    assert d.cost({"prompt_tokens": 1_000_000}) == 0.042
    assert d.cost({"prompt_tokens": 1000, "cost": 0.00005}) == 0.00005


def test_parse_answer_shapes():
    assert parse_answer({"choice": "p1", "probabilities": {"p1": 0.8, "p2": 0.2}}) == ("p1", {"p1": 0.8, "p2": 0.2})
    assert parse_answer({"probabilities": {"p1": 0.3, "p2": 0.7}})[0] == "p2"
    assert parse_answer([{"scores": [{"option": "none", "score": 0.9}, {"option": "p1", "score": 0.1}]}])[0] == "none"


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


def test_follow_up_question_carries_recent_conversation_into_rewrite_and_answer(settings):
    old = [{"role": "user", "text": f"오래된 질문 {i}"} for i in range(2)]
    recent = [
        {"role": "user", "text": "인증 메일이 안 와요"},
        {"role": "bot", "text": "재" * 700},
        {"role": "user", "text": "휴대폰 번호로 인증할게요"},
        {"role": "bot", "text": "인증 코드를 보내 드릴까요?"},
        {"role": "user", "text": "계정은 helloTest 입니다"},
        {"role": "bot", "text": "확인했습니다."},
    ]
    llm = FakeLLM()

    out = Pipeline(settings, llm=llm).run("발송해주세요", history=old + recent)

    # 최근 6개 메시지만 쓰고, 검색어 변환에는 고객 질문만, 답변 생성에는 챗봇 답변을 500자로 잘라 넘긴다.
    assert llm.rewrite_previous == ["인증 메일이 안 와요", "휴대폰 번호로 인증할게요", "계정은 helloTest 입니다"]
    assert llm.answer_history[1] == {"role": "bot", "text": "재" * 500 + "…"}
    assert llm.answer_history[2:] == recent[2:]
    assert out["trace"]["history_messages"] == 6


def test_english_follow_up_is_rewritten_only_when_there_is_earlier_question(settings):
    llm = FakeLLM()
    pipeline = Pipeline(settings, llm=llm)

    alone = pipeline.run("yes please send it")
    follow = pipeline.run("yes please send it", history=[{"role": "user", "text": "I did not get the verification code"}])

    assert alone["trace"]["steps"][0]["status"] == "skipped"
    assert follow["trace"]["steps"][0]["detail"]["query"] == "cancel order"
    assert llm.rewrite_previous == ["I did not get the verification code"]


@pytest.mark.parametrize("text", [
    "배송지 변경 요청을 담당자에게 전달했어요. 처리되면 안내해 드릴게요.\n[HANDOFF]",
    "배송지 변경 요청을 [HANDOFF] 담당자에게 전달했어요. 처리되면 안내해 드릴게요.",
])
def test_handoff_marker_is_removed_and_reported_as_handoff(settings, text):
    pipeline = Pipeline(settings, llm=FakeLLM(answer_text=text))

    out = pipeline.run("주문 배송지를 바꿔 주세요")
    cmp = pipeline.compare("주문 배송지를 바꿔 주세요")

    assert "[HANDOFF]" not in out["answer"] and "담당자에게 전달했어요" in out["answer"]
    assert out["route"] == "handoff" and out["trace"]["route"] == "handoff"
    assert cmp["with_decision"]["route"] == "handoff"


def test_answer_without_marker_stays_generate(settings):
    out = Pipeline(settings, llm=FakeLLM(answer_text="주문 내역에서 바꿀 수 있어요.")).run("주문 배송지를 바꿔 주세요")

    assert (out["route"], out["answer"]) == ("generate", "주문 내역에서 바꿀 수 있어요.")


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


def test_compare_runs_both_paths_on_shared_search(settings):
    llm, decision = FakeLLM(), FakeDecision({NONE_KEY: 0.9})
    out = Pipeline(settings, llm=llm, decision=decision).compare("주문을 취소하고 싶어요")
    a, b = out["with_decision"], out["bypass"]
    # 판단 모델 경로는 '해당 없음'으로 상담원 연결, 바이패스는 BM25 1위로 LLM 답변
    assert a["route"] == "escalate" and a["trace"]["path"] == "decision"
    assert b["route"] == "generate" and b["answer"] == "생성된 답변" and b["trace"]["path"] == "bypass"
    # 검색어 변환·검색은 한 번만 하고 같은 결과를 공유한다
    assert a["trace"]["steps"][:2] == b["trace"]["steps"][:2]
    assert b["trace"]["steps"][2]["status"] == "skipped" and "바이패스" in b["trace"]["steps"][2]["title"]
    # 각 경로 비용에는 공통 단계(검색어 변환 $0.001)가 포함된다
    assert a["trace"]["total_cost_usd"] == round(0.001 + 0.00004, 6)
    assert b["trace"]["total_cost_usd"] == round(0.001 + 0.001, 6)


def test_http_api(settings):
    from app.main import app

    client = TestClient(app)
    assert client.get("/api/status").json()["pages"] == 87
    r = client.post("/api/chat", json={"question": "refund please"})
    assert r.status_code == 200 and r.json()["trace"]["steps"]
    page_id = r.json()["trace"]["selected_pages"][0]["id"]
    assert client.get(f"/api/wiki/{page_id}").status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/").headers["cache-control"] == "no-cache"
    assert client.get("/static/app.js").headers["cache-control"] == "no-cache"
    assert client.post("/api/chat", json={"question": ""}).status_code == 422
    g = client.get("/api/guide").json()
    assert g["total"]["pages"] == 87 and g["total"]["datasets"] == 3
    topics = [t for d in g["datasets"] for grp in d["groups"] for t in grp["topics"]]
    assert len(topics) == 87 and all(t["examples"] and "{{" not in t["answer"] for t in topics)
    cmp = client.post("/api/chat", json={"question": "refund please", "compare": True}).json()
    assert cmp["with_decision"]["trace"]["steps"] and cmp["bypass"]["trace"]["path"] == "bypass"


def test_chat_api_passes_history_to_pipeline(settings):
    from app.main import app

    client = TestClient(app)
    history = [{"role": "user", "text": "refund please"}, {"role": "bot", "text": "Sure."}]

    single = client.post("/api/chat", json={"question": "how long does it take", "history": history}).json()
    cmp = client.post("/api/chat", json={"question": "how long does it take", "history": history, "compare": True}).json()

    assert single["trace"]["history_messages"] == 2
    assert cmp["with_decision"]["trace"]["history_messages"] == 2
    bad = client.post("/api/chat", json={"question": "x", "history": [{"role": "agent", "text": "hi"}]})
    assert bad.status_code == 422
