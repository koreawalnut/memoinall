"""Python 환경 전환과 빌드 환경 검사. python tests/test_build_exe.py"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build_exe


class BuildEnvironmentTests(unittest.TestCase):
    def test_ready_current_python_does_not_switch_or_build_in_check_mode(self):
        with patch.object(build_exe, "missing_packages", return_value=[]), \
             patch.object(build_exe, "check", return_value=True), \
             patch.object(build_exe, "project_python") as choose, \
             patch.object(build_exe.subprocess, "run") as run, \
             patch.object(build_exe.shutil, "rmtree") as remove:
            self.assertEqual(build_exe.main(["--check"]), 0)
            choose.assert_not_called()
            run.assert_not_called()
            remove.assert_not_called()

    def test_missing_packages_use_project_environment_and_propagate_status(self):
        candidate = Path("ready-venv-python.exe")
        with patch.object(build_exe, "missing_packages", return_value=["xlrd"]), \
             patch.object(build_exe, "project_python", return_value=candidate), \
             patch.object(build_exe.subprocess, "run", return_value=SimpleNamespace(returncode=7)) as run, \
             patch.object(build_exe.shutil, "rmtree") as remove:
            self.assertEqual(build_exe.main(["--check"]), 7)
            self.assertEqual(run.call_args.args[0], [str(candidate), "-X", "utf8", str(build_exe.ROOT / "build_exe.py"), "--check"])
            remove.assert_not_called()

    def test_incomplete_environments_fail_before_touching_artifacts(self):
        with patch.object(build_exe, "missing_packages", return_value=["xlrd"]), \
             patch.object(build_exe, "project_python", return_value=None), \
             patch.object(build_exe, "check", return_value=False), \
             patch.object(build_exe.shutil, "rmtree") as remove:
            self.assertEqual(build_exe.main([]), 1)
            remove.assert_not_called()

    def test_project_environment_must_contain_every_required_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
            candidate.parent.mkdir(parents=True)
            candidate.touch()
            with patch.object(build_exe, "ROOT", root), patch.object(build_exe.subprocess, "run") as run:
                run.return_value = SimpleNamespace(returncode=0, stdout=json.dumps(["defusedxml"]))
                self.assertIsNone(build_exe.project_python())
                run.return_value = SimpleNamespace(returncode=0, stdout="[]")
                self.assertEqual(build_exe.project_python(), candidate)
                run.side_effect = subprocess.TimeoutExpired("probe", 30)
                self.assertIsNone(build_exe.project_python())

    def test_project_python_does_not_relaunch_itself(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
            candidate.parent.mkdir(parents=True)
            candidate.touch()
            with patch.object(build_exe, "ROOT", root), patch.object(build_exe.sys, "executable", str(candidate)), \
                 patch.object(build_exe.subprocess, "run") as run:
                self.assertIsNone(build_exe.project_python())
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
