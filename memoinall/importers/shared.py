"""동료가 내보낸 메모 파일(.json)을 읽어들인다.

다른 임포터와 같은 자리에 끼워 넣어서 미리보기·중복 방지·초기화를 그대로 쓴다.
다른 점은 하나 — 소스가 달라도 내용이 같으면 건너뛴다. 내가 쓴 메모가 동료를
거쳐 돌아왔을 때 두 벌이 되면 안 되기 때문이다(dedupe_content).
"""

from __future__ import annotations

from pathlib import Path

from . import Note, clean

EXTENSIONS = {".json"}


class SharedFileImporter:
    name = "shared"
    label = "받은 메모 파일"
    # run_import 가 내용 기준 중복까지 걸러준다
    dedupe_content = True

    def __init__(self, path: str | Path = "", content: str = ""):
        self.raw = str(path or "").strip()
        # 빈 경로를 Path 로 만들면 '.' 이 된다 — 미지정과 '현재 폴더'는 달라야 한다.
        self.path = Path(self.raw).expanduser() if self.raw else None
        self.content = content or ""

    def available(self) -> bool:
        if self.content.strip():
            return True
        return bool(self.path) and self.path.is_file()

    def unavailable_reason(self) -> str:
        if not self.raw:
            return "받은 메모 파일(.json)을 지정하세요."
        if self.path and self.path.is_dir():
            return f"폴더가 아니라 파일을 지정하세요: {self.path}"
        return f"파일을 찾을 수 없습니다: {self.path}"

    def read(self) -> list[Note]:
        from .. import exchange

        if not self.available():
            raise ValueError(self.unavailable_reason())
        data = (
            exchange.parse(self.content)
            if self.content.strip()
            else exchange.read_file(self.path)
        )

        notes: list[Note] = []
        for item in data.get("memos") or []:
            if not isinstance(item, dict):
                continue
            body = clean(str(item.get("body") or ""))
            if not body:
                continue
            created = _text(item.get("created_at"))
            # uid 가 없거나 깨졌으면 내용으로 다시 만든다 — 손으로 편집한 파일도 받아준다.
            uid = _text(item.get("uid")) or exchange.content_uid(created, body)
            tags = [t for t in (item.get("tags") or []) if isinstance(t, str) and t.strip()]
            notes.append(
                Note(
                    external_id=uid,
                    body=body,
                    created_at=created or None,
                    updated_at=_text(item.get("updated_at")) or None,
                    tags=tags,
                    origin=_text(item.get("origin")),
                )
            )
        return notes

    def describe(self) -> dict:
        """넣기 전에 '누가 언제 몇 건 보냈는지'만 훑어본다."""
        from .. import exchange

        data = (
            exchange.parse(self.content)
            if self.content.strip()
            else exchange.read_file(self.path)
        )
        return {
            "count": len(data.get("memos") or []),
            "exported_at": _text(data.get("exported_at")),
            "note": _text(data.get("note")),
            "version": data.get("version"),
        }


def _text(value) -> str:
    return str(value).strip() if isinstance(value, (str, int, float)) else ""
