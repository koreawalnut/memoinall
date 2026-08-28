"""전체 스모크 테스트.  python tests/test_memoinall.py

임시 DB 를 쓰고, 기본은 해시 임베더(모델 다운로드 없음)로 돈다.
실제 모델로 검색 품질까지 보려면:  python tests/test_memoinall.py --real
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

REAL = "--real" in sys.argv
TMP = tempfile.mkdtemp(prefix="memoinall-test-")
os.environ["MEMOINALL_HOME"] = TMP
if not REAL:
    os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memoinall import context, db, embed, extract, organize, search, store, textutil  # noqa: E402

PASS = FAIL = 0


def check(label: str, cond, extra="") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {extra}")
    return bool(cond)


def section(name: str) -> None:
    print(f"\n[{name}]")


SEED = [
    "결제 모듈 타임아웃 계속 남. @김민수 랑 얘기해봐야 함 #결제 #장애\n[ ] APM 로그 3일치 뽑기",
    "신규 온보딩 화면 초안 봤는데 단계가 너무 많다. 3단계로 줄이는 게 나을 듯. #기획\n결정: 온보딩 3단계로 축소",
    "다음주 화요일 스프린트 회고. 회고 안건 미리 정리해야 함 #회고",
    "PG사 응답 지연이 결제 실패의 주원인인 것 같다. 재시도 정책 다시 설계할 것 #결제",
    "채용 면접 2건. @박서연 코드 리뷰 과제 준비 필요함 #채용",
    "온보딩 이탈률 데이터 뽑아보니 2단계에서 40% 빠짐. 역시 단계 줄이는 게 맞다 #기획 #데이터",
    "https://example.com/postmortem 링크 나중에 읽기. 2026-08-03 까지 #참고",
    "왜 결제 재시도가 중복 승인을 만드는 걸까? 멱등키 확인해야 한다 #결제",
]


def main() -> int:
    db.init()
    if REAL:
        embed.ensure_loaded_async()
        for _ in range(300):
            if embed.status()["state"] in {"ready", "failed"}:
                break
            time.sleep(0.5)
    print("embedder:", embed.status())

    section("텍스트 유틸")
    check("한글 n-gram", textutil.ngrams("회의록") == ["회의", "의록"], textutil.ngrams("회의록"))
    check("라틴 토큰 유지", "api" in textutil.ngrams("API 문서"), textutil.ngrams("API 문서"))
    check("문장 분리", len(textutil.sentences("회고 잡자. 안건 정리해야 함")) == 2)
    long_text = "\n\n".join(["가" * 300] * 5)
    pieces = textutil.chunk(long_text, 420, 700)
    check("긴 메모 청킹", len(pieces) > 1 and all(len(p) <= 700 for p in pieces), [len(p) for p in pieces])

    section("규칙 추출")
    from datetime import datetime

    now = datetime(2026, 7, 27)
    e = extract.extract(SEED[0], now)
    check("태그", set(e["tag"]) == {"결제", "장애"}, e["tag"])
    check("사람", e["person"] == ["김민수"], e["person"])
    check("체크박스 할일", any("APM" in t["text"] for t in e["todos"]), e["todos"])
    check("어미 기반 할일", any("얘기해봐야" in t["text"] for t in e["todos"]), e["todos"])
    check("결정", extract.extract(SEED[1], now)["decision"] == ["온보딩 3단계로 축소"])
    e7 = extract.extract(SEED[6], now)
    check("링크", e7["link"] == ["https://example.com/postmortem"], e7["link"])
    check("절대 날짜", "2026-08-03" in e7["date"], e7["date"])
    check("상대 날짜(다음주)", "2026-08-03" in extract.extract("다음주 회의", now)["date"], extract.extract("다음주 회의", now)["date"])
    check("질문", extract.extract(SEED[7], now)["question"], extract.extract(SEED[7], now)["question"])

    # 회귀: 보고서 문투가 통째로 할일로 잡히던 문제. 액션 표현은 문장 끝에만 유효하다.
    prose = (
        "연간 1억 건 이상의 고객 응대 업무를 처리할 것으로 기대.\n"
        "비용을 20% 절감할 것으로 기대된다.\n"
        "정보보호 가이드라인 필요함에 따라 2가지 방안을 제시함.\n"
        "수익 향상에 기여할 것으로 추정"
    )
    check("산문은 할일이 아님", extract.extract(prose, now)["todos"] == [],
          [t["text"] for t in extract.extract(prose, now)["todos"]])
    real_todos = extract.extract("보고서 초안 다시 볼 것\n감사 자료 준비 필요함\n#업무", now)["todos"]
    check("진짜 할일은 여전히 잡힘", len(real_todos) == 2, [t["text"] for t in real_todos])
    check("해시태그 뒤 앵커 처리", extract.extract("과제 준비 필요함 #채용", now)["todos"], "태그 뒤")
    adnominal = extract.extract("보고서 다시 볼 것\n스크립트 만들 것\n자료 읽을 것", now)["todos"]
    check("관형형 -ㄹ 것 인식", len(adnominal) == 3, [t["text"] for t in adnominal])
    check("'이 것/그 것'은 제외", extract.extract("중요한 건 그 것", now)["todos"] == [],
          extract.extract("중요한 건 그 것", now)["todos"])
    # 회귀: 가져온 계약서·공고문의 합쇼체 의무 조항이 내 할일로 잡히던 문제
    formal = extract.extract(
        "계약금은 지정된 계좌로 납부하여야 합니다.\n출입국사실증명서를 사업주체에 제출해야 합니다.", now
    )["todos"]
    check("합쇼체 문서 조항은 할일 아님", formal == [], [t["text"] for t in formal])
    check("문장 단위 분리", all(len(t["text"]) < 60 for t in extract.extract(SEED[2], now)["todos"]),
          extract.extract(SEED[2], now)["todos"])

    section("저장 · 보강")
    ids = []
    for text in SEED:
        memo = store.add_memo(text, source="test")
        store.enrich(memo["id"])
        ids.append(memo["id"])
    st = store.stats()
    check("메모 수", st["memos"] == len(SEED), st["memos"])
    check("임베딩 생성", st["embedded"] > 0, st["embedded"])
    check("빈 메모 거부", _raises(lambda: store.add_memo("   ")))

    section("검색")
    for q, expect in [("결제", "결제"), ("온보딩 단계", "온보딩"), ("회고", "회고")]:
        hits = search.search(q, limit=5)
        check(f"q={q!r}", hits and expect in hits[0]["body"], hits[0]["body"][:40] if hits else "없음")
    check("조사 결합 부분일치", len(search.search("모듈")) > 0)
    check("AND 실패 시 OR 낙하", len(search.search("결제 모듈 온보딩 채용 회고 데이터")) > 0)
    check("빈 질의 → 최근순", len(search.search("")) > 0)
    check("태그 필터", all("결제" in h["facets"].get("tag", []) for h in search.search("결제", tag="결제")))
    check("유사 메모", len(search.similar(ids[1])) > 0)

    section("컨텍스트 팩")
    pack = context.build("결제 관련해서 무슨 문제가 있었지?", budget_tokens=600)
    check("근거 포함", len(pack["sources"]) > 0, len(pack["sources"]))
    check("예산 준수", pack["used_tokens"] <= 600, f"{pack['used_tokens']}/600")
    check("출처 표기", "[M" in pack["prompt"])
    tight = context.build("결제", budget_tokens=200)
    check("초과분 드롭 기록", tight["dropped"] or len(tight["sources"]) <= 1, len(tight["dropped"]))
    check("근거 없을 때도 프롬프트 생성", "관련 메모 없음" in context.build("zzz없는주제qq", budget_tokens=500)["prompt"]
          or len(context.build("zzz없는주제qq")["sources"]) > 0)

    section("정리")
    cs = organize.cluster()
    check("클러스터 생성", len(cs) >= 2, [(c["label"], c["size"]) for c in cs])
    check("전체 커버", sum(c["size"] for c in cs) == len(SEED), sum(c["size"] for c in cs))
    r = organize.rollup("month")
    check("롤업 집계", r["memo_count"] == len(SEED), r["memo_count"])
    check("롤업 프롬프트", "결정" in organize.rollup_prompt(r))

    section("할일")
    todos = store.open_todos()
    check("미완 할일", len(todos) > 0, len(todos))
    target = todos[0]
    store.toggle_todo(target["id"], True)
    check("완료 토글", len(store.open_todos()) == len(todos) - 1)
    # 회귀: 재보강이 사용자의 완료 체크를 지워버리던 버그
    store.enrich(target["memo_id"])
    still_done = [t for t in store.open_todos() if t["text"] == target["text"]]
    check("재보강 후에도 완료 유지", not still_done, still_done)

    section("수정 · 삭제")
    store.update_memo(ids[2], "회고 일정 목요일로 변경됨 #회고")
    store.enrich(ids[2])
    check("수정 후 재색인", any(h["id"] == ids[2] for h in search.search("목요일")))
    check("옛 내용 사라짐", not any("화요일" in h["body"] for h in search.search("화요일")))
    store.delete_memo(ids[4])
    check("삭제", store.stats()["memos"] == len(SEED) - 1)
    check("FTS 정리", not any(h["id"] == ids[4] for h in search.search("채용")))
    check("삭제된 메모 보강은 무시", store.enrich(ids[4]) is False)
    check("보관 처리", store.set_flag(ids[5], archived=True)["archived"] == 1)
    check("보관 메모는 검색 제외", not any(h["id"] == ids[5] for h in search.search("이탈률")))

    if REAL and embed.status()["state"] == "ready":
        section("의미 검색 (어휘 무중복)")
        cases = [("사용자가 중간에 빠져나가는 문제", "온보딩"), ("멱등성 관련 걱정", "중복 승인")]
        for q, expect in cases:
            hits = search.search(q, limit=3)
            check(f"q={q!r}", any(expect in h["body"] for h in hits[:3]),
                  [h["body"][:24] for h in hits[:3]])

    print(f"\n통과 {PASS} · 실패 {FAIL}")
    return 0 if FAIL == 0 else 1


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except Exception:
        return True


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
