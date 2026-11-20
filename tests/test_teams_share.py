"""Exercise desktop Teams handoff without addressing or sending a message."""
import os
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from memoinall import teams_share, windows_share
from memoinall.teams_share import NEW_CHAT, _open_file_chat, _open_text_chat, _share_file, _stage_attachment


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

    @patch("memoinall.teams_share.os.startfile")
    def test_new_chat_draft_and_clipboard(self, launch):
        text = "한글 & < > 😀\n둘째 줄 #메모? +%20"
        copy = Mock()
        self.assertEqual(_open_text_chat(text, copy), "draft-copied")
        link = urlsplit(launch.call_args.args[0])
        self.assertEqual((link.scheme, link.netloc, link.path), ("msteams", "teams.microsoft.com", "/l/chat/0/0"))
        self.assertEqual(parse_qs(link.query, keep_blank_values=True), {"users": [""], "message": [text]})
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
    def test_windows_file_payload_and_recipient_link(self, launch):
        """공유 창을 쓸 수 없는 환경(관리자 권한 등)에서는 예전처럼 클립보드 + 새 채팅으로 대신한다."""
        import clr
        clr.AddReference("System.Windows.Forms")
        from System.Windows.Forms import DataFormats, DataObject

        content = '{"body":"' + "한글 😀" * 10000 + '"}'
        clipboard = DataObject()
        window = Mock()
        window.native.Invoke.side_effect = lambda action: action.Invoke()
        teams_share.install(window)
        share, reveal = window.expose.call_args.args
        self.assertEqual(reveal.__name__, "reveal_teams_file")
        with patch.dict(sys.modules, {"System.Windows.Forms": SimpleNamespace(Clipboard=clipboard)}), \
             patch.object(windows_share.Sharer, "share", side_effect=windows_share.ShareUnavailable("관리자")):
            result = share(content, self.filename)
        self.assertEqual(result["method"], "clipboard")
        self.assertTrue(result["opened"] and result["copied"])
        self.assertTrue(clipboard.GetDataPresent(DataFormats.FileDrop))
        self.assertEqual(list(clipboard.GetFileDropList()), [result["path"]])
        self.assertEqual(Path(result["path"]).read_bytes(), content.encode("utf-8"))
        launch.assert_called_once_with(NEW_CHAT)

    @patch("memoinall.teams_share.os.startfile", side_effect=OSError)
    def test_recover_when_clipboard_and_teams_unavailable(self, launch):
        result = _open_file_chat("{}", self.filename, Mock(side_effect=OSError))
        self.assertFalse(result["opened"] or result["copied"])
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
        launch.assert_called_once_with(NEW_CHAT)

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

    def test_bad_inputs_never_launch_or_copy(self):
        with patch.object(teams_share.os, "startfile") as launch, patch.object(teams_share.exchange, "MAX_BYTES", 5):
            copy = Mock()
            for content, name in (("한글", self.filename), ("", self.filename), ("{}", "../" + self.filename)):
                with self.assertRaises(ValueError):
                    _open_file_chat(content, name, copy)
            copy.assert_not_called()
            launch.assert_not_called()
            self.assertFalse((self.home / "teams-share").exists())


    # ------------------------------------------------------------------ Windows 공유 창으로 첨부
    @patch("memoinall.teams_share.os.startfile")
    def test_file_goes_to_windows_share_sheet_with_message(self, launch):
        """파일은 Teams 링크로 첨부할 수 없어 Windows 공유 창으로 넘긴다 — 클립보드·새 채팅은 건드리지 않는다."""
        sheet, copy = Mock(), Mock()
        result = _share_file('{"memos":[]}', self.filename, "메모 1건을 보냅니다.", sheet, copy)
        self.assertEqual(result["method"], "share")
        path = Path(result["path"])
        self.assertEqual(path.read_text(encoding="utf-8"), '{"memos":[]}')
        sheet.assert_called_once_with(path, "memoinall 메모 공유", "메모 1건을 보냅니다.")
        copy.assert_not_called()
        launch.assert_not_called()

    @patch("memoinall.teams_share.os.startfile")
    def test_schedule_ics_is_shareable_like_memo_files(self, launch):
        name = "일정공유-2026-11-01-2026-11-30.ics"
        sheet = Mock()
        first = _share_file("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", name, "공유 일정 1건", sheet, Mock())
        second = _share_file("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", name, None, sheet, Mock())
        self.assertEqual(Path(first["path"]).suffix, ".ics")
        self.assertEqual(second["name"], "일정공유-2026-11-01-2026-11-30 (2).ics")
        self.assertEqual(sheet.call_args_list[0].args[1:], ("memoinall 일정 공유", "공유 일정 1건"))
        self.assertEqual(sheet.call_args_list[1].args[2], "")
        with patch.object(teams_share.subprocess, "Popen") as explorer:
            teams_share.reveal_teams_file(second["name"])
            explorer.assert_called_once()
        for bad in ("일정공유-2026-11-01.ics", "일정공유-2026-11-01-2026-11-30.exe", "../" + name):
            with self.assertRaises(ValueError):
                _share_file("x", bad, None, sheet, Mock())
        with self.assertRaisesRegex(ValueError, "함께 보낼 글"):
            _share_file("x", name, "가" * 20001, sheet, Mock())
        old = time.time() - 25 * 3600
        os.utime(first["path"], (old, old))
        teams_share.install(Mock())
        self.assertFalse(Path(first["path"]).exists())        # 하루 지난 일정 파일도 정리된다
        self.assertTrue(Path(second["path"]).exists())
        launch.assert_not_called()

    def test_share_sheet_unavailable_when_elevated_without_touching_winrt(self):
        sharer = windows_share.Sharer()
        with patch.object(windows_share, "_elevated", return_value=True), \
             patch.object(windows_share, "_storage_file") as load:
            with self.assertRaisesRegex(windows_share.ShareUnavailable, "관리자"):
                sharer.share(Mock(), Mock(), self.home / "x.json", title="t")
            load.assert_not_called()

    def test_share_sheet_failure_on_ui_thread_becomes_share_unavailable(self):
        sharer = windows_share.Sharer()
        with patch.object(windows_share, "_elevated", return_value=False), \
             patch.object(windows_share, "_storage_file", return_value=object()), \
             patch.object(sharer, "_show", side_effect=OSError(-2147024891, "access denied")):
            with self.assertRaises(windows_share.ShareUnavailable):
                sharer.share(lambda fn: fn(), lambda: 1234, self.home / "x.json", title="t")
        with patch.object(windows_share, "_elevated", return_value=False), \
             patch.object(windows_share, "_storage_file", side_effect=OSError("없는 파일")):
            with self.assertRaises(windows_share.ShareUnavailable):
                sharer.share(lambda fn: fn(), lambda: 1234, self.home / "x.json", title="t")

    def test_data_requested_fills_title_text_and_file(self):
        """공유 창이 내용을 달라고 할 때 제목·설명·글·파일을 채운다. 준비된 것이 없으면 이유를 보여준다."""
        sharer = windows_share.Sharer()
        props = SimpleNamespace(title=None, description=None)
        data = SimpleNamespace(properties=props, set_text=Mock(), set_storage_items=Mock())
        request = SimpleNamespace(data=data, fail_with_display_text=Mock())
        sharer._on_requested(None, SimpleNamespace(request=request))
        request.fail_with_display_text.assert_called_once()
        data.set_storage_items.assert_not_called()
        file = object()
        sharer._pending = {"file": file, "title": "memoinall 일정 공유", "description": "일정공유.ics", "text": "공유 일정 2건"}
        request.fail_with_display_text.reset_mock()
        sharer._on_requested(None, SimpleNamespace(request=request))
        self.assertEqual((props.title, props.description), ("memoinall 일정 공유", "일정공유.ics"))
        data.set_text.assert_called_once_with("공유 일정 2건")
        data.set_storage_items.assert_called_once_with([file])
        request.fail_with_display_text.assert_not_called()

    def test_real_winrt_prepares_file_and_window_manager_without_showing_ui(self):
        """실제 WinRT 로 파일을 열고, 창의 DataTransferManager 를 얻어 DataRequested 를 건다(공유 창은 열지 않는다)."""
        try:
            import winrt  # noqa: F401
        except ImportError:
            self.skipTest("winrt 가 설치되지 않음")
        import clr
        clr.AddReference("System.Windows.Forms")
        from System.Threading import ApartmentState, Thread, ThreadStart
        from System.Windows.Forms import Form

        path = _stage_attachment("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", "일정공유-2026-11-01-2026-11-30.ics")
        storage = windows_share._storage_file(path)
        self.assertEqual(storage.name, path.name)
        outcome = {}

        def on_sta():
            try:
                form = Form()
                hwnd = form.Handle.ToInt64()
                sharer = windows_share.Sharer()
                factory, pointer, get_for_window, _show = sharer._interop()
                first = sharer._manager(hwnd, pointer, get_for_window)
                again = sharer._manager(hwnd, pointer, get_for_window)
                outcome["manager"] = type(first).__name__
                outcome["cached"] = first is again
                del factory
                form.Dispose()
            except Exception as exc:  # noqa: BLE001
                outcome["error"] = repr(exc)

        thread = Thread(ThreadStart(on_sta))
        thread.SetApartmentState(ApartmentState.STA)
        thread.Start()
        thread.Join()
        self.assertNotIn("error", outcome, outcome.get("error"))
        self.assertEqual(outcome, {"manager": "DataTransferManager", "cached": True})


if __name__ == "__main__":
    unittest.main(verbosity=2)
