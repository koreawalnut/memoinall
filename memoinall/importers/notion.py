"""Notion 임포터 — 공식 API 로, 사용자가 '연결'을 추가한 페이지만 읽는다.

Redmine 과 같은 **네트워크 소스**다. 신경 쓴 것:

  - **읽기만 한다.** 토큰은 '내용 읽기' 권한이면 충분하다. 페이지를 고치거나 만들지 않는다.
  - **사용자가 고른 페이지만 보인다.** Notion 연결은 기본으로 아무 페이지도 못 읽고, 사용자가
    페이지의 ••• → 연결(Connections)에서 추가한 페이지(와 그 하위 페이지)만 읽는다.
  - **원래 시각을 지킨다.** 페이지의 작성·마지막 수정 시각을 메모 시각으로 쓴다.
  - **Notion 의 호출 제한(평균 초당 3회)을 지킨다.** 요청 사이 간격을 두고, 429 면 기다렸다 다시 한다.
  - **미리보기와 가져오기를 두 번 읽지 않는다.** 페이지가 많으면 몇 분 걸리므로, 방금 미리 본 결과를
    10분 동안 재사용한다(같은 토큰·조건일 때만).
  - **의존성을 늘리지 않는다.** urllib 로 충분하다.

페이지 하나 = 메모 하나. 하위 페이지는 각자 메모가 되고, 부모 본문에는 '하위 페이지: 제목' 한 줄만 남긴다.
데이터베이스 항목은 속성(상태·담당자·날짜 …)을 본문 위쪽에 적고, 다중 선택 값과 데이터베이스 이름을 태그로 붙인다.
이미지·파일은 내려받지 않고 '[이미지] 설명' 한 줄로 남긴다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import Note, clean, legacy_aware_time, parse_any_time

log = logging.getLogger(__name__)

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"     # 오래 지원되는 안정판. 새 판의 '데이터 소스' 구조 없이 페이지·블록을 그대로 읽는다.
TIMEOUT = 30
MIN_INTERVAL = 0.35               # 초당 3회 이하
MAX_RETRIES = 5
MAX_DEPTH = 6                     # 블록 중첩(토글 안의 목록 안의 …) 깊이 상한
CACHE_SECONDS = 600
TOKEN_HELP = "Notion 개발자 페이지의 연결 → Configuration 탭에서 API token 을 복사하세요."

_cache: dict[str, tuple[float, list, list]] = {}
_cache_lock = threading.Lock()


class NotionError(RuntimeError):
    pass


class NotionImporter:
    name = "notion"
    label = "Notion"

    def __init__(self, token: str = "", *, limit: int = 300, since: str = "", reuse_preview: bool = False):
        self.token = (token or "").strip()
        self.limit = max(1, int(limit or 300))
        self.since = (since or "").strip()
        # 미리보기는 늘 새로 읽고, 바로 이어지는 '가져오기'만 방금 읽은 것을 다시 쓴다.
        self.reuse_preview = reuse_preview
        self.path = "Notion" if self.token else ""
        self.warnings: list[str] = []
        self._last_call = 0.0
        self._database_titles: dict[str, str] = {}

    # ------------------------------------------------------------------ 가용성
    def available(self) -> bool:
        return bool(self.token)

    def unavailable_reason(self) -> str:
        return "Notion API 토큰이 설정되지 않았습니다. 아래 'Notion에서 가져오기'의 안내대로 연결을 만들고 토큰을 넣으세요."

    # ------------------------------------------------------------------ HTTP
    def _request(self, method: str, path: str, body: dict | None = None, **params) -> dict:
        try:
            self.token.encode("ascii")
        except UnicodeEncodeError:
            raise NotionError("토큰에 한글이나 공백이 섞여 있습니다. " + TOKEN_HELP) from None
        url = f"{API}/{path.lstrip('/')}"
        query = {k: v for k, v in params.items() if v not in (None, "")}
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        for attempt in range(MAX_RETRIES + 1):
            wait = MIN_INTERVAL - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "memoinall",
            })
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if (exc.code == 429 or exc.code >= 500) and attempt < MAX_RETRIES:
                    retry = exc.headers.get("Retry-After") if exc.headers else None
                    try:
                        delay = float(retry) if retry else 2 ** attempt
                    except ValueError:
                        delay = 2 ** attempt
                    time.sleep(min(max(delay, 1.0), 30.0))
                    continue
                raise NotionError(_http_hint(exc)) from exc
            except urllib.error.URLError as exc:
                raise NotionError(
                    "Notion 에 연결할 수 없습니다. 인터넷 연결과 회사 방화벽·프록시 설정을 확인하세요. "
                    f"— {exc.reason}") from exc
            except json.JSONDecodeError as exc:
                raise NotionError("Notion 이 알 수 없는 응답을 보냈습니다. 잠시 뒤 다시 시도하세요.") from exc
        raise NotionError("Notion 이 잠시 요청을 막았습니다. 몇 분 뒤 다시 시도하세요.")

    def _paged(self, method: str, path: str, body: dict | None = None):
        cursor = None
        while True:
            if method == "POST":
                page = self._request("POST", path, {**(body or {}), "page_size": 100,
                                                    **({"start_cursor": cursor} if cursor else {})})
            else:
                page = self._request("GET", path, page_size=100, start_cursor=cursor)
            yield from page.get("results") or []
            cursor = page.get("next_cursor")
            if not page.get("has_more") or not cursor:
                return

    # ------------------------------------------------------------------ 연결 확인
    def test_connection(self) -> dict:
        if not self.available():
            return {"ok": False, "message": self.unavailable_reason()}
        try:
            me = self._request("GET", "users/me")
            bot = me.get("bot") or {}
            name = me.get("name") or "연결"
            workspace = bot.get("workspace_name") or ""
            found = self._request("POST", "search", {"filter": {"property": "object", "value": "page"},
                                                     "page_size": 100})
            count = len(found.get("results") or [])
            more = "개 이상" if found.get("has_more") else "개"
            where = f" · {workspace}" if workspace else ""
            if not count:
                return {"ok": True, "pages": 0, "message":
                        f"연결 성공 — ‘{name}’{where}. 아직 읽을 수 있는 페이지가 없습니다. "
                        "가져올 페이지를 열고 오른쪽 위 ••• → 연결(Connections)에서 이 연결을 추가하세요."}
            return {"ok": True, "pages": count, "message": f"연결 성공 — ‘{name}’{where} · 읽을 수 있는 페이지 {count}{more}"}
        except NotionError as exc:
            return {"ok": False, "message": str(exc)}
        except Exception as exc:
            return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}

    # ------------------------------------------------------------------ 읽기
    def _cache_key(self) -> str:
        return hashlib.sha256(f"{self.token}\n{self.limit}\n{self.since}".encode()).hexdigest()

    def read(self) -> list[Note]:
        if not self.available():
            raise NotionError(self.unavailable_reason())
        key = self._cache_key()
        with _cache_lock:
            hit = _cache.get(key) if self.reuse_preview else None
            if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
                self.warnings = list(hit[2])
                return list(hit[1])
        notes = self._read_pages()
        with _cache_lock:
            _cache[key] = (time.monotonic(), notes, list(self.warnings))
        return list(notes)

    def _read_pages(self) -> list[Note]:
        notes: list[Note] = []
        since = self.since[:10]
        search = {"filter": {"property": "object", "value": "page"},
                  "sort": {"direction": "descending", "timestamp": "last_edited_time"}}
        for page in self._paged("POST", "search", search):
            if len(notes) >= self.limit:
                self.warnings.append(f"최대 {self.limit}개까지만 가져왔습니다. 더 필요하면 ‘최대 페이지 수’를 늘리세요.")
                break
            if page.get("archived") or page.get("in_trash"):
                continue
            # 검색은 최근 수정순이라 기준일보다 오래된 페이지가 나오면 나머지도 모두 오래됐다.
            if since and (page.get("last_edited_time") or "")[:10] < since:
                break
            try:
                notes.append(self._page_note(page))
            except NotionError as exc:
                log.info("Notion 페이지를 읽지 못함 (%s): %s", page.get("id"), exc)
                self.warnings.append(f"읽지 못한 페이지가 있습니다: {_title(page) or page.get('id')} — {exc}")
        return notes

    def _page_note(self, page: dict) -> Note:
        title = _title(page) or "제목 없음"
        lines = [title]
        props = _property_lines(page)
        if props:
            lines.extend(props)
        body = self._blocks_text(page["id"])
        if body:
            lines.extend(["", body])
        tags = []
        parent = page.get("parent") or {}
        if parent.get("type") == "database_id":
            database = self._database_title(parent.get("database_id"))
            if database:
                tags.append(_tag(database))
        tags.extend(_tag(v) for v in _multi_select_values(page))
        warnings = ["Notion의 이미지·파일은 가져오지 않습니다. 원본 페이지에서 확인하세요."] \
            if "[이미지]" in body or "[파일]" in body else []
        return Note(
            external_id=f"page:{page['id']}",
            body=clean("\n".join(lines)),
            created_at=parse_any_time(page.get("created_time")),
            updated_at=parse_any_time(page.get("last_edited_time")),
            legacy_created_at=legacy_aware_time(page.get("created_time")),
            legacy_updated_at=legacy_aware_time(page.get("last_edited_time")),
            tags=[t for t in dict.fromkeys(tags) if t],
            warnings=warnings,
            origin="page",
        )

    def _database_title(self, database_id: str | None) -> str:
        if not database_id:
            return ""
        if database_id not in self._database_titles:
            try:
                data = self._request("GET", f"databases/{database_id}")
                self._database_titles[database_id] = _plain(data.get("title"))
            except NotionError:
                self._database_titles[database_id] = ""
        return self._database_titles[database_id]

    # ------------------------------------------------------------------ 블록 → 글
    def _blocks_text(self, block_id: str, depth: int = 0) -> str:
        if depth > MAX_DEPTH:
            return ""
        out: list[str] = []
        number = 0
        for block in self._paged("GET", f"blocks/{block_id}/children"):
            kind = block.get("type", "")
            number = number + 1 if kind == "numbered_list_item" else 0
            text = _block_line(block, number)
            children = ""
            if block.get("has_children") and kind not in ("child_page", "child_database"):
                source = block["id"]
                if kind == "synced_block":   # 다른 곳의 동기화 블록을 복사해 둔 것이면 원본을 읽는다
                    source = ((block.get("synced_block") or {}).get("synced_from") or {}).get("block_id") or source
                if kind == "table":
                    children = self._table_text(source)
                else:
                    children = self._blocks_text(source, depth + 1)
            if text is not None:
                out.append(text)
            if children:
                nested = kind not in ("column_list", "column", "synced_block", "table")
                out.append(_indent(children) if nested else children)
        return "\n".join(out)

    def _table_text(self, table_id: str) -> str:
        rows = []
        for row in self._paged("GET", f"blocks/{table_id}/children"):
            cells = (row.get("table_row") or {}).get("cells") or []
            rows.append(" | ".join(_plain(cell) for cell in cells))
        return "\n".join(rows)


# --------------------------------------------------------------------------- 변환 도우미


def _plain(rich) -> str:
    """Notion 의 rich_text 배열 → 글. 글자와 다른 주소가 걸린 링크는 '글 (주소)' 로 남긴다."""
    parts = []
    for item in rich or []:
        text = item.get("plain_text") or ""
        href = item.get("href")
        if href and href not in text and item.get("type") == "text":
            text = f"{text} ({href})"
        parts.append(text)
    return "".join(parts)


def _title(page: dict) -> str:
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            return _plain(prop.get("title")).strip()
    return ""


def _multi_select_values(page: dict) -> list[str]:
    values = []
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "multi_select":
            values.extend(o.get("name", "") for o in prop.get("multi_select") or [])
    return values


def _date_text(date: dict | None) -> str:
    if not date or not date.get("start"):
        return ""
    start = date["start"][:16].replace("T", " ")
    end = (date.get("end") or "")[:16].replace("T", " ")
    return f"{start} ~ {end}" if end else start


def _property_value(prop: dict) -> str:
    kind = prop.get("type")
    value = prop.get(kind)
    if kind in ("rich_text",):
        return _plain(value).strip()
    if kind in ("select", "status"):
        return (value or {}).get("name", "")
    if kind == "multi_select":
        return ", ".join(o.get("name", "") for o in value or [])
    if kind == "date":
        return _date_text(value)
    if kind == "people":
        return ", ".join(p.get("name") or "" for p in value or [] if p.get("name"))
    if kind == "checkbox":
        return "예" if value else ""
    if kind == "number":
        return "" if value is None else f"{value:g}" if isinstance(value, float) else str(value)
    if kind in ("url", "email", "phone_number"):
        return value or ""
    if kind == "formula":
        inner = (value or {}).get((value or {}).get("type"))
        return _date_text(inner) if isinstance(inner, dict) else ("" if inner in (None, False) else str(inner))
    return ""   # title·작성/수정 시각·관계·롤업 등은 본문에 따로 적지 않는다


def _property_lines(page: dict) -> list[str]:
    lines = []
    for name, prop in (page.get("properties") or {}).items():
        if prop.get("type") == "title":
            continue
        value = _property_value(prop)
        if value:
            lines.append(f"{name}: {value}")
    return lines


def _block_line(block: dict, number: int) -> str | None:
    """블록 하나를 한 줄(또는 몇 줄)로. None 이면 이 블록 자체는 글이 없다(자식만 있을 수 있다)."""
    kind = block.get("type", "")
    data = block.get(kind) or {}
    text = _plain(data.get("rich_text"))
    caption = _plain(data.get("caption"))
    if kind == "paragraph":
        return text
    if kind in ("heading_1", "heading_2", "heading_3"):
        return "\n" + "#" * int(kind[-1]) + " " + text if text else ""
    if kind == "bulleted_list_item":
        return "- " + text
    if kind == "numbered_list_item":
        return f"{number}. " + text
    if kind == "to_do":
        return ("[x] " if data.get("checked") else "[ ] ") + text
    if kind == "toggle":
        return "▸ " + text
    if kind == "quote":
        return "> " + text
    if kind == "callout":
        icon = (data.get("icon") or {}).get("emoji") or "💡"
        return f"{icon} {text}"
    if kind == "code":
        return f"```{data.get('language') or ''}\n{text}\n```"
    if kind == "equation":
        return data.get("expression") or ""
    if kind == "divider":
        return "---"
    if kind == "child_page":
        return f"↳ 하위 페이지: {data.get('title') or '제목 없음'}"
    if kind == "child_database":
        return f"↳ 데이터베이스: {data.get('title') or '제목 없음'}"
    if kind in ("bookmark", "embed", "link_preview"):
        url = data.get("url") or ""
        return f"{caption} ({url})" if caption and url else (url or caption)
    if kind in ("image", "file", "pdf", "video", "audio"):
        label = {"image": "이미지", "file": "파일", "pdf": "파일", "video": "동영상", "audio": "소리"}[kind]
        name = caption or data.get("name") or ""
        return f"[{label}] {name}".rstrip()
    if kind == "link_to_page":
        return "→ 다른 페이지로 가는 링크"
    if kind in ("column_list", "column", "synced_block", "table", "table_of_contents", "breadcrumb", "template"):
        return None
    return text or None


def _indent(text: str) -> str:
    return "\n".join("  " + line if line else line for line in text.splitlines())


def _http_hint(exc: urllib.error.HTTPError) -> str:
    """상태코드마다 사용자가 할 일이 다르다 — 그걸 알려준다."""
    detail = ""
    try:
        detail = json.loads(exc.read().decode("utf-8")).get("code", "")
    except Exception:
        pass
    if exc.code == 401:
        return "토큰이 올바르지 않습니다. " + TOKEN_HELP
    if exc.code == 403:
        return "이 연결에 읽기 권한이 없습니다. 연결의 Configuration 탭에서 ‘내용 읽기(Read content)’를 켜세요."
    if exc.code == 404:
        return "페이지를 찾을 수 없습니다. 그 페이지에 이 연결을 추가했는지(••• → 연결) 확인하세요."
    if exc.code == 429:
        return "Notion 이 잠시 요청을 막았습니다. 몇 분 뒤 다시 시도하세요."
    if exc.code >= 500:
        return f"Notion 서버에 문제가 있습니다(HTTP {exc.code}). 잠시 뒤 다시 시도하세요."
    return f"Notion 이 요청을 거부했습니다(HTTP {exc.code}{' ' + detail if detail else ''})."


def _tag(value: str) -> str:
    """데이터베이스 이름·다중 선택 값을 태그로 쓸 수 있게 다듬는다(공백은 _ 로)."""
    joined = "_".join((value or "").split())
    return "".join(ch for ch in joined if ch.isalnum() or ch in "_-")[:40]
