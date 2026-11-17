/* memoinall QR 공유 — PC 앱과 휴대폰 앱이 같은 파일을 쓴다.
 *
 * 방향은 휴대폰 → PC 하나뿐이다(사내 정보 보호). 휴대폰 앱은 show(보여주기)만,
 * PC 앱은 scan(웹캠으로 찍기)만 쓰고, 각자 필요한 라이브러리만 싣는다.
 *
 * 주고받는 내용은 기존 공유 파일(.json, 암호화 봉투 포함) 그대로다. 그걸
 *   JSON 공백 제거 → deflate-raw 압축 → Base45(RFC 9285) → QR 영숫자 모드
 * 로 담는다. 영숫자 모드는 바이트 모드보다 칸이 덜 들고, 어떤 리더기도 글자를
 * 바꾸지 않는다(바이너리는 리더기마다 문자 인코딩 추측이 달라 깨진다).
 *
 * 한 장에 안 들어가면 여러 장으로 나눈다:  MI1:<묶음 id>:<번호>:<전체>:<조각>
 * 보내는 쪽은 장을 자동으로 넘기고, 받는 쪽은 순서와 상관없이 모은다.
 *
 * 필요한 것: qrcode-generator(전역 qrcode) — 보여줄 때만.
 *            jsQR — BarcodeDetector 가 없는 브라우저에서 찍을 때만, 필요할 때 불러온다.
 */
(function (root) {
  "use strict";

  const PREFIX = "MI1";
  const B45 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:";
  // 영숫자 1,000자 = 압축된 666바이트. 오류 정정 L 로 버전 18(89칸) — 모니터에 띄운
  // QR을 휴대폰으로 찍기 편한 크기다. 이보다 키우면 칸이 잘아져 초점 잡기가 어렵다.
  const PART_CHARS = 1000;
  const MAX_PARTS = 40;
  const FLAG_PLAIN = 0, FLAG_DEFLATE = 1;

  const here = root.document && root.document.currentScript ? root.document.currentScript.src : "";
  const config = {
    // 이 파일 옆의 vendor/jsQR.js — PC는 /static/vendor/, 휴대폰 앱은 ./vendor/
    jsqrUrl: here ? new URL("vendor/jsQR.js", here).href : "vendor/jsQR.js",
    interval: 1100,
  };

  class QrError extends Error {}
  class QrTooLarge extends QrError {
    constructor(parts) {
      super(`QR ${parts}장이 필요합니다 — 한 번에 ${MAX_PARTS}장까지라 메모를 나눠서 보내주세요.`);
      this.parts = parts;
    }
  }

  // ------------------------------------------------------------- Base45 (RFC 9285)
  function b45encode(bytes) {
    let out = "";
    for (let i = 0; i + 1 < bytes.length; i += 2) {
      let n = bytes[i] * 256 + bytes[i + 1];
      const c = n % 45; n = (n - c) / 45;
      const d = n % 45; const e = (n - d) / 45;
      out += B45[c] + B45[d] + B45[e];
    }
    if (bytes.length % 2) {
      const n = bytes[bytes.length - 1];
      out += B45[n % 45] + B45[Math.floor(n / 45)];
    }
    return out;
  }

  function b45decode(text) {
    if (text.length % 3 === 1) throw new QrError("QR 내용이 손상됐습니다.");
    const out = new Uint8Array(Math.floor(text.length / 3) * 2 + (text.length % 3 ? 1 : 0));
    let o = 0;
    for (let i = 0; i < text.length; i += 3) {
      const c = B45.indexOf(text[i]), d = B45.indexOf(text[i + 1]);
      const last = i + 2 >= text.length;
      const e = last ? 0 : B45.indexOf(text[i + 2]);
      if (c < 0 || d < 0 || e < 0) throw new QrError("QR 내용이 손상됐습니다.");
      const n = c + d * 45 + e * 2025;
      if (last) {
        if (n > 0xff) throw new QrError("QR 내용이 손상됐습니다.");
        out[o++] = n;
      } else {
        if (n > 0xffff) throw new QrError("QR 내용이 손상됐습니다.");
        out[o++] = n >> 8; out[o++] = n & 0xff;
      }
    }
    return out;
  }

  // ------------------------------------------------------------- 압축
  async function through(bytes, stream) {
    const piped = new Blob([bytes]).stream().pipeThrough(stream);
    return new Uint8Array(await new Response(piped).arrayBuffer());
  }

  async function pack(text) {
    let compact = text;
    try { compact = JSON.stringify(JSON.parse(text)); } catch (_) { /* JSON 이 아니면 그대로 */ }
    const raw = new TextEncoder().encode(compact);
    let flag = FLAG_PLAIN, body = raw;
    if (typeof CompressionStream === "function") {
      const zipped = await through(raw, new CompressionStream("deflate-raw"));
      if (zipped.length < raw.length) { flag = FLAG_DEFLATE; body = zipped; }
    }
    const out = new Uint8Array(body.length + 1);
    out[0] = flag;
    out.set(body, 1);
    return out;
  }

  async function unpack(bytes) {
    const flag = bytes[0], body = bytes.subarray(1);
    let raw;
    if (flag === FLAG_PLAIN) raw = body;
    else if (flag === FLAG_DEFLATE) {
      if (typeof DecompressionStream !== "function") {
        throw new QrError("이 브라우저는 압축된 QR을 풀지 못합니다 — 브라우저를 최신으로 올려주세요.");
      }
      try { raw = await through(body, new DecompressionStream("deflate-raw")); }
      catch (_) { throw new QrError("QR 내용이 손상됐습니다 — 처음부터 다시 찍어주세요."); }
    } else throw new QrError("더 새로운 형식의 QR입니다 — 앱을 최신으로 올려주세요.");
    try { return new TextDecoder("utf-8", { fatal: true }).decode(raw); }
    catch (_) { throw new QrError("QR 내용이 손상됐습니다 — 처음부터 다시 찍어주세요."); }
  }

  // ------------------------------------------------------------- 나누기·모으기
  async function encode(text) {
    const data = b45encode(await pack(text));
    const total = Math.max(1, Math.ceil(data.length / PART_CHARS));
    if (total > MAX_PARTS) throw new QrTooLarge(total);
    const id = Array.from(crypto.getRandomValues(new Uint8Array(4)), (b) => B45[b % 36]).join("");
    return Array.from({ length: total }, (_, i) =>
      `${PREFIX}:${id}:${i + 1}:${total}:${data.slice(i * PART_CHARS, (i + 1) * PART_CHARS)}`);
  }

  const PART_RE = /^MI1:([0-9A-Z]{4}):(\d{1,2}):(\d{1,2}):([0-9A-Z $%*+\-./:]*)$/;

  function parsePart(text) {
    const m = PART_RE.exec(String(text || ""));
    if (!m) return null;
    const index = +m[2], total = +m[3];
    if (!(total >= 1 && total <= MAX_PARTS && index >= 1 && index <= total)) return null;
    return { id: m[1], index, total, data: m[4] };
  }

  // 받은 장을 모은다. 다른 묶음의 QR이 들어오면 새로 보내기 시작한 것으로 보고 갈아탄다.
  class Collector {
    constructor() { this.reset(); }
    reset() { this.id = null; this.total = 0; this.parts = new Map(); }
    add(text) {
      const part = parsePart(text);
      if (!part) return { status: "foreign", got: this.parts.size, total: this.total };
      let status = "new";
      if (part.id !== this.id || part.total !== this.total) {
        if (this.id) status = "switched";
        this.id = part.id; this.total = part.total; this.parts = new Map();
      } else if (this.parts.has(part.index)) status = "dup";
      this.parts.set(part.index, part.data);
      return { status, index: part.index, got: this.parts.size, total: this.total };
    }
    get complete() { return this.total > 0 && this.parts.size === this.total; }
    has(index) { return this.parts.has(index); }
    async text() {
      if (!this.complete) throw new QrError("아직 다 받지 못했습니다.");
      let data = "";
      for (let i = 1; i <= this.total; i++) data += this.parts.get(i);
      return unpack(b45decode(data));
    }
  }

  async function decode(parts) {
    const c = new Collector();
    parts.forEach((p) => c.add(p));
    return c.text();
  }

  // ------------------------------------------------------------- 화면
  let styled = false;
  function style() {
    if (styled || !root.document) return;
    styled = true;
    const css = `
      .qrs { display: grid; gap: 10px; justify-items: center; }
      .qrs-code { width: min(100%, 360px); aspect-ratio: 1; image-rendering: pixelated; background: #fff;
        border-radius: 8px; display: block; }
      .qrs-bar { display: flex; gap: 8px; align-items: center; justify-content: center; flex-wrap: wrap; }
      .qrs-bar button { min-width: 44px; min-height: 36px; }
      .qrs-count { font-variant-numeric: tabular-nums; min-width: 4.5em; text-align: center; }
      .qrs-video { width: min(100%, 420px); aspect-ratio: 4 / 3; object-fit: cover; background: #000;
        border-radius: 10px; display: block; }
      .qrs-frame { position: relative; width: min(100%, 420px); }
      .qrs-frame::after { content: ""; position: absolute; inset: 14%; border: 2px solid rgba(255,255,255,.75);
        border-radius: 12px; box-shadow: 0 0 0 999px rgba(0,0,0,.18); pointer-events: none; }
      .qrs-cells { display: flex; gap: 4px; flex-wrap: wrap; justify-content: center; max-width: 420px; }
      .qrs-cell { width: 14px; height: 14px; border-radius: 3px; border: 1px solid currentColor; opacity: .35; }
      .qrs-cell.on { background: currentColor; opacity: .9; }
      .qrs-status { text-align: center; min-height: 1.6em; }
    `;
    const el = root.document.createElement("style");
    el.textContent = css;
    root.document.head.appendChild(el);
  }

  function drawCode(canvas, text) {
    if (typeof root.qrcode !== "function") throw new QrError("QR을 그리는 모듈을 불러오지 못했습니다.");
    const q = root.qrcode(0, "L");
    q.addData(text, "Alphanumeric");
    q.make();
    const count = q.getModuleCount(), quiet = 4, size = count + quiet * 2;
    const scale = Math.max(2, Math.floor(720 / size));
    canvas.width = canvas.height = size * scale;
    const g = canvas.getContext("2d");
    g.fillStyle = "#fff";
    g.fillRect(0, 0, canvas.width, canvas.height);
    g.fillStyle = "#000";
    for (let y = 0; y < count; y++) {
      for (let x = 0; x < count; x++) {
        if (q.isDark(y, x)) g.fillRect((x + quiet) * scale, (y + quiet) * scale, scale, scale);
      }
    }
  }

  // QR을 보여준다. 여러 장이면 자동으로 넘기고, 멈추거나 직접 넘길 수 있다.
  function show(container, parts, { interval = config.interval } = {}) {
    style();
    container.innerHTML = "";
    const box = el("div", "qrs");
    const canvas = el("canvas", "qrs-code");
    canvas.setAttribute("role", "img");
    box.appendChild(canvas);
    let index = 0, timer = null, playing = parts.length > 1;
    const bar = el("div", "qrs-bar");
    const prev = button("◀", "이전 QR"), next = button("▶", "다음 QR"), play = button("", "");
    const count = el("span", "qrs-count");
    count.setAttribute("aria-live", "polite");
    if (parts.length > 1) { bar.append(prev, count, next, play); box.appendChild(bar); }
    container.appendChild(box);

    function paint() {
      drawCode(canvas, parts[index]);
      canvas.setAttribute("aria-label", `메모 공유 QR ${index + 1}/${parts.length}`);
      count.textContent = `${index + 1} / ${parts.length}`;
      play.textContent = playing ? "일시정지" : "자동 넘김";
      play.setAttribute("aria-label", play.textContent);
    }
    function go(step) { index = (index + step + parts.length) % parts.length; paint(); }
    function schedule() {
      clearInterval(timer);
      timer = playing ? setInterval(() => go(1), interval) : null;
    }
    prev.onclick = () => { playing = false; schedule(); go(-1); };
    next.onclick = () => { playing = false; schedule(); go(1); };
    play.onclick = () => { playing = !playing; schedule(); paint(); };
    paint();
    schedule();
    return { stop() { clearInterval(timer); timer = null; }, get index() { return index; } };
  }

  function el(tag, cls) { const e = root.document.createElement(tag); if (cls) e.className = cls; return e; }
  function button(text, label) {
    const b = el("button", "btn");
    b.type = "button";
    b.textContent = text;
    if (label) b.setAttribute("aria-label", label);
    return b;
  }

  let jsqrLoading = null;
  function loadJsQR() {
    if (typeof root.jsQR === "function") return Promise.resolve(root.jsQR);
    if (!jsqrLoading) {
      jsqrLoading = new Promise((resolve, reject) => {
        const s = root.document.createElement("script");
        s.src = config.jsqrUrl;
        s.onload = () => (typeof root.jsQR === "function" ? resolve(root.jsQR) : reject(new QrError("QR 인식 모듈을 불러오지 못했습니다.")));
        s.onerror = () => { jsqrLoading = null; reject(new QrError("QR 인식 모듈을 불러오지 못했습니다.")); };
        root.document.head.appendChild(s);
      });
    }
    return jsqrLoading;
  }

  async function makeDetector() {
    if ("BarcodeDetector" in root) {
      try {
        const formats = await root.BarcodeDetector.getSupportedFormats();
        if (formats.includes("qr_code")) {
          const d = new root.BarcodeDetector({ formats: ["qr_code"] });
          return async (video) => (await d.detect(video)).map((r) => r.rawValue);
        }
      } catch (_) { /* 지원하지 않으면 jsQR로 */ }
    }
    const jsQR = await loadJsQR();
    const canvas = root.document.createElement("canvas");
    const g = canvas.getContext("2d", { willReadFrequently: true });
    return async (video) => {
      const w = video.videoWidth, h = video.videoHeight;
      if (!w || !h) return [];
      const scale = Math.min(1, 960 / Math.max(w, h));
      canvas.width = Math.round(w * scale); canvas.height = Math.round(h * scale);
      g.drawImage(video, 0, 0, canvas.width, canvas.height);
      const img = g.getImageData(0, 0, canvas.width, canvas.height);
      const hit = jsQR(img.data, img.width, img.height, { inversionAttempts: "dontInvert" });
      return hit ? [hit.data] : [];
    };
  }

  function cameraMessage(err) {
    const name = err && err.name;
    if (name === "NotAllowedError" || name === "SecurityError") {
      return "카메라 권한이 막혀 있습니다 — 브라우저나 휴대폰 설정에서 이 앱의 카메라를 허용하세요.";
    }
    if (name === "NotFoundError" || name === "OverconstrainedError") return "카메라를 찾지 못했습니다.";
    if (name === "NotReadableError") return "다른 앱이 카메라를 쓰고 있습니다 — 그 앱을 닫고 다시 시도하세요.";
    return "카메라를 켜지 못했습니다: " + ((err && err.message) || err);
  }

  // 카메라로 찍어 모은다. 다 모이면 onDone(text). 돌려받은 stop() 으로 언제든 끈다.
  function scan(container, { onDone, onError } = {}) {
    style();
    container.innerHTML = "";
    const box = el("div", "qrs");
    const frame = el("div", "qrs-frame");
    const video = el("video", "qrs-video");
    video.muted = true; video.playsInline = true;
    video.setAttribute("playsinline", ""); video.setAttribute("aria-label", "카메라 화면");
    frame.appendChild(video);
    const status = el("div", "qrs-status");
    status.setAttribute("aria-live", "polite");
    const cells = el("div", "qrs-cells");
    box.append(frame, status, cells);
    container.appendChild(box);

    const collector = new Collector();
    let stream = null, stopped = false, timer = null, finishing = false;
    status.textContent = "카메라를 켜는 중…";

    function paintCells() {
      cells.innerHTML = "";
      for (let i = 1; i <= collector.total && collector.total > 1; i++) {
        const c = el("span", "qrs-cell" + (collector.has(i) ? " on" : ""));
        c.title = `${i}번째`;
        cells.appendChild(c);
      }
    }
    function stop() {
      stopped = true;
      clearTimeout(timer);
      if (stream) stream.getTracks().forEach((t) => t.stop());
      stream = null;
    }
    function fail(message) {
      stop();
      status.textContent = message;
      if (onError) onError(new QrError(message));
    }

    (async () => {
      if (!root.navigator.mediaDevices || !root.navigator.mediaDevices.getUserMedia) {
        fail("이 화면에서는 카메라를 쓸 수 없습니다 — HTTPS 주소나 앱에서 열어주세요.");
        return;
      }
      try {
        stream = await root.navigator.mediaDevices.getUserMedia({
          audio: false,
          video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 }, height: { ideal: 720 } },
        });
      } catch (err) { fail(cameraMessage(err)); return; }
      if (stopped) { stream.getTracks().forEach((t) => t.stop()); return; }
      video.srcObject = stream;
      try { await video.play(); } catch (_) { /* 자동 재생 거부여도 프레임은 들어온다 */ }
      let detect;
      try { detect = await makeDetector(); } catch (err) { fail(err.message); return; }
      status.textContent = "QR을 네모 안에 맞춰주세요.";

      const tick = async () => {
        if (stopped || finishing) return;
        let found = [];
        try { found = await detect(video); } catch (_) { found = []; }
        for (const text of found) {
          const r = collector.add(text);
          if (r.status === "foreign") { status.textContent = "memoinall 공유 QR이 아닙니다."; continue; }
          status.textContent = collector.total > 1
            ? `${r.got} / ${r.total}장 받음${r.got < r.total ? " — 그대로 비추고 있으면 나머지도 받습니다." : ""}`
            : "받았습니다.";
          paintCells();
        }
        if (collector.complete) {
          finishing = true;
          try {
            const text = await collector.text();
            stop();
            status.textContent = "다 받았습니다.";
            if (onDone) onDone(text);
          } catch (err) {
            collector.reset(); paintCells(); finishing = false;
            status.textContent = err.message;
            timer = setTimeout(tick, 400);
          }
          return;
        }
        timer = setTimeout(tick, 120);
      };
      tick();
    })();

    return { stop, collector };
  }

  root.QrShare = { encode, decode, show, scan, Collector, QrError, QrTooLarge, config,
    PART_CHARS, MAX_PARTS, _b45: { encode: b45encode, decode: b45decode } };
})(typeof window !== "undefined" ? window : globalThis);
