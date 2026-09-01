"""메모를 파일 하나로 내보내고, 그 파일을 다시 읽어들이기.

사람끼리 주고받는 용도다. 그래서 형식은 사람이 열어봐도 읽히는 JSON 이고,
받는 쪽이 무엇을 받았는지 확인한 뒤 넣을 수 있게 미리보기를 거친다.

주고받는 것은 '적은 내용'뿐이다. 임베딩·추출 결과는 넣지 않는다 —
받는 쪽 임베딩 모델이 다를 수 있고, 파생물은 어차피 다시 만들면 되기 때문이다.
파일 크기도 수십 배 차이가 난다.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from . import db, search, store
from .importers import clean

FORMAT = "memoinall/memos"
VERSION = 1
MAX_BYTES = 50_000_000  # 받은 파일이 이보다 크면 사고로 본다


def content_uid(created_at: str | None, body: str) -> str:
    """내용으로 정하는 식별자.

    같은 메모를 두 번 받아도(또는 돌고 돌아 자기 것을 다시 받아도) 중복되지
    않게 하려면 보내는 쪽 DB 의 id 가 아니라 내용이 기준이어야 한다.

    받아들일 때 clean() 을 거치면 공백·빈 줄이 조금 달라진다. 같은 함수로
    맞춰두지 않으면 A→B→A 로 돌아온 자기 메모가 남남이 되어 중복으로 쌓인다.
    """
    raw = f"{(created_at or '')}\n{clean(body)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- 내보내기


def select(
    *,
    ids: list[int] | None = None,
    q: str = "",
    tag: str | None = None,
    tags: list[str] | None = None,
    person: str | None = None,
    source: str | None = None,
    since: str | None = None,
    until: str | None = None,
    archived: bool = False,
    limit: int = 10000,
) -> list[dict]:
    """내보낼 메모를 고른다. 아무 조건도 없으면 전체.

    조건은 AND 로 겹친다 — '결제 관련 + 지난달' 처럼 좁혀 보낼 수 있어야 한다.
    태그를 여럿 주면 전부 달린 메모만 나간다.
    """
    if ids:
        # 화면에서 직접 고른 경우. 순서는 고른 쪽이 아니라 시간순으로 맞춘다.
        rows = [store.get_memo(i, with_facets=False) for i in _existing(ids)]
        rows.sort(key=lambda m: m["created_at"] or "")
        return rows[:limit]

    if q.strip():
        hits = search.search(q, limit=limit, tag=tag, tags=tags, person=person,
                             since=since, until=until)
        rows = [store.get_memo(h["id"], with_facets=False) for h in hits]
    else:
        rows = store.list_memos(
            limit=limit, tag=tag, tags=tags, person=person,
            since=since, until=until, archived=archived,
        )
    if source:
        rows = [m for m in rows if (m.get("source") or "") == source]
    rows.sort(key=lambda m: m["created_at"] or "")
    return rows


def _existing(ids: list[int]) -> list[int]:
    """없는 id 는 조용히 버린다 — 화면에서 고른 뒤 지워졌을 수 있다."""
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = db.query(f"SELECT id FROM memos WHERE id IN ({marks})", [int(i) for i in ids])
    return [int(r["id"]) for r in rows]


def build(rows: list[dict], *, note: str = "") -> dict:
    """내보낼 꾸러미를 만든다."""
    memos = []
    for m in rows:
        tags = store.facets_of(m["id"]).get("tag", [])
        memos.append(
            {
                "uid": content_uid(m["created_at"], m["body"]),
                "title": m["title"],
                "body": m["body"],
                "created_at": m["created_at"],
                "updated_at": m["updated_at"],
                "tags": tags,
                # 보낸 쪽에서 어디서 온 메모였는지. 받는 쪽 저장에는 안 쓰고 참고용이다.
                "origin": m.get("source") or "",
            }
        )
    return {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": store.now_iso(),
        "note": note.strip(),
        "count": len(memos),
        "memos": memos,
    }


def dumps(payload: dict) -> str:
    # ensure_ascii=False 라야 한글이 \uXXXX 로 부풀지 않고, 받는 사람이 열어서 읽을 수 있다.
    return json.dumps(payload, ensure_ascii=False, indent=2)


def default_filename(count: int) -> str:
    return f"memoinall-{datetime.now():%Y%m%d-%H%M}-{count}건.json"


def save(payload: dict, path: str | Path) -> Path:
    """파일로 쓴다. 폴더를 주면 그 안에 기본 이름으로 만든다."""
    target = Path(str(path)).expanduser()
    if target.is_dir():
        target = target / default_filename(payload["count"])
    if not target.suffix:
        target = target.with_suffix(".json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dumps(payload), encoding="utf-8")
    return target


def export_dir() -> Path:
    """저장 위치를 안 정했을 때 쓸 폴더. 사람이 찾아갈 수 있는 곳이어야 한다."""
    downloads = Path.home() / "Downloads"
    return downloads if downloads.is_dir() else Path.home()


# --------------------------------------------------------------------------- 읽기


class ExchangeError(ValueError):
    """받은 파일이 우리가 읽을 수 있는 것이 아닐 때. 메시지는 그대로 사용자에게 보인다."""


def parse(text: str) -> dict:
    """받은 파일 내용을 검증해서 꾸러미로. 사용자에게 보일 문장으로만 실패한다."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExchangeError(
            f"메모 파일 형식이 아닙니다 — JSON 을 읽지 못했습니다 ({exc.lineno}번째 줄)."
        ) from exc

    if not isinstance(data, dict) or "memos" not in data:
        raise ExchangeError("메모 파일 형식이 아닙니다 — memos 항목이 없습니다.")
    if data.get("format") not in (FORMAT, None):
        raise ExchangeError(f"다른 프로그램의 파일로 보입니다 (format={data.get('format')!r}).")
    if not isinstance(data["memos"], list):
        raise ExchangeError("메모 파일이 손상됐습니다 — memos 가 목록이 아닙니다.")
    try:
        version = int(data.get("version") or VERSION)
    except (TypeError, ValueError):
        version = VERSION
    if version > VERSION:
        raise ExchangeError(
            f"더 새로운 형식의 파일입니다 (version {version}). memoinall 을 최신으로 올려주세요."
        )
    return data


def read_file(path: str | Path) -> dict:
    file = Path(str(path)).expanduser()
    if not file.exists():
        raise ExchangeError(f"파일을 찾을 수 없습니다: {file}")
    if file.is_dir():
        raise ExchangeError(f"폴더가 아니라 파일을 지정하세요: {file}")
    if file.stat().st_size > MAX_BYTES:
        raise ExchangeError(f"파일이 너무 큽니다 ({file.stat().st_size // 1_000_000}MB).")
    try:
        text = file.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ExchangeError("파일 인코딩을 읽지 못했습니다 — UTF-8 로 저장된 파일이어야 합니다.") from exc
    return parse(text)
