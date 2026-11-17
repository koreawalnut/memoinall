"""휴대폰 앱(PWA)을 올릴 수 있는 폴더 하나로 모은다.

    python build_mobile.py              # → dist/mobile/
    python build_mobile.py --out 경로

결과 폴더를 HTTPS 정적 호스팅(GitHub Pages·Cloudflare Pages·Netlify 등)에 그대로 올리면 된다.
서버 코드는 없다 — 메모는 각 휴대폰의 브라우저 저장소(IndexedDB)에만 저장된다.

QR 모듈(qrshare.js)과 QR 라이브러리는 PC 앱과 같은 파일을 쓴다. 그래서 mobile/ 에 복사본을 두지 않고
여기서 memoinall/static/ 의 것을 가져온다 — 한쪽만 고쳐서 형식이 어긋나는 일을 막는다.

버전은 파일 내용의 해시다. 내용이 바뀌어야 서비스 워커 캐시가 바뀌고, 휴대폰이 새 버전을 받는다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "mobile"
STATIC = ROOT / "memoinall" / "static"

# 게시 경로 → 원본
FILES = {
    "index.html": SRC / "index.html",
    "app.js": SRC / "app.js",
    "exchange.js": SRC / "exchange.js",
    "manifest.webmanifest": SRC / "manifest.webmanifest",
    "icons/icon-192.png": SRC / "icons" / "icon-192.png",
    "icons/icon-512.png": SRC / "icons" / "icon-512.png",
    "icons/icon-maskable-512.png": SRC / "icons" / "icon-maskable-512.png",
    "icons/apple-touch-icon.png": SRC / "icons" / "apple-touch-icon.png",
    "qrshare.js": STATIC / "qrshare.js",
    # 휴대폰은 QR 을 보여주기만 한다(휴대폰 → PC 한 방향). 찍는 쪽 라이브러리(jsQR)는 PC 에만 있다.
    "vendor/qrcode.js": STATIC / "vendor" / "qrcode.js",
}
# 오프라인에서 열리려면 캐시에 있어야 하는 것. 라이선스 문서는 빼도 된다.
OFFLINE = ["./"] + [p for p in FILES if not p.endswith(".txt")]
FILES_RE = re.compile(r"/\*FILES\*/.*?/\*END\*/", re.S)


# 배포용 저장소 폴더에 바로 빌드해도 되게, git 기록·도메인 설정·설명 파일은 남긴다.
KEEP = {".git", ".github", "CNAME", "README.md"}


def _clear(out: Path) -> None:
    """이전 빌드를 지운다. 이 앱이 만든 폴더가 아니면 손대지 않는다 — 잘못 준 경로를 통째로 지우지 않게."""
    out = out.resolve()
    for guarded in (ROOT, SRC, STATIC):
        if out == guarded or guarded.is_relative_to(out) or out.is_relative_to(SRC) or out.is_relative_to(STATIC):
            raise SystemExit(f"소스 폴더에는 빌드할 수 없습니다: {out}")
    if not out.exists():
        return
    if not out.is_dir():
        raise SystemExit(f"폴더가 아닙니다: {out}")
    others = [p for p in out.iterdir() if p.name not in KEEP]
    if not others:
        return
    sw = out / "sw.js"
    if not (sw.is_file() and "memoinall-" in sw.read_text(encoding="utf-8", errors="ignore")):
        raise SystemExit(f"{out} 은 비어 있지 않고 이전 휴대폰 앱 빌드도 아닙니다 — 빈 폴더나 이전 빌드 폴더를 지정하세요.")
    for p in others:
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()


def build(out: Path) -> dict:
    missing = [str(src) for src in FILES.values() if not src.is_file()]
    if missing:
        raise SystemExit("빠진 파일이 있습니다:\n  " + "\n  ".join(missing))

    digest = hashlib.sha256()
    for path, src in sorted(FILES.items()):
        digest.update(path.encode() + b"\0" + src.read_bytes() + b"\0")
    sw_src = (SRC / "sw.js").read_text(encoding="utf-8")
    digest.update(sw_src.encode())
    version = digest.hexdigest()[:10]

    _clear(out)
    for path, src in FILES.items():
        target = out / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if path == "app.js":
            target.write_text(src.read_text(encoding="utf-8").replace("__BUILD__", version), encoding="utf-8")
        else:
            shutil.copyfile(src, target)

    if not FILES_RE.search(sw_src) or "__BUILD__" not in sw_src:
        raise SystemExit("mobile/sw.js 에서 버전·파일 목록 자리를 찾지 못했습니다.")
    sw = FILES_RE.sub(lambda _m: json.dumps(OFFLINE, ensure_ascii=False), sw_src).replace("__BUILD__", version)
    (out / "sw.js").write_text(sw, encoding="utf-8")
    # GitHub Pages 가 밑줄로 시작하는 파일을 숨기지 않게
    (out / ".nojekyll").write_text("", encoding="utf-8")

    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    return {"out": out, "version": version, "files": len(FILES) + 1, "bytes": size}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="휴대폰 앱(PWA) 폴더 만들기")
    parser.add_argument("--out", default=str(ROOT / "dist" / "mobile"), help="결과 폴더 (기본: dist/mobile)")
    args = parser.parse_args(argv)
    r = build(Path(args.out).resolve())
    print(f"휴대폰 앱을 만들었습니다: {r['out']}")
    print(f"  버전 {r['version']} · 파일 {r['files']}개 · {r['bytes'] / 1024:.0f}KB")
    print("  이 폴더를 HTTPS 정적 호스팅에 그대로 올리세요. 자세한 방법은 docs/mobile.md 를 보세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
