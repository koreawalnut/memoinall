"""Windows 스티커 메모(Microsoft Sticky Notes) 임포터.

데이터는 UWP 패키지의 plum.sqlite 에 들어 있다. 버전마다 컬럼 이름이 조금씩
달라서(Text/NoteText, CreatedAt/CreationTime …) 스키마를 읽어 후보 중 실제로
존재하는 것을 고른다.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from . import Note, clean, open_readonly_copy, parse_any_time

PACKAGE = "Microsoft.MicrosoftStickyNotes_8wekyb3d8bbwe"

TEXT_COLUMNS = ["Text", "NoteText", "Snippet", "Content"]
CREATED_COLUMNS = ["CreatedAt", "CreationTime", "CreatedTime", "Created"]
UPDATED_COLUMNS = ["UpdatedAt", "LastModified", "ModifiedTime", "ChangedAt", "UpdatedTime"]
DELETED_COLUMNS = ["IsDeleted", "DeletedAt", "Deleted"]


def default_path() -> Path:
    local = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData/Local")
    return Path(local) / "Packages" / PACKAGE / "LocalState" / "plum.sqlite"


class StickyNotesImporter:
    name = "sticky"
    label = "Windows 스티커 메모"

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else default_path()

    def available(self) -> bool:
        return self.path.exists()

    def unavailable_reason(self) -> str:
        pkg_dir = self.path.parent
        if not pkg_dir.exists():
            return "스티커 메모 앱이 설치되어 있지 않습니다."
        return "스티커 메모 데이터 파일(plum.sqlite)이 없습니다 — 저장된 메모가 없는 것으로 보입니다."

    def read(self) -> list[Note]:
        conn, tmpdir = open_readonly_copy(self.path)
        try:
            tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            table = next((t for t in ("Note", "Notes", "note") if t in tables), None)
            if not table:
                raise RuntimeError(f"메모 테이블을 찾지 못했습니다. 있는 테이블: {sorted(tables)}")

            cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
            text_col = _pick(cols, TEXT_COLUMNS)
            if not text_col:
                raise RuntimeError(f"본문 컬럼을 찾지 못했습니다. 컬럼: {cols}")
            id_col = _pick(cols, ["Id", "ID", "Guid", "rowid"]) or "rowid"
            created_col = _pick(cols, CREATED_COLUMNS)
            updated_col = _pick(cols, UPDATED_COLUMNS)
            deleted_col = _pick(cols, DELETED_COLUMNS)

            select = [f'"{id_col}" AS ext_id', f'"{text_col}" AS body']
            select.append(f'"{created_col}" AS created' if created_col else "NULL AS created")
            select.append(f'"{updated_col}" AS updated' if updated_col else "NULL AS updated")
            select.append(f'"{deleted_col}" AS deleted' if deleted_col else "NULL AS deleted")

            notes: list[Note] = []
            for row in conn.execute(f'SELECT {", ".join(select)} FROM "{table}"'):
                if _is_deleted(row["deleted"]):
                    continue
                body = clean(_strip_markup(row["body"]))
                if not body:
                    continue
                notes.append(
                    Note(
                        external_id=str(row["ext_id"]),
                        body=body,
                        created_at=parse_any_time(row["created"]),
                        updated_at=parse_any_time(row["updated"]),
                        origin=f"{table}.{text_col}",
                    )
                )
            return notes
        finally:
            conn.close()
            shutil.rmtree(tmpdir, ignore_errors=True)


def _pick(available: list[str], candidates: list[str]) -> str | None:
    lowered = {c.lower(): c for c in available}
    for cand in candidates:
        if cand.lower() in lowered:
            return lowered[cand.lower()]
    return None


def _is_deleted(value) -> bool:
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    return bool(str(value).strip())


# 최신 스티커 메모는 본문을 '문단마다 id 를 붙인' 형태로 저장한다.
#     id=f5024f0e-4c7c-431e-8b7e-2bc941475a6a 네트워크 사용량 확인방법
#     id=f0ebc9a4-4b0b-4065-b659-f82bbe281ec2          ← 내용 없는 줄 = 빈 줄
# 이걸 그대로 넣으면 메모가 온통 GUID 로 뒤덮이고, 제목도 'id=...' 로 잡히며,
# 검색 색인에도 쓸모없는 16진수가 잔뜩 들어간다.
PARAGRAPH_ID_RE = re.compile(
    r"^id=[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?:\s+|$)"
)


def _strip_markup(text: str | None) -> str:
    """스티커 메모 본문에서 저장 형식 부산물을 걷어낸다."""
    if not text:
        return ""
    out = []
    for line in str(text).split("\n"):
        stripped = line.strip()
        # 문단 id 접두어 제거. 완전한 GUID 형태일 때만 지워서
        # 'id=12345' 같은 진짜 메모 내용은 건드리지 않는다.
        stripped = PARAGRAPH_ID_RE.sub("", stripped, count=1).strip()
        if stripped.startswith("\\") and len(stripped) > 1:  # 이스케이프 흔적
            stripped = stripped[1:]
        out.append(stripped)
    return "\n".join(out)
