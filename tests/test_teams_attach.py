"""Teams 새 채팅에 글·파일을 넣는 순서와 안전장치. 실제 Teams 화면은 건드리지 않는다."""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from memoinall import teams_attach
from memoinall.teams_attach import AttachFailed, _NewChat, fill_new_chat, message_marker


class FakeChat:
    """_NewChat 대신: 무엇을 어떤 순서로 했는지만 적는다."""
    instances = []

    def __init__(self, uia, marker, *, found=True, fresh=True, shows=True):
        self.marker, self.found, self.fresh, self.shown = marker, found, fresh, shows
        self.calls = []
        FakeChat.instances.append(self)

    def locate(self):
        return self.found

    def has_message(self):
        return self.fresh

    def replace_message(self, message, put_text):
        self.calls.append("replace")
        put_text(message)
        self.fresh = True

    def remove_old_attachments(self):
        self.calls.append("remove")

    def open_upload_dialog(self):
        self.calls.append("open")
        return 4242

    def choose_file(self, dialog, path):
        self.calls.append(("choose", dialog, path))

    def shows(self, name):
        self.calls.append(("shows", name))
        return self.shown

    def focus_recipients(self):
        self.calls.append("recipients")


def fake(**kw):
    return lambda uia, marker: FakeChat(uia, marker, **kw)


@patch.object(teams_attach, "_uia", return_value={})
class FillNewChatTests(unittest.TestCase):
    def setUp(self):
        FakeChat.instances.clear()

    def test_marker_ignores_line_breaks_and_is_long_enough_to_tell_drafts_apart(self, _uia):
        self.assertEqual(message_marker("공유 일정 2건\n· 11/20(금)  회의\r\n"), "공유 일정 2건 · 11/20(금) 회의")
        self.assertEqual(len(message_marker("가" * 500)), 120)
        self.assertNotEqual(message_marker("공유 일정 1건\n· 11/20 회의"), message_marker("공유 일정 1건\n· 11/21 보고"))

    def test_fresh_chat_gets_file_then_focus_moves_to_recipients(self, _uia):
        put_text = Mock()
        with patch.object(teams_attach, "_NewChat", fake()):
            result = fill_new_chat("메모 1건", put_text, file_path=r"C:\x\memoinall-20261123-0900-1건.json")
        self.assertEqual(result, {"message": True, "attached": True})
        self.assertEqual(FakeChat.instances[0].calls, [
            "remove", "open", ("choose", 4242, r"C:\x\memoinall-20261123-0900-1건.json"),
            ("shows", "memoinall-20261123-0900-1건.json"), "recipients"])
        put_text.assert_not_called()          # 링크의 글이 들어 있으면 클립보드를 건드리지 않는다

    def test_old_draft_text_is_replaced_and_old_attachments_removed_first(self, _uia):
        put_text = Mock()
        with patch.object(teams_attach, "_NewChat", fake(fresh=False)), patch.object(teams_attach, "_wait",
                                                                                     side_effect=lambda ready, *a: ready()):
            result = fill_new_chat("공유 일정 1건", put_text, file_path="C:/x/일정공유-2026-11-01-2026-11-30.ics")
        put_text.assert_called_once_with("공유 일정 1건")
        calls = FakeChat.instances[0].calls
        self.assertEqual(calls[:2], ["replace", "remove"])
        self.assertIn(("shows", "일정공유-2026-11-01-2026-11-30.ics"), calls)
        self.assertTrue(result["message"])

    def test_text_only_and_unconfirmed_attachment(self, _uia):
        with patch.object(teams_attach, "_NewChat", fake()):
            self.assertEqual(fill_new_chat("본문", Mock()), {"message": True, "attached": None})
        self.assertEqual(FakeChat.instances[-1].calls, ["recipients"])
        with patch.object(teams_attach, "_NewChat", fake(shows=False)), \
             patch.object(teams_attach, "_wait", side_effect=lambda ready, *a: ready()):
            self.assertFalse(fill_new_chat("메모", Mock(), file_path="C:/x/a.json")["attached"])

    def test_no_new_chat_means_no_action(self, _uia):
        put_text = Mock()
        with patch.object(teams_attach, "_NewChat", fake(found=False)):
            with self.assertRaisesRegex(AttachFailed, "새 채팅"):
                fill_new_chat("메모", put_text, file_path="C:/x/a.json", timeout=0.3)
        self.assertEqual(FakeChat.instances[0].calls, [])
        put_text.assert_not_called()
        with self.assertRaises(AttachFailed):
            fill_new_chat(" \n ", put_text)


class NewChatSafetyTests(unittest.TestCase):
    """키를 보내기 전 확인: 앞 창이 Teams 이고 포커스가 그 입력란. 아니면 키를 하나도 보내지 않는다."""

    def chat(self, focused_id="new-message-1"):
        focused = SimpleNamespace(Current=SimpleNamespace(AutomationId=focused_id))
        uia = {"AE": SimpleNamespace(FocusedElement=focused)}
        chat = _NewChat(uia, "공유 일정 1건")
        chat.compose, chat.compose_id = Mock(), "new-message-1"
        chat.value = Mock(return_value="공유 일정 1건")
        return chat

    def test_check_requires_teams_in_front_and_focus_in_compose(self):
        with patch.object(teams_attach, "_teams_foreground", return_value=1234):
            self.chat().check()
            with self.assertRaises(AttachFailed):
                self.chat("people-picker-input").check()
        with patch.object(teams_attach, "_teams_foreground", return_value=None), self.assertRaises(AttachFailed):
            self.chat().check()

    def test_replace_message_stops_before_erasing_when_unsafe(self):
        keys = []
        with patch.object(teams_attach, "_keys", side_effect=lambda *s: keys.append(s)), \
             patch.object(teams_attach, "_ctrl", side_effect=lambda vk: keys.append(("ctrl", vk))), \
             patch.object(teams_attach.time, "sleep"), \
             patch.object(teams_attach, "_teams_foreground", return_value=1234):
            with self.assertRaisesRegex(AttachFailed, "클립보드"):          # 클립보드에 못 담으면 지우지 않는다
                self.chat().replace_message("새 글", Mock(side_effect=OSError("busy")))
            self.assertEqual(keys, [])
            with self.assertRaises(AttachFailed):                           # 포커스가 딴 데 있으면 지우지 않는다
                self.chat("people-picker-input").replace_message("새 글", Mock())
            self.assertEqual(keys, [])
            self.chat().replace_message("새 글", Mock())
        self.assertEqual(keys[0], ("ctrl", teams_attach.VK_A))
        self.assertEqual(keys[-1], ("ctrl", teams_attach.VK_V))


@unittest.skipUnless(sys.platform == "win32", "Windows UI Automation")
class RealWindowsTests(unittest.TestCase):
    def test_ui_automation_loads_and_helpers_run_without_touching_teams(self):
        uia = teams_attach._uia()
        self.assertTrue(uia["AE"].RootElement is not None)
        self.assertTrue(teams_attach._process_name(os.getpid()).startswith("python"))
        self.assertIsInstance(teams_attach._teams_file_dialogs(), list)


if __name__ == "__main__":
    unittest.main(verbosity=2)
