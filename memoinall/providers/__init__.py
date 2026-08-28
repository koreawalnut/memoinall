"""LLM 프로바이더 추상화 — Claude / ChatGPT / Gemini / Ollama.

구현이 두 개뿐인 이유:
  - Claude 는 Anthropic 네이티브 SDK 를 쓴다.
  - **Gemini 와 Ollama 는 둘 다 OpenAI 호환 엔드포인트를 제공한다.**
    그래서 ChatGPT 어댑터 하나가 base_url 만 바꿔 셋을 모두 처리한다.
    프로바이더마다 SDK 를 따로 붙이면 코드가 네 배가 되고, 각자 다른 속도로
    바뀌는 API 를 네 개 쫓아다녀야 한다.

같은 이유로 LM Studio · vLLM · Groq · OpenRouter 처럼 OpenAI 호환을 표방하는
서버는 'ChatGPT' 를 고르고 base_url 만 바꾸면 그대로 동작한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    label: str
    kind: str  # anthropic | openai_compat
    default_model: str
    model_choices: list[str]
    needs_key: bool = True
    default_base_url: str = ""
    base_url_editable: bool = False
    supports_effort: bool = True
    default_effort: str = "high"
    key_hint: str = ""
    note: str = ""
    extras: dict = field(default_factory=dict)


SPECS: dict[str, ProviderSpec] = {
    "anthropic": ProviderSpec(
        name="anthropic",
        label="Claude (Anthropic)",
        kind="anthropic",
        default_model="claude-opus-5",
        model_choices=["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"],
        default_effort="high",
        key_hint="sk-ant-…",
        note="긴 문서 작성과 근거 인용에 가장 강합니다. 추론 강도(effort)를 조절할 수 있습니다.",
    ),
    "openai": ProviderSpec(
        name="openai",
        label="ChatGPT (OpenAI)",
        kind="openai_compat",
        default_model="gpt-5",
        model_choices=["gpt-5", "gpt-5-mini", "gpt-4.1", "gpt-4o", "o4-mini"],
        default_base_url="https://api.openai.com/v1",
        base_url_editable=True,
        default_effort="medium",
        key_hint="sk-…",
        note="base_url 을 바꾸면 LM Studio · vLLM · Groq · OpenRouter 등 OpenAI 호환 서버도 씁니다.",
    ),
    "gemini": ProviderSpec(
        name="gemini",
        label="Gemini (Google)",
        kind="openai_compat",
        default_model="gemini-2.5-pro",
        model_choices=["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"],
        default_base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        base_url_editable=True,
        default_effort="medium",
        key_hint="AIza…",
        note="Google 의 OpenAI 호환 엔드포인트로 접속합니다. 키는 Google AI Studio 에서 발급합니다.",
    ),
    "ollama": ProviderSpec(
        name="ollama",
        label="Ollama (로컬)",
        kind="openai_compat",
        default_model="llama3.1",
        model_choices=["llama3.1", "qwen2.5", "gemma3", "mistral", "exaone3.5"],
        needs_key=False,
        default_base_url="http://localhost:11434/v1",
        base_url_editable=True,
        # 작은 로컬 추론 모델(qwen3/gemma4 등)은 추론에만 수천 토큰을 쓰다가
        # 본문을 못 내는 일이 잦다. 그래서 기본을 '추론 끄기'로 둔다.
        default_effort="none",
        note="내 PC 에서 도는 모델이라 메모가 외부로 나가지 않습니다. 먼저 `ollama serve` 와 `ollama pull <모델>` 이 필요합니다. "
        "작은 모델은 '추론 끄기'로 두어야 답이 안정적으로 나옵니다.",
    ),
}

DEFAULT_PROVIDER = "anthropic"


def spec(name: str) -> ProviderSpec:
    if name not in SPECS:
        raise ValueError(f"알 수 없는 프로바이더: {name} (사용 가능: {', '.join(SPECS)})")
    return SPECS[name]


class ProviderError(RuntimeError):
    """프로바이더 호출 실패 전반."""


class Unavailable(ProviderError):
    """키가 없거나 SDK 가 없거나 서버가 안 떠 있을 때."""


class Refused(ProviderError):
    """모델/안전장치가 요청을 거절했을 때."""

    def __init__(self, category: str | None = None, explanation: str | None = None):
        self.category = category
        self.explanation = explanation
        super().__init__(f"모델이 요청을 거절했습니다 ({category or '사유 미상'})")


def build(name: str, cfg: dict):
    """설정 dict 로 어댑터를 만든다.

    cfg: {api_key, model, base_url, max_tokens, effort}
    """
    s = spec(name)
    if s.kind == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(s, cfg)
    from .openai_compat import OpenAICompatProvider

    return OpenAICompatProvider(s, cfg)


def public_specs() -> list[dict]:
    return [
        {
            "name": s.name,
            "label": s.label,
            "needs_key": s.needs_key,
            "default_model": s.default_model,
            "model_choices": s.model_choices,
            "default_base_url": s.default_base_url,
            "base_url_editable": s.base_url_editable,
            "supports_effort": s.supports_effort,
            "default_effort": s.default_effort,
            "key_hint": s.key_hint,
            "note": s.note,
        }
        for s in SPECS.values()
    ]
