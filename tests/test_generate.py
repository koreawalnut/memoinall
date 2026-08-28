"""설정 · 프로바이더 · 생성 파이프라인 테스트.  python tests/test_generate.py

실제 LLM 호출은 가짜 SDK 클라이언트로 대체한다 — API 키 없이도
각 어댑터가 어떤 파라미터를 보내는지까지 검증한다.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

TMP = tempfile.mkdtemp(prefix="memoinall-gen-")
os.environ["MEMOINALL_HOME"] = TMP
os.environ["MEMOINALL_DISABLE_ST"] = "1"
for _v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
    os.environ.pop(_v, None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memoinall import db, generate, llm, providers, settings, store  # noqa: E402
from memoinall.providers import Refused, Unavailable  # noqa: E402
from memoinall.providers.anthropic_provider import AnthropicProvider  # noqa: E402
from memoinall.providers.openai_compat import OpenAICompatProvider, _parse_json  # noqa: E402

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {extra}")


def section(name):
    print(f"\n[{name}]")


SEED = [
    "AX 세미나 1회차: 금융권 AI 도입 사례 공유. 챗봇 상담 자동화가 핵심 #연수원업무",
    "AX 세미나 2회차: MLOps 파이프라인 필요함. 한글 금융데이터 확보가 과제 #연수원업무",
    "기업금융 연수과정 개편 논의. 실무 케이스 비중을 늘려야 함 #교육기획",
    "신입 행원 온보딩 과정 이탈률이 높다. 단계를 줄이는 게 낫겠다 #교육기획",
    "채용 면접 일정 조율. @박서연 과제 준비 #채용",
]


# --------------------------------------------------------------------------- 가짜 SDK


class AnthMessage:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [type("B", (), {"type": "text", "text": text})()]
        self.stop_reason = stop_reason
        self.stop_details = type("D", (), {"category": "cyber", "explanation": "테스트"})()
        self.model = "fake-claude"


class _Ctx:
    def __init__(self, m):
        self._m = m

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self._m


class FakeAnthropicSDK:
    """anthropic.Anthropic() 흉내. 보낸 파라미터를 전부 기록한다."""

    def __init__(self, text="결과 [M1]", refuse=False, beta_fails=False):
        self.text, self.refuse, self.beta_fails = text, refuse, beta_fails
        self.calls = []
        outer = self

        class Msgs:
            def __init__(self, is_beta):
                self.is_beta = is_beta

            def stream(self, **kw):
                if self.is_beta and outer.beta_fails:
                    raise TypeError("unexpected keyword argument 'fallbacks'")
                outer.calls.append(("beta.stream" if self.is_beta else "stream", kw))
                return _Ctx(AnthMessage(outer.text, "refusal" if outer.refuse else "end_turn"))

            def create(self, **kw):
                outer.calls.append(("create", kw))
                return AnthMessage(outer.text, "refusal" if outer.refuse else "end_turn")

        self.messages = Msgs(False)
        self.beta = type("B", (), {"messages": Msgs(True)})()


class OAIMessage:
    def __init__(self, content, refusal=None):
        self.content = content
        self.refusal = refusal


class OAIResponse:
    def __init__(self, content, finish_reason="stop", refusal=None):
        self.choices = [type("C", (), {"message": OAIMessage(content, refusal), "finish_reason": finish_reason})()]
        self.model = "fake-oai"


class FakeOpenAISDK:
    """openai.OpenAI() 흉내. 특정 파라미터를 거부하도록 설정할 수 있다."""

    def __init__(self, content="결과", reject: set[str] | None = None, connection_error=False, refusal=None):
        self.content, self.reject = content, reject or set()
        self.connection_error, self.refusal = connection_error, refusal
        self.calls = []
        outer = self

        class Completions:
            def create(self, **kw):
                if outer.connection_error:
                    raise ConnectionError("Connection refused")
                outer.calls.append(kw)
                for bad in outer.reject:
                    if bad in kw:
                        raise ValueError(f"400 Unsupported parameter: '{bad}' is not supported")
                return OAIResponse(outer.content, refusal=outer.refusal)

        self.chat = type("Chat", (), {"completions": Completions()})()


class FakeAdapter:
    """llm 디스패처 테스트용 — 프로바이더 내부는 건드리지 않는다."""

    def __init__(self, text="생성물 [M1]", raises=None):
        self.text, self.raises = text, raises
        self.calls = []

    def complete(self, system, user, *, max_tokens=None, effort=None):
        self.calls.append((system, user, max_tokens, effort))
        if self.raises:
            raise self.raises
        return self.text

    def complete_json(self, system, user, schema, *, max_tokens=1200):
        return {"queries": ["가짜질의1", "가짜질의2"], "topic": "가짜주제"}

    def test_connection(self):
        return {"model": "fake", "reply": "확인"}


def use_adapter(**kwargs):
    fake = FakeAdapter(**kwargs)
    llm.adapter = lambda: fake
    settings.set_many({"llm.provider": "anthropic", "llm.anthropic.api_key": "test-key"})
    return fake


def restore_adapter():
    llm.adapter = llm.__dict__["_real_adapter"]
    llm.reset()


def clear_keys():
    settings.set_many({f"llm.{n}.api_key": "" for n in providers.SPECS if providers.spec(n).needs_key})
    settings.set_many({"llm.provider": "anthropic"})
    llm.reset()


def main() -> int:
    db.init()
    llm.__dict__["_real_adapter"] = llm.adapter

    section("프로바이더 레지스트리")
    check("네 종류 등록", set(providers.SPECS) == {"anthropic", "openai", "gemini", "ollama"}, list(providers.SPECS))
    check("Ollama 는 키 불필요", providers.spec("ollama").needs_key is False)
    check("Gemini 는 OpenAI 호환", providers.spec("gemini").kind == "openai_compat")
    check("Gemini base_url 기본값", "generativelanguage" in providers.spec("gemini").default_base_url)
    check("Ollama base_url 기본값", "11434" in providers.spec("ollama").default_base_url)
    check("모든 프로바이더가 effort 지원(미지원 서버는 자동 제거)",
          all(providers.spec(n).supports_effort for n in providers.SPECS))
    check("Ollama 기본은 추론 끄기", providers.spec("ollama").default_effort == "none",
          providers.spec("ollama").default_effort)
    check("Claude 기본은 high", providers.spec("anthropic").default_effort == "high")
    check("알 수 없는 이름 거부", _raises(lambda: providers.spec("없음")))

    section("설정 — 프로바이더별")
    check("기본 프로바이더", settings.provider_name() == "anthropic")
    check("프로바이더별 키 분리", "llm.openai.api_key" in settings.SCHEMA and "llm.gemini.api_key" in settings.SCHEMA)
    check("Ollama 는 키 항목 없음", "llm.ollama.api_key" not in settings.SCHEMA)
    settings.set_many({"llm.anthropic.api_key": "sk-ant-aaa", "llm.openai.api_key": "sk-oai-bbb"})
    check("키가 서로 안 섞임",
          settings.provider_config("anthropic")["api_key"] == "sk-ant-aaa"
          and settings.provider_config("openai")["api_key"] == "sk-oai-bbb")
    settings.set_many({"llm.provider": "openai"})
    check("전환 시 해당 키 사용", settings.provider_config()["api_key"] == "sk-oai-bbb")
    check("전환해도 준비됨", settings.provider_ready() is True)
    settings.set_many({"llm.provider": "gemini"})
    check("키 없는 프로바이더는 미준비", settings.provider_ready() is False)
    settings.set_many({"llm.provider": "ollama"})
    check("Ollama 는 키 없이 준비됨", settings.provider_ready() is True)
    check("Ollama 기본 모델", settings.provider_config()["model"] == "llama3.1")
    clear_keys()

    view = settings.public_view()
    check("public_view 프로바이더 목록", len(view["providers"]) == 4)
    settings.set_many({"llm.anthropic.api_key": "sk-ant-verysecret12345"})
    check("public_view 원문 미노출", "verysecret" not in str(settings.public_view()), "누출!")
    clear_keys()

    section("설정 — 구버전 키 이관")
    db.set_meta("settings.llm.api_key", "sk-ant-legacy999")
    db.set_meta("settings._migrated_providers", "")
    settings.migrate_legacy()
    check("구버전 키가 anthropic 으로 이동", settings.get("llm.anthropic.api_key") == "sk-ant-legacy999")
    check("구버전 키 자리 비움", not db.get_meta("settings.llm.api_key"))
    db.set_meta("settings.llm.api_key", "다시넣음")
    settings.migrate_legacy()
    check("이관은 한 번만", db.get_meta("settings.llm.api_key") == "다시넣음")
    clear_keys()

    section("Claude 어댑터")
    sdk = FakeAnthropicSDK(text="클로드 결과")
    ap = AnthropicProvider(providers.spec("anthropic"), {"api_key": "k", "model": "claude-opus-5", "effort": "high"})
    ap._client = sdk
    check("텍스트 반환", ap.complete("sys", "user") == "클로드 결과")
    kind, kw = sdk.calls[-1]
    check("스트리밍 사용", kind.endswith("stream"), kind)
    check("적응형 사고", kw.get("thinking") == {"type": "adaptive"}, kw.get("thinking"))
    check("effort 전달", kw.get("output_config", {}).get("effort") == "high")
    check("temperature/top_p 미사용", "temperature" not in kw and "top_p" not in kw, list(kw))
    check("서버측 fallback 시도", kind == "beta.stream", kind)

    sdk2 = FakeAnthropicSDK(text="ok", beta_fails=True)
    ap2 = AnthropicProvider(providers.spec("anthropic"), {"api_key": "k"})
    ap2._client = sdk2
    check("fallback 미지원 SDK 도 동작", ap2.complete("s", "u") == "ok")
    check("일반 스트림으로 낙하", sdk2.calls[-1][0] == "stream", sdk2.calls[-1][0])
    check("한 번 확인 후 기억", ap2._supports_fallbacks is False)

    ap3 = AnthropicProvider(providers.spec("anthropic"), {"api_key": "k"})
    ap3._client = FakeAnthropicSDK(refuse=True)
    check("거절 감지", _raises(lambda: ap3.complete("s", "u"), Refused))
    check("키 없으면 Unavailable",
          _raises(lambda: AnthropicProvider(providers.spec("anthropic"), {}).client(), Unavailable))

    section("OpenAI 호환 어댑터 (ChatGPT/Gemini/Ollama)")
    oai = FakeOpenAISDK(content="지피티 결과")
    op = OpenAICompatProvider(providers.spec("openai"), {"api_key": "k", "model": "gpt-5"})
    op._client = oai
    check("텍스트 반환", op.complete("sys", "user") == "지피티 결과")
    kw = oai.calls[-1]
    check("system 이 messages 로", kw["messages"][0]["role"] == "system")
    check("max_tokens 기본 사용", "max_tokens" in kw, list(kw))

    # 회귀: 최신 OpenAI 모델은 max_tokens 를 거부하고 max_completion_tokens 를 요구한다
    strict = FakeOpenAISDK(content="ok", reject={"max_tokens"})
    op2 = OpenAICompatProvider(providers.spec("openai"), {"api_key": "k"})
    op2._client = strict
    check("max_completion_tokens 로 자동 전환", op2.complete("s", "u") == "ok")
    check("전환 결과 기억", op2._tokens_param == "max_completion_tokens")
    check("두 번째 호출은 곧장 맞는 파라미터", "max_completion_tokens" in (op2.complete("s", "u") or "") or
          "max_completion_tokens" in strict.calls[-1], list(strict.calls[-1]))

    noeffort = FakeOpenAISDK(content="ok", reject={"reasoning_effort"})
    op3 = OpenAICompatProvider(providers.spec("openai"), {"api_key": "k", "effort": "high"})
    op3._client = noeffort
    check("reasoning_effort 미지원 서버도 동작", op3.complete("s", "u") == "ok")
    check("미지원 파라미터 기억", "reasoning_effort" in op3._unsupported)

    ollama = FakeOpenAISDK(content="로컬 결과")
    olp = OpenAICompatProvider(providers.spec("ollama"), {"model": "qwen2.5"})
    olp._client = ollama
    check("Ollama 키 없이 동작", olp.complete("s", "u") == "로컬 결과")
    check("Ollama 는 available", olp.available() is True)
    check("reasoning_effort 안 보냄(미지원 프로바이더)", "reasoning_effort" not in ollama.calls[-1])

    down = OpenAICompatProvider(providers.spec("ollama"), {"model": "x"})
    down._client = FakeOpenAISDK(connection_error=True)
    err = _capture(lambda: down.complete("s", "u"))
    check("서버 꺼짐 → 친절한 안내", isinstance(err, Unavailable) and "ollama serve" in str(err), err)

    ref = OpenAICompatProvider(providers.spec("openai"), {"api_key": "k"})
    ref._client = FakeOpenAISDK(content=None, refusal="정책상 거절")
    check("refusal 필드 감지", _raises(lambda: ref.complete("s", "u"), Refused))

    section("OpenAI 호환 — JSON")
    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"], "additionalProperties": False}
    j1 = FakeOpenAISDK(content='{"a": "값"}')
    jp = OpenAICompatProvider(providers.spec("openai"), {"api_key": "k"})
    jp._client = j1
    check("json_schema 모드 성공", jp.complete_json("s", "u", schema) == {"a": "값"})
    check("json_schema 사용됨", j1.calls[-1]["response_format"]["type"] == "json_schema")
    check("모드 기억", jp._json_mode == "schema")

    j2 = FakeOpenAISDK(content='```json\n{"a": "펜스"}\n```', reject={"response_format"})
    jp2 = OpenAICompatProvider(providers.spec("ollama"), {"model": "x"})
    jp2._client = j2
    check("response_format 미지원 → 프롬프트 모드", jp2.complete_json("s", "u", schema) == {"a": "펜스"})
    check("코드펜스 제거", _parse_json('```json\n{"b":1}\n```') == {"b": 1})
    check("앞뒤 설명 제거", _parse_json('네, 결과입니다: {"c":2} 이상입니다') == {"c": 2})

    section("디스패처")
    settings.set_many({"llm.provider": "ollama"})
    check("Ollama 선택 시 available", llm.available() is True)
    check("라벨 반영", "Ollama" in llm.current_label(), llm.current_label())
    settings.set_many({"llm.provider": "anthropic"})
    check("키 없으면 unavailable", llm.available() is False)
    settings.set_many({"llm.anthropic.api_key": "k"})
    check("키 있으면 available", llm.available() is True)
    a1 = llm.adapter()
    check("어댑터 캐시", llm.adapter() is a1)
    settings.set_many({"llm.provider": "ollama"})
    check("설정 바뀌면 새 어댑터", llm.adapter() is not a1)
    check("타입도 바뀜", llm.adapter().kind == "openai_compat")
    clear_keys()

    t = llm.test_connection("openai")
    check("키 없는 프로바이더 테스트", not t["ok"] and "키" in t["message"], t)
    t = llm.test_connection("없는프로바이더")
    check("잘못된 프로바이더 테스트", not t["ok"] and "알 수 없는" in t["message"], t)

    section("질의 도출 (규칙)")
    d = generate.derive_queries("연수원 AX 세미나 내용으로 교육과정 기획안 초안 써줘")
    check("규칙 방식 사용", d["method"] == "rule", d["method"])
    joined = " ".join(d["queries"])
    check("내용어 보존", "세미나" in joined and "교육과정" in joined, joined)
    check("지시 동사 제거", "써줘" not in joined and "초안" not in joined, joined)
    check("빈 지시사항 처리", generate.derive_queries("")["queries"] == [])
    check("단일 절도 여러 각도로", len(d["queries"]) >= 2, d["queries"])
    check("각 질의가 서로 다름", len(set(d["queries"])) == len(d["queries"]), d["queries"])

    section("질의 도출 (LLM)")
    use_adapter()
    d2 = generate.derive_queries("가짜질의 관련 지시사항")
    check("LLM 방식 사용", d2["method"] == "llm", d2["method"])
    check("LLM 질의 사용", d2["queries"] == ["가짜질의1", "가짜질의2"], d2["queries"])
    restore_adapter()

    # 회귀: 작은 모델이 스키마만 맞추고 'DOCUMENT_TITLE' 같은 자리표시자를 채워
    # 그대로 검색에 쓰이던 문제. 실제 qwen3.5:2b 에서 나온 값들이다.
    check("자리표시자 거부", generate._degenerate(["dOCUMENT_TITLE", "AUTHOR_PAPER_ID"], "AX 세미나 정리해줘"))
    check("한글 지시사항인데 한글 없음 거부",
          generate._degenerate(["document title", "author"], "AX 세미나 내용 정리해줘"))
    check("무관한 질의 거부", generate._degenerate(["날씨 예보", "주식 시세"], "결제 모듈 타임아웃 정리"))
    # 회귀: qwen3.5:2b 가 'AI 개발자 회고록摘抄' 처럼 한자를 섞어 냈다
    check("없던 한자 섞임 거부",
          generate._degenerate(["MLOps 팀 구성", "AI 개발자 회고록摘抄"], "MLOps 관련 메모 정리해줘"))
    check("원래 한자가 있으면 허용",
          not generate._degenerate(["漢字 문서"], "漢字 문서 정리해줘"))
    check("정상 질의는 통과", not generate._degenerate(["AX 세미나", "교육과정 기획"], "AX 세미나 내용으로 교육과정 기획안"))
    check("영문 지시사항의 영문 질의는 통과", not generate._degenerate(["payment timeout"], "payment timeout summary"))

    fake_bad = FakeAdapter()
    fake_bad.complete_json = lambda *a, **k: {"queries": ["dOCUMENT_TITLE"], "topic": "x"}
    llm.adapter = lambda: fake_bad
    d3 = generate.derive_queries("AX 세미나 내용 정리해줘")
    check("부적합 시 규칙으로 대체", d3["method"].startswith("rule"), d3["method"])
    check("대체된 질의는 한글", any("세미나" in q for q in d3["queries"]), d3["queries"])
    restore_adapter()
    clear_keys()

    section("메모 준비")
    for text in SEED:
        m = store.add_memo(text, source="test")
        store.enrich(m["id"])
    check("메모 저장", store.stats()["memos"] == len(SEED))

    section("다중 질의 검색")
    hits = generate.multi_search(["AX 세미나", "교육과정 개편"], limit=10)
    check("결과 있음", len(hits) > 0, len(hits))
    check("일치 질의 기록", all(h["matched_queries"] for h in hits))
    check("빈 질의 목록", generate.multi_search([]) == [])

    section("팩 조립")
    pack = generate.build_pack("AX 세미나 내용 정리해줘", budget_tokens=900, fmt="brief")
    check("근거 포함", len(pack["sources"]) > 0, len(pack["sources"]))
    check("예산 준수", pack["used_tokens"] <= 900, f"{pack['used_tokens']}/900")
    check("형식 지시 포함", "브리핑" in pack["prompt"] or "핵심" in pack["prompt"])
    check("출처 표기", "[M" in pack["prompt"])
    check("질의 노출", pack["queries"] and "검색 질의" in pack["prompt"])
    manual = generate.build_pack("아무거나", queries=["채용 면접"], budget_tokens=2000)
    check("지정 질의 사용", manual["queries"] == ["채용 면접"] and manual["query_method"] == "manual")

    section("예산 불변식 (긴 메모)")
    # 회귀: 첫 근거는 무조건 통째로 넣던 탓에 긴 메모 하나가 예산을 3배 넘기던 문제.
    # 실제 데이터(19,000자 노트)에서 예산 3000 → 실사용 10954 로 터졌다.
    big = store.add_memo("AX 세미나 상세 기록. " + ("세부 논의 내용이 아주 길게 이어진다. " * 900), source="test")
    store.enrich(big["id"])
    for budget in (300, 900, 3000):
        p = generate.build_pack("AX 세미나 상세 기록", budget_tokens=budget)
        check(f"예산 {budget} 준수", p["used_tokens"] <= budget, f"{p['used_tokens']}/{budget}")
        check(f"예산 {budget} 에서도 근거 있음", len(p["sources"]) > 0, len(p["sources"]))
    p = generate.build_pack("AX 세미나 상세 기록", budget_tokens=900)
    check("부분 수록 표시", any(s["included_as"] != "full" for s in p["sources"]),
          [s["included_as"] for s in p["sources"]])
    check("프롬프트에 부분 수록 명시", "부분만" in p["prompt"] or "앞부분만" in p["prompt"])
    ctx = __import__("memoinall.context", fromlist=["build"]).build("AX 세미나 상세 기록", budget_tokens=400, full_body=True)
    check("컨텍스트 팩도 예산 준수", ctx["used_tokens"] <= 400, f"{ctx['used_tokens']}/400")
    store.delete_memo(big["id"])

    section("생성")
    clear_keys()
    r = generate.generate("AX 세미나 정리해줘")
    check("미설정 시 출력 없음", r["output"] is None)
    check("미설정 사유 안내", "설정" in r["reason"], r["reason"])
    check("프롬프트는 제공", "[M" in r["prompt"])

    fake = use_adapter(text="## 요약\nMLOps 필요성이 제기됨 [M2]")
    r = generate.generate("AX 세미나 정리해줘", fmt="report")
    check("출력 생성됨", r["output"] and "MLOps" in r["output"], r["output"])
    check("프로바이더 표시", r.get("provider"), r.get("provider"))
    check("시스템 프롬프트에 출처 규칙", "[M{id}]" in fake.calls[-1][0])

    use_adapter(raises=Refused("cyber", "테스트"))
    r = generate.generate("무언가 정리해줘")
    check("거절 시 출력 없음", r["output"] is None)
    check("거절 사유 안내", "거절" in (r["reason"] or ""), r["reason"])

    use_adapter(raises=RuntimeError("네트워크 끊김"))
    r = generate.generate("정리해줘")
    check("예외가 밖으로 안 샘", r["output"] is None and "네트워크 끊김" in r["reason"], r["reason"])
    restore_adapter()

    section("stats 연동")
    clear_keys()
    check("미설정 시 llm_enabled=False", store.stats()["llm_enabled"] is False)
    settings.set_many({"llm.provider": "ollama"})
    check("Ollama 는 enabled", store.stats()["llm_enabled"] is True)
    check("프로바이더 이름 노출", store.stats()["llm_provider"] == "ollama")
    check("모델 이름 노출", store.stats()["llm_model"] == "llama3.1")
    clear_keys()

    print(f"\n통과 {PASS} · 실패 {FAIL}")
    return 0 if FAIL == 0 else 1


def _raises(fn, exc_type=Exception) -> bool:
    try:
        fn()
        return False
    except exc_type:
        return True
    except Exception:
        return False


def _capture(fn):
    try:
        fn()
        return None
    except Exception as exc:
        return exc


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
