"""Exercise desktop Teams handoff without addressing or sending a message."""
import os
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from memoinall import teams_share
from memoinall.teams_attach import AttachFailed
from memoinall.teams_share import MAX_CHAT_URI, NEW_CHAT, _open_text_chat, _share_file, _stage_attachment


def chat_message(uri):
    link = urlsplit(uri)
    assert (link.scheme, link.netloc, link.path) == ("msteams", "teams.microsoft.com", "/l/chat/0/0"), uri
    return parse_qs(link.query, keep_blank_values=True).get("message", [None])[0]


@unittest.skipUnless(sys.platform == "win32", "Windows desktop integration")
class TeamsShareTests(unittest.TestCase):
    filename = "memoinall-20261120-1720-1건.json"

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        home_patch = patch.object(teams_share.config, "HOME", self.home)
        home_patch.start()
        self.addCleanup(home_patch.stop)

    # ------------------------------------------------------------------ 본문
    @patch("memoinall.teams_share.os.startfile")
    def test_new_chat_draft_and_clipboard(self, launch):
        text = "한글 & < > 😀\n둘째 줄 #메모? +%20"
        copy = Mock()
        self.assertEqual(_open_text_chat(text, copy), "draft-copied")
        self.assertEqual(chat_message(launch.call_args.args[0]), text)
        copy.assert_called_once_with(text)
        long = "가나다" * 600
        self.assertEqual(_open_text_chat(long, copy), "paste")
        copy.assert_called_with(long)
        launch.assert_called_with(NEW_CHAT)
        copy.side_effect = OSError("clipboard busy")
        self.assertEqual(_open_text_chat(text, copy), "draft")
        launch.reset_mock()
        with self.assertRaisesRegex(RuntimeError, "클립보드"):
            _open_text_chat(long, copy)
        launch.assert_not_called()
        launch.side_effect = OSError("URI includes sensitive text")
        with self.assertRaisesRegex(RuntimeError, "Teams 설치") as err:
            _open_text_chat(text, copy)
        self.assertNotIn("sensitive", str(err.exception))

    @patch("memoinall.teams_share.os.startfile")
    def test_text_is_filled_into_new_chat_even_over_an_old_draft(self, launch):
        """링크의 글은 예전 초안이 있으면 무시된다 — 화면에서 직접 넣고 'filled'. 못 넣으면 예전 안내로."""
        text, long = "회의 메모\n둘째 줄", "가나다" * 600
        fill = Mock(return_value={"message": True, "attached": None})
        self.assertEqual(_open_text_chat(text, Mock(), fill), "filled")
        fill.assert_called_once_with(text)
        self.assertEqual(_open_text_chat(long, Mock(), fill), "filled")   # 긴 본문도 붙여 넣어 채운다
        launch.assert_called_with(NEW_CHAT)
        for failure in (AttachFailed("새 채팅 없음"), RuntimeError("UIA 요소가 사라짐")):
            fill = Mock(side_effect=failure)
            self.assertEqual(_open_text_chat(text, Mock(), fill), "draft-copied")
            self.assertEqual(_open_text_chat(long, Mock(), fill), "paste")
        fill = Mock(return_value={"message": False, "attached": None})
        self.assertEqual(_open_text_chat(text, Mock(), fill), "draft-copied")

    # ------------------------------------------------------------------ 파일
    @patch("memoinall.teams_share.os.startfile")
    def test_file_is_attached_in_new_chat_with_message(self, launch):
        """파일을 준비하고 새 채팅을 연 뒤, 그 화면에 글과 파일을 넣는다."""
        fill = Mock(return_value={"message": True, "attached": True})
        result = _share_file('{"memos":[]}', self.filename, "메모 1건을 보냅니다.", fill)
        self.assertEqual((result["method"], result["opened"]), ("attached", True))
        path = Path(result["path"])
        self.assertEqual(path.read_text(encoding="utf-8"), '{"memos":[]}')
        self.assertEqual(chat_message(launch.call_args.args[0]), "메모 1건을 보냅니다.")
        fill.assert_called_once_with("메모 1건을 보냅니다.", path)
        # 붙은 것을 확인하지 못했거나 자동으로 못 넣으면 '직접 올리기' 안내(manual)
        for fill in (Mock(return_value={"message": True, "attached": False}),
                     Mock(side_effect=AttachFailed("파일 선택 창이 뜨지 않았습니다.")),
                     Mock(side_effect=RuntimeError("ElementNotAvailable"))):
            self.assertEqual(_share_file("{}", self.filename, None, fill)["method"], "manual")
        self.assertEqual(fill.call_args.args[0], "memoinall 메모 파일을 보냅니다.")

    @patch("memoinall.teams_share.os.startfile")
    def test_desktop_bridge_fills_with_clipboard_text(self, launch):
        import clr   # 앱에서는 pywebview 가 이미 불러 둔다
        clr.AddReference("System.Windows.Forms")
        from System.Threading import ApartmentState, Thread, ThreadStart
        from System.Windows.Forms import Clipboard

        window = Mock()
        window.native.Invoke.side_effect = lambda action: action.Invoke()
        teams_share.install(window)
        share, reveal = window.expose.call_args.args
        self.assertEqual(reveal.__name__, "reveal_teams_file")
        with patch.object(teams_share, "fill_new_chat", return_value={"message": True, "attached": True}) as fill:
            result = share('{"body":"' + "한글 😀" * 10000 + '"}', self.filename, "메모 1건")
            self.assertEqual(result["method"], "attached")
            message, put_text = fill.call_args.args
            self.assertEqual((message, fill.call_args.kwargs), ("메모 1건", {"file_path": result["path"]}))
            self.assertEqual(share("회의 메모", None), "filled")
            self.assertEqual(fill.call_args.kwargs, {"file_path": None})
        self.assertEqual(Path(result["path"]).read_text(encoding="utf-8")[:9], '{"body":"')

        def on_sta():
            put_text("클립보드 시험 😀")
            seen.append(Clipboard.GetText())

        seen = []
        thread = Thread(ThreadStart(on_sta))
        thread.SetApartmentState(ApartmentState.STA)
        thread.Start()
        thread.Join()
        self.assertEqual(seen, ["클립보드 시험 😀"])

    @patch("memoinall.teams_share.os.startfile", side_effect=OSError)
    def test_recover_when_teams_unavailable(self, launch):
        fill = Mock()
        result = _share_file("{}", self.filename, None, fill)
        self.assertEqual((result["method"], result["opened"]), ("manual", False))
        fill.assert_not_called()
        path = Path(result["path"])
        self.assertEqual(path.read_bytes(), b"{}")
        with patch.object(teams_share.subprocess, "Popen") as explorer:
            teams_share.reveal_teams_file(result["name"])
            explorer.assert_called_once_with(["explorer.exe", "/select,", str(path)])
            for name in ("../" + self.filename, str(path), "other.json"):
                with self.assertRaises(ValueError):
                    teams_share.reveal_teams_file(name)
            path.unlink()
            with self.assertRaisesRegex(ValueError, "다시 공유"):
                teams_share.reveal_teams_file(result["name"])
            explorer.assert_called_once()

    def test_repeated_exports_keep_separate_content_and_expire_only_old_exports(self):
        contents = ["첫 파일", "다른 파일", "세 번째 파일"]
        with ThreadPoolExecutor() as pool:
            paths = list(pool.map(lambda content: _stage_attachment(content, self.filename), contents))
        self.assertEqual([p.read_text(encoding="utf-8") for p in paths], contents)
        self.assertEqual(len(set(paths)), len(paths))
        unrelated = paths[0].parent / "other.json"
        unrelated.write_text("keep", encoding="utf-8")
        old = time.time() - 25 * 3600
        for path in (paths[0], unrelated):
            os.utime(path, (old, old))
        teams_share.install(Mock())
        self.assertFalse(paths[0].exists())
        self.assertTrue(unrelated.is_file() and all(p.exists() for p in paths[1:]))

    def test_bad_inputs_never_launch_or_fill(self):
        with patch.object(teams_share.os, "startfile") as launch, patch.object(teams_share.exchange, "MAX_BYTES", 5):
            fill = Mock()
            for content, name, text in (("한글", self.filename, None), ("", self.filename, None),
                                        ("{}", "../" + self.filename, None), ("{}", self.filename, "가" * 20001),
                                        ("{}", self.filename, 3)):
                with self.assertRaises(ValueError):
                    _share_file(content, name, text, fill)
            fill.assert_not_called()
            launch.assert_not_called()
            self.assertFalse((self.home / "teams-share").exists())

    @patch("memoinall.teams_share.os.startfile")
    def test_schedule_ics_is_shareable_like_memo_files(self, launch):
        name = "일정공유-2026-11-01-2026-11-30.ics"
        fill = Mock(return_value={"message": True, "attached": True})
        first = _share_file("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", name, "공유 일정 1건", fill)
        second = _share_file("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", name, None, fill)
        self.assertEqual(Path(first["path"]).suffix, ".ics")
        self.assertEqual(second["name"], "일정공유-2026-11-01-2026-11-30 (2).ics")
        self.assertEqual(fill.call_args_list[0].args, ("공유 일정 1건", Path(first["path"])))
        self.assertEqual(fill.call_args_list[1].args[0], "memoinall 일정 파일을 보냅니다.")
        # 링크에 다 못 싣는 긴 일정 목록은 새 채팅만 열고 화면에서 붙여 넣는다
        long = "공유 일정 300건\n" + "· 11/20(금) 회의\n" * 700
        _share_file("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", name, long, fill)
        self.assertGreater(len(long), MAX_CHAT_URI)
        launch.assert_called_with(NEW_CHAT)
        self.assertEqual(fill.call_args.args[0], long)
        with patch.object(teams_share.subprocess, "Popen") as explorer:
            teams_share.reveal_teams_file(second["name"])
            explorer.assert_called_once()
        for bad in ("일정공유-2026-11-01.ics", "일정공유-2026-11-01-2026-11-30.exe", "../" + name):
            with self.assertRaises(ValueError):
                _share_file("x", bad, None, fill)
        old = time.time() - 25 * 3600
        os.utime(first["path"], (old, old))
        teams_share.install(Mock())
        self.assertFalse(Path(first["path"]).exists())        # 하루 지난 일정 파일도 정리된다
        self.assertTrue(Path(second["path"]).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
