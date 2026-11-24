"""가져온 앱을 알아볼 수 있는 기본 태그. 내용 분류나 AI 호출은 하지 않는다."""

from .extract import TAG_RE

SOURCE_TAGS = {
    "sticky": "스티커메모",
    "samsung": "삼성노트",
    "redmine": "Redmine",
    "notion": "Notion",
    "evernote": "에버노트",
    "onenote": "OneNote",
    "onenote_local": "OneNote",
    "files": "텍스트파일",
}

IMPORT_SOURCES = (*SOURCE_TAGS, "shared", "document")


def append_source_tag(body: str, source: str) -> str:
    tag = SOURCE_TAGS.get(source)
    if not tag or tag.casefold() in {t.casefold() for t in TAG_RE.findall(body)}:
        return body
    return body + "\n#" + tag
