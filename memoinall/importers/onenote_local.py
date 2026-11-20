"""Read open Windows desktop OneNote notebooks, one memo per OneNote page."""

import json
import os
import subprocess
from html.parser import HTMLParser
from pathlib import Path

from . import Note, clean, parse_any_time
from .onenote import OneNoteError


class _RichText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")

    @classmethod
    def plain(cls, fragments):
        parser = cls()
        parser.feed("\n".join(fragments))
        parser.close()
        return clean("".join(parser.parts))


def _read_desktop():
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    script = Path(__file__).with_name("read_onenote.ps1").read_text(encoding="utf-8")
    try:
        result = subprocess.run(
            [str(shell), "-NoProfile", "-NonInteractive", "-STA", "-Command", script],
            stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW, timeout=600,
        )
    except subprocess.TimeoutExpired:
        raise OneNoteError("OneNote가 응답하지 않았습니다. 전자 필기장을 열어 동기화 상태를 확인한 뒤 다시 시도하세요.") from None
    except OSError:
        raise OneNoteError("Windows PowerShell을 실행하지 못했습니다. OneNote 내보내기 파일로도 가져올 수 있습니다.") from None
    if result.returncode:
        raise OneNoteError("Windows용 OneNote에 연결하지 못했습니다. 같은 Windows 계정에서 OneNote와 전자 필기장을 연 뒤 다시 시도하세요.")
    for line in result.stdout.split("\n"):
        if line.strip():
            yield json.loads(line)


class LocalOneNoteImporter:
    name = "onenote_local"
    label = "PC의 OneNote 전자 필기장"
    path = None

    def available(self):
        if os.name != "nt":
            return False
        import winreg

        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"OneNote.Application\CLSID",
                                    0, winreg.KEY_READ | view):
                    return True
            except OSError:
                continue
        return False

    def unavailable_reason(self):
        return "Windows용 OneNote 데스크톱 앱이 필요합니다. 웹용 OneNote는 아래에서 내보낸 파일을 선택하세요."

    def read(self) -> list[Note]:
        notes, self.warnings = [], []
        summary = None
        for row in _read_desktop():
            if "notebooks" in row:
                summary = row
                continue
            title = _RichText.plain([row["title"]])
            text = _RichText.plain(row["lines"])
            if not title and not text:
                notes.append(Note(external_id=row["id"], body=""))
                continue
            body = "\n".join(filter(None, (title or row["name"], text)))
            location = row["path"]
            if location:
                body += f"\n\n원본: {location}"
            notes.append(Note(external_id=row["id"], body=body, origin=location,
                              created_at=parse_any_time(row["created"]), updated_at=parse_any_time(row["updated"])))
        if summary is None:
            raise OneNoteError("전자 필기장 읽기가 중단됐습니다. OneNote를 확인한 뒤 다시 시도하세요.")
        if not summary["notebooks"]:
            self.warnings.append("열린 전자 필기장이 없습니다. Windows용 OneNote에서 가져올 전자 필기장을 먼저 여세요.")
        if summary["locked"]:
            self.warnings.append(f"잠긴 섹션 {summary['locked']}개를 건너뛰었습니다. 가져오려면 OneNote에서 잠금을 푸세요.")
        if summary["failed"]:
            self.warnings.append(f"읽지 못한 페이지가 {summary['failed']}개 있습니다. OneNote에서 해당 페이지를 열고 동기화한 뒤 다시 시도하세요.")
        return notes
