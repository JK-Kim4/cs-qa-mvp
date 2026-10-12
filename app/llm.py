"""Claude 호출: 검색어 변환과 답변 생성."""
import time

import anthropic

# 100만 토큰당 (입력, 출력) USD. 비용 추정 표시용.
PRICING = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

REWRITE_SYSTEM = (
    "You turn a customer-support message into a short English keyword search query. "
    "Output only the query: 3-12 lowercase English words, no punctuation, no explanation."
)

ANSWER_SYSTEM = """당신은 고객센터 상담원입니다. 아래 <wiki> 안의 사내 위키 페이지만 근거로 고객 질문에 답합니다.

규칙:
- 고객이 쓴 언어로 답합니다. 위키가 영어여도 고객이 한국어로 물었으면 한국어로 답합니다.
- 위키에 없는 정책, 금액, 기한, 연락처를 지어내지 않습니다.
- 위키의 대표 답변과 답변 사례는 과거 특정 고객 건의 기록입니다. 그 건의 금액, 날짜, 주문번호를 지금 고객 것처럼 쓰거나, 환불·크레딧 지급처럼 이미 처리한 조치로 말하지 않습니다. 처리 방법과 절차만 안내합니다.
- 위키의 {{Order Number}} 같은 자리표시자는 그대로 쓰지 말고 "주문 번호" 처럼 일반적인 표현으로 바꿉니다.
- 위키 내용이 질문과 맞지 않으면 추측하지 말고, 담당 상담원에게 연결하겠다고 짧게 안내합니다.
- 답변은 간결하게, 필요한 경우 단계별 목록으로 씁니다."""


class LLMError(RuntimeError):
    pass


def estimate_cost(model, usage):
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
        return {
            "text": text,
            "model": resp.model,
            "stop_reason": resp.stop_reason,
            "usage": usage,
            "cost_usd": estimate_cost(model, usage),
            "latency_ms": round((time.perf_counter() - start) * 1000),
        }

    def rewrite_query(self, question):
        return self._call(self.rewrite_model, REWRITE_SYSTEM, question, "low", 2000)

    def answer(self, question, pages):
        wiki = "\n\n".join(f'<page path="{path}">\n{body}\n</page>' for path, body in pages)
        user = f"<wiki>\n{wiki}\n</wiki>\n\n<question>\n{question}\n</question>"
        result = self._call(self.model, ANSWER_SYSTEM, user, self.effort, 8000)
        result["prompt_chars"] = len(ANSWER_SYSTEM) + len(user)
        return result
