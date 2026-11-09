"""Read ENEX locally. ENML is converted to text; resources are never executed."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from . import Note, clean

MAX_BYTES = 50_000_000


class EvernoteError(ValueError):
    pass


class _ENML(HTMLParser):
    blocks = {"div", "p", "li", "ul", "ol", "blockquote", "pre", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.links = []
        self.hidden = []
        self.encrypted = False
        self.media = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.hidden:
            self.hidden.append(tag)
            return
        if tag in {"en-crypt", "script", "style"}:
            self.hidden.append(tag)
            if tag == "en-crypt":
                self.encrypted = True
                self.parts.append("[암호화된 내용 — Evernote에서 확인]\n")
            return
        if tag in self.blocks or tag in {"br", "hr"}:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")
        if tag in {"td", "th"}:
            self.parts.append("\t")
        if tag == "en-todo":
            self.parts.append("[x] " if attrs.get("checked") == "true" else "[ ] ")
        if tag == "en-media":
            self.media = True
            self.parts.append("[첨부파일 — 원본 ENEX에서 확인]\n")
        if tag == "a":
            href = attrs.get("href") or ""
            self.links.append(href if href.lower().startswith(("https://", "http://", "mailto:", "evernote://")) else "")

    def handle_endtag(self, tag):
        if self.hidden:
            if tag in self.hidden:
                self.hidden = self.hidden[:self.hidden.index(tag)]
            return
        if tag == "a" and self.links:
            href = self.links.pop()
            if href:
                self.parts.append(f" ({href})")
        if tag in self.blocks:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _time(value: str) -> str | None:
    if not value:
        return None
    try:
        # The rest of the app uses local, timezone-naive ISO timestamps.
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None).isoformat()
    except ValueError:
        raise EvernoteError("ENEX의 작성·수정 날짜를 읽지 못했습니다. Evernote에서 다시 내보내세요.")


class EvernoteImporter:
    name = "evernote"
    label = "Evernote (에버노트)"

    def __init__(self, path: str | Path = "", content: str = ""):
        self.path = Path(path).expanduser() if path else None
        self.content = content

    def available(self):
        return bool(self.content.strip() or (self.path and self.path.is_file()))

    def unavailable_reason(self):
        return "Evernote에서 내보낸 .enex 파일을 선택하세요."

    def read(self) -> list[Note]:
        if self.content:
            raw = self.content.encode("utf-8")
        elif self.path:
            with self.path.open("rb") as stream:
                raw = stream.read(MAX_BYTES + 1)
        else:
            raise EvernoteError(self.unavailable_reason())
        if len(raw) > MAX_BYTES:
            raise EvernoteError("50MB 이하의 ENEX 파일을 선택하세요. Evernote에서 메모를 나눠 내보낼 수 있습니다.")
        try:
            root = ElementTree.fromstring(raw, forbid_entities=True, forbid_external=True)
        except (ElementTree.ParseError, DefusedXmlException, ValueError):
            raise EvernoteError("올바른 ENEX 파일이 아닙니다. Evernote에서 .enex 형식으로 다시 내보내세요.") from None
        if root.tag != "en-export":
            raise EvernoteError("Evernote ENEX 파일이 아닙니다 (en-export 요소 없음).")
        notes = []
        for element in root.findall("note"):
            title = clean(element.findtext("title"))
            parser = _ENML()
            parser.feed(element.findtext("content") or "")
            parser.close()
            text = clean("".join(parser.parts))
            body = clean(f"{title}\n\n{text}" if title and title != text else text or title)
            warnings = []
            resources = element.findall("resource")
            if resources or parser.media:
                warnings.append("이미지·첨부파일은 저장하지 않습니다. 원본 ENEX 파일을 보관하세요.")
                names = [r.findtext("resource-attributes/file-name") or r.findtext("mime") or "이름 없음" for r in resources]
                if names:
                    body += "\n\n첨부파일 (원본에 보관): " + ", ".join(names)
            if parser.encrypted:
                warnings.append("암호화된 내용은 가져오지 않습니다. Evernote에서 암호를 해제한 뒤 다시 내보내세요.")
            url = element.findtext("note-attributes/source-url") or ""
            if url:
                body += "\n\n원본 URL: " + url
            tags = []
            originals = [clean(t.text) for t in element.findall("tag") if clean(t.text)]
            for tag in originals:
                normalized = re.sub(r"\s+", "_", tag)
                if re.fullmatch(r"[0-9A-Za-z가-힣_/\-]{1,40}", normalized):
                    tags.append(normalized)
                else:
                    warnings.append("일부 태그는 앱의 태그 형식과 달라 본문에 원문으로 남깁니다.")
            if originals:
                body += "\n\nEvernote 태그: " + ", ".join(originals)
            created = element.findtext("created") or ""
            updated = element.findtext("updated") or ""
            # ENEX has no guaranteed stable note ID. Content identity prevents an
            # edited export from silently overwriting a different/local revision.
            identity = "\n".join([created, body])
            notes.append(Note(
                external_id=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                body=clean(body), created_at=_time(created), updated_at=_time(updated),
                tags=tags, warnings=list(dict.fromkeys(warnings)), origin="Evernote",
            ))
        return notes
