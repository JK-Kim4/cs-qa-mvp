"""System One 판단 모델 클라이언트: Microsoft-Decision-1(Foundry) 또는 TypeSafe Jev(OpenRouter).

둘 다 텍스트를 생성하지 않고, state(판단 대상)와 typed question 들을 받아 선택지별 확률을 돌려준다.
  azure:      POST {endpoint}/providers/microsoft/v1/systemone   (api-key 헤더)
  openrouter: POST https://openrouter.ai/api/alpha/decisions      (Bearer 토큰)
https://learn.microsoft.com/azure/foundry/foundry-models/how-to/use-foundry-models-microsoft-decision
https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request
"""
import httpx

NONE_KEY = "none"
OPENROUTER_URL = "https://openrouter.ai/api/alpha/decisions"


class DecisionError(RuntimeError):
    pass


def _parse_answer(answer, criteria):
    """응답 형태가 provider 마다 조금씩 달라서 choice/probabilities 를 너그럽게 꺼낸다."""
    if isinstance(answer, list):
        answer = answer[0] if answer else {}
    if not isinstance(answer, dict):
        return answer, None
    probs = None
    for k in ("probabilities", "probs", "scores", "distribution"):
        if isinstance(answer.get(k), dict):
            probs = {str(c): float(v) for c, v in answer[k].items()}
            break
        if isinstance(answer.get(k), list):  # [{"choice"/"option": .., "probability"/"score": ..}]
            probs = {}
            for item in answer[k]:
                key = item.get("choice") or item.get("option") or item.get("key")
                val = item.get("probability", item.get("score"))
                if key is not None and val is not None:
                    probs[str(key)] = float(val)
            break
    choice = answer.get("choice") or answer.get("answer") or answer.get("value")
    if choice is None and probs:
        choice = max(probs, key=probs.get)
    return choice, probs


class DecisionClient:
    def __init__(self, url, model, headers, label, timeout=30.0):
        self.url = url
        self.model = model
        self.headers = {**headers, "Content-Type": "application/json", "Accept": "application/json"}
        self.label = label
        self.timeout = timeout

    def choose(self, state, instructions, criteria):
        """criteria = {키: 설명}. (선택된 키, {키: 확률}, 요청 본문, 응답 본문) 을 돌려준다."""
        body = {
            "model": self.model,
            "state": state,
            "questions": {"page": {"type": "choice", "instructions": instructions, "criteria": criteria}},
        }
        try:
            resp = httpx.post(self.url, json=body, headers=self.headers, timeout=self.timeout)
        except httpx.HTTPError as e:
            raise DecisionError(f"{self.label} 호출 실패: {e}") from e
        if resp.status_code != 200:
            raise DecisionError(f"{self.label} HTTP {resp.status_code}: {resp.text[:300]}")
        payload = resp.json()
        answers = payload.get("answers") or payload.get("results") or {}
        answer = answers.get("page") if isinstance(answers, dict) else answers
        choice, probs = _parse_answer(answer or {}, criteria)
        if choice not in criteria:
            raise DecisionError(f"{self.label} 이 알 수 없는 선택지를 반환했습니다: {choice!r} (응답: {str(payload)[:300]})")
        return choice, probs or {choice: 1.0}, body, payload


def make_decision(settings):
    """설정된 provider 의 판단 모델 클라이언트를 만든다. 설정이 없으면 None."""
    if not settings.decision_enabled:
        return None
    if settings.decision_provider == "openrouter":
        return DecisionClient(OPENROUTER_URL, settings.decision_model,
                              {"Authorization": f"Bearer {settings.openrouter_api_key}"}, settings.decision_label)
    return DecisionClient(f"{settings.decision_endpoint}/providers/microsoft/v1/systemone", settings.decision_deployment,
                          {"api-key": settings.decision_api_key}, settings.decision_label)
