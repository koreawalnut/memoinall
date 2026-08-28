"""Claude (Anthropic) 어댑터 — 네이티브 SDK.

API 관련해서 신경 쓴 것:
  - 적응형 사고(adaptive thinking) + effort 로 깊이를 조절한다.
    temperature/top_p 는 최신 Claude 모델에서 400 이므로 쓰지 않는다.
  - 긴 출력은 스트리밍으로 받는다(비스트리밍은 HTTP 타임아웃 위험).
  - stop_reason == "refusal" 을 content 읽기 전에 확인한다.
  - 안전 분류기 거절 시 서버측 fallback 으로 자동 복구를 시도한다.
  - JSON 은 프리필이 아니라 structured outputs 로 강제한다(프리필은 400).
"""

from __future__ import annotations

import json
import logging

from . import ProviderSpec, Refused, Unavailable

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider:
    kind = "anthropic"

    def __init__(self, spec: ProviderSpec, cfg: dict):
        self.spec = spec
        self.cfg = cfg
        self._client = None
        # SDK 가 서버측 fallback 을 모를 수 있어 한 번 시도해 보고 기억한다.
        self._supports_fallbacks: bool | None = None

    # ------------------------------------------------------------------ 기본
    @property
    def model(self) -> str:
        return self.cfg.get("model") or self.spec.default_model

    def available(self) -> bool:
        return bool(self.cfg.get("api_key"))

    def client(self):
        if self._client is not None:
            return self._client
        if not self.cfg.get("api_key"):
            raise Unavailable("Anthropic API 키가 설정되지 않았습니다.")
        try:
            import anthropic
        except ImportError as exc:
            raise Unavailable("anthropic 패키지가 없습니다.  pip install anthropic") from exc
        kwargs = {"api_key": self.cfg["api_key"]}
        if self.cfg.get("base_url"):
            kwargs["base_url"] = self.cfg["base_url"]
        self._client = anthropic.Anthropic(**kwargs)
        return self._client

    def _params(self, max_tokens: int | None, effort: str | None) -> dict:
        level = effort or self.cfg.get("effort") or "high"
        params = {
            "model": self.model,
            "max_tokens": max_tokens or int(self.cfg.get("max_tokens") or 8000),
        }
        if level == "none":
            # 사고를 끈다. Opus 5 는 effort 가 high 를 넘으면 이 조합이 400 이므로
            # effort 를 아예 보내지 않아 기본값(high)에 맡긴다.
            params["thinking"] = {"type": "disabled"}
        else:
            params["thinking"] = {"type": "adaptive"}
            params["output_config"] = {"effort": level}
        return params

    @staticmethod
    def _text(message) -> str:
        return "".join(b.text for b in message.content if getattr(b, "type", "") == "text").strip()

    @staticmethod
    def _check_refusal(message) -> None:
        """content 를 읽기 전에 호출한다. 거절이면 content 가 비었거나 부분적이다."""
        if getattr(message, "stop_reason", None) != "refusal":
            return
        d = getattr(message, "stop_details", None)
        raise Refused(getattr(d, "category", None), getattr(d, "explanation", None))

    # ------------------------------------------------------------------ 호출
    def complete(self, system: str, user: str, *, max_tokens=None, effort=None) -> str:
        client = self.client()
        kwargs = self._params(max_tokens, effort)
        kwargs.update(system=system, messages=[{"role": "user", "content": user}])
        message = self._stream(client, kwargs)
        self._check_refusal(message)
        return self._text(message)

    def _stream(self, client, kwargs: dict):
        if self._supports_fallbacks is not False:
            try:
                with client.beta.messages.stream(
                    **kwargs, betas=[FALLBACK_BETA], fallbacks="default"
                ) as stream:
                    message = stream.get_final_message()
                self._supports_fallbacks = True
                return message
            except TypeError:
                self._supports_fallbacks = False
                log.info("설치된 anthropic SDK 가 서버측 fallback 을 지원하지 않습니다.")
            except Exception as exc:
                if self._supports_fallbacks is None and _unknown_param(exc):
                    self._supports_fallbacks = False
                    log.info("서버측 fallback 비활성화: %s", exc)
                else:
                    raise
        with client.messages.stream(**kwargs) as stream:
            return stream.get_final_message()

    def complete_json(self, system: str, user: str, schema: dict, *, max_tokens: int = 1200) -> dict:
        client = self.client()
        kwargs = self._params(max_tokens, None)
        kwargs["output_config"] = {
            **kwargs.get("output_config", {}),
            "format": {"type": "json_schema", "schema": schema},
        }
        kwargs.update(system=system, messages=[{"role": "user", "content": user}])
        message = client.messages.create(**kwargs)
        self._check_refusal(message)
        return json.loads(self._text(message))

    def list_models(self) -> list[str]:
        """서버에 실제로 있는 모델 목록. 못 가져오면 빈 목록."""
        try:
            return [m.id for m in self.client().models.list()]
        except Exception as exc:
            log.info("anthropic: 모델 목록 조회 실패 — %s", exc)
            return []

    def test_connection(self) -> dict:
        client = self.client()
        message = client.messages.create(
            model=self.model,
            max_tokens=32,
            messages=[{"role": "user", "content": "연결 확인. '확인'이라고만 답하세요."}],
        )
        self._check_refusal(message)
        return {"model": getattr(message, "model", self.model), "reply": self._text(message)[:60]}


def _unknown_param(exc: Exception) -> bool:
    text = str(exc).lower()
    return "fallback" in text or "unexpected keyword" in text or "unknown field" in text
