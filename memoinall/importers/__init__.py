"""외부 메모 앱에서 일괄 가져오기.

각 임포터는 소스를 읽어 `Note` 목록만 돌려주고, 저장은 `run_import` 가 담당한다.
원칙:
  - 원본은 절대 건드리지 않는다(잠금 회피를 위해 DB 는 임시 복사본으로 읽는다).
  - 원본 작성 시각을 유지한다. 임포트 시각으로 덮으면 시간 검색이 무의미해진다.
  - external_id 로 멱등하다. 몇 번을 돌려도 중복되지 않는다.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .. import store


@dataclass
class Note:
    """임포터가 돌려주는 표준 형태."""

    external_id: str
    body: str
    created_at: str | None = None
    updated_at: str | None = None
    tags: list[str] = field(default_factory=list)
    origin: str = ""  # 어느 컬럼/파일에서 본문을 건졌는지 (진단용)


_BLANKS_RE = re.compile(r"\n{3,}")
_TRAILING_WS_RE = re.compile(r"[ \t]+\n")


def clean(text: str | None) -> str:
    """메모 앱 특유의 빈 줄 폭탄과 제어문자를 정리한다."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("​", "")
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    text = _TRAILING_WS_RE.sub("\n", text)
    text = _BLANKS_RE.sub("\n\n", text)
    return text.strip()


def epoch_ms_to_iso(value) -> str | None:
    """밀리초 epoch → ISO. 초 단위로 들어와도 알아서 처리한다."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    if n > 10_000_000_000:  # 밀리초
        n //= 1000
    try:
        return datetime.fromtimestamp(n).replace(microsecond=0).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def parse_any_time(value) -> str | None:
    """ISO 문자열 · epoch 숫자 · .NET ticks 를 모두 받아 ISO 로."""
    if value is None:
        return None
    if isinstance(value, str):
        raw = value.strip().replace("Z", "+00:00")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw).replace(tzinfo=None, microsecond=0).isoformat()
        except ValueError:
            if raw.isdigit():
                return epoch_ms_to_iso(raw)
            return None
    if isinstance(value, (int, float)):
        n = int(value)
        # .NET ticks (0001-01-01 기준 100ns). 스티커 메모 일부 버전이 쓴다.
        if n > 500_000_000_000_000:
            return epoch_ms_to_iso((n - 621_355_968_000_000_000) // 10_000)
        return epoch_ms_to_iso(n)
    return None


def open_readonly_copy(path: Path) -> tuple[sqlite3.Connection, Path]:
    """실행 중인 앱이 DB 를 잠갔을 수 있으니 복사본을 읽는다.

    호출자가 반환된 임시 디렉터리를 지워야 한다.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="memoinall-import-"))
    target = tmpdir / path.name
    shutil.copy2(path, target)
    for suffix in ("-wal", "-shm"):  # 커밋 안 된 최근 메모까지 살리려면 WAL 도 함께
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, target.with_name(target.name + suffix))
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    return conn, tmpdir


# --------------------------------------------------------------------------- 실행


@dataclass
class ImportResult:
    source: str
    available: bool
    path: str = ""
    found: int = 0
    imported: int = 0
    skipped_existing: int = 0
    skipped_empty: int = 0
    error: str = ""
    samples: list[str] = field(default_factory=list)
    memo_ids: list[int] = field(default_factory=list)
    skipped_short: int = 0
    lengths: list[int] = field(default_factory=list)


def run_import(
    importer,
    *,
    dry_run: bool = True,
    sample: int = 5,
    background_enrich: bool = True,
    min_chars: int = 0,
) -> ImportResult:
    """임포터 하나를 실행한다. dry_run 이면 읽기만 하고 아무것도 쓰지 않는다.

    background_enrich=False 면 보강 큐에 넣지 않는다. CLI 처럼 프로세스가 곧
    종료되는 환경에서는 호출자가 memo_ids 로 직접 돌려야 한다.
    """
    result = ImportResult(source=importer.name, available=importer.available(), path=str(importer.path or ""))
    if not result.available:
        result.error = importer.unavailable_reason()
        return result

    try:
        notes = importer.read()
    except Exception as exc:
        # 우리가 직접 만든 오류는 이미 사용자용 문장이다 — 클래스 이름을 덧붙이면
        # "RedmineError: API 키가 올바르지 않습니다" 처럼 지저분해진다.
        from .redmine import RedmineError

        result.error = str(exc) if isinstance(exc, RedmineError) else f"{type(exc).__name__}: {exc}"
        return result

    result.found = len(notes)
    for note in notes:
        body = clean(note.body)
        if not body:
            result.skipped_empty += 1
            continue
        result.lengths.append(len(body))
        if min_chars and len(body) < min_chars:
            result.skipped_short += 1
            continue
        if store.find_by_external(importer.name, note.external_id) is not None:
            result.skipped_existing += 1
            continue
        if len(result.samples) < sample:
            first = body.splitlines()[0][:50]
            result.samples.append(f"{(note.created_at or '')[:10]}  {first}")
        if dry_run:
            result.imported += 1
            continue

        tagged = body
        if note.tags:
            existing = {t.lower() for t in re.findall(r"#([0-9A-Za-z가-힣_/-]+)", body)}
            new_tags = [t for t in note.tags if t.lower() not in existing]
            if new_tags:
                tagged = body + "\n" + " ".join("#" + t for t in new_tags)
        memo = store.add_memo(
            tagged,
            source=importer.name,
            created_at=note.created_at,
            updated_at=note.updated_at,
            external_id=note.external_id,
            enqueue_enrich=background_enrich,
        )
        result.memo_ids.append(memo["id"])
        result.imported += 1
    return result


def build_redmine(**kwargs):
    """설정에 저장된 주소/키를 기본으로, 호출 인자로 덮어쓴다."""
    from .. import settings
    from .redmine import DEFAULT_KINDS, RedmineImporter

    kinds = kwargs.get("redmine_kinds")
    if isinstance(kinds, str):
        kinds = [k.strip() for k in kinds.split(",") if k.strip()]
    if not kinds:
        stored = settings.get("import.redmine.kinds")
        kinds = [k.strip() for k in stored.split(",") if k.strip()] or DEFAULT_KINDS

    return RedmineImporter(
        url=kwargs.get("redmine_url") or settings.get("import.redmine.url"),
        api_key=kwargs.get("redmine_api_key") or settings.get("import.redmine.api_key"),
        projects=kwargs.get("redmine_projects")
        if kwargs.get("redmine_projects") is not None
        else settings.get("import.redmine.projects"),
        kinds=kinds,
        limit=int(kwargs.get("redmine_limit") or settings.get_int("import.redmine.limit", 300)),
        since=kwargs.get("redmine_since") or settings.get("import.redmine.since"),
        include_notes=bool(kwargs.get("redmine_include_notes")),
    )


def all_importers(**kwargs) -> list:
    from .files import FilesImporter
    from .samsung import SamsungNotesImporter
    from .sticky import StickyNotesImporter

    importers = [StickyNotesImporter(), SamsungNotesImporter(), build_redmine(**kwargs)]
    if kwargs.get("files_path"):
        importers.append(FilesImporter(kwargs["files_path"]))
    return importers


def get_importer(name: str, **kwargs):
    from .files import FilesImporter
    from .samsung import SamsungNotesImporter
    from .sticky import StickyNotesImporter

    table = {
        "sticky": StickyNotesImporter,
        "samsung": SamsungNotesImporter,
    }
    if name == "files":
        path = kwargs.get("files_path")
        if not path:
            raise ValueError("files 소스는 --path 가 필요합니다.")
        return FilesImporter(path)
    if name == "redmine":
        return build_redmine(**kwargs)
    if name not in table:
        raise ValueError(f"알 수 없는 소스: {name} (사용 가능: sticky, samsung, redmine, files)")
    return table[name]()
