"""엑셀 폴더 취합 — 파일 읽기와 구조 확인.

원본 파일은 디스크에 저장하지 않는다. 읽은 표는 확정 전까지 메모리의 작업(job)에만
두고, 사용자가 확인한 결과만 collection_store 가 SQLite 에 보관한다. 앱을 다시 켜면
확정하지 않은 작업은 사라지므로 완료 자료와 섞이지 않는다.

셀은 (종류, 표시값, 숫자값) 튜플로 다룬다.
    n 숫자(숫자값은 Decimal 문자열) · s 텍스트 · b 참/거짓 · d 날짜·시각
    e 셀 오류(#DIV/0! 등) · f 저장된 계산 결과가 없는 수식 · x 이 파일에 없는 열
비어 있는 셀은 None 이다. 비어 있음·0·오류·수식 결과 없음을 서로 다른 상태로 남긴다.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import threading
import time
import uuid
from collections import Counter
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal
from pathlib import PurePosixPath

from . import documents

MAX_FILES = 100
MAX_TOTAL_BYTES = 200_000_000
MAX_FILE_BYTES = documents.MAX_BYTES
MAX_FILE_CELLS = documents.MAX_CELLS
MAX_TOTAL_CELLS = 1_000_000
MAX_SHEETS = documents.MAX_PARTS
MAX_JOBS = 3
JOB_TTL = 6 * 3600
EXTENSIONS = (".xlsx", ".xlsm", ".xls")
HEADER_SCAN = 20
PREVIEW_ROWS = 5
SPECIAL_SHOWN = 10

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_SPACE_RE = re.compile(r"\s+")
_TOTAL_RE = re.compile(r"(총|전체)?(합계|소계|총계|총합|합|계|누계|total|subtotal|grandtotal|sum)")
_UNIT_RE = re.compile(r"[(\[（［]([^)\]）］]{1,10})[)\]）］]\s*$")
_CORE_STRIP_RE = re.compile(r"[(\[（［][^)\]）］]*[)\]）］]|[\s\-_.·:/]")
# 비슷한 이름 후보. 확인 전에는 연결하지 않는다 — 사용자가 '같은 항목으로 사용'을 눌러야 합친다.
SYNONYMS = [
    {"부서", "부서명", "소속", "소속부서", "팀", "팀명", "조직"},
    {"성명", "이름", "성함", "name", "담당자", "직원명", "사원명"},
    {"금액", "신청금액", "신청액", "요청액", "요청금액", "청구액", "청구금액", "지급액", "지급금액", "사용액", "사용금액"},
    {"일자", "날짜", "신청일", "신청일자", "요청일", "요청일자", "접수일", "접수일자", "date"},
    {"상태", "처리상태", "진행상태", "처리여부", "진행여부", "status"},
    {"비고", "메모", "참고", "특이사항", "note", "notes"},
    {"연락처", "전화", "전화번호", "휴대폰", "휴대전화", "핸드폰"},
    {"사번", "직원번호", "사원번호", "사원코드"},
    {"수량", "개수", "건수", "qty"},
]


class CollectionError(ValueError):
    """화면에 그대로 보여줄 수 있는 취합 오류."""


class JobNotFound(KeyError):
    pass


# --------------------------------------------------------------------------- 작업(메모리)


class Job:
    def __init__(self, title: str, collection_id: int | None, skipped: list[dict]):
        self.id = uuid.uuid4().hex
        self.created = self.touched = time.monotonic()
        self.title = title
        self.collection_id = collection_id
        self.skipped = skipped
        self.files: list[dict] = []
        self.bytes = 0
        self.cells = 0
        self.lock = threading.RLock()
        self.cache: dict = {}
        self.result: dict | None = None


_jobs: dict[str, Job] = {}
_jobs_lock = threading.Lock()


def _sweep() -> None:
    now = time.monotonic()
    for key in [k for k, j in _jobs.items() if now - j.touched > JOB_TTL]:
        _jobs.pop(key, None)


def _text_value(value, limit: int) -> str:
    return _CONTROL_RE.sub("", str(value or "")).strip()[:limit]


def create_job(title: str = "", collection_id=None, skipped=None) -> Job:
    if collection_id is not None and (not isinstance(collection_id, int) or isinstance(collection_id, bool) or collection_id < 1):
        raise CollectionError("다시 취합할 자료를 확인하세요.")
    if skipped is None:
        skipped = []
    if not isinstance(skipped, list) or len(skipped) > 1000:
        raise CollectionError("제외한 파일 목록을 확인하세요.")
    clean_skipped = []
    for item in skipped:
        if not isinstance(item, dict):
            raise CollectionError("제외한 파일 목록을 확인하세요.")
        path = _text_value(item.get("path"), 300).replace("\\", "/")
        reason = _text_value(item.get("reason"), 120)
        if path:
            clean_skipped.append({"path": path, "reason": reason or "읽지 않음"})
    job = Job(_text_value(title, 100), collection_id, clean_skipped)
    with _jobs_lock:
        _sweep()
        while len(_jobs) >= MAX_JOBS:
            oldest = min(_jobs.values(), key=lambda j: j.touched)
            _jobs.pop(oldest.id, None)
        _jobs[job.id] = job
    return job


def get_job(job_id: str) -> Job:
    with _jobs_lock:
        _sweep()
        job = _jobs.get(str(job_id))
        if job is None:
            raise JobNotFound("취합 작업을 찾을 수 없습니다. 시간이 지났거나 앱을 다시 시작했습니다. 폴더를 다시 선택하세요.")
        job.touched = time.monotonic()
        return job


def cancel_job(job_id: str) -> bool:
    with _jobs_lock:
        return _jobs.pop(str(job_id), None) is not None


def clean_path(value) -> str:
    """브라우저가 보낸 폴더 안 상대 경로만 받는다. 절대 경로·상위 폴더는 거절한다."""
    if not isinstance(value, str):
        raise CollectionError("파일 경로를 확인하세요.")
    path = value.replace("\\", "/").strip().strip("/")
    parts = path.split("/")
    if (not path or len(path) > 300 or ":" in path or _CONTROL_RE.search(path)
            or any(p.strip() in ("", ".", "..") for p in parts)):
        raise CollectionError("폴더 안의 파일 경로를 확인하세요.")
    name = parts[-1]
    if name.startswith("~$"):
        raise CollectionError("엑셀 임시 파일(~$)은 취합하지 않습니다.")
    if PurePosixPath(name).suffix.lower() not in EXTENSIONS:
        raise CollectionError("엑셀 파일(.xlsx·.xlsm·.xls)만 취합할 수 있습니다.")
    return "/".join(parts)


def add_file(job: Job, path: str, data: bytes) -> dict:
    path = clean_path(path)
    with job.lock:
        _check_room(job, path, len(data))
        remaining = MAX_TOTAL_CELLS - job.cells
    entry = {"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
             "status": "read", "error": "", "sheets": []}
    try:
        if not data:
            raise CollectionError("파일이 비어 있습니다.")
        if len(data) > MAX_FILE_BYTES:
            raise CollectionError("20MB 이하의 파일만 취합할 수 있습니다. 파일을 나눠 저장하세요.")
        entry["sheets"] = read_workbook(path, data, remaining)
    except CollectionError as exc:
        entry.update(status="failed", error=str(exc), sheets=[])
    except ImportError:
        entry.update(status="failed", error="이 설치 버전에는 이 형식을 읽는 구성요소가 없습니다. .xlsx로 저장해 다시 선택하세요.")
    except Exception:
        entry.update(status="failed", error="파일을 읽지 못했습니다. 손상되었거나 지원하지 않는 형식일 수 있습니다.", sheets=[])
    cells = sum(s["cells"] for s in entry["sheets"])
    with job.lock:
        _check_room(job, path, len(data))
        if job.cells + cells > MAX_TOTAL_CELLS:
            entry.update(status="failed", sheets=[], error="이 파일을 더하면 한 번에 취합할 수 있는 100만 셀을 넘습니다. 폴더를 나눠 취합하세요.")
            cells = 0
        job.files.append(entry)
        job.bytes += len(data)
        job.cells += cells
        job.cache.clear()
        job.touched = time.monotonic()
    return file_summary(entry)


def add_failed(job: Job, path: str, size: int, reason: str) -> dict:
    """읽지 않고 실패로 기록한다(크기 초과 등). 결과의 제외 내역에 남는다."""
    path = clean_path(path)
    entry = {"path": path, "size": size, "sha256": "", "status": "failed", "error": reason, "sheets": []}
    with job.lock:
        _check_room(job, path, 0)
        job.files.append(entry)
        job.cache.clear()
    return file_summary(entry)


def _check_room(job: Job, path: str, size: int) -> None:
    if job.result is not None:
        raise CollectionError("이미 저장한 작업입니다. 폴더를 다시 선택하세요.")
    if any(f["path"] == path for f in job.files):
        raise CollectionError("같은 경로의 파일을 이미 읽었습니다.")
    if len(job.files) >= MAX_FILES:
        raise CollectionError("한 번에 100개 파일까지 취합할 수 있습니다. 폴더를 나눠 취합하세요.")
    if job.bytes + size > MAX_TOTAL_BYTES:
        raise CollectionError("한 번에 200MB까지 취합할 수 있습니다. 폴더를 나눠 취합하세요.")


def file_summary(entry: dict) -> dict:
    visible = [s for s in entry["sheets"] if not s["hidden"]]
    return {"path": entry["path"], "status": entry["status"], "error": entry["error"], "size": entry["size"],
            "sheets": len(visible), "hidden_sheets": len(entry["sheets"]) - len(visible),
            "rows": sum(len(s["rows"]) for s in visible)}


# --------------------------------------------------------------------------- 파일 읽기


def read_workbook(path: str, data: bytes, cell_budget: int = MAX_TOTAL_CELLS) -> list[dict]:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix == ".xls":
        return _read_xls(data, cell_budget)
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raise CollectionError("암호가 걸렸거나 형식이 다른 파일입니다. 암호를 해제한 .xlsx 사본을 선택하세요.")
    return _read_xlsx(data, cell_budget)


class _Counter:
    def __init__(self, budget: int):
        self.total = 0
        self.budget = min(budget, MAX_FILE_CELLS)
        self.limit_is_file = budget >= MAX_FILE_CELLS

    def add(self, count: int, sheet: str, row_no: int) -> None:
        self.total += count
        if self.total > self.budget:
            if self.limit_is_file:
                raise CollectionError(f"셀이 20만 개를 넘습니다(‘{sheet}’ 시트 {row_no}행 부근). 파일을 나눠 저장하세요.")
            raise CollectionError("이 파일을 더하면 한 번에 취합할 수 있는 100만 셀을 넘습니다. 폴더를 나눠 취합하세요.")


def _read_xlsx(data: bytes, cell_budget: int) -> list[dict]:
    from openpyxl import load_workbook

    try:
        with documents._archive(data):
            pass
    except documents.DocumentError as exc:
        raise CollectionError(str(exc)) from None
    values = load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_links=False)
    formulas = None
    try:
        # 계산 결과가 저장되지 않은 수식을 '빈칸'과 구별하려고 수식도 함께 읽는다. 계산은 하지 않는다.
        formulas = load_workbook(io.BytesIO(data), read_only=True, data_only=False, keep_links=False)
        if len(values.worksheets) > MAX_SHEETS:
            raise CollectionError("시트가 500개를 넘습니다. 파일을 나눠 저장하세요.")
        counter, sheets = _Counter(cell_budget), []
        for ws in values.worksheets:
            sheet = {"name": ws.title, "hidden": ws.sheet_state != "visible", "rows": [], "cells": 0}
            sheets.append(sheet)
            if sheet["hidden"]:
                continue
            ws.reset_dimensions()
            fws = formulas[ws.title]
            fws.reset_dimensions()
            for row_no, (row, frow) in enumerate(zip(ws.iter_rows(), fws.iter_rows()), 1):
                cells = [_xlsx_cell(c, f) for c, f in zip(row, frow)]
                while cells and cells[-1] is None:
                    cells.pop()
                if not cells:
                    continue
                counter.add(len(cells), ws.title, row_no)
                sheet["cells"] += len(cells)
                sheet["rows"].append((row_no, cells))
        return sheets
    finally:
        values.close()
        if formulas is not None:
            formulas.close()


def _xlsx_cell(cell, formula):
    value = cell.value
    if value is None:
        if getattr(formula, "data_type", None) == "f" and formula.value:
            return ("f", _text(str(formula.value))[:300], None)
        return None
    if getattr(cell, "data_type", None) == "e":
        return ("e", str(value), None)
    return typed(value, getattr(cell, "number_format", "") or "")


def _read_xls(data: bytes, cell_budget: int) -> list[dict]:
    import xlrd

    try:
        book = xlrd.open_workbook(file_contents=data, on_demand=True)
    except xlrd.biffh.XLRDError as exc:
        if "encrypt" in str(exc).lower():
            raise CollectionError("암호가 걸린 파일입니다. 암호를 해제한 사본을 선택하세요.") from None
        raise
    try:
        if book.nsheets > MAX_SHEETS:
            raise CollectionError("시트가 500개를 넘습니다. 파일을 나눠 저장하세요.")
        counter, sheets = _Counter(cell_budget), []
        for index in range(book.nsheets):
            ws = book.sheet_by_index(index)
            sheet = {"name": ws.name, "hidden": bool(ws.visibility), "rows": [], "cells": 0}
            sheets.append(sheet)
            if not sheet["hidden"]:
                for r in range(ws.nrows):
                    cells = [_xls_cell(ws.cell(r, c), book) for c in range(ws.row_len(r))]
                    while cells and cells[-1] is None:
                        cells.pop()
                    if not cells:
                        continue
                    counter.add(len(cells), ws.name, r + 1)
                    sheet["cells"] += len(cells)
                    sheet["rows"].append((r + 1, cells))
            book.unload_sheet(index)
        return sheets
    finally:
        book.release_resources()


def _xls_cell(cell, book):
    import xlrd

    kind = cell.ctype
    if kind in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        return None
    if kind == xlrd.XL_CELL_ERROR:
        return ("e", xlrd.error_text_from_code.get(cell.value, "#ERROR"), None)
    if kind == xlrd.XL_CELL_BOOLEAN:
        return typed(bool(cell.value), "")
    if kind == xlrd.XL_CELL_DATE:
        try:
            return typed(xlrd.xldate_as_datetime(cell.value, book.datemode), "")
        except Exception:
            return ("e", "날짜 오류", None)
    return typed(cell.value, "")


def plain(number: Decimal) -> str:
    """지수 표기 없는 Decimal 문자열."""
    if number == 0:
        return "0"
    text = format(number.normalize(), "f")
    return text


def _text(value: str) -> str:
    return _CONTROL_RE.sub("", value.replace("\r\n", "\n").replace("\r", "\n")).strip()


def typed(value, number_format: str = ""):
    if isinstance(value, bool):
        return ("b", "TRUE" if value else "FALSE", None)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return ("e", str(value), None)
        if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
            value = int(value)
        number = Decimal(value) if isinstance(value, int) else Decimal(repr(value))
        text = plain(number)
        display = text
        fmt = number_format.strip()
        # '00000' 서식으로 보이는 앞자리 0(사번·우편번호)을 그대로 보존한다.
        if re.fullmatch(r"0{2,}", fmt) and number == number.to_integral_value() and number >= 0:
            display = text.zfill(len(fmt))
        return ("n", display, text)
    if isinstance(value, datetime):
        text = value.date().isoformat() if value.time() == dtime(0) else value.isoformat(sep=" ", timespec="seconds")
        return ("d", text, None)
    if isinstance(value, date):
        return ("d", value.isoformat(), None)
    if isinstance(value, dtime):
        return ("d", value.isoformat(timespec="seconds"), None)
    if isinstance(value, timedelta):
        return ("s", str(value), None)
    text = _text(str(value))
    return ("s", text, None) if text else None


# --------------------------------------------------------------------------- 구조 확인


def name_key(name: str) -> str:
    return _SPACE_RE.sub(" ", str(name)).strip().casefold()


def _letter(index: int) -> str:
    out, n = "", index + 1
    while n:
        n, rest = divmod(n - 1, 26)
        out = chr(65 + rest) + out
    return out


def unit_of(name: str) -> str:
    match = _UNIT_RE.search(name)
    return match.group(1).strip().casefold() if match else ""


def _core(name: str) -> str:
    return _CORE_STRIP_RE.sub("", name).casefold()


def similarity(a: str, b: str) -> float:
    """비슷한 열 이름 점수. 단위가 다르면(원/천원 등) 절대 후보로 내지 않는다."""
    ua, ub = unit_of(a), unit_of(b)
    if ua and ub and ua != ub:
        return 0.0
    ca, cb = _core(a), _core(b)
    if not ca or not cb:
        return 0.0
    if ca == cb:
        return 1.0
    if any(ca in group and cb in group for group in SYNONYMS):
        return 0.9
    if min(len(ca), len(cb)) >= 2 and (ca in cb or cb in ca):
        return 0.75
    grams = lambda s: {s[i:i + 2] for i in range(len(s) - 1)} or {s}  # noqa: E731
    ga, gb = grams(ca), grams(cb)
    return len(ga & gb) / len(ga | gb)


def _detect_header(rows: list) -> int:
    """제목 줄을 건너뛰고 항목 이름이 있는 행을 찾는다. 못 찾으면 첫 행."""
    for i, (_, cells) in enumerate(rows[:HEADER_SCAN]):
        filled = [c for c in cells if c]
        if len(filled) < 2 or any(c[0] != "s" for c in filled):
            continue
        following = rows[i + 1:i + 11]
        if not following:
            return i
        width = max(sum(1 for c in r if c) for _, r in following)
        if len(filled) >= max(2, round(width * 0.6)):
            return i
    return 0


def _special_reason(cells: list, names: list[dict]) -> str:
    filled = [c for c in cells if c]
    for cell in filled[:3]:
        if cell[0] == "s" and _TOTAL_RE.fullmatch(_SPACE_RE.sub("", cell[1]).casefold()):
            return "합계 행"
    same = sum(1 for j, c in enumerate(cells)
               if c and j < len(names) and not names[j]["generated"] and name_key(c[1]) == names[j]["key"])
    if filled and same >= max(1 if len(names) == 1 else 2, math.ceil(len(filled) * 0.8)):
        return "반복 머리글"
    return ""


def _numeric_like(cell) -> bool:
    return bool(cell) and (cell[0] == "n" or (cell[0] == "s" and bool(NUMERIC_TEXT.fullmatch(cell[1]))))


NUMERIC_TEXT = re.compile(r"[+-]?(?:\d{1,3}(?:,\d{3})+|0|[1-9]\d*)(?:\.\d+)?")


def _layout(job: Job, fi: int, si: int, header_row: int | None) -> dict:
    key = (fi, si, header_row)
    cached = job.cache.get(key)
    if cached is not None:
        return cached
    rows = job.files[fi]["sheets"][si]["rows"]
    auto = _detect_header(rows)
    index, invalid = auto, False
    if header_row is not None:
        found = next((i for i, (n, _) in enumerate(rows) if n == header_row), None)
        if found is None:
            invalid = True
        else:
            index = found
    header = rows[index][1] if rows else []
    data = rows[index + 1:]
    width = max([len(header)] + [len(c) for _, c in data])
    filled = [0] * width
    numeric = [0] * width
    for _, cells in data:
        for j, cell in enumerate(cells):
            if cell:
                filled[j] += 1
                numeric[j] += _numeric_like(cell)
    names, seen = [], Counter()
    for j in range(width):
        cell = header[j] if j < len(header) else None
        text = _SPACE_RE.sub(" ", cell[1]).strip() if cell else ""
        if not text and not filled[j]:
            continue  # 머리글도 값도 없는 열은 표의 일부가 아니다.
        name = text[:100] or f"열 {_letter(j)}"
        seen[name_key(name)] += 1
        problem = "" if text else "empty"
        if text and seen[name_key(name)] > 1:
            name, problem = f"{name} ({seen[name_key(name)]})", "duplicate"
        names.append({"index": j, "letter": _letter(j), "name": name, "key": name_key(name),
                      "generated": not text, "problem": problem, "filled": filled[j],
                      "numeric": numeric[j]})
    by_index = {n["index"]: n for n in names}
    aligned = [by_index.get(j, {"generated": True, "key": ""}) for j in range(width)]
    special = []
    for i, (row_no, cells) in enumerate(data):
        reason = _special_reason(cells, aligned)
        if reason:
            first = next((c[1] for c in cells if c), "")
            special.append({"index": i, "row": row_no, "reason": reason, "text": first[:40]})
    layout = {"index": index, "auto": auto, "invalid": invalid, "header_row": rows[index][0] if rows else None,
              "names": names, "count": len(data), "special": special,
              "special_index": {s["index"]: s["reason"] for s in special}}
    job.cache[key] = layout
    return layout


def normalize_rules(raw) -> dict:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise CollectionError("취합 규칙을 확인하세요.")
    files_in, sheets_in = raw.get("files") or {}, raw.get("sheets") or {}
    if not isinstance(files_in, dict) or not isinstance(sheets_in, dict) or len(files_in) > 5000 or len(sheets_in) > 5000:
        raise CollectionError("취합 규칙을 확인하세요.")
    files = {}
    for path, value in files_in.items():
        if isinstance(value, dict) and isinstance(value.get("selected"), bool):
            files[str(path)[:300]] = {"selected": value["selected"]}
    sheets = {}
    for key, value in sheets_in.items():
        if not isinstance(value, dict):
            continue
        entry = {}
        if isinstance(value.get("selected"), bool):
            entry["selected"] = value["selected"]
        header = value.get("header_row")
        if isinstance(header, int) and not isinstance(header, bool) and 1 <= header <= 1_048_576:
            entry["header_row"] = header
        if value.get("confirmed") is True:
            entry["confirmed"] = True
        if value.get("special") in ("exclude", "include"):
            entry["special"] = value["special"]
        mapping = value.get("mapping")
        if isinstance(mapping, dict):
            clean = {}
            for column, target in list(mapping.items())[:2000]:
                if str(column).isdigit() and isinstance(target, str):
                    text = _SPACE_RE.sub(" ", _CONTROL_RE.sub("", target)).strip()[:100]
                    clean[str(int(column))] = "-" if target == "-" else text
            if clean:
                entry["mapping"] = clean
        if entry:
            sheets[str(key)[:700]] = entry
    return {"files": files, "sheets": sheets}


def sheet_key(path: str, sheet: str) -> str:
    # 엑셀 시트 이름에는 ':' 를 쓸 수 없어서 구분자로 안전하다.
    return f"{path}::{sheet}"


def analyze(job: Job, raw_rules=None, *, full: bool = False) -> dict:
    """규칙을 적용한 취합 결과. full=False 는 확인 화면용 요약, True 는 저장할 전체 행까지."""
    rules = normalize_rules(raw_rules)
    with job.lock:
        files = list(job.files)
        skipped = list(job.skipped)
        file_views, entries = [], []
        for fi, f in enumerate(files):
            view = {"path": f["path"], "status": f["status"], "error": f["error"], "size": f["size"],
                    "selected": False, "sheets": []}
            file_views.append(view)
            if f["status"] != "read":
                continue
            view["selected"] = rules["files"].get(f["path"], {}).get("selected", True)
            for si, s in enumerate(f["sheets"]):
                key = sheet_key(f["path"], s["name"])
                sv = {"key": key, "name": s["name"], "hidden": s["hidden"], "empty": not s["rows"],
                      "selected": False, "issues": [], "notes": []}
                view["sheets"].append(sv)
                if s["hidden"] or not s["rows"]:
                    continue
                rule = rules["sheets"].get(key, {})
                entries.append({"file": view, "sheet": sv, "rule": rule, "fi": fi, "si": si,
                                "layout": _layout(job, fi, si, rule.get("header_row"))})

    # 1) 기준 구조: 가장 많은 시트가 공유하는 항목 이름 묶음.
    def signature(e):
        return tuple(sorted({n["key"] for n in e["layout"]["names"] if not n["generated"]}))

    candidates = [e for e in entries if e["file"]["selected"]]
    counts = Counter(signature(e) for e in candidates)
    ref_sig = max(counts, key=lambda s: (counts[s], -next(i for i, e in enumerate(candidates) if signature(e) == s))) if counts else ()
    ref_keys = set(ref_sig)
    per_file = Counter(e["fi"] for e in entries)
    for e in entries:
        keys = set(signature(e))
        overlap = len(keys & ref_keys) / max(len(ref_keys), 1)
        default = per_file[e["fi"]] == 1 or overlap >= 0.5
        e["sheet"]["default_selected"] = default
        e["sheet"]["selected"] = e["rule"].get("selected", default)
        if not default:
            e["sheet"]["notes"].append("다른 구조의 시트라 기본으로 제외했습니다.")
    selected = [e for e in entries if e["file"]["selected"] and e["sheet"]["selected"]]
    reference = next((e for e in selected if signature(e) == ref_sig), selected[0] if selected else None)

    # 2) 항목(열) 목록과 시트별 연결.
    fields, field_index = [], {}

    def ensure(name: str) -> int:
        key = name_key(name)
        if key not in field_index:
            field_index[key] = len(fields)
            fields.append({"name": name, "key": key})
        return field_index[key]

    def targets_of(e) -> list:
        mapping = e["rule"].get("mapping", {})
        out = []
        for n in e["layout"]["names"]:
            m = mapping.get(str(n["index"]))
            out.append(None if m == "-" else ensure(m or n["name"]))
        return out

    ref_fields: set[int] = set()
    ref_numeric: dict[int, float] = {}
    if reference is not None:
        reference["targets"] = targets_of(reference)
        ref_fields = {t for t in reference["targets"] if t is not None}
        for n, t in zip(reference["layout"]["names"], reference["targets"]):
            if t is not None:
                ref_numeric[t] = n["numeric"] / max(n["filled"], 1)
    blocking = 0
    for e in entries:
        if e not in selected:
            continue
        if "targets" not in e:
            e["targets"] = targets_of(e)
        blocking += _sheet_issues(e, fields, ref_fields, ref_numeric)

    # 3) 범위 요약.
    rows_total = 0
    for view in file_views:
        view["rows"] = 0
    for e in selected:
        layout = e["layout"]
        excluded = len(layout["special"]) if e["rule"].get("special") == "exclude" else 0
        e["sheet"]["rows"] = layout["count"] - excluded
        e["file"]["rows"] += e["sheet"]["rows"]
        rows_total += e["sheet"]["rows"]
    for e in entries:
        sv = e["sheet"]
        sv["header_row"] = e["layout"]["header_row"]
        sv["auto_header_row"] = _row_no(job, e, e["layout"]["auto"])
        sv["data_rows"] = e["layout"]["count"]
        sv.setdefault("rows", 0)
        if full is False:
            sv["preview"] = _preview(job, e)
    included_files = [v for v in file_views if v["status"] == "read" and v["selected"] and any(s["selected"] for s in v["sheets"])]
    failed = [v for v in file_views if v["status"] != "read"]
    excluded_files = [v for v in file_views if v["status"] == "read" and v not in included_files]
    for v in excluded_files:
        v["reason"] = "사용자가 제외" if not v["selected"] else "취합할 시트 없음"
    if rows_total == 0:
        blocking += 1
    summary = {
        "found": len(files) + len(skipped),
        "files": len(included_files),
        "excluded": len(excluded_files),
        "failed": len(failed) + len(skipped),
        "rows": rows_total,
        "blocking": blocking,
        "hidden_sheets": sum(1 for v in file_views for s in v["sheets"] if s["hidden"]),
    }
    revision = hashlib.sha256(json.dumps(
        [[(f["path"], f["sha256"], f["status"]) for f in files], skipped, rules],
        sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    result = {"revision": revision, "title": job.title, "collection_id": job.collection_id,
              "summary": summary, "files": file_views, "skipped": skipped, "rules": rules,
              "fields": [f["name"] for f in fields]}
    if full:
        result.update(_rows(job, selected, fields))
    return result


def _row_no(job: Job, e: dict, index: int):
    rows = job.files[e["fi"]]["sheets"][e["si"]]["rows"]
    return rows[index][0] if 0 <= index < len(rows) else None


def _sheet_issues(e: dict, fields: list, ref_fields: set, ref_numeric: dict) -> int:
    layout, rule, sv = e["layout"], e["rule"], e["sheet"]
    mapping = rule.get("mapping", {})
    issues = sv["issues"]
    if layout["invalid"]:
        issues.append({"kind": "header", "blocking": True, "row": layout["header_row"],
                       "message": f"{rule['header_row']}행에는 내용이 없습니다. 항목 이름이 있는 행을 다시 지정하세요."})
    elif layout["index"] > 0 and "header_row" not in rule:
        issues.append({"kind": "header", "blocking": True, "row": layout["header_row"],
                       "message": f"표 위에 제목이 있어요. 항목 이름이 있는 행: {layout['header_row']}행"})
    targets = e["targets"]
    used = Counter(t for t in targets if t is not None)
    columns, extra = [], []
    for n, target in zip(layout["names"], targets):
        column = {"index": n["index"], "letter": n["letter"], "name": n["name"], "problem": n["problem"],
                  "target": fields[target]["name"] if target is not None else None,
                  "mapped": str(n["index"]) in mapping, "extra": target is not None and target not in ref_fields,
                  "suggestion": None, "unit_warning": ""}
        if target is not None and column["mapped"]:
            ua, ub = unit_of(n["name"]), unit_of(fields[target]["name"])
            if ua and ub and ua != ub:
                column["unit_warning"] = f"단위가 다릅니다({unit_of(n['name'])} / {unit_of(fields[target]['name'])}). 연결하면 숫자를 환산하지 않고 그대로 합칩니다."
        columns.append(column)
        if column["extra"]:
            extra.append(column)
    missing = [fields[i]["name"] for i in sorted(ref_fields) if i not in used]
    # 비슷한 이름 후보 — 아직 정하지 않은 추가 열과 이 시트에 없는 기준 항목 사이에서만 찾는다.
    for column in extra:
        if column["mapped"]:
            continue
        scored = sorted(((similarity(column["name"], m), m) for m in missing), reverse=True)
        if scored and scored[0][0] >= 0.5:
            column["suggestion"] = scored[0][1]
    open_extra = [c for c in extra if not c["mapped"]]
    if len(open_extra) == 1 and len(missing) == 1 and not open_extra[0]["suggestion"]:
        # 이름은 달라도 한 열이 빠지고 한 열이 새로 생겼다면 같은 항목일 수 있다.
        # 단위가 같고 값 종류(숫자/글자)도 같을 때만 묻는다. 연결은 사용자가 정한다.
        name = next(n for n in layout["names"] if n["index"] == open_extra[0]["index"])
        ratio = name["numeric"] / max(name["filled"], 1)
        ref_ratio = ref_numeric.get(field_index_of(fields, missing[0]), -1)
        same_kind = (ratio >= 0.8 and ref_ratio >= 0.8) or (ratio < 0.2 and 0 <= ref_ratio < 0.2)
        units = unit_of(name["name"]), unit_of(missing[0])
        if same_kind and (units[0] == units[1] or not all(units)):
            open_extra[0]["suggestion"] = missing[0]
    sv["columns"] = columns
    sv["missing"] = missing
    problems = [c for c in columns if c["problem"] and not c["mapped"] and c["target"] is not None]
    blocking = 0
    conflicts = [fields[t]["name"] for t, count in used.items() if count > 1]
    if conflicts:
        issues.append({"kind": "conflict", "blocking": True,
                       "message": "한 시트의 여러 열이 같은 항목에 연결되어 있어요: " + ", ".join(f"‘{c}’" for c in conflicts)
                                  + ". 한 열은 다른 항목으로 바꾸거나 제외하세요."})
    if (extra or missing or problems) and not rule.get("confirmed"):
        parts = []
        if any(c["suggestion"] for c in extra):
            parts.append("이름이 다른 항목이 있어요. 같은 항목인지 확인해 주세요.")
        plain_extra = [c["name"] for c in extra if not c["suggestion"]]
        if plain_extra:
            parts.append("추가 열: " + ", ".join(f"‘{n}’" for n in plain_extra) + " (새 항목으로 유지)")
        if missing:
            parts.append("이 파일에는 " + ", ".join(f"‘{m}’" for m in missing) + "이(가) 없어요 (빈칸으로 둡니다)")
        if problems:
            parts.append("항목 이름을 확인해 주세요: " + ", ".join(
                f"{c['letter']}열 {'이름 없음' if c['problem'] == 'empty' else '이름 중복'} → ‘{c['name']}’" for c in problems))
        issues.append({"kind": "structure", "blocking": True, "message": " · ".join(parts)})
    if layout["special"] and "special" not in rule:
        shown = layout["special"][:SPECIAL_SHOWN]
        kinds = sorted({s["reason"] for s in layout["special"]})
        issues.append({"kind": "special", "blocking": True, "count": len(layout["special"]),
                       "rows": [{"row": s["row"], "reason": s["reason"], "text": s["text"]} for s in shown],
                       "message": f"{' · '.join(kinds)}으로 보이는 {len(layout['special'])}행이 있어요. 합산 전에 포함 여부를 정해 주세요."})
    elif layout["special"]:
        sv["notes"].append(f"합계·반복 머리글 후보 {len(layout['special'])}행: "
                           + ("취합에서 제외" if rule["special"] == "exclude" else "일반 행으로 포함"))
    for issue in issues:
        blocking += issue["blocking"]
    return blocking


def field_index_of(fields: list, name: str) -> int:
    key = name_key(name)
    return next((i for i, f in enumerate(fields) if f["key"] == key), -1)


def _preview(job: Job, e: dict) -> dict:
    rows = job.files[e["fi"]]["sheets"][e["si"]]["rows"]
    layout = e["layout"]
    columns = [n["index"] for n in layout["names"]]
    start = max(0, layout["index"] - 3)
    above = [{"row": n, "cells": [_display(c, j) for j in columns]} for n, c in rows[start:layout["index"]]]
    data = rows[layout["index"] + 1:layout["index"] + 1 + PREVIEW_ROWS]
    return {"above": above, "header": [n["name"] for n in layout["names"]],
            "rows": [{"row": n, "cells": [_display(c, j) for j in columns]} for n, c in data]}


def _display(cells: list, j: int) -> str:
    cell = cells[j] if j < len(cells) else None
    return cell[1] if cell else ""


MISSING = ("x", "", None)


def _rows(job: Job, selected: list, fields: list) -> dict:
    sources, rows = [], []
    for e in selected:
        layout = e["layout"]
        sheet_rows = job.files[e["fi"]]["sheets"][e["si"]]["rows"]
        src = len(sources)
        sources.append({"file": e["file"]["path"], "sheet": e["sheet"]["name"], "header_row": layout["header_row"]})
        present = {t for t in e["targets"] if t is not None}
        exclude_special = e["rule"].get("special") == "exclude"
        for i, (row_no, cells) in enumerate(sheet_rows[layout["index"] + 1:]):
            values = [None if f in present else MISSING for f in range(len(fields))]
            for n, target in zip(layout["names"], e["targets"]):
                if target is not None and n["index"] < len(cells):
                    values[target] = cells[n["index"]]
            reason = layout["special_index"].get(i, "") if exclude_special else ""
            rows.append((src, row_no, not reason, reason, values))
    types = infer_types(fields, (r[4] for r in rows if r[2]))
    return {"sources": sources, "rows": rows, "field_types": types}


ID_NAME = re.compile(r"(사번|번호|코드|아이디|우편|계좌|주민|연락처|전화|휴대|^id$|^no\.?$|(^|[\s_])id$)", re.IGNORECASE)


def infer_types(fields: list, rows) -> list[str]:
    count = len(fields)
    numeric, dates, filled, leading_zero = [0] * count, [0] * count, [0] * count, [False] * count
    for values in rows:
        for j, cell in enumerate(values):
            if not cell or cell[0] == "x":
                continue
            filled[j] += 1
            if _numeric_like(cell):
                numeric[j] += 1
                if re.fullmatch(r"0\d+", cell[1]):
                    leading_zero[j] = True
            elif cell[0] == "s" and re.fullmatch(r"0\d+", cell[1]):
                leading_zero[j] = True
            elif cell[0] == "d":
                dates[j] += 1
    out = []
    for j, field in enumerate(fields):
        name = field["name"] if isinstance(field, dict) else str(field)
        if leading_zero[j] or (ID_NAME.search(name) and numeric[j]):
            out.append("id")
        elif filled[j] and numeric[j] >= 0.8 * filled[j]:
            out.append("number")
        elif filled[j] and dates[j] >= 0.8 * filled[j]:
            out.append("date")
        else:
            out.append("text")
    return out
