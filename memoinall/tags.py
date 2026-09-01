"""직접 정해 두고 쓰는 태그.

메모 안의 `#태그` 는 지금도 자동으로 뽑히지만(facets), 그건 '적힌 것'이라
오타든 한 번 쓰고 만 것이든 다 섞인다. 여기서 다루는 것은 '쓰기로 정한' 태그다.
적을 때 골라 넣고, 조회·내보내기·가져오기 조건으로 쓴다.

태그의 진짜 저장소는 여전히 메모 본문이다. 이 표는 목록일 뿐이다 —
본문이 원본이어야 내보낸 파일을 받은 쪽에서도 태그가 그대로 살아난다.
"""

from __future__ import annotations

import re

from . import db, extract, store

# 본문에서 태그를 뽑는 규칙과 같은 글자만 허용한다. 여기서만 넓게 받으면
# 등록은 되는데 본문에 넣어도 태그로 인식이 안 되는 태그가 생긴다.
CHARS = r"0-9A-Za-z가-힣_/\-"
NAME_RE = re.compile(f"^[{CHARS}]{{1,40}}$")

# 색은 고르는 게 아니라 주어진 것에서 집는다 — 자유 입력이면 화면이 금방 지저분해진다.
COLORS = ["slate", "blue", "green", "amber", "red", "purple", "teal", "pink"]
MAX_TAGS = 200


class TagError(ValueError):
    """사용자에게 그대로 보여줄 메시지만 담는다."""


def normalize(raw: str) -> str:
    """'#결제 ' → '결제', '결제 팀' → '결제_팀'. 못 쓰는 이름이면 예외."""
    name = str(raw or "").strip().lstrip("#").strip()
    name = re.sub(r"\s+", "_", name)
    if not name:
        raise TagError("태그 이름을 입력하세요.")
    if len(name) > 40:
        raise TagError(f"태그가 너무 깁니다 (40자까지, 지금 {len(name)}자).")
    if not NAME_RE.match(name):
        bad = "".join(sorted({ch for ch in name if not re.match(f"[{CHARS}]", ch)}))
        raise TagError(f"태그에 쓸 수 없는 문자가 있습니다: {bad}  (한글·영문·숫자·_ - / 만 됩니다)")
    return name


# --------------------------------------------------------------------------- 조회


def all_tags(*, with_counts: bool = True) -> list[dict]:
    """정해 둔 태그 전부. 화면에 그리는 순서 그대로."""
    rows = db.query("SELECT * FROM tag_defs ORDER BY sort, name")
    counts = _usage() if with_counts else {}
    return [
        {
            "name": r["name"],
            "color": r["color"] or "slate",
            "note": r["note"] or "",
            "sort": r["sort"],
            "count": counts.get(r["name"], 0),
        }
        for r in rows
    ]


def names() -> list[str]:
    return [r["name"] for r in db.query("SELECT name FROM tag_defs ORDER BY sort, name")]


def exists(name: str) -> bool:
    return db.one("SELECT 1 FROM tag_defs WHERE name=?", (name,)) is not None


def _usage() -> dict[str, int]:
    return {
        r["value"]: r["n"]
        for r in db.query(
            "SELECT f.value, COUNT(*) n FROM facets f JOIN memos m ON m.id=f.memo_id "
            "WHERE f.kind='tag' AND m.archived=0 GROUP BY f.value"
        )
    }


def unregistered(limit: int = 12) -> list[dict]:
    """본문에서는 자주 쓰는데 아직 등록 안 한 태그.

    처음 쓰는 사람이 목록을 맨손으로 채우게 두면 이 기능을 안 쓰게 된다.
    """
    known = set(names())
    return [t for t in store.top_facets("tag", limit=limit + len(known)) if t["value"] not in known][:limit]


# --------------------------------------------------------------------------- 쓰기


def add(name: str, *, color: str = "", note: str = "") -> dict:
    name = normalize(name)
    if exists(name):
        raise TagError(f"이미 있는 태그입니다: #{name}")
    if len(names()) >= MAX_TAGS:
        raise TagError(f"태그는 {MAX_TAGS}개까지 만들 수 있습니다.")
    nxt = db.one("SELECT COALESCE(MAX(sort), 0) + 1 AS n FROM tag_defs")["n"]
    db.execute(
        "INSERT INTO tag_defs(name, color, note, sort, created_at) VALUES(?,?,?,?,?)",
        (name, _color(color, name), str(note or "").strip()[:200], nxt, store.now_iso()),
    )
    return get(name)


def get(name: str) -> dict:
    row = db.one("SELECT * FROM tag_defs WHERE name=?", (name,))
    if not row:
        raise TagError(f"없는 태그입니다: #{name}")
    return {
        "name": row["name"],
        "color": row["color"] or "slate",
        "note": row["note"] or "",
        "sort": row["sort"],
        "count": _usage().get(row["name"], 0),
    }


def update(name: str, *, color: str | None = None, note: str | None = None) -> dict:
    if not exists(name):
        raise TagError(f"없는 태그입니다: #{name}")
    if color is not None:
        db.execute("UPDATE tag_defs SET color=? WHERE name=?", (_color(color, name), name))
    if note is not None:
        db.execute("UPDATE tag_defs SET note=? WHERE name=?", (str(note).strip()[:200], name))
    return get(name)


def rename(old: str, new: str) -> dict:
    """이름을 바꾸고 본문의 `#옛이름` 도 같이 고친다.

    목록만 바꾸면 메모에 남은 옛 태그가 미아가 된다 — 본문이 원본이기 때문이다.
    """
    if not exists(old):
        raise TagError(f"없는 태그입니다: #{old}")
    new = normalize(new)
    if new == old:
        return get(old)
    if exists(new):
        raise TagError(f"이미 있는 태그입니다: #{new}")

    changed = _rewrite_bodies(old, new)
    db.execute("UPDATE tag_defs SET name=? WHERE name=?", (new, old))
    out = get(new)
    out["memos_changed"] = changed
    return out


def remove(name: str, *, purge: bool = False) -> dict:
    """목록에서 뺀다. purge 면 메모 본문에서도 지운다.

    기본이 purge=False 인 이유 — 목록에서 빼는 것과 적어둔 내용을 고치는 것은
    전혀 다른 일이다. 말없이 본문을 건드리면 안 된다.
    """
    if not exists(name):
        raise TagError(f"없는 태그입니다: #{name}")
    changed = _rewrite_bodies(name, None) if purge else 0
    db.execute("DELETE FROM tag_defs WHERE name=?", (name,))
    return {"name": name, "purged": purge, "memos_changed": changed}


def reorder(order: list[str]) -> list[dict]:
    """화면에 나올 순서. 목록에 없는 이름은 무시하고, 빠진 것은 뒤로 민다."""
    known = names()
    seen, rank = set(), 0
    for name in order:
        if name in known and name not in seen:
            rank += 1
            seen.add(name)
            db.execute("UPDATE tag_defs SET sort=? WHERE name=?", (rank, name))
    for name in known:  # 순서를 안 준 것들은 뒤에 그대로
        if name not in seen:
            rank += 1
            db.execute("UPDATE tag_defs SET sort=? WHERE name=?", (rank, name))
    return all_tags()


def adopt(candidates: list[str]) -> list[dict]:
    """본문에 이미 쓰던 태그를 목록으로 데려온다. 이미 있는 것은 조용히 건너뛴다."""
    added = []
    for raw in candidates:
        try:
            name = normalize(raw)
        except TagError:
            continue
        if not exists(name):
            added.append(add(name))
    return added


def _color(color: str, name: str) -> str:
    color = str(color or "").strip().lower()
    if color in COLORS:
        return color
    # 안 고르면 이름에서 정한다 — 같은 태그는 늘 같은 색이어야 눈에 익는다.
    return COLORS[sum(ord(ch) for ch in name) % len(COLORS)]


# --------------------------------------------------------------------------- 본문 고치기


def _tag_pattern(name: str) -> re.Pattern:
    """`#결제` 는 잡고 `#결제팀` 은 건드리지 않는다."""
    return re.compile(f"(^|\\s)#{re.escape(name)}(?![{CHARS}])", re.MULTILINE)


def _rewrite_bodies(name: str, new_name: str | None) -> int:
    """본문의 태그를 바꾸거나(new_name) 지운다(None). 바꾼 메모 수를 돌려준다.

    facets 도 여기서 같이 고친다. update_memo 는 재추출을 백그라운드 큐에 맡기는데,
    그 사이 조회·내보내기가 옛 태그로 걸리기 때문이다 — 워커가 없는 CLI 에서는
    영영 안 고쳐진다. 뭐가 바뀌었는지 이미 아는 자리라 직접 고치는 게 맞다.
    """
    pattern = _tag_pattern(name)
    hits = db.query(
        "SELECT m.id, m.body FROM memos m JOIN facets f ON f.memo_id=m.id "
        "WHERE f.kind='tag' AND f.value=?",
        (name,),
    )
    changed = 0
    for row in hits:
        body = pattern.sub((r"\1#" + new_name) if new_name else "", row["body"])
        # 태그만 있던 줄이 빈 줄로 남지 않게 다듬는다
        body = re.sub(r"[ \t]+\n", "\n", body)
        body = re.sub(r"\n{3,}", "\n\n", body).strip()
        if body == row["body"] or not body:
            continue  # 본문이 통째로 사라지는 경우는 건드리지 않는다 — 태그뿐인 메모
        memo_id = int(row["id"])
        store.update_memo(memo_id, body)
        db.execute("DELETE FROM facets WHERE memo_id=? AND kind='tag' AND value=?", (memo_id, name))
        if new_name:
            # 그 메모가 새 이름을 이미 갖고 있을 수 있다 — 그때는 그냥 둔다
            db.execute(
                "INSERT OR IGNORE INTO facets(memo_id, kind, value) VALUES(?,'tag',?)",
                (memo_id, new_name),
            )
        changed += 1
    return changed


# --------------------------------------------------------------------------- 본문에 붙이기


def append_to_body(body: str, wanted: list[str]) -> str:
    """본문에 없는 태그만 뒤에 붙인다. 이미 적어둔 것은 두 번 붙이지 않는다."""
    body = str(body or "")
    have = {t.lower() for t in extract.TAG_RE.findall(body)}
    add_these = []
    for raw in wanted:
        try:
            name = normalize(raw)
        except TagError:
            continue
        if name.lower() not in have and name.lower() not in {t.lower() for t in add_these}:
            add_these.append(name)
    if not add_these:
        return body
    joined = " ".join("#" + t for t in add_these)
    return (body.rstrip() + "\n" + joined) if body.strip() else joined
