"""Microsoft-Decision-1 (Microsoft Foundry) 클라이언트.

API: POST {endpoint}/providers/microsoft/v1/systemone
요청은 state(판단 대상)와 typed question 들, 응답은 answers.<질문명> 에 선택지와 확률이 온다.
https://learn.microsoft.com/azure/foundry/foundry-models/how-to/use-foundry-models-microsoft-decision
"""
import httpx

NONE_KEY = "none"


class DecisionError(RuntimeError):
    pass


class DecisionClient:
    def __init__(self, endpoint, deployment, api_key, timeout=30.0):
        self.url = f"{endpoint}/providers/microsoft/v1/systemone"
        self.deployment = deployment
        self.headers = {"api-key": api_key, "Content-Type": "application/json", "Accept": "application/json"}
        self.timeout = timeout

    def choose(self, state, instructions, criteria):
        """criteria = {키: 설명}. (선택된 키, {키: 확률}, 요청 본문, 응답 본문) 을 돌려준다."""
        body = {
            "model": self.deployment,
            "state": state,
            "questions": {"page": {"type": "choice", "instructions": instructions, "criteria": criteria}},
        }
        try:
            resp = httpx.post(self.url, json=body, headers=self.headers, timeout=self.timeout)
        except httpx.HTTPError as e:
            raise DecisionError(f"Decision-1 호출 실패: {e}") from e
        if resp.status_code != 200:
            raise DecisionError(f"Decision-1 HTTP {resp.status_code}: {resp.text[:300]}")
        payload = resp.json()
        answer = (payload.get("answers") or {}).get("page") or {}
        choice = answer.get("choice")
        if choice not in criteria:
            raise DecisionError(f"Decision-1 이 알 수 없는 선택지를 반환했습니다: {choice!r}")
        probs = answer.get("probabilities") or {choice: 1.0}
        return choice, {k: float(v) for k, v in probs.items()}, body, payload
