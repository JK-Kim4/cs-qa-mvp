"""System One 판단 모델. 텍스트를 생성하지 않고 state(판단 대상)와 typed question 을 받아 선택지별 확률을 돌려준다.

파이프라인은 DecisionModel 인터페이스(label, choose, cost)만 알고, 어떤 구현체를 쓸지는 앱 시작 시
select_decision() 이 설정(DECISION_PROVIDER 와 각 provider 의 키)을 보고 한 번 정한다.

구현체
  typesafe:   TypeSafe Jev 직접 호출   POST https://api.typesafe.ai/v1/systemone          (Bearer)
  openrouter: TypeSafe Jev via OpenRouter POST https://openrouter.ai/api/alpha/decisions  (Bearer)
  azure:      Microsoft-Decision-1     POST {endpoint}/providers/microsoft/v1/systemone   (api-key 헤더)
"""
from abc import ABC, abstractmethod

import httpx

NONE_KEY = "none"
PRICE_PER_M = 0.042  # USD / 100만 입력 토큰, 출력은 무료 (Jev, Decision-1 공개 단가)


class DecisionError(RuntimeError):
    pass


def parse_answer(answer):
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


class DecisionModel(ABC):
    """판단 모델 인터페이스. 구현체는 name, env(필요한 설정), from_settings, url/headers 만 정하면 된다."""

    name = ""
    env = ()  # 이 구현체에 꼭 필요한 Settings 필드명 -> 환경변수명 쌍

    def __init__(self, model, timeout=30.0):
        self.model = model
        self.timeout = timeout

    @classmethod
    def missing(cls, settings):
        """설정되지 않은 필수 환경변수 이름 목록."""
        return [env_name for field, env_name in cls.env if not getattr(settings, field)]

    @classmethod
    @abstractmethod
    def from_settings(cls, settings): ...

    @property
    @abstractmethod
    def label(self): ...

    @property
    @abstractmethod
    def url(self): ...

    @abstractmethod
    def headers(self): ...

    def build_body(self, state, instructions, criteria):
        return {
            "model": self.model,
            "state": state,
            "questions": {"page": {"type": "choice", "instructions": instructions, "criteria": criteria}},
        }

    def choose(self, state, instructions, criteria):
        """criteria = {키: 설명}. (선택된 키, {키: 확률}, 요청 본문, 응답 본문) 을 돌려준다."""
        body = self.build_body(state, instructions, criteria)
        headers = {**self.headers(), "Content-Type": "application/json", "Accept": "application/json"}
        try:
            resp = httpx.post(self.url, json=body, headers=headers, timeout=self.timeout)
        except httpx.HTTPError as e:
            raise DecisionError(f"{self.label} 호출 실패: {e}") from e
        if resp.status_code != 200:
            raise DecisionError(f"{self.label} HTTP {resp.status_code}: {resp.text[:300]}")
        payload = resp.json()
        answers = payload.get("answers") or payload.get("results") or {}
        answer = answers.get("page") if isinstance(answers, dict) else answers
        choice, probs = parse_answer(answer or {})
        if choice not in criteria:
            raise DecisionError(f"{self.label} 이 알 수 없는 선택지를 반환했습니다: {choice!r} (응답: {str(payload)[:300]})")
        return choice, probs or {choice: 1.0}, body, payload

    def cost(self, usage):
        """응답에 실제 청구액(usage.cost)이 있으면 그 값을, 없으면 공개 단가로 추정한 값을 돌려준다."""
        if usage.get("cost") is not None:
            return round(float(usage["cost"]), 8)
        tokens = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        return round(tokens / 1e6 * PRICE_PER_M, 8)


class TypeSafeJev(DecisionModel):
    name = "typesafe"
    env = (("typesafe_api_key", "TYPESAFE_API_KEY"),)
    URL = "https://api.typesafe.ai/v1/systemone"

    def __init__(self, api_key, model, **kw):
        super().__init__(model, **kw)
        self.api_key = api_key

    @classmethod
    def from_settings(cls, settings):
        return cls(settings.typesafe_api_key, settings.typesafe_model)

    @property
    def label(self):
        return f"Jev ({self.model})"

    @property
    def url(self):
        return self.URL

    def headers(self):
        return {"Authorization": f"Bearer {self.api_key}"}


class OpenRouterJev(DecisionModel):
    name = "openrouter"
    env = (("openrouter_api_key", "OPENROUTER_API_KEY"),)
    URL = "https://openrouter.ai/api/alpha/decisions"

    def __init__(self, api_key, model, **kw):
        super().__init__(model, **kw)
        self.api_key = api_key

    @classmethod
    def from_settings(cls, settings):
        return cls(settings.openrouter_api_key, settings.openrouter_model)

    @property
    def label(self):
        return f"Jev via OpenRouter ({self.model})"

    @property
    def url(self):
        return self.URL

    def headers(self):
        return {"Authorization": f"Bearer {self.api_key}"}


class AzureDecision1(DecisionModel):
    name = "azure"
    env = (
        ("decision_endpoint", "DECISION_ENDPOINT"),
        ("decision_deployment", "DECISION_DEPLOYMENT"),
        ("decision_api_key", "DECISION_API_KEY"),
    )

    def __init__(self, endpoint, deployment, api_key, **kw):
        super().__init__(deployment, **kw)
        self.endpoint = endpoint
        self.api_key = api_key

    @classmethod
    def from_settings(cls, settings):
        return cls(settings.decision_endpoint, settings.decision_deployment, settings.decision_api_key)

    @property
    def label(self):
        return "Microsoft-Decision-1"

    @property
    def url(self):
        return f"{self.endpoint}/providers/microsoft/v1/systemone"

    def headers(self):
        return {"api-key": self.api_key}


# DECISION_PROVIDER 를 비워 두면 이 순서대로 키가 모두 설정된 첫 구현체를 쓴다.
PROVIDERS = {cls.name: cls for cls in (TypeSafeJev, OpenRouterJev, AzureDecision1)}


def select_decision(settings):
    """앱 시작 시 한 번 호출. (구현체 인스턴스 또는 None, 선택 이유) 를 돌려준다."""
    name = settings.decision_provider
    if name:
        cls = PROVIDERS[name]
        missing = cls.missing(settings)
        if missing:
            return None, f"DECISION_PROVIDER={name} 인데 {', '.join(missing)} 미설정"
        return cls.from_settings(settings), f"DECISION_PROVIDER={name}"
    for cls in PROVIDERS.values():
        if not cls.missing(settings):
            return cls.from_settings(settings), f"자동 선택: {cls.name} 키가 설정됨"
    return None, "판단 모델 키 미설정 (TYPESAFE_API_KEY, OPENROUTER_API_KEY, DECISION_* 중 하나 필요)"
