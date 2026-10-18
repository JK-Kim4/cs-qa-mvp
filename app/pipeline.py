"""질문 하나를 처리하는 파이프라인. 각 단계의 입력, 판단, 소요 시간을 trace 로 남긴다.

검색어 변환 -> BM25 검색 -> 판단 모델(Jev 또는 Decision-1) 선택 -> 라우팅 -> 답변 생성
라우팅 결과는 셋 중 하나다.
  generate: 선택된 위키 페이지를 근거로 LLM(OpenAI 또는 Claude) 이 답변 생성
  direct:   확신이 매우 높으면 위키 대표 답변을 그대로 발송 (DIRECT_ANSWER_THRESHOLD 설정 시)
  escalate: 맞는 페이지가 없거나 확신이 낮으면 상담원 연결
"""
import copy
import re
import time
from concurrent.futures import ThreadPoolExecutor

from .decision import NONE_KEY, DecisionError, select_decision
from .llm import LLMError, make_llm
from .wiki import Wiki

CANNED_PREFIX = "[LLM 미사용: 위키 대표 답변 원문입니다. 과거 특정 고객 건의 기록일 수 있습니다]\n\n"
# 채팅 화면은 이 경로의 답변이 나오면 바로 노션 티켓을 만들어 담당자에게 넘긴다 (static/app.js createTicket).
ESCALATE_MESSAGE = "문의하신 내용은 정확한 안내를 위해 담당자에게 전달해 드릴게요. 담당자가 확인한 뒤 안내해 드릴게요."
HANGUL = re.compile(r"[가-힣]")
# 이어지는 질문("발송해 주세요")을 이해하도록 넘기는 이전 대화의 범위. 길수록 입력 토큰이 늘어난다.
HISTORY_MAX_MESSAGES = 6
HISTORY_ANSWER_CHARS = 500  # 답변 생성에 넘기는 이전 챗봇 답변은 이 길이까지만


def recent_history(history):
    """최근 메시지만 남기고, 챗봇 답변은 앞부분만 남긴다."""
    out = []
    for m in list(history)[-HISTORY_MAX_MESSAGES:]:
        text = m["text"]
        if m["role"] == "bot" and len(text) > HISTORY_ANSWER_CHARS:
            text = text[:HISTORY_ANSWER_CHARS] + "…"
        out.append({"role": m["role"], "text": text})
    return out


def canned_answer(body):
    m = re.search(r"^## 대표 답변\n(.*?)(?=^## )", body, flags=re.S | re.M)
    if not m:
        return body
    lines = [l for l in m.group(1).strip().splitlines() if not l.startswith(">")]
    return "\n".join(lines).strip()


class Step:
    def __init__(self, trace, key, title):
        self.data = {"key": key, "title": title, "status": "ok", "detail": {}}
        self.trace = trace

    def __enter__(self):
        self.start = time.perf_counter()
        return self.data

    def __exit__(self, *exc):
        self.data["duration_ms"] = round((time.perf_counter() - self.start) * 1000)
        self.trace["steps"].append(self.data)
        return False


class Pipeline:
    def __init__(self, settings, wiki=None, llm=None, decision=None):
        self.s = settings
        self.wiki = wiki or Wiki(settings.wiki_dir)
        self.llm = llm if llm is not None else make_llm(settings)
        if decision is not None:
            self.decision, self.decision_reason = decision, "직접 주입"
        else:
            self.decision, self.decision_reason = select_decision(settings)

    def status(self):
        return {
            "pages": len(self.wiki.pages),
            "llm": {
                "enabled": self.llm is not None,
                "provider": self.s.llm_provider,
                "model": self.s.llm_model if self.llm else None,
            },
            "decision": {
                "enabled": self.decision is not None,
                "provider": getattr(self.decision, "name", None),
                "label": self.decision_label,
                "reason": self.decision_reason,
            },
        }

    @property
    def decision_label(self):
        return getattr(self.decision, "label", None) or "판단 모델"

    def run(self, question, bypass_decision=False, history=()):
        return self._finish(self._prepare(question, history), bypass_decision)

    def compare(self, question, history=()):
        """비교 모드: 검색어 변환과 검색은 한 번만 하고, 판단 모델 경로와 바이패스 경로를 동시에 실행한다.

        두 경로가 같은 검색어와 후보를 쓰므로 차이는 판단 단계에서만 생긴다. 동시에 돌려서
        한쪽이 먼저 실행되며 생기는 지연/캐시 이점도 없앤다. 각 결과의 비용·소요 시간에는
        공통 단계도 포함되어, 각각이 단독으로 운영될 때의 값과 같다.
        """
        prepared = self._prepare(question, history)
        with ThreadPoolExecutor(max_workers=2) as pool:
            with_decision = pool.submit(self._finish, prepared, False)
            bypass = pool.submit(self._finish, prepared, True)
            return {"question": question, "with_decision": with_decision.result(), "bypass": bypass.result()}

    # 검색어 변환과 검색: 비교 모드에서 두 경로가 공유한다.
    def _prepare(self, question, history=()):
        started = time.perf_counter()
        history = recent_history(history)
        trace = {"question": question, "steps": [], "selected_pages": [], "warnings": [],
                 "history_messages": len(history)}
        costs = []
        previous = [m["text"] for m in history if m["role"] == "user"]
        query = self._rewrite(question, previous, trace, costs)
        candidates = self._search(query, trace)
        return {"trace": trace, "costs": costs, "query": query, "candidates": candidates, "history": history,
                "ms": (time.perf_counter() - started) * 1000}

    def _finish(self, prepared, bypass_decision):
        started = time.perf_counter()
        trace, costs = copy.deepcopy(prepared["trace"]), list(prepared["costs"])
        question, query, candidates = trace["question"], prepared["query"], prepared["candidates"]

        route, reason, chosen = self._decide(question, query, candidates, trace, costs, bypass_decision)
        answer = self._answer(question, route, chosen, trace, costs, prepared["history"])
        answer_mode = next(s for s in trace["steps"] if s["key"] == "answer")["detail"]["mode"]

        trace.update(
            path="bypass" if bypass_decision else "decision",
            route=route,
            answer_mode=answer_mode,
            route_reason=reason,
            total_ms=round(prepared["ms"] + (time.perf_counter() - started) * 1000),
            total_cost_usd=round(sum(costs), 6) if costs else 0.0,
        )
        return {"answer": answer, "route": route, "answer_mode": answer_mode, "trace": trace}

    # 1. 위키가 영어라서 한국어 질문은 영어 검색어로 바꿔야 키워드 검색이 맞는다.
    # 이전 질문이 있으면 영어 질문이라도 앞 대화를 반영해야 하므로 변환한다.
    def _rewrite(self, question, previous, trace, costs):
        with Step(trace, "rewrite", "검색어 변환") as st:
            if not HANGUL.search(question) and not previous:
                st.update(status="skipped", reason="영어 질문이라 원문을 그대로 검색어로 사용")
                return question
            if not self.llm:
                st.update(status="skipped", reason=f"{self.s.llm_key_name} 미설정. 한국어 원문으로 검색하면 영어 위키와 거의 매칭되지 않습니다.")
                trace["warnings"].append("한국어 질문인데 LLM 키가 없어 검색어 변환을 건너뜀")
                return question
            try:
                r = self.llm.rewrite_query(question, previous)
            except LLMError as e:
                st.update(status="error", reason=str(e))
                return question
            costs.append(r["cost_usd"] or 0)
            st["detail"] = {"query": r["text"], "previous_questions": previous, "model": r["model"],
                            "usage": r["usage"], "cost_usd": r["cost_usd"]}
            return r["text"] or question

    # 2. BM25 키워드 검색으로 후보 페이지를 좁힌다.
    def _search(self, query, trace):
        with Step(trace, "search", "위키 검색 (BM25)") as st:
            tokens, hits = self.wiki.search(query, self.s.top_k)
            st["detail"] = {
                "query": query,
                "tokens": tokens,
                "candidates": [
                    {"rank": i + 1, "id": p.id, "path": f"wiki/{p.path}", "title": p.title, "bm25": round(s, 3)}
                    for i, (p, s) in enumerate(hits)
                ],
            }
            if not hits:
                st["reason"] = "일치하는 키워드가 있는 페이지가 없음"
            return hits

    # 3. 판단 모델이 후보 중 하나 또는 '해당 없음'을 고른다.
    def _decide(self, question, query, candidates, trace, costs, bypass=False):
        title = "페이지 선택 (바이패스: BM25 1위)" if bypass else f"페이지 선택 ({self.decision_label})"
        with Step(trace, "decide", title) as st:
            if not candidates:
                st.update(status="skipped", reason="검색 후보가 없어 판단할 대상이 없음")
                return "escalate", "검색 결과 없음", []

            if bypass:
                st.update(status="skipped", reason="비교 모드 바이패스 경로: 판단 모델을 거치지 않고 BM25 1위 페이지를 사용")
            elif self.decision:
                try:
                    return self._decide_with_model(question, query, candidates, st, costs)
                except DecisionError as e:
                    st.update(status="error", reason=f"{e} -> BM25 1위로 대체")
                    trace["warnings"].append(f"{self.decision_label} 호출 실패로 BM25 1위 페이지를 사용")
            else:
                st.update(status="skipped", reason=f"{self.decision_reason} -> BM25 1위 페이지를 사용 (확률 기반 상담원 연결 판단 없음)")

            top = candidates[0][0]
            st["detail"]["chosen"] = {"id": top.id, "path": f"wiki/{top.path}", "by": "bm25"}
            return "generate", "BM25 검색 1위 페이지 사용", [top]

    def _decide_with_model(self, question, query, candidates, st, costs):
        keys = {f"p{i + 1}": p for i, (p, _) in enumerate(candidates)}
        criteria = {k: f"{p.title}. Typical requests: " + " | ".join(p.examples) for k, p in keys.items()}
        criteria[NONE_KEY] = "None of the other topics matches what the customer is asking for"
        state = {"customer_message": question}
        if query != question:
            state["english_search_query"] = query
        choice, probs, body, payload = self.decision.choose(
            state,
            "Which knowledge-base topic answers this customer's support request? "
            "Choose 'none' if no topic fits the main request.",
            criteria,
        )
        ranked = sorted(probs.items(), key=lambda kv: -kv[1])
        usage = payload.get("usage") or {}
        cost = self.decision.cost(usage)
        costs.append(cost)
        st["detail"] = {
            "options": [
                {"key": k, "id": keys[k].id if k in keys else None, "title": keys[k].title if k in keys else "해당 없음",
                 "probability": round(v, 4)}
                for k, v in ranked
            ],
            "choice": choice,
            "usage": usage,
            "cost_usd": cost,
            "request": body,
            "response": payload,
        }

        p_top = probs.get(choice, 0.0)
        if choice == NONE_KEY:
            return "escalate", f"{self.decision_label} 이 '해당 없음'을 선택 (p={p_top:.2f})", []
        if p_top < self.s.min_confidence:
            return "escalate", f"최고 확률 {p_top:.2f} < MIN_CONFIDENCE {self.s.min_confidence}", []

        chosen = [keys[choice]]
        # 2순위가 충분히 높으면 근거 페이지로 같이 넘겨 오선택 위험을 줄인다.
        for k, v in ranked:
            if k != choice and k in keys and v >= self.s.second_page_min:
                chosen.append(keys[k])
                break
        st["detail"]["chosen"] = [{"id": p.id, "path": f"wiki/{p.path}", "by": self.decision_label} for p in chosen]
        if self.s.direct_threshold is not None and p_top >= self.s.direct_threshold:
            return "direct", f"확률 {p_top:.2f} >= DIRECT_ANSWER_THRESHOLD {self.s.direct_threshold}", chosen[:1]
        return "generate", f"{self.decision_label} 선택 확률 {p_top:.2f} >= MIN_CONFIDENCE {self.s.min_confidence}", chosen

    # 4. 라우팅 결과에 따라 답변을 만든다.
    def _answer(self, question, route, chosen, trace, costs, history=()):
        bodies = [(f"wiki/{p.path}", p.body(self.wiki.dir)) for p in chosen]
        trace["selected_pages"] = [
            {"id": p.id, "path": path, "title": p.title, "body": body} for p, (path, body) in zip(chosen, bodies)
        ]
        with Step(trace, "answer", "답변 구성") as st:
            if route == "escalate":
                st.update(reason="상담원 연결 안내. LLM 을 호출하지 않음")
                st["detail"] = {"mode": "escalate"}
                return ESCALATE_MESSAGE
            if route == "direct":
                st.update(reason="위키 대표 답변을 그대로 사용. LLM 을 호출하지 않음")
                st["detail"] = {"mode": "direct", "source": bodies[0][0]}
                return canned_answer(bodies[0][1])
            if not self.llm:
                st.update(status="skipped", reason=f"{self.s.llm_key_name} 미설정 -> 위키 대표 답변(영어)을 그대로 표시")
                st["detail"] = {"mode": "canned", "source": bodies[0][0]}
                return CANNED_PREFIX + canned_answer(bodies[0][1])
            try:
                r = self.llm.answer(question, bodies, history)
            except LLMError as e:
                st.update(status="error", reason=f"{e} -> 위키 대표 답변으로 대체")
                st["detail"] = {"mode": "canned", "source": bodies[0][0]}
                trace["warnings"].append("LLM 호출 실패로 위키 대표 답변을 표시")
                return CANNED_PREFIX + canned_answer(bodies[0][1])
            costs.append(r["cost_usd"] or 0)
            st["detail"] = {
                "mode": "generate",
                "model": r["model"],
                "effort": self.s.llm_effort,
                "sources": [path for path, _ in bodies],
                "prompt_chars": r["prompt_chars"],
                "usage": r["usage"],
                "cost_usd": r["cost_usd"],
                "stop_reason": r["stop_reason"],
            }
            return r["text"]
