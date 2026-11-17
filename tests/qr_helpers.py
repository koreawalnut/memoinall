"""QR 공유 테스트 도구.

- Node 로 실제 qrshare.js 를 돌려 공유 내용을 QR 조각으로 만든다(브라우저와 같은 코드).
- 그 QR 들을 Chromium 가짜 카메라용 영상(.y4m)으로 굽는다. 카메라로 찍는 길을 그대로 시험한다.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "memoinall" / "static"

_NODE = r"""
const fs = require("fs");
const [qrshare, qrcodeLib, input, output, mode] = process.argv.slice(2);
globalThis.qrcode = require(qrcodeLib);
require(qrshare);
(async () => {
  const req = JSON.parse(fs.readFileSync(input, "utf8"));
  const out = {};
  if (req.text != null) out.parts = await QrShare.encode(req.text);
  if (req.parts) out.text = await QrShare.decode(req.parts);
  const parts = out.parts || req.parts || [];
  out.matrices = parts.map((p) => {
    const q = qrcode(0, "L"); q.addData(p, "Alphanumeric"); q.make();
    const n = q.getModuleCount(), rows = [];
    for (let y = 0; y < n; y++) { let r = ""; for (let x = 0; x < n; x++) r += q.isDark(y, x) ? "1" : "0"; rows.push(r); }
    return rows;
  });
  fs.writeFileSync(output, JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


def node_available() -> bool:
    return shutil.which("node") is not None


def run_qrshare(*, text: str | None = None, parts: list[str] | None = None) -> dict:
    """text 를 QR 조각으로(encode) 또는 parts 를 원문으로(decode). QR 모듈 행렬도 같이 준다."""
    with tempfile.TemporaryDirectory(prefix="memoinall-qr-") as tmp:
        tmp = Path(tmp)
        (tmp / "run.js").write_text(_NODE, encoding="utf-8")
        (tmp / "in.json").write_text(json.dumps({"text": text, "parts": parts}, ensure_ascii=False), encoding="utf-8")
        res = subprocess.run(
            ["node", str(tmp / "run.js"), str(STATIC / "qrshare.js"), str(STATIC / "vendor" / "qrcode.js"),
             str(tmp / "in.json"), str(tmp / "out.json")],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        if res.returncode:
            raise RuntimeError(res.stderr)
        return json.loads((tmp / "out.json").read_text(encoding="utf-8"))


def write_camera_video(path: Path, matrices: list[list[str]], *, width=640, height=480, fps=10, frames_each=6) -> None:
    """QR 들을 차례로 보여주는 가짜 카메라 영상. Chromium 은 끝나면 처음부터 다시 돈다."""
    frames = []
    for rows in matrices:
        n = len(rows)
        quiet = 4
        scale = max(2, min(width, height) * 9 // 10 // (n + quiet * 2))
        size = (n + quiet * 2) * scale
        ox, oy = (width - size) // 2, (height - size) // 2
        y_plane = bytearray([200]) * (width * height)   # 회색 배경 — 실제 화면처럼 흰 QR 이 떠 보이게
        for yy in range(size):
            row = (oy + yy) * width + ox
            y_plane[row:row + size] = bytes([255]) * size
        for my, line in enumerate(rows):
            for mx, bit in enumerate(line):
                if bit == "1":
                    x0, y0 = ox + (mx + quiet) * scale, oy + (my + quiet) * scale
                    for dy in range(scale):
                        start = (y0 + dy) * width + x0
                        y_plane[start:start + scale] = bytes(scale)
        frames.append(bytes(y_plane))
    chroma = bytes([128]) * (width * height // 2)
    with open(path, "wb") as f:
        f.write(f"YUV4MPEG2 W{width} H{height} F{fps}:1 Ip A1:1 C420jpeg\n".encode())
        for frame in frames:
            for _ in range(frames_each):
                f.write(b"FRAME\n")
                f.write(frame)
                f.write(chroma)


def camera_args(video: Path) -> list[str]:
    return ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
            f"--use-file-for-fake-video-capture={video}"]
