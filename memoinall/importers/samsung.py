"""Samsung Notes 임포터.

본문이 한 컬럼에 얌전히 들어있지 않다. 노트 종류(타이핑/손글씨/PDF)에 따라
살아있는 컬럼이 달라서, 실측한 커버리지 순서대로 폴백 체인을 태운다.

    TextSearchDB.StrippedContent  (타이핑 본문)
    NoteDB.StrippedContent
    TextSearchDB.HWTextContent    (손글씨 인식 결과)
    NoteDB.InsertedTextboxContents
    TextSearchDB/NoteDB.PDFTextContents

폴더명은 태그로 붙인다. 단, '폴더' 같은 기본 이름은 정보가 없으므로 버린다.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import Note, clean, epoch_ms_to_iso

PACKAGE = "SAMSUNGELECTRONICSCoLtd.SamsungNotes_wyx1vj98g3asy"

# 태그로 삼을 가치가 없는 기본 폴더명
GENERIC_FOLDERS = {"폴더", "folder", "notes", "노트", "uncategorized", "미분류", ""}


def default_path() -> Path:
    local = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData/Local")
    return Path(local) / "Packages" / PACKAGE / "LocalState" / "Storage.sqlite"


QUERY = """
SELECT
    n.UUID                                   AS uuid,
    NULLIF(TRIM(COALESCE(n.Title, '')), '')  AS title,
    NULLIF(TRIM(COALESCE(n.RecommendedTitle, '')), '') AS rec_title,
    n.CreatedAt                              AS created,
    n.LastModifiedAt                         AS updated,
    NULLIF(TRIM(COALESCE(t.StrippedContent, '')), '')        AS t_stripped,
    NULLIF(TRIM(COALESCE(n.StrippedContent, '')), '')        AS n_stripped,
    NULLIF(TRIM(COALESCE(t.HWTextContent, '')), '')          AS hw,
    NULLIF(TRIM(COALESCE(n.InsertedTextboxContents, '')), '') AS textbox,
    NULLIF(TRIM(COALESCE(t.PDFTextContents, n.PDFTextContents, '')), '') AS pdf,
    NULLIF(TRIM(COALESCE(c.DisplayName, '')), '')            AS folder
FROM NoteDB n
LEFT JOIN TextSearchDB   t ON t.UUID = n.UUID
LEFT JOIN CategoryTreeDB c ON c.UUID = n.CategoryUUID
WHERE COALESCE(n.DeletedStatus, 0) = 0
  AND COALESCE(n.IsFolderDeleted, 0) = 0
ORDER BY n.CreatedAt
"""

# (컬럼, 이 컬럼에서 왔다는 표시)
FALLBACK_CHAIN = [
    ("t_stripped", "text"),
    ("n_stripped", "text"),
    ("hw", "손글씨"),
    ("textbox", "텍스트박스"),
    ("pdf", "PDF"),
]


class SamsungNotesImporter:
    name = "samsung"
    label = "Samsung Notes"

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else default_path()

    def available(self) -> bool:
        return self.path.exists()

    def unavailable_reason(self) -> str:
        if not self.path.parent.exists():
            return "Samsung Notes 가 설치되어 있지 않습니다."
        return "Samsung Notes 데이터 파일(Storage.sqlite)이 없습니다."

    def read(self) -> list[Note]:
        from . import open_readonly_copy

        conn, tmpdir = open_readonly_copy(self.path)
        try:
            notes: list[Note] = []
            for row in conn.execute(QUERY):
                body, origin = "", ""
                for column, tag in FALLBACK_CHAIN:
                    value = clean(row[column])
                    if value:
                        body, origin = value, tag
                        break
                if not body:
                    continue

                # 제목이 본문 첫 줄과 다르면 제목을 살려 맨 앞에 둔다.
                title = row["title"] or row["rec_title"]
                if title and not body.startswith(title.strip()):
                    body = f"{title.strip()}\n{body}"

                tags = []
                folder = (row["folder"] or "").strip()
                if folder and folder.lower() not in GENERIC_FOLDERS:
                    tags.append(folder.replace(" ", "_"))
                if origin != "text":
                    tags.append(origin)

                notes.append(
                    Note(
                        external_id=str(row["uuid"]),
                        body=body,
                        created_at=epoch_ms_to_iso(row["created"]),
                        updated_at=epoch_ms_to_iso(row["updated"]),
                        tags=tags,
                        origin=origin,
                    )
                )
            return notes
        finally:
            conn.close()
            shutil.rmtree(tmpdir, ignore_errors=True)
