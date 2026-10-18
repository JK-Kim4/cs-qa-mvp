"""LLM 호출: 검색어 변환과 답변 생성. LLM_PROVIDER 로 OpenAI(기본) 와 Claude 중 하나를 쓴다."""
import time

import anthropic
import openai

# 100만 토큰당 (입력, 출력) USD. 비용 추정 표시용.
PRICING = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    # 캐시 할인은 반영하지 않은 정가 기준.
    "gpt-5": (1.25, 10.0),
    "gpt-5-mini": (0.25, 2.0),
    "gpt-5-nano": (0.05, 0.4),
}

REWRITE_SYSTEM = (
    "You turn a customer-support message into a short English keyword search query. "
    "Output only the query: 3-12 lowercase English words, no punctuation, no explanation. "
    "Earlier customer messages may be given for context: use them only to resolve what the latest message "
    "refers to. The query must describe the latest message."
)

ANSWER_SYSTEM = """당신은 고객센터 상담원입니다. 아래 <wiki> 안의 사내 위키 페이지만 근거로 고객 질문에 답합니다.

규칙:
- 고객이 쓴 언어로 답합니다. 위키가 영어여도 고객이 한국어로 물었으면 한국어로 답합니다.
- 위키에 없는 정책, 금액, 기한, 연락처를 지어내지 않습니다.
- 위키의 대표 답변과 답변 사례는 과거 특정 고객 건의 기록입니다. 그 건의 금액, 날짜, 주문번호를 지금 고객 것처럼 쓰거나, 환불·크레딧 지급처럼 이미 처리한 조치로 말하지 않습니다. 처리 방법과 절차만 안내합니다.
- 위키의 {{Order Number}} 같은 자리표시자는 그대로 쓰지 말고 "주문 번호" 처럼 일반적인 표현으로 바꿉니다.
- 위키 내용이 질문과 맞지 않으면 추측하지 말고, 담당자에게 전달하겠다고 짧게 안내합니다.
- 주문·배송지 변경, 취소·환불 처리, 계정 정보 변경, 인증 코드 발송처럼 시스템에서 실제 조치가 필요한 일은 당신이 할 수 없습니다. 처리했다거나 처리하겠다고 말하지 않습니다.
- 그런 요청이 확정되고 담당자가 처리하는 데 필요한 정보가 대화에 있으면, 담당자에게 전달했으며 처리되면 안내한다고 답합니다.
- 담당자에게 전달하거나 연결한다고 말하는 답변은 반드시 마지막 줄에 [HANDOFF] 를 씁니다. 그 밖의 답변에는 쓰지 않습니다.
- 고객에게 질문하거나 추가 정보를 요청하는 답변에는 [HANDOFF] 를 쓰지 않습니다. 필요한 정보를 받은 다음 답변에서 씁니다.
- 답변은 간결하게, 필요한 경우 단계별 목록으로 씁니다.
- <conversation> 이 있으면 지금까지의 대화입니다. 마지막 질문이 앞 대화를 이어 받는 말(예: "발송해 주세요")이면 그 맥락으로 이해하고 이어서 답합니다. 사실의 근거는 여전히 <wiki> 만 씁니다."""


class LLMError(RuntimeError):
    pass


def estimate_cost(model, usage):
    # OpenAI 는 응답 모델명에 날짜가 붙으므로(gpt-5-mini-2025-08-07) 요청한 모델명으로 찾는다.
    price = PRICING.get(model)
    if not price or not usage:
        return None
    return round(usage["input_tokens"] / 1e6 * price[0] + usage["output_tokens"] / 1e6 * price[1], 6)


class Claude:
    def __init__(self, api_key, model, effort, rewrite_model):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.effort = effort
        self.rewrite_model = rewrite_model

    def _call(self, model, system, user, effort, max_tokens):
        start = time.perf_counter()
        try:
            resp = self.client.beta.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": effort},
                # 안전 분류기가 거절하면 서버가 권장 모델로 자동 재시도한다.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as e:
            raise LLMError("ANTHROPIC_API_KEY 가 올바르지 않습니다.") from e
        except anthropic.RateLimitError as e:
            raise LLMError("Claude API 요청 한도를 넘었습니다. 잠시 후 다시 시도하세요.") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude API 오류 {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("Claude API 에 연결할 수 없습니다.") from e

        if resp.stop_reason == "refusal":
            raise LLMError("Claude 가 이 요청에 대한 답변을 거절했습니다.")
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
        cached = getattr(resp.usage, "cache_read_input_tokens", 0) or 0
        if cached:
            usage["cached_input_tokens"] = cached
        return {
            "text": text,
            "model": resp.model,
            "stop_reason": resp.stop_reason,
            "usage": usage,
            "cost_usd": estimate_cost(model, usage),
            "latency_ms": round((time.perf_counter() - start) * 1000),
        }

    def rewrite_query(self, question, previous=()):
        user = question
        if previous:
            earlier = "\n".join(f"- {q}" for q in previous)
            user = (f"<earlier_customer_messages>\n{earlier}\n</earlier_customer_messages>\n"
                    f"<latest_message>\n{question}\n</latest_message>")
        return self._call(self.rewrite_model, REWRITE_SYSTEM, user, "low", 2000)

    def answer(self, question, pages, history=()):
        wiki = "\n\n".join(f'<page path="{path}">\n{body}\n</page>' for path, body in pages)
        user = f"<wiki>\n{wiki}\n</wiki>\n\n<question>\n{question}\n</question>"
        if history:
            lines = "\n".join(f"{'고객' if m['role'] == 'user' else '상담원'}: {m['text']}" for m in history)
            user = f"<conversation>\n{lines}\n</conversation>\n\n" + user
        result = self._call(self.model, ANSWER_SYSTEM, user, self.effort, 8000)
        result["prompt_chars"] = len(ANSWER_SYSTEM) + len(user)
        return result

    def summarize_ticket(self, system, transcript):
        """상담 종료 시 대화를 티켓용으로 요약·분류한다. 프롬프트는 app/ticket.py 가 정한다."""
        return self._call(self.rewrite_model, system, transcript, "low", 4000)


class OpenAIChat:
    """Claude 와 같은 인터페이스(rewrite_query, answer)를 Responses API 로 구현한다."""

    def __init__(self, api_key, model, effort, rewrite_model):
        self.client = openai.OpenAI(api_key=api_key)
        self.model = model
        self.effort = effort
        self.rewrite_model = rewrite_model

    def _call(self, model, system, user, effort, max_tokens):
        start = time.perf_counter()
        try:
            resp = self.client.responses.create(
                model=model,
                instructions=system,
                input=user,
                # 추론 토큰도 max_output_tokens 에 포함되므로 여유 있게 둔다.
                max_output_tokens=max_tokens,
                reasoning={"effort": effort},
                store=False,
            )
        except openai.AuthenticationError as e:
            raise LLMError("OPENAI_API_KEY 가 올바르지 않습니다.") from e
        except openai.RateLimitError as e:
            raise LLMError("OpenAI API 요청 한도를 넘었거나 크레딧이 부족합니다. 잠시 후 다시 시도하세요.") from e
        except openai.APIStatusError as e:
            raise LLMError(f"OpenAI API 오류 {e.status_code}: {e.message}") from e
        except openai.APIConnectionError as e:
            raise LLMError("OpenAI API 에 연결할 수 없습니다.") from e

        refusals = [c.refusal for item in resp.output if item.type == "message"
                    for c in item.content if c.type == "refusal"]
        if refusals:
            raise LLMError("OpenAI 모델이 이 요청에 대한 답변을 거절했습니다.")
        text = resp.output_text.strip()
        stop_reason = resp.status
        if resp.status == "incomplete" and resp.incomplete_details:
            stop_reason = resp.incomplete_details.reason
            if not text:
                raise LLMError(f"OpenAI 응답이 중간에 끊겼습니다 ({stop_reason}).")
        usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
        # 프롬프트 캐시 적중분. 비교 모드에서 캐시가 비용에 끼친 영향을 확인하는 용도 (비용 추정은 정가 기준)
        cached = getattr(getattr(resp.usage, "input_tokens_details", None), "cached_tokens", 0) or 0
        if cached:
            usage["cached_input_tokens"] = cached
        return {
            "text": text,
            "model": resp.model,
            "stop_reason": stop_reason,
            "usage": usage,
            "cost_usd": estimate_cost(model, usage),
            "latency_ms": round((time.perf_counter() - start) * 1000),
        }

    rewrite_query = Claude.rewrite_query
    answer = Claude.answer
    summarize_ticket = Claude.summarize_ticket


def make_llm(settings):
    """설정된 provider 의 클라이언트를 만든다. 키가 없으면 None."""
    if not settings.llm_enabled:
        return None
    cls = OpenAIChat if settings.llm_provider == "openai" else Claude
    return cls(settings.llm_api_key, settings.llm_model, settings.llm_effort, settings.rewrite_model)
