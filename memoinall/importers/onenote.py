"""Read text from OneNote's PDF and Word exports without an Office installation."""

from __future__ import annotations

import base64
import binascii
import hashlib
import zipfile
from pathlib import Path

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from .. import documents
from . import Note, clean


class OneNoteError(ValueError):
    pass


def _word_pages(data: bytes) -> list[tuple[str, str]]:
    """Split only at explicit Word page breaks. Word's visual pagination needs a layout engine."""
    try:
        with documents._archive(data) as archive, archive.open("word/document.xml") as stream:
            root = ElementTree.parse(stream, forbid_dtd=True).getroot()
    except (KeyError, zipfile.BadZipFile, ElementTree.ParseError, DefusedXmlException):
        raise OneNoteError("Word 문서를 읽지 못했습니다. OneNote에서 .docx로 다시 내보내세요.") from None
    namespaces = ("http://schemas.openxmlformats.org/wordprocessingml/2006/main",
                  "http://purl.oclc.org/ooxml/wordprocessingml/main")
    body = next((root.find(f"{{{ns}}}body") for ns in namespaces if root.tag == f"{{{ns}}}document"), None)
    if body is None:
        raise OneNoteError("Word 문서 본문을 찾지 못했습니다.")
    w = root.tag.rsplit("}", 1)[0] + "}"
    parts, chunks = [], []
    page, total = 1, 0
    for element in body.iter():
        page_break = element.tag == w + "br" and element.get(w + "type") == "page"
        if element.tag == w + "p":
            before = element.find(f"{w}pPr/{w}pageBreakBefore")
            if before is not None and before.get(w + "val", "true") in {"1", "true", "on"}:
                page_break = any(chunk.strip() for chunk in chunks)
        if page_break:
            parts.append((f"{page}쪽", clean("".join(chunks))))
            page += 1
            if page > documents.MAX_PARTS:
                raise OneNoteError("500쪽 이하로 나눠 내보내세요.")
            chunks.clear()
            continue
        if element.tag == w + "t":
            text = element.text or ""
        elif element.tag in (w + "p", w + "br", w + "cr"):
            text = "\n"
        elif element.tag == w + "tab":
            text = "\t"
        else:
            continue
        total += len(text)
        if total > documents.MAX_TEXT:
            raise OneNoteError("텍스트 100만 자 이하로 나눠 내보내세요.")
        chunks.append(text)
    parts.append((f"{page}쪽", clean("".join(chunks))))
    return parts


class OneNoteImporter:
    name = "onenote"
    label = "OneNote 내보내기 파일 (PDF·Word)"

    def __init__(self, path="", filename="", content=""):
        self.path = Path(path).expanduser() if path else None
        self.content = content
        self.filename = self.path.name if self.path and not content else filename

    def available(self):
        return bool(self.content or (self.path and self.path.is_file()))

    def unavailable_reason(self):
        return "OneNote에서 내보낸 PDF 또는 Word(.docx) 파일을 선택하세요."

    def read(self) -> list[Note]:
        if not self.available() or not isinstance(self.filename, str):
            raise OneNoteError(self.unavailable_reason())
        name = clean(self.filename.replace("\\", "/").rsplit("/", 1)[-1])
        suffix = Path(name).suffix.lower()
        if suffix not in {".pdf", ".docx"} or len(name) > 200 or "\n" in name or "\t" in name:
            raise OneNoteError(self.unavailable_reason())
        if self.content:
            if not isinstance(self.content, str) or len(self.content) > 4 * ((documents.MAX_BYTES + 2) // 3):
                raise OneNoteError("20MB 이하의 OneNote 내보내기 파일을 선택하세요.")
            try:
                raw = base64.b64decode(self.content, validate=True)
            except (binascii.Error, ValueError):
                raise OneNoteError("파일 데이터를 읽지 못했습니다. 파일을 다시 선택하세요.") from None
        else:
            with self.path.open("rb") as stream:
                raw = stream.read(documents.MAX_BYTES + 1)
        if len(raw) > documents.MAX_BYTES:
            raise OneNoteError("20MB 이하의 OneNote 내보내기 파일을 선택하세요.")
        warnings = []
        try:
            if suffix == ".pdf":
                result = documents.extract(name, raw)
                parts = [(p["label"], p["text"]) for p in result["parts"]]
                warnings.extend(result["warnings"])
            else:
                parts = _word_pages(raw)
                if any(not text for _, text in parts):
                    warnings.append("텍스트가 없는 Word 구역은 건너뜁니다.")
        except documents.DocumentError as exc:
            raise OneNoteError(str(exc)) from exc
        notes = []
        for label, text in parts:
            if text:
                location = f"{name} · {label}"
                uid = hashlib.sha256(f"{location}\0{text}".encode()).hexdigest()
                notes.append(Note(external_id=uid, body=f"{text}\n\n원본: {location}",
                                  origin=location, warnings=warnings))
        if not notes:
            raise OneNoteError("읽을 수 있는 텍스트가 없습니다. 필기·이미지는 OneNote에서 텍스트로 변환한 뒤 내보내세요.")
        return notes
