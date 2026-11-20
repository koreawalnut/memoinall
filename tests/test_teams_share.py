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
from memoinall import teams_share
from memoinall.teams_share import NEW_CHAT, _open_file_chat, _open_text_chat, _stage_attachment


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
        with patch.dict(sys.modules, {"System.Windows.Forms": SimpleNamespace(Clipboard=clipboard)}):
            result = share(content, self.filename)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
