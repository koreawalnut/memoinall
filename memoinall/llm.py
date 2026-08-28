"""LLM 계층 — 프로바이더 선택과 앱 수준의 프롬프트.

설계 원칙은 하나다: **LLM 은 의존 대상이 아니라 보강이다.**
키가 없으면 모든 함수가 '완성된 프롬프트'를 돌려주고, 앱의 나머지는 그대로 돈다.

실제 API 호출은 providers/ 로 넘긴다. 여기는 어떤 프로바이더를 쓸지 고르고,
설정이 바뀌면 어댑터를 다시 만드는 일만 한다.
"""

from __future__ import annotations

import logging

from . import providers, settings
from .providers import ProviderError, Refused, Unavailable

log = logging.getLogger(__name__)

# 하위 호환 별칭 — 기존 호출부가 llm.LLMRefused 등을 잡고 있다.
LLMUnavailable = Unavailable
LLMRefused = Refused

_adapter = None
_adapter_key: tuple | None = None


def _config_key(name: str, cfg: dict) -> tuple:
    return (name, cfg.get("api_key"), cfg.get("model"), cfg.get("base_url"))


def adapter():
    """설정이 바뀌면 어댑터를 새로 만든다."""
    global _adapter, _adapter_key
    name = settings.provider_name()
    cfg = settings.provider_config(name)
    key = _config_key(name, cfg)
    if _adapter is None or _adapter_key != key:
        _adapter = providers.build(name, cfg)
        _adapter_key = key
    return _adapter


def reset() -> None:
    """설정 저장 후 캐시된 어댑터를 버린다."""
    global _adapter, _adapter_key
    _adapter, _adapter_key = None, None


def available() -> bool:
    return settings.provider_ready()


def current_label() -> str:
    return providers.spec(settings.provider_name()).label


def complete(system: str, user: str, *, max_tokens: int | None = None, effort: str | None = None) -> str:
    return adapter().complete(system, user, max_tokens=max_tokens, effort=effort)


def complete_json(system: str, user: str, schema: dict, *, max_tokens: int = 1200) -> dict:
    return adapter().complete_json(system, user, schema, max_tokens=max_tokens)


def test_connection(name: str | None = None, overrides: dict | None = None) -> dict:
    """설정 화면의 '연결 테스트'. 실패해도 예외를 밖으로 던지지 않는다.

    overrides 는 아직 저장하지 않은 입력값이다. 저장하기 전에 확인할 수 있어야
    잘못된 키를 커밋하지 않는다.
    """
    name = name or settings.provider_name()
    try:
        spec = providers.spec(name)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}

    cfg = settings.provider_config(name)
    for field in ("api_key", "model", "base_url"):
        value = (overrides or {}).get(field)
        if value:
            cfg[field] = value
    if spec.needs_key and not cfg.get("api_key"):
        return {"ok": False, "message": f"{spec.label} API 키가 설정되지 않았습니다."}

    try:
        info = providers.build(name, cfg).test_connection()
        return {"ok": True, "message": f"연결 성공 — {spec.label} / {info.get('model')}", **info}
    except Unavailable as exc:
        return {"ok": False, "message": str(exc)}
    except Refused as exc:
        return {"ok": True, "message": f"연결은 되지만 이 요청은 거절됐습니다: {exc}"}
    except Exception as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------- 메모 보강

ENRICH_SYSTEM = (
    "너는 개인 업무 메모 정리 도우미다. 주어진 메모에서 제목, 한 줄 요약, 주제 태그를 뽑는다.\n"
    "title 은 40자 이내, summary 는 100자 이내 한 문장, tags 는 최대 4개의 한국어 명사구."
)

ENRICH_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "summary", "tags"],
    "additionalProperties": False,
}


def enrich_memo(body: str) -> dict | None:
    if not available():
        return None
    try:
        data = complete_json(ENRICH_SYSTEM, body[:4000], ENRICH_SCHEMA)
        return {
            "title": str(data.get("title", ""))[:80],
            "summary": str(data.get("summary", ""))[:300],
            "tags": [str(t)[:40] for t in data.get("tags", [])][:4],
        }
    except Exception:
        log.exception("LLM 보강 실패")
        return None


# --------------------------------------------------------------------------- 질의응답

ANSWER_SYSTEM = (
    "너는 사용자의 개인 업무 메모를 근거로만 답하는 어시스턴트다.\n"
    "근거에 없는 내용은 지어내지 말고 '메모에 없습니다'라고 말한다.\n"
    "인용한 문장에는 [M{id}] 형식으로 출처를 붙인다. 한국어로 간결하게 답한다."
)


def _no_key_reason() -> str:
    spec = providers.spec(settings.provider_name())
    what = "서버 주소" if not spec.needs_key else "API 키"
    return (
        f"{spec.label} {what}가 설정되지 않아 프롬프트까지만 만들었습니다. "
        "설정 탭에서 등록하거나, 아래 프롬프트를 복사해 원하는 LLM 에 붙여 넣으세요."
    )


def answer(query: str, *, budget_tokens: int = 3000, **kwargs) -> dict:
    """메모 기반 RAG 응답. 준비가 안 됐으면 컨텍스트 팩만 돌려준다."""
    from . import context

    pack = context.build(query, budget_tokens=budget_tokens, **kwargs)
    if not available():
        return {"answer": None, "reason": _no_key_reason(), **pack}
    try:
        return {"answer": complete(ANSWER_SYSTEM, pack["prompt"]), "reason": None, **pack}
    except ProviderError as exc:
        return {"answer": None, "reason": str(exc), **pack}
    except Exception as exc:
        return {"answer": None, "reason": f"LLM 호출 실패: {type(exc).__name__}: {exc}", **pack}


# --------------------------------------------------------------------------- 브리핑

DIGEST_SYSTEM = (
    "너는 주간 업무 브리핑을 쓰는 비서다. 주어진 메모 집계를 바탕으로\n"
    "1) 이번 기간 핵심 3~5줄 2) 결정된 것 3) 남은 할일 4) 챙겨야 할 리스크/열린 질문\n"
    "순서로 한국어 마크다운을 쓴다. 근거 메모는 [M{id}] 로 표기한다. 없는 내용은 만들지 않는다."
)


def digest(period: str = "week", anchor: str | None = None) -> dict:
    from . import organize

    data = organize.rollup(period, anchor)
    prompt = organize.rollup_prompt(data)
    if not available():
        return {"digest": None, "reason": _no_key_reason(), "prompt": prompt, "data": data}
    try:
        return {"digest": complete(DIGEST_SYSTEM, prompt), "reason": None, "prompt": prompt, "data": data}
    except ProviderError as exc:
        return {"digest": None, "reason": str(exc), "prompt": prompt, "data": data}
    except Exception as exc:
        return {"digest": None, "reason": f"LLM 호출 실패: {type(exc).__name__}: {exc}", "prompt": prompt, "data": data}
