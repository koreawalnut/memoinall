"""휴대폰 앱의 QR·공유 형식이 PC 와 맞는지, 빌드 결과가 온전한지.  python tests/test_mobile.py

브라우저 코드(qrshare.js·mobile/exchange.js)를 Node 로 직접 돌려 PC(Python) 결과와 맞춰 본다.
Node 가 없으면 빌드 검사만 하고 나머지는 건너뛴다.
"""
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_mobile  # noqa: E402
from memoinall import exchange, textutil  # noqa: E402
from memoinall.importers import clean  # noqa: E402
from qr_helpers import node_available  # noqa: E402

FAILS = 0


def check(ok, name):
    global FAILS
    print(("PASS " if ok else "FAIL ") + name)
    FAILS += 0 if ok else 1


def node(script: str, *args: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="memoinall-node-") as tmp:
        path = Path(tmp) / "t.js"
        path.write_text(script, encoding="utf-8")
        res = subprocess.run(["node", str(path), str(ROOT), *args], capture_output=True, text=True, encoding="utf-8", timeout=120)
        if res.returncode:
            raise RuntimeError(res.stderr)
        return json.loads(res.stdout)


QR_JS = r"""
const path = require("path");
const root = process.argv[2];
globalThis.qrcode = require(path.join(root, "memoinall/static/vendor/qrcode.js"));
const jsQR = require(path.join(root, "memoinall/static/vendor/jsQR.js"));
require(path.join(root, "memoinall/static/qrshare.js"));
const Q = globalThis.QrShare;
let seed = 7;
const rand = (n) => { seed = (seed * 1103515245 + 12345) % 2147483648; return seed % n; };
const words = "내일 오후 결제 시스템 점검 회의 보고서 금요일 서버 로그 확인 김과장 고객사 요청 배포 일정 변경 검토 예산 승인 견적서 계약 담당자 연락 회신 대기 자료 정리 공유 장애 원인 분석 재발 방지 대책 테스트 환경 데이터 이관 백업 복구 결재 출장 정산 교육 신청 마감 면담 회의실 예약".split(" ");
const sentence = (n) => Array.from({ length: n }, () => words[rand(words.length)]).join(" ") + ". ";
const memo = (i, n) => ({ uid: "u" + i, body: Array.from({ length: n }, () => sentence(8 + rand(8))).join(""), created_at: "2026-11-17T10:00:00", tags: ["회의"] });
function read(text) {
  const q = qrcode(0, "L"); q.addData(text, "Alphanumeric"); q.make();
  const n = q.getModuleCount(), s = 4, size = (n + 8) * s, data = new Uint8ClampedArray(size * size * 4).fill(255);
  for (let y = 0; y < n; y++) for (let x = 0; x < n; x++) if (q.isDark(y, x))
    for (let dy = 0; dy < s; dy++) for (let dx = 0; dx < s; dx++) { const i = (((y + 4) * s + dy) * size + (x + 4) * s + dx) * 4; data[i] = data[i + 1] = data[i + 2] = 0; }
  const hit = jsQR(data, size, size);
  return { text: hit && hit.data, version: (n - 17) / 4 };
}
(async () => {
  const out = {};
  const enc = (s) => Q._b45.encode(new TextEncoder().encode(s));
  out.rfc = [enc("AB"), enc("Hello!!"), enc("base-45"), new TextDecoder().decode(Q._b45.decode("QED8WEX0"))];
  try { Q._b45.decode("GGW"); out.badTriple = false; } catch (e) { out.badTriple = true; }
  const small = JSON.stringify({ format: "memoinall/memos", version: 1, count: 1, memos: [memo(1, 2)] }, null, 2);
  const one = await Q.encode(small);
  out.small = { parts: one.length, prefix: /^MI1:[0-9A-Z]{4}:1:1:/.test(one[0]), back: (await Q.decode(one)) === JSON.stringify(JSON.parse(small)) };
  const big = JSON.stringify({ format: "memoinall/memos", version: 1, count: 30, memos: Array.from({ length: 30 }, (_, i) => memo(i, 3 + (i % 5))) });
  const parts = await Q.encode(big);
  const c = new Q.Collector();
  [...parts].reverse().forEach((p) => c.add(p)); c.add(parts[0]);
  out.big = { parts: parts.length, back: c.complete && (await c.text()) === big };
  const reads = parts.map(read);
  out.reads = reads.every((r, i) => r.text === parts[i]);
  out.maxVersion = Math.max(...reads.map((r) => r.version));
  const c2 = new Q.Collector(); c2.add(parts[0]);
  out.switched = c2.add(one[0]).status === "switched" && c2.complete && (await c2.text()) === JSON.stringify(JSON.parse(small));
  out.foreign = new Q.Collector().add("https://example.com").status;
  let fit = 0, body = "";
  while (true) {
    const next = body + sentence(10);
    if ((await Q.encode(JSON.stringify({ format: "memoinall/memos", version: 1, count: 1, memos: [{ ...memo(1, 0), body: next }] }))).length > 1) break;
    body = next; fit = next.length;
  }
  out.fit = fit;
  try {
    await Q.encode(Array.from({ length: 40000 }, () => String.fromCharCode(0xac00 + Math.floor(Math.random() * 11000))).join(""));
    out.tooBig = "";
  } catch (e) { out.tooBig = e instanceof Q.QrTooLarge ? e.message : "wrong error"; }
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e.stack || e); process.exit(1); });
"""

XCHK_JS = r"""
const fs = require("fs"), path = require("path");
const [root, work, pw] = process.argv.slice(2);
require(path.join(root, "mobile/exchange.js"));
const X = globalThis.Exchange;
(async () => {
  const cases = JSON.parse(fs.readFileSync(path.join(work, "cases.json"), "utf8"));
  const out = { uids: [], titles: [], cleans: [] };
  for (const c of cases) { out.uids.push(await X.contentUid(c.created_at, c.body)); out.titles.push(X.titleFrom(c.body)); out.cleans.push(X.clean(c.body)); }
  const sealed = fs.readFileSync(path.join(work, "from_pc.json"), "utf8");
  const t0 = Date.now();
  out.received = (await X.parse(sealed, pw.normalize("NFD"))).memos.map((m) => m.body);
  out.unsealMs = Date.now() - t0;
  try { await X.parse(sealed, "틀린 비번"); } catch (e) { out.wrong = e.message; }
  try { await X.parse(sealed, ""); } catch (e) { out.need = e instanceof X.PasswordRequired; }
  const mine = await X.build([{ body: "휴대폰에서 쓴 메모\n둘째 줄", created_at: "2026-11-17T08:00:00", tags: ["현장"] }],
    { schedules: [{ title: "점검", date: "2026-11-20", time: "15:00", tags: [], done: false, created_at: "2026-11-17T08:00:00" }], backup: true });
  const toPc = await X.build([{ body: "PC로 보낼 메모", created_at: "2026-11-17T08:00:00", tags: [] }]);
  out.backupMarks = [X.isBackup(mine), X.isBackup(toPc), X.isBackup(await X.parse(sealed, pw))];
  fs.writeFileSync(path.join(work, "from_phone.json"), JSON.stringify(await X.seal(mine, pw), null, 2));
  fs.writeFileSync(path.join(work, "from_phone_plain.json"), JSON.stringify(mine, null, 2));
  const rfc = await X.scrypt(new TextEncoder().encode("password"), new TextEncoder().encode("NaCl"), 1024, 8, 16, 64);
  out.rfc = Array.from(rfc, (b) => b.toString(16).padStart(2, "0")).join("");
  try { await X.parse(JSON.stringify({ format: "memoinall/sealed", version: 1, kdf: { name: "scrypt", n: 2 ** 22, r: 8, p: 1, salt: "AAAA" }, cipher: "AES-256-GCM", nonce: "AAAA", data: "AAAA" }), "x"); }
  catch (e) { out.costly = e.message; }
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e.stack || e); process.exit(1); });
"""


def test_build():
    with tempfile.TemporaryDirectory(prefix="memoinall-mobile-build-") as tmp:
        out = Path(tmp) / "site"
        r = build_mobile.build(out)
        files = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
        check({"index.html", "app.js", "sw.js", "manifest.webmanifest", "qrshare.js", "vendor/qrcode.js", "icons/icon-512.png"} <= files,
              "build folder has the page, scripts, service worker, manifest, QR modules and icons")
        check(not any("jsQR" in f for f in files), "phone app ships no QR reader — it can only show QRs to the PC (one way)")
        check(re.fullmatch(r"[0-9a-f]{10}", r["version"]) is not None, "version is a content hash")
        app_js = (out / "app.js").read_text(encoding="utf-8")
        sw = (out / "sw.js").read_text(encoding="utf-8")
        check("__BUILD__" not in app_js + sw and f'"{r["version"]}"' in app_js and f'"{r["version"]}"' in sw,
              "version is stamped into the app and the service worker")
        offline = json.loads(re.search(r"const FILES = (\[.*?\]);", sw).group(1))
        check(offline[0] == "./" and all(p in files for p in offline[1:]) and "app.js" in offline,
              "every file the service worker caches for offline use exists")
        check((out / "qrshare.js").read_bytes() == (ROOT / "memoinall/static/qrshare.js").read_bytes(),
              "phone and PC use the very same QR module")
        manifest = json.loads((out / "manifest.webmanifest").read_text(encoding="utf-8"))
        check(manifest["display"] == "standalone" and all((out / i["src"]).is_file() for i in manifest["icons"])
              and any(i.get("purpose") == "maskable" for i in manifest["icons"]), "manifest makes it installable with real icons")
        html = (out / "index.html").read_text(encoding="utf-8")
        refs = re.findall(r'(?:src|href)="([^"#:]+)"', html)
        check(all((out / ref).is_file() for ref in refs), "everything the page links to is in the build")
        check(not re.search(r'(?:src|href)="(?:https?:)?//', html), "page loads nothing from other sites")
        again = build_mobile.build(Path(tmp) / "again")
        check(again["version"] == r["version"], "same sources give the same version (no needless re-download on phones)")

        # 배포 저장소 폴더에 다시 빌드: git 기록·README 는 남고, 지난 빌드의 남은 파일은 지운다
        (out / ".git").mkdir()
        (out / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (out / "README.md").write_text("배포용", encoding="utf-8")
        (out / "old-leftover.js").write_text("", encoding="utf-8")
        build_mobile.build(out)
        check((out / ".git" / "HEAD").is_file() and (out / "README.md").is_file() and not (out / "old-leftover.js").exists(),
              "rebuilding into the deploy repo keeps .git and README and drops stale files")
        stranger = Path(tmp) / "stranger"
        stranger.mkdir()
        (stranger / "important.docx").write_text("x", encoding="utf-8")
        refused = []
        for target in (stranger, ROOT, ROOT / "mobile", ROOT / "memoinall" / "static" / "vendor"):
            try:
                build_mobile.build(target)
                refused.append(False)
            except SystemExit:
                refused.append(True)
        check(all(refused) and (stranger / "important.docx").is_file() and (ROOT / "mobile" / "app.js").is_file(),
              "a non-empty folder that isn't a previous build, or the source folders, are never wiped")


def test_qr():
    out = node(QR_JS)
    check(out["rfc"] == ["BB8", "%69 VD92EX0", "UJCLQE7W581", "ietf!"], "Base45 matches the RFC 9285 examples")
    check(out["badTriple"], "Base45 rejects an out-of-range group")
    check(out["small"] == {"parts": 1, "prefix": True, "back": True}, "a short memo is one QR and decodes to the same JSON")
    check(out["big"]["parts"] > 1 and out["big"]["back"], f"30 memos split into {out['big']['parts']} QR codes and rebuild in any order")
    check(out["reads"], "every QR image reads back exactly with jsQR")
    check(out["maxVersion"] <= 18, f"QR codes stay small enough to scan off a screen (version {out['maxVersion']})")
    check(out["switched"], "a QR from a new share replaces a half-collected one")
    check(out["foreign"] == "foreign", "unrelated QR codes are ignored")
    check(out["fit"] >= 300, f"one QR holds about {out['fit']} characters of Korean memo text")
    check("나눠서" in out["tooBig"], "very large shares are refused with a 'send fewer memos at a time' message")


def test_exchange_matches_pc():
    bodies = [
        "내일 오후 3시 회의\r\n자료 준비  \r\n\r\n\r\n\r\n끝",
        "  - [ ] 보고서 보내기\t \n#태그 테스트​\x07  ",
        "# 제목입니다 아주 길게 " + "가" * 80,
        "",
        "😀 이모지와 \U0001F680 그리고 NFD 한글 한",
    ]
    cases = [{"created_at": "2026-11-17T10:00:00", "body": b} for b in bodies]
    pack = {"format": exchange.FORMAT, "version": 1, "count": 2, "note": "", "memos": [
        {"uid": "abc", "body": "PC에서 보낸 메모 1", "created_at": "2026-11-01T09:00:00", "tags": ["회의"]},
        {"uid": "def", "body": "두번째 #결제", "created_at": "2026-11-02T09:00:00", "tags": []}]}
    pw = "한글 비번 123"
    with tempfile.TemporaryDirectory(prefix="memoinall-xchk-") as tmp:
        work = Path(tmp)
        (work / "cases.json").write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
        (work / "from_pc.json").write_text(exchange.dumps(exchange.seal(pack, pw)), encoding="utf-8")
        out = node(XCHK_JS, str(work), pw)
        check(out["cleans"] == [clean(c["body"]) for c in cases], "text clean-up matches the PC (CRLF, blank lines, zero-width, control chars)")
        check(out["uids"] == [exchange.content_uid(c["created_at"], c["body"]) for c in cases],
              "memo ids match the PC, so the same memo is never imported twice")
        check(out["titles"] == [textutil.title_from(c["body"]) for c in cases], "titles match the PC")
        check(out["received"] == ["PC에서 보낸 메모 1", "두번째 #결제"], "phone opens a PC file locked with a Korean password (even NFD input)")
        check("비밀번호가 맞지 않" in out.get("wrong", ""), "wrong password gets a clear message")
        check(out.get("need") is True, "missing password asks for one")
        check("손상" in out.get("costly", ""), "a file demanding absurd scrypt cost is refused instead of freezing the phone")
        expect = hashlib.scrypt(b"password", salt=b"NaCl", n=1024, r=8, p=16, dklen=64).hex()
        check(out["rfc"] == expect, "scrypt matches the RFC 7914 test vector and Python")
        print(f"INFO unlocking took {out['unsealMs']} ms in Node")
        got = exchange.parse((work / "from_phone.json").read_text(encoding="utf-8"), pw)
        check(got["memos"][0]["body"] == "휴대폰에서 쓴 메모\n둘째 줄" and got["memos"][0]["tags"] == ["현장"],
              "PC opens a file locked on the phone")
        check(got["memos"][0]["uid"] == exchange.content_uid("2026-11-17T08:00:00", "휴대폰에서 쓴 메모\n둘째 줄"),
              "id written by the phone is the PC's id")
        check(got.get("schedules", [{}])[0].get("title") == "점검", "phone backup carries schedules in the same file")
        check(got.get("backup") == "memoinall/mobile-backup" and out["backupMarks"] == [True, False, False],
              "only the phone's own backups carry the backup mark; memos sent to the PC and PC files do not")
        check(exchange.parse((work / "from_phone_plain.json").read_text(encoding="utf-8"))["count"] == 1,
              "PC reads the phone's unlocked file")


def main():
    test_build()
    if node_available():
        test_qr()
        test_exchange_matches_pc()
    else:
        print("SKIP node 가 없어 브라우저 코드 비교를 건너뜁니다")
    print("ALL PASS" if not FAILS else f"{FAILS} FAILED")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
