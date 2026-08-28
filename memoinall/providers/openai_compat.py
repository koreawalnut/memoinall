"""OpenAI 호환 어댑터 — ChatGPT · Gemini · Ollama 를 하나로 처리한다.

Gemini 와 Ollama 는 각자 OpenAI 호환 엔드포인트를 제공하므로, 달라지는 건
base_url 과 키뿐이다. 그래서 어댑터 하나로 셋을 모두 커버한다.

문제는 '호환'의 범위가 서버마다 다르다는 것이다. 어떤 서버는
`max_tokens` 을 받고 어떤 서버는 `max_completion_tokens` 만 받는다.
`reasoning_effort` 나 `response_format: json_schema` 는 지원 여부가 더 제각각이다.

그래서 **거절당한 파라미터를 하나씩 떨어뜨리며 재시도**하고, 그 결과를
인스턴스에 기억해 다음 호출부터는 곧장 맞는 조합으로 간다. 사용자가
서버별 차이를 몰라도 되게 하려는 것이다.
"""

from __future__ import annotations

import json
import logging
import re

from . import ProviderError, ProviderSpec, Refused, Unavailable

log = logging.getLogger(__name__)

# 순서가 곧 포기 순서다. 뒤로 갈수록 더 아쉬운 기능.
DROPPABLE = ["reasoning_effort", "response_format"]


class OpenAICompatProvider:
    kind = "openai_compat"

    def __init__(self, spec: ProviderSpec, cfg: dict):
        self.spec = spec
        self.cfg = cfg
        self._client = None
        # 이 서버가 못 받는다고 확인된 파라미터들
        self._unsupported: set[str] = set()
        # max_tokens / max_completion_tokens 중 무엇을 받는지
        self._tokens_param = "max_tokens"
        self._json_mode: str | None = None  # None=미확인, "schema" | "object" | "prompt"

    # ------------------------------------------------------------------ 기본
    @property
    def model(self) -> str:
        return self.cfg.get("model") or self.spec.default_model

    @property
    def base_url(self) -> str:
        return self.cfg.get("base_url") or self.spec.default_base_url

    def available(self) -> bool:
        if self.spec.needs_key and not self.cfg.get("api_key"):
            return False
        return bool(self.base_url)

    def client(self):
        if self._client is not None:
            return self._client
        if self.spec.needs_key and not self.cfg.get("api_key"):
            raise Unavailable(f"{self.spec.label} API 키가 설정되지 않았습니다.")
        try:
            import openai
        except ImportError as exc:
            raise Unavailable("openai 패키지가 없습니다.  pip install openai") from exc
        # Ollama 처럼 키가 없는 로컬 서버도 SDK 가 키를 요구하므로 더미를 넣는다.
        self._client = openai.OpenAI(
            api_key=self.cfg.get("api_key") or "not-needed",
            base_url=self.base_url,
        )
        return self._client

    # ------------------------------------------------------------------ 호출
    def _build(self, messages, max_tokens: int, effort: str | None, response_format=None) -> dict:
        # effort 를 안 넘기면 설정값을 쓴다. 예전에 None 을 그대로 흘려보내
        # '추론 끄기' 설정이 JSON 경로에서만 무시되던 버그가 있었다 —
        # 그 결과 작은 로컬 모델이 추론만 하다 빈 응답을 냈다.
        level = effort or self.cfg.get("effort")
        kwargs: dict = {"model": self.model, "messages": messages}
        kwargs[self._tokens_param] = max_tokens
        if level and self.spec.supports_effort and "reasoning_effort" not in self._unsupported:
            kwargs["reasoning_effort"] = _map_effort(level)
        if response_format and "response_format" not in self._unsupported:
            kwargs["response_format"] = response_format
        return kwargs

    def _invoke(self, kwargs: dict):
        """거절당한 파라미터를 떨어뜨리며 최대 5회까지 재시도한다."""
        client = self.client()
        last: Exception | None = None
        for _ in range(5):
            try:
                return client.chat.completions.create(**kwargs)
            except Exception as exc:
                last = exc
                if _is_connection_error(exc):
                    raise Unavailable(self._connection_hint(exc)) from exc
                if not self._adapt(kwargs, exc):
                    raise
        raise last  # 도달하지 않지만 방어적으로

    def _adapt(self, kwargs: dict, exc: Exception) -> bool:
        """오류 메시지를 보고 파라미터를 하나 조정한다. 조정했으면 True."""
        text = str(exc).lower()

        # max_tokens → max_completion_tokens (최신 OpenAI 모델)
        if "max_tokens" in kwargs and (
            "max_completion_tokens" in text or ("max_tokens" in text and _rejected(text))
        ):
            kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
            self._tokens_param = "max_completion_tokens"
            log.info("%s: max_completion_tokens 로 전환", self.spec.name)
            return True

        for param in DROPPABLE:
            if param in kwargs and param.replace("_", "") in text.replace("_", "").replace(" ", ""):
                kwargs.pop(param)
                self._unsupported.add(param)
                log.info("%s: %s 미지원 — 빼고 재시도", self.spec.name, param)
                return True

        # 파라미터 이름이 안 보이지만 400 이면, 남은 선택 파라미터부터 버린다.
        if _rejected(text):
            for param in DROPPABLE:
                if param in kwargs:
                    kwargs.pop(param)
                    self._unsupported.add(param)
                    log.info("%s: 400 → %s 제거 후 재시도", self.spec.name, param)
                    return True
        return False

    def _connection_hint(self, exc: Exception) -> str:
        if self.spec.name == "ollama":
            return (
                f"Ollama 서버에 연결할 수 없습니다 ({self.base_url}). "
                "터미널에서 `ollama serve` 가 떠 있는지, "
                f"`ollama pull {self.model}` 로 모델을 받았는지 확인하세요."
            )
        return f"{self.spec.label} 서버에 연결할 수 없습니다 ({self.base_url}): {exc}"

    def _extract(self, response) -> str:
        choice = response.choices[0]
        message = choice.message
        # OpenAI structured outputs 는 거절을 별도 필드로 준다.
        refusal = getattr(message, "refusal", None)
        if refusal:
            raise Refused("refusal", str(refusal))
        finish = getattr(choice, "finish_reason", None)
        if finish == "content_filter":
            raise Refused("content_filter", "콘텐츠 필터에 걸렸습니다.")

        content = (message.content or "").strip()
        if content:
            return content

        # 추론 모델(qwen3, gemma4, o-시리즈 …)은 추론을 먼저 쏟아내고 본문을 나중에 낸다.
        # 예산이 모자라면 추론만 하다 끝나 content 가 빈 채로 온다. 조용히 ""를 돌려주면
        # 한참 뒤 JSON 파싱 실패 같은 엉뚱한 곳에서 터지므로 여기서 잡아 설명한다.
        if _reasoning_of(message):
            budget = int(self.cfg.get("max_tokens") or 8000)
            raise ProviderError(
                f"모델이 추론에만 토큰을 다 써서 답을 내지 못했습니다 (최대 출력 {budget} 토큰). "
                "설정에서 '최대 출력 토큰'을 늘리거나, 추론을 적게 하는 모델을 고르세요."
            )
        if finish == "length":
            raise ProviderError(
                "출력이 최대 토큰에서 잘려 빈 응답이 됐습니다. 설정에서 '최대 출력 토큰'을 늘려 보세요."
            )
        raise ProviderError("모델이 빈 응답을 돌려줬습니다.")

    def list_models(self) -> list[str]:
        """서버에 실제로 있는 모델 목록. 못 가져오면 빈 목록."""
        try:
            return sorted(m.id for m in self.client().models.list().data)
        except Exception as exc:
            log.info("%s: 모델 목록 조회 실패 — %s", self.spec.name, exc)
            return []

    def complete(self, system: str, user: str, *, max_tokens=None, effort=None) -> str:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        kwargs = self._build(
            messages, max_tokens or int(self.cfg.get("max_tokens") or 8000), effort or self.cfg.get("effort")
        )
        return self._extract(self._invoke(kwargs))

    def complete_json(self, system: str, user: str, schema: dict, *, max_tokens: int = 1200) -> dict:
        """json_schema → json_object → 프롬프트 지시 순으로 물러난다."""
        modes = [self._json_mode] if self._json_mode else ["schema", "object", "prompt"]
        last: Exception | None = None

        for mode in modes:
            try:
                text = self._json_attempt(system, user, schema, max_tokens, mode)
                self._json_mode = mode
                return _parse_json(text)
            except (Unavailable, Refused):
                raise
            except Exception as exc:
                last = exc
                log.info("%s: JSON 모드 '%s' 실패 — 다음 방식으로", self.spec.name, mode)

        raise last if last else RuntimeError("JSON 생성 실패")

    def _json_attempt(self, system: str, user: str, schema: dict, max_tokens: int, mode: str) -> str:
        sys_prompt, fmt = system, None
        if mode == "schema":
            fmt = {
                "type": "json_schema",
                "json_schema": {"name": "result", "strict": True, "schema": schema},
            }
        elif mode == "object":
            fmt = {"type": "json_object"}
            sys_prompt = f"{system}\n\n반드시 다음 스키마를 따르는 JSON 만 출력하세요:\n{json.dumps(schema, ensure_ascii=False)}"
        else:
            sys_prompt = (
                f"{system}\n\n다음 스키마를 따르는 JSON 만 출력하세요. 설명이나 코드펜스를 붙이지 마세요:\n"
                f"{json.dumps(schema, ensure_ascii=False)}"
            )
        messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}]
        return self._extract(self._invoke(self._build(messages, max_tokens, None, fmt)))

    def test_connection(self) -> dict:
        messages = [{"role": "user", "content": "연결 확인. '확인'이라고만 답하세요."}]
        # 추론 모델은 32토큰으로는 본문을 못 낸다. 넉넉히 주되, 그래도 비면
        # '연결은 됐다'는 사실만은 정확히 보고한다 — 연결 문제와 구분되어야 한다.
        response = self._invoke(self._build(messages, 1024, None))
        info = {"model": getattr(response, "model", self.model)}
        try:
            info["reply"] = self._extract(response)[:60]
        except ProviderError as exc:
            info["reply"] = ""
            info["warning"] = str(exc)
        return info


# --------------------------------------------------------------------------- 유틸

# Anthropic 의 effort 어휘를 OpenAI 계열로 옮긴다. xhigh/max 는 대응이 없어 high 로.
# "none" 은 그대로 보낸다 — Ollama 에서 추론을 완전히 끄는 유일한 방법이다.
EFFORT_MAP = {
    "none": "none",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}


def _map_effort(effort: str) -> str:
    return EFFORT_MAP.get(effort, "medium")


# 추론 내용을 담는 필드 이름이 서버마다 다르다.
REASONING_FIELDS = ("reasoning", "reasoning_content", "thinking")


def _reasoning_of(message) -> str:
    for field in REASONING_FIELDS:
        value = getattr(message, field, None)
        if value:
            return str(value)
    extra = getattr(message, "model_extra", None) or {}
    for field in REASONING_FIELDS:
        if extra.get(field):
            return str(extra[field])
    return ""


def _rejected(text: str) -> bool:
    return any(k in text for k in ("400", "invalid", "unsupported", "unrecognized", "not supported", "unknown"))


def _is_connection_error(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    if "connection" in name or "timeout" in name:
        return True
    text = str(exc).lower()
    return "connection refused" in text or "failed to connect" in text or "max retries exceeded" in text


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _parse_json(text: str) -> dict:
    """코드펜스나 앞뒤 설명이 섞여 나와도 건져낸다."""
    text = (text or "").strip()
    fence = _FENCE.search(text)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise
