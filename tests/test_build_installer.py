"""설치 파일 빌드 검증: python tests/test_build_installer.py"""
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build_installer


def make_bundle(folder, machine=0x8664):
    folder.mkdir(parents=True, exist_ok=True)
    executable = bytearray(256)
    executable[:2] = b"MZ"
    struct.pack_into("<I", executable, 0x3C, 128)
    executable[128:132] = b"PE\0\0"
    struct.pack_into("<H", executable, 132, machine)
    (folder / "memoinall.exe").write_bytes(executable)
    page = folder / "_internal" / "memoinall" / "static" / "index.html"
    page.parent.mkdir(parents=True)
    page.write_text("memo editor", encoding="utf-8")


class InstallerBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.compiler = self.root / "ISCC.exe"
        self.compiler.touch()
        self.bootstrapper = self.root / "WebViewSetup.exe"
        self.bootstrapper.touch()
        self.options = ["--iscc", str(self.compiler), "--webview-bootstrapper", str(self.bootstrapper)]

    def test_explicit_missing_compiler_does_not_fall_back(self):
        self.assertIsNone(build_installer.find_compiler(str(self.root / "missing.exe")))

    def test_missing_or_wrong_architecture_bundle_is_rejected(self):
        folder = self.root / "app"
        with self.assertRaises(ValueError):
            build_installer.validate_bundle(folder)
        make_bundle(folder, machine=0x14C)
        with self.assertRaisesRegex(ValueError, "x64"):
            build_installer.validate_bundle(folder)
        executable = folder / "memoinall.exe"
        raw = bytearray(executable.read_bytes())
        struct.pack_into("<H", raw, 132, 0x8664)
        executable.write_bytes(raw)
        build_installer.validate_bundle(folder)

    def test_unverified_prerequisite_stops_before_building(self):
        with patch.object(build_installer, "verify_bootstrapper", return_value=False), \
             patch.object(build_installer.subprocess, "run") as run:
            self.assertEqual(build_installer.main(self.options), 1)
            run.assert_not_called()

    def test_check_only_does_not_build_or_install(self):
        with patch.object(build_installer, "verify_bootstrapper", return_value=True), \
             patch.object(build_installer.subprocess, "run") as run:
            self.assertEqual(build_installer.main([*self.options, "--check"]), 0)
            run.assert_not_called()

    def test_app_build_failure_is_not_packaged(self):
        with patch.object(build_installer, "verify_bootstrapper", return_value=True), \
             patch.object(build_installer.subprocess, "run", return_value=SimpleNamespace(returncode=9)) as run:
            self.assertEqual(build_installer.main(self.options), 9)
            self.assertEqual(run.call_count, 1)
            self.assertIn("--onedir", run.call_args.args[0])

    def test_packages_existing_bundle_without_installing_it(self):
        folder, output = self.root / "app", self.root / "output"
        make_bundle(folder)

        def compile_setup(command, **kwargs):
            self.assertEqual(command[0], str(self.compiler.resolve()))
            self.assertIn("/DAppVersion=1.2.3", command)
            self.assertIn(f"/DAppSource={folder}", command)
            (output / "memoinall-Setup-1.2.3.exe").write_bytes(b"installer")
            return SimpleNamespace(returncode=0)

        with patch.object(build_installer, "APP_DIR", folder), \
             patch.object(build_installer, "OUTPUT_DIR", output), \
             patch.object(build_installer, "verify_bootstrapper", return_value=True), \
             patch.object(build_installer.subprocess, "run", side_effect=compile_setup) as run:
            self.assertEqual(build_installer.main([*self.options, "--skip-app-build", "--version", "1.2.3"]), 0)
            self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
