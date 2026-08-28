"""폴더 임포터 — .txt / .md 파일 뭉치를 그대로 가져온다.

다른 앱에서 내보낸 결과물이나 그냥 쌓아둔 메모 파일을 넣는 용도.
파일 하나가 메모 하나이고, 상위 폴더명이 태그가 된다.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from . import Note, clean

EXTENSIONS = {".txt", ".md", ".markdown", ".text"}
MAX_BYTES = 1_000_000


class FilesImporter:
    name = "files"
    label = "텍스트 파일 폴더"

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    def available(self) -> bool:
        return self.path.exists() and self.path.is_dir()

    def unavailable_reason(self) -> str:
        return f"폴더를 찾을 수 없습니다: {self.path}"

    def read(self) -> list[Note]:
        notes: list[Note] = []
        for file in sorted(self.path.rglob("*")):
            if not file.is_file() or file.suffix.lower() not in EXTENSIONS:
                continue
            if file.stat().st_size > MAX_BYTES:
                continue
            body = clean(_read_text(file))
            if not body:
                continue

            # 파일명이 본문에 없으면 제목처럼 앞에 붙인다.
            stem = file.stem.strip()
            if stem and stem not in body[:200]:
                body = f"{stem}\n{body}"

            tags = []
            parent = file.parent
            if parent != self.path and parent.name:
                tags.append(parent.name.replace(" ", "_"))

            stat = file.stat()
            notes.append(
                Note(
                    external_id=str(file.relative_to(self.path)).replace("\\", "/"),
                    body=body,
                    created_at=_iso(stat.st_ctime),
                    updated_at=_iso(stat.st_mtime),
                    tags=tags,
                    origin=file.suffix.lstrip("."),
                )
            )
        return notes


def _read_text(file: Path) -> str:
    raw = file.read_bytes()
    for encoding in ("utf-8-sig", "cp949", "euc-kr", "utf-16"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).replace(microsecond=0).isoformat()
