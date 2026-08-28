"""Redmine 임포터 — REST API 로 이슈·위키·문서·공지를 가져온다.

다른 임포터와 달리 **네트워크 소스**다. 그래서 신경 쓴 것:

  - **한 번에 다 긁지 않는다.** 이슈가 수만 건인 서버도 있다. 기본 상한을 두고,
    프로젝트·갱신일로 좁힐 수 있게 했다.
  - **실패 원인을 구분해서 알려준다.** 401(키), 403(권한), 404(주소), 연결 실패는
    사용자가 할 일이 전혀 다르다. "가져오기 실패"만 띄우면 고칠 수가 없다.
  - **의존성을 늘리지 않는다.** urllib 로 충분하다.

API 키는 Redmine 의 [내 계정] 화면 오른쪽에서 확인할 수 있다.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from . import Note, clean

log = logging.getLogger(__name__)

PAGE_SIZE = 100  # Redmine 의 최대 limit
TIMEOUT = 30

# 가져올 수 있는 자료 종류
KINDS = {
    "issues": "이슈",
    "wiki": "위키",
    "documents": "문서",
    "news": "공지",
}
DEFAULT_KINDS = ["issues", "wiki", "documents"]


class RedmineError(RuntimeError):
    pass


class RedmineImporter:
    name = "redmine"
    label = "Redmine"

    def __init__(
        self,
        url: str = "",
        api_key: str = "",
        *,
        projects: str = "",
        kinds: list[str] | None = None,
        limit: int = 300,
        since: str = "",
        include_notes: bool = False,
    ):
        self.url = (url or "").strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.projects = [p.strip() for p in (projects or "").split(",") if p.strip()]
        self.kinds = [k for k in (kinds or DEFAULT_KINDS) if k in KINDS] or DEFAULT_KINDS
        self.limit = max(1, int(limit or 300))
        self.since = (since or "").strip()
        self.include_notes = include_notes
        self.path = self.url  # UI 가 소스 위치로 표시한다

    # ------------------------------------------------------------------ 가용성
    def available(self) -> bool:
        return bool(self.url and self.api_key)

    def unavailable_reason(self) -> str:
        if not self.url:
            return "Redmine 주소가 설정되지 않았습니다 (설정 탭에서 등록하세요)."
        return "Redmine API 키가 설정되지 않았습니다. Redmine 의 [내 계정] 화면에서 확인할 수 있습니다."

    # ------------------------------------------------------------------ HTTP
    def _get(self, path: str, **params) -> dict:
        query = {k: v for k, v in params.items() if v not in (None, "", [])}
        url = f"{self.url}/{path.lstrip('/')}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        # HTTP 헤더는 latin-1 로 인코딩된다. 키에 한글이 섞이면 urllib 이
        # UnicodeEncodeError 로 죽는데, 그 메시지로는 사용자가 원인을 알 수 없다.
        try:
            self.api_key.encode("ascii")
        except UnicodeEncodeError:
            raise RedmineError(
                "API 키에 한글이나 특수문자가 섞여 있습니다. "
                "Redmine 의 [내 계정] 화면에 있는 키를 그대로 붙여넣으세요."
            ) from None

        req = urllib.request.Request(url, headers={
            "X-Redmine-API-Key": self.api_key,
            "Accept": "application/json",
            "User-Agent": "memoinall",
        })
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RedmineError(_http_hint(exc, url)) from exc
        except urllib.error.URLError as exc:
            raise RedmineError(
                f"Redmine 서버에 연결할 수 없습니다 ({self.url}). 주소와 네트워크를 확인하세요. — {exc.reason}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise RedmineError(
                f"Redmine 이 JSON 이 아닌 응답을 보냈습니다 ({url}). "
                "주소가 Redmine 루트가 맞는지 확인하세요(예: https://redmine.example.com)."
            ) from exc

    def _paged(self, path: str, key: str, budget: int, **params):
        """total_count 를 보고 필요한 만큼만 돈다. budget 을 넘기면 멈춘다."""
        offset = 0
        while offset < budget:
            page = self._get(path, offset=offset, limit=min(PAGE_SIZE, budget - offset), **params)
            items = page.get(key) or []
            yield from items
            offset += len(items)
            if not items or offset >= int(page.get("total_count") or 0):
                return

    # ------------------------------------------------------------------ 연결 확인
    def test_connection(self) -> dict:
        """가져오기 전에 주소·키가 맞는지 확인한다."""
        if not self.available():
            return {"ok": False, "message": self.unavailable_reason()}
        try:
            me = self._get("users/current.json").get("user", {})
            projects = self._get("projects.json", limit=1).get("total_count", 0)
            who = me.get("login") or f"{me.get('lastname', '')}{me.get('firstname', '')}" or "?"
            return {"ok": True, "message": f"연결 성공 — {who} 로 접속, 프로젝트 {projects}개",
                    "user": who, "projects": projects}
        except RedmineError as exc:
            return {"ok": False, "message": str(exc)}
        except Exception as exc:
            return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}

    def list_projects(self) -> list[dict]:
        try:
            out = []
            for p in self._paged("projects.json", "projects", budget=200):
                out.append({"id": p.get("identifier") or p.get("id"), "name": p.get("name", "")})
            return out
        except Exception as exc:
            log.info("프로젝트 목록 조회 실패: %s", exc)
            return []

    # ------------------------------------------------------------------ 읽기
    def read(self) -> list[Note]:
        if not self.available():
            raise RedmineError(self.unavailable_reason())

        readers = {
            "issues": self._read_issues,
            "wiki": self._read_wiki,
            "documents": self._read_documents,
            "news": self._read_news,
        }
        # 상한은 **종류별**로 준다. 전체 상한을 순서대로 소진시키면
        # 이슈가 한도를 다 먹고 위키·문서는 0건이 되는데, 사용자는 왜 안 왔는지 알 수 없다.
        notes: list[Note] = []
        for kind in self.kinds:
            notes.extend(readers[kind](self.limit))
        return notes

    # -- 이슈 -----------------------------------------------------------
    def _read_issues(self, budget: int) -> list[Note]:
        params = {"status_id": "*", "sort": "updated_on:desc"}
        if self.since:
            params["updated_on"] = f">={self.since}"

        notes: list[Note] = []
        for project in self.projects or [None]:
            if len(notes) >= budget:
                break
            p = dict(params)
            if project:
                p["project_id"] = project
            for issue in self._paged("issues.json", "issues", budget - len(notes), **p):
                notes.append(self._issue_note(issue))
        return notes

    def _issue_note(self, issue: dict) -> Note:
        iid = issue.get("id")
        project = (issue.get("project") or {}).get("name", "")
        tracker = (issue.get("tracker") or {}).get("name", "")
        status = (issue.get("status") or {}).get("name", "")
        author = (issue.get("author") or {}).get("name", "")
        assignee = (issue.get("assigned_to") or {}).get("name", "")

        meta = [x for x in (tracker, status, f"담당 {assignee}" if assignee else "") if x]
        lines = [f"[#{iid}] {issue.get('subject', '')}"]
        if meta:
            lines.append(" · ".join(meta))
        body = clean(issue.get("description") or "")
        if body:
            lines.append("")
            lines.append(body)

        if self.include_notes:
            for entry in self._issue_notes(iid):
                lines.append("")
                lines.append(entry)

        tags = [t for t in (_tag(project), _tag(tracker)) if t]
        return Note(
            external_id=f"issue:{iid}",
            body="\n".join(lines),
            created_at=_iso(issue.get("created_on")),
            updated_at=_iso(issue.get("updated_on")),
            tags=tags,
            origin="issue",
        )

    def _issue_notes(self, issue_id) -> list[str]:
        """코멘트는 이슈 상세를 따로 불러야 나온다 — 건별 요청이라 기본은 끈다."""
        try:
            data = self._get(f"issues/{issue_id}.json", include="journals")
        except RedmineError:
            return []
        out = []
        for j in (data.get("issue", {}).get("journals") or []):
            text = clean(j.get("notes") or "")
            if text:
                who = (j.get("user") or {}).get("name", "")
                out.append(f"— {who} ({(j.get('created_on') or '')[:10]})\n{text}")
        return out

    # -- 위키 -----------------------------------------------------------
    def _read_wiki(self, budget: int) -> list[Note]:
        projects = self.projects or [p["id"] for p in self.list_projects()]
        notes: list[Note] = []
        for project in projects:
            if len(notes) >= budget:
                break
            try:
                index = self._get(f"projects/{project}/wiki/index.json").get("wiki_pages") or []
            except RedmineError as exc:
                log.info("위키 없음/접근 불가 (%s): %s", project, exc)
                continue
            for page in index:
                if len(notes) >= budget:
                    break
                title = page.get("title")
                try:
                    detail = self._get(
                        f"projects/{project}/wiki/{urllib.parse.quote(str(title))}.json"
                    ).get("wiki_page", {})
                except RedmineError:
                    continue
                text = clean(detail.get("text") or "")
                if not text:
                    continue
                notes.append(Note(
                    external_id=f"wiki:{project}/{title}",
                    body=f"{title}\n\n{text}",
                    created_at=_iso(detail.get("created_on")),
                    updated_at=_iso(detail.get("updated_on")),
                    tags=[t for t in (_tag(str(project)), "위키") if t],
                    origin="wiki",
                ))
        return notes

    # -- 문서 -----------------------------------------------------------
    def _read_documents(self, budget: int) -> list[Note]:
        projects = self.projects or [p["id"] for p in self.list_projects()]
        notes: list[Note] = []
        for project in projects:
            if len(notes) >= budget:
                break
            try:
                docs = list(self._paged(
                    f"projects/{project}/documents.json", "documents", budget - len(notes)
                ))
            except RedmineError as exc:
                log.info("문서 모듈 없음/접근 불가 (%s): %s", project, exc)
                continue
            for doc in docs:
                text = clean(doc.get("description") or "")
                title = doc.get("title", "")
                if not (text or title):
                    continue
                category = (doc.get("category") or {}).get("name", "")
                notes.append(Note(
                    external_id=f"document:{doc.get('id')}",
                    body=f"{title}\n\n{text}".strip(),
                    created_at=_iso(doc.get("created_on")),
                    updated_at=_iso(doc.get("created_on")),
                    tags=[t for t in (_tag(str(project)), _tag(category), "문서") if t],
                    origin="document",
                ))
        return notes

    # -- 공지 -----------------------------------------------------------
    def _read_news(self, budget: int) -> list[Note]:
        notes: list[Note] = []
        for project in self.projects or [None]:
            if len(notes) >= budget:
                break
            path = f"projects/{project}/news.json" if project else "news.json"
            try:
                items = list(self._paged(path, "news", budget - len(notes)))
            except RedmineError:
                continue
            for item in items:
                text = clean(item.get("description") or "")
                title = item.get("title", "")
                if not (text or title):
                    continue
                proj = (item.get("project") or {}).get("name", "") or str(project or "")
                notes.append(Note(
                    external_id=f"news:{item.get('id')}",
                    body=f"{title}\n\n{text}".strip(),
                    created_at=_iso(item.get("created_on")),
                    updated_at=_iso(item.get("created_on")),
                    tags=[t for t in (_tag(proj), "공지") if t],
                    origin="news",
                ))
        return notes


# --------------------------------------------------------------------------- 유틸


def _http_hint(exc: urllib.error.HTTPError, url: str) -> str:
    """상태코드마다 사용자가 할 일이 다르다 — 그걸 알려준다."""
    hints = {
        401: "API 키가 올바르지 않습니다. Redmine 의 [내 계정] 화면에서 다시 확인하세요.",
        403: "권한이 없습니다. 해당 프로젝트 접근 권한이나 REST API 사용 설정을 확인하세요.",
        404: "주소를 찾을 수 없습니다. Redmine 루트 주소가 맞는지 확인하세요.",
        422: "요청이 거부됐습니다(필터 값 오류일 수 있습니다).",
    }
    hint = hints.get(exc.code, f"HTTP {exc.code} {exc.reason}")
    return f"{hint}  ({url})"


def _iso(value: str | None) -> str | None:
    """Redmine 은 '2026-03-04T05:06:07Z' 형태로 준다."""
    if not value:
        return None
    text = str(value).replace("Z", "").replace("T", " ").strip()
    return text.replace(" ", "T")[:19] or None


def _tag(value: str) -> str:
    """프로젝트/트래커 이름을 태그로 쓸 수 있게 다듬는다."""
    cleaned = "".join(ch for ch in (value or "").strip() if ch.isalnum() or ch in "가-힣_-")
    cleaned = "_".join((value or "").split()) if not cleaned else cleaned
    cleaned = "".join(ch for ch in cleaned if ch.isalnum() or ch in "_-")
    return cleaned[:40]
