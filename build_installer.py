"""사용자별 Windows 설치 파일 빌드: python build_installer.py"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

from memoinall import __version__

ROOT = Path(__file__).resolve().parent
APP_DIR = ROOT / "dist" / "memoinall"
OUTPUT_DIR = ROOT / "dist" / "installer"
TOOLS_DIR = ROOT / "build" / "tools"


def find_compiler(explicit: str | None = None) -> Path | None:
    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate.resolve() if candidate.is_file() else None
    candidates = [TOOLS_DIR / "inno" / "ISCC.exe"]
    for supplied in (os.environ.get("ISCC"), shutil.which("ISCC.exe")):
        if supplied:
            candidates.append(Path(supplied))
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs")):
        if base:
            candidates.extend(Path(base) / f"Inno Setup {version}" / "ISCC.exe" for version in (7, 6))
    return next((path.resolve() for path in candidates if path.is_file()), None)


def validate_bundle(folder: Path) -> None:
    required = [folder / "memoinall.exe", folder / "_internal" / "memoinall" / "static" / "index.html"]
    if any(not path.is_file() for path in required):
        raise ValueError("폴더 배포판이 없습니다. 먼저 python build_exe.py --onedir 를 실행하세요.")
    # 이 설치 스크립트는 Windows x64 배포판 전용이다.
    with (folder / "memoinall.exe").open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise ValueError("memoinall.exe가 Windows 실행 파일이 아닙니다.")
        stream.seek(0x3C)
        offset = struct.unpack("<I", stream.read(4))[0]
        stream.seek(offset)
        if stream.read(4) != b"PE\0\0" or stream.read(2) != b"\x64\x86":
            raise ValueError("이 설치 파일에는 Windows x64 실행 파일이 필요합니다.")


def verify_bootstrapper(path: Path) -> bool:
    # 전달받은 파일도 검증한다. 설치 중 실행될 외부 프로그램이므로 Microsoft 서명이 필요하다.
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(ROOT / "installer" / "verify_webview.ps1"), "-Path", str(path.resolve())],
        cwd=ROOT, check=False,
    )
    return result.returncode == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="memoinall 설치 파일 생성 (Windows x64)")
    parser.add_argument("--iscc", help="Inno Setup 6.3 이상 ISCC.exe 경로")
    parser.add_argument("--webview-bootstrapper", type=Path,
                        help="선택: Microsoft 설치 도우미를 포함. 생략하면 필요한 PC에서 내려받습니다")
    parser.add_argument("--skip-app-build", action="store_true", help="이미 검증한 dist/memoinall을 패키징")
    parser.add_argument("--check", action="store_true", help="설치 파일 제작 도구만 확인")
    parser.add_argument("--version", default=__version__, help="설치 파일에 표시할 버전")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:\.\d+)?", args.version):
        parser.error("버전은 0.1.0 형식으로 지정하세요.")
    compiler = find_compiler(args.iscc)
    if compiler is None:
        print("Inno Setup 컴파일러가 없습니다: https://jrsoftware.org/isdl.php")
        print("설치 후 --iscc 경로를 지정하거나 build/tools/inno/ISCC.exe에 준비하세요.")
        return 1
    if args.webview_bootstrapper is not None and not args.webview_bootstrapper.is_file():
        print("Microsoft WebView2 Evergreen Bootstrapper가 없습니다:", args.webview_bootstrapper)
        print("https://developer.microsoft.com/microsoft-edge/webview2/ 에서 받아 경로를 지정하세요.")
        return 1
    if args.webview_bootstrapper is not None and not verify_bootstrapper(args.webview_bootstrapper):
        return 1
    if args.check:
        print("설치 파일 제작 준비 완료:", compiler)
        return 0
    if not args.skip_app_build:
        result = subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "build_exe.py"), "--onedir"], cwd=ROOT)
        if result.returncode:
            return result.returncode
    try:
        validate_bundle(APP_DIR)
    except (ValueError, OSError, struct.error) as exc:
        print("배포판 확인 실패:", exc)
        return 1
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    command = [str(compiler), f"/DAppVersion={args.version}", f"/DAppSource={APP_DIR}", f"/O{OUTPUT_DIR}"]
    if args.webview_bootstrapper is not None:
        command.append(f"/DWebViewBootstrapper={args.webview_bootstrapper.resolve()}")
    command.append(str(ROOT / "installer" / "memoinall.iss"))
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode:
        return result.returncode
    artifact = OUTPUT_DIR / f"memoinall-Setup-{args.version}.exe"
    if not artifact.is_file():
        print("설치 파일이 생성되지 않았습니다:", artifact)
        return 1
    print(f"\n배포 파일: {artifact} ({artifact.stat().st_size / 1048576:.1f} MB)")
    print("이 파일 하나를 전달하세요. 설치 후 시작 메뉴에서 memoinall을 실행합니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
