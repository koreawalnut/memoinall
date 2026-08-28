"""단일 exe 빌드.  python build_exe.py

빌드 전에 왜 이런 구성인지:
  - torch(496MB)/transformers(94MB) 를 빼고 onnxruntime 으로 임베딩을 돌린다.
    두 백엔드는 같은 가중치라 결과 벡터가 완전히 동일하다(테스트로 검증).
  - 임베딩 가중치(약 450MB)는 exe 에 넣지 않는다. 첫 실행 때 받아
    ~/.memoinall/models 에 두므로, exe 를 새로 받아도 다시 내려받지 않는다.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"

REQUIRED = ["webview", "onnxruntime", "tokenizers", "fastapi", "uvicorn", "numpy"]
OPTIONAL = ["anthropic", "openai"]


def check() -> bool:
    import importlib.util

    missing = [m for m in REQUIRED if importlib.util.find_spec(m) is None]
    if missing:
        print("필수 패키지가 없습니다:", ", ".join(missing))
        print("  pip install pywebview onnxruntime tokenizers fastapi uvicorn numpy")
        return False
    for m in OPTIONAL:
        state = "포함" if importlib.util.find_spec(m) else "없음(해당 프로바이더 비활성)"
        print(f"  선택 패키지 {m}: {state}")
    if importlib.util.find_spec("PyInstaller") is None:
        print("PyInstaller 가 없습니다:  pip install pyinstaller")
        return False
    return True


def main() -> int:
    if not check():
        return 1

    for path in (DIST, BUILD):
        if path.exists():
            print(f"이전 결과 삭제: {path}")
            shutil.rmtree(path, ignore_errors=True)

    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(ROOT / "memoinall.spec")]
    print("\n빌드 시작 (수 분 걸립니다)…\n  " + " ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        print("\n빌드 실패")
        return result.returncode

    exe = DIST / "memoinall.exe"
    if not exe.exists():
        print("\nexe 가 생성되지 않았습니다.")
        return 1

    size = exe.stat().st_size / (1024 * 1024)
    print(f"\n완료: {exe}  ({size:.0f} MB)")
    print("첫 실행 때 임베딩 모델 약 450MB 를 내려받습니다(한 번만).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
