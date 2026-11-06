"""단일 exe 빌드.  python build_exe.py

빌드 전에 왜 이런 구성인지:
  - torch(496MB)/transformers(94MB) 를 빼고 onnxruntime 으로 임베딩을 돌린다.
    두 백엔드는 같은 가중치라 결과 벡터가 완전히 동일하다(테스트로 검증).
  - 임베딩 가중치(약 450MB)는 exe 에 넣지 않는다. 첫 실행 때 받아
    ~/.memoinall/models 에 두므로, exe 를 새로 받아도 다시 내려받지 않는다.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"

REQUIRED = ["webview", "onnxruntime", "tokenizers", "fastapi", "uvicorn", "numpy",
            "pypdf", "openpyxl", "xlrd", "olefile", "defusedxml"]
OPTIONAL = ["anthropic", "openai"]


def missing_packages() -> list[str]:
    return [m for m in REQUIRED + ["PyInstaller"] if importlib.util.find_spec(m) is None]


def project_python() -> Path | None:
    candidate = ROOT / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not candidate.is_file() or candidate.resolve() == Path(sys.executable).resolve():
        return None
    probe = (
        "import importlib.util,json; "
        f"modules={json.dumps(REQUIRED + ['PyInstaller'])}; "
        "print(json.dumps([m for m in modules if importlib.util.find_spec(m) is None]))"
    )
    try:
        result = subprocess.run([str(candidate), "-X", "utf8", "-c", probe], cwd=ROOT,
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        if result.returncode == 0 and json.loads(result.stdout) == []:
            return candidate
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return None


def check() -> bool:
    missing = [m for m in REQUIRED if importlib.util.find_spec(m) is None]
    if missing:
        print("현재 Python:", sys.executable)
        print("필수 패키지가 없습니다:", ", ".join(missing))
        print("  python -X utf8 -m pip install pywebview onnxruntime tokenizers fastapi uvicorn numpy -r requirements-documents.txt")
        return False
    for m in OPTIONAL:
        state = "포함" if importlib.util.find_spec(m) else "없음(해당 프로바이더 비활성)"
        print(f"  선택 패키지 {m}: {state}")
    if importlib.util.find_spec("PyInstaller") is None:
        print("PyInstaller 가 없습니다:  python -m pip install pyinstaller")
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="memoinall 단일 실행 파일 빌드")
    parser.add_argument("--check", action="store_true", help="빌드 환경만 확인하고 파일을 만들지 않습니다")
    parser.add_argument("--onedir", action="store_true", help="실행 시 압축 해제가 없는 폴더 배포판을 만듭니다")
    arguments = sys.argv[1:] if argv is None else argv
    args = parser.parse_args(arguments)
    if missing_packages():
        candidate = project_python()
        if candidate is not None:
            print("필수 패키지가 준비된 프로젝트 가상환경으로 전환합니다:", candidate, flush=True)
            return subprocess.run([str(candidate), "-X", "utf8", str(ROOT / "build_exe.py"), *arguments], cwd=ROOT).returncode
    if not check():
        return 1
    if args.check:
        print("빌드 준비 완료:", sys.executable)
        return 0

    output = DIST / ("memoinall" if args.onedir else "memoinall.exe")
    work = BUILD / ("onedir" if args.onedir else "onefile")
    # 다른 배포 형식은 남겨두고 이번 빌드 대상만 정리한다.
    if (any(path.resolve().parent != ROOT.resolve() for path in (DIST, BUILD))
            or output.resolve().parent != DIST.resolve()
            or work.resolve().parent != BUILD.resolve()):
        print("빌드 출력 경로가 프로젝트 밖을 가리킵니다. dist/build 경로를 확인하세요.")
        return 1
    for path in (output, work):
        if path.exists():
            print(f"이전 결과 삭제: {path}")
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()

    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--distpath", str(DIST), "--workpath", str(work), str(ROOT / "memoinall.spec")]
    build_env = os.environ.copy()
    build_env["MEMOINALL_BUILD_ONEDIR"] = "1" if args.onedir else "0"
    print("\n빌드 시작 (수 분 걸립니다)…\n  " + " ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT, env=build_env)
    if result.returncode != 0:
        print("\n빌드 실패")
        return result.returncode

    exe = output / "memoinall.exe" if args.onedir else output
    if not exe.exists():
        print("\nexe 가 생성되지 않았습니다.")
        return 1

    size = exe.stat().st_size / (1024 * 1024)
    print(f"\n완료: {exe}  ({size:.0f} MB)")
    if args.onedir:
        print("배포할 때는 dist/memoinall 폴더 전체를 전달하세요. exe 파일만 옮기면 실행되지 않습니다.")
    print("첫 실행 때 임베딩 모델 약 450MB 를 내려받습니다(한 번만).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
