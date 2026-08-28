"""런타임 설정 — 웹 UI 에서 바꾸고 DB 에 남는 값들.

우선순위: DB(사용자가 UI 에서 지정) > 환경변수 > 기본값.
UI 로 명시적으로 설정한 값이 환경변수를 이기는 게 덜 놀랍기 때문이다.
API 키는 절대 원문으로 되돌려주지 않는다(마스킹만).

프로바이더별 키/모델/base_url 은 `llm.<프로바이더>.<필드>` 로 따로 둔다.
한 칸을 돌려쓰면 Claude 에서 Gemini 로 바꿨다가 되돌아올 때 키를 다시 넣어야 한다.
"""

from __future__ import annotations

import os

from . import config, db, providers

PREFIX = "settings."

# key -> (환경변수명, 기본값, 비밀여부)
Entry = tuple[str | None, str, bool]

# 프로바이더별 환경변수 관례
PROVIDER_ENV = {
    "anthropic": {"api_key": "ANTHROPIC_API_KEY", "model": "MEMOINALL_LLM_MODEL", "base_url": "ANTHROPIC_BASE_URL"},
    "openai": {"api_key": "OPENAI_API_KEY", "model": None, "base_url": "OPENAI_BASE_URL"},
    "gemini": {"api_key": "GEMINI_API_KEY", "model": None, "base_url": None},
    "ollama": {"api_key": None, "model": None, "base_url": "OLLAMA_BASE_URL"},
}


def _build_schema() -> dict[str, Entry]:
    schema: dict[str, Entry] = {
        "llm.provider": ("MEMOINALL_LLM_PROVIDER", providers.DEFAULT_PROVIDER, False),
        "llm.max_tokens": ("MEMOINALL_LLM_MAX_TOKENS", "8000", False),
        "gen.budget_tokens": ("MEMOINALL_GEN_BUDGET", "6000", False),
        "gen.query_count": ("MEMOINALL_GEN_QUERIES", "3", False),
        # Redmine 가져오기 — 주소/키는 매번 입력하지 않도록 저장한다.
        "import.redmine.url": ("REDMINE_URL", "", False),
        "import.redmine.api_key": ("REDMINE_API_KEY", "", True),
        "import.redmine.projects": (None, "", False),
        "import.redmine.kinds": (None, "issues,wiki,documents", False),
        "import.redmine.limit": (None, "300", False),
        "import.redmine.since": (None, "", False),
    }
    for name, spec in providers.SPECS.items():
        env = PROVIDER_ENV.get(name, {})
        if spec.needs_key:
            schema[f"llm.{name}.api_key"] = (env.get("api_key"), "", True)
        schema[f"llm.{name}.model"] = (env.get("model"), spec.default_model, False)
        schema[f"llm.{name}.base_url"] = (env.get("base_url"), spec.default_base_url, False)
        # 추론 강도는 프로바이더마다 적정값이 다르다. Claude 는 high 가 좋고,
        # 작은 로컬 모델은 추론을 켜면 답을 못 낸다. 그래서 항목을 따로 둔다.
        schema[f"llm.{name}.effort"] = (
            "MEMOINALL_LLM_EFFORT" if name == providers.DEFAULT_PROVIDER else None,
            spec.default_effort,
            False,
        )
    return schema


SCHEMA: dict[str, Entry] = _build_schema()

# "none" = 추론 끄기. 작은 로컬 모델에는 사실상 필수다.
EFFORT_CHOICES = ["none", "low", "medium", "high", "xhigh", "max"]
EFFORT_LABELS = {
    "none": "추론 끄기 (가장 빠름)",
    "low": "낮음",
    "medium": "보통",
    "high": "높음 (권장)",
    "xhigh": "매우 높음",
    "max": "최대 (느리고 비쌈)",
}

# 프로바이더가 여러 개가 되기 전의 평평한 키들. 있으면 옮겨준다.
LEGACY_MOVES = {"llm.api_key": "llm.anthropic.api_key", "llm.model": "llm.anthropic.model"}


def migrate_legacy() -> None:
    """단일 프로바이더 시절 키를 anthropic 항목으로 옮긴다. 한 번만 동작한다."""
    if db.get_meta(PREFIX + "_migrated_providers"):
        return
    for old, new in LEGACY_MOVES.items():
        value = db.get_meta(PREFIX + old)
        if value and not db.get_meta(PREFIX + new):
            db.set_meta(PREFIX + new, value)
        if value is not None:
            db.set_meta(PREFIX + old, "")
    db.set_meta(PREFIX + "_migrated_providers", "1")


def get(key: str) -> str:
    """DB → 환경변수 → 기본값 순으로 해석."""
    if key not in SCHEMA:
        raise KeyError(key)
    env_name, default, _ = SCHEMA[key]
    stored = db.get_meta(PREFIX + key)
    if stored:
        return stored
    if env_name:
        from_env = os.environ.get(env_name, "")
        if from_env:
            return from_env
    return default


def source_of(key: str) -> str:
    """이 값이 어디서 왔는지 — UI 에서 '환경변수로 설정됨'을 보여주기 위해."""
    env_name, _, _ = SCHEMA[key]
    if db.get_meta(PREFIX + key):
        return "설정"
    if env_name and os.environ.get(env_name):
        return "환경변수"
    return "기본값"


def set_many(values: dict[str, str]) -> None:
    for key, value in values.items():
        if key not in SCHEMA:
            continue
        # 빈 문자열은 '해제'로 본다 — 환경변수/기본값으로 되돌아간다.
        db.set_meta(PREFIX + key, "" if value is None else str(value))


def get_int(key: str, fallback: int) -> int:
    try:
        return int(get(key))
    except (ValueError, TypeError):
        return fallback


def mask(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 12:
        return "•" * len(secret)
    return f"{secret[:7]}…{secret[-4:]}"


# --------------------------------------------------------------------------- 프로바이더


def provider_name() -> str:
    name = get("llm.provider")
    return name if name in providers.SPECS else providers.DEFAULT_PROVIDER


def provider_config(name: str | None = None) -> dict:
    """어댑터에 넘길 설정 뭉치."""
    name = name or provider_name()
    spec = providers.spec(name)
    return {
        "api_key": get(f"llm.{name}.api_key") if spec.needs_key else "",
        "model": get(f"llm.{name}.model"),
        "base_url": get(f"llm.{name}.base_url"),
        "max_tokens": get_int("llm.max_tokens", 8000),
        "effort": get(f"llm.{name}.effort"),
    }


def provider_ready(name: str | None = None) -> bool:
    name = name or provider_name()
    spec = providers.spec(name)
    if not spec.needs_key:
        return True  # Ollama 는 키가 없어도 '설정됨' — 실제 연결은 테스트로 확인
    return bool(get(f"llm.{name}.api_key"))


def public_view() -> dict:
    """UI 로 내보내는 형태. 비밀값은 마스킹해서 나간다."""
    out: dict[str, dict] = {}
    for key, (env_name, default, secret) in SCHEMA.items():
        value = get(key)
        out[key] = {
            "value": mask(value) if secret else value,
            "configured": bool(value),
            "secret": secret,
            "source": source_of(key),
            "env": env_name,
            "default": default,
        }
    active = provider_name()
    return {
        "settings": out,
        "provider": active,
        "providers": [
            {**s, "ready": provider_ready(s["name"])} for s in providers.public_specs()
        ],
        "llm_ready": provider_ready(active),
        "effort_choices": [{"value": v, "label": EFFORT_LABELS[v]} for v in EFFORT_CHOICES],
        "db_path": str(config.DB_PATH),
    }
