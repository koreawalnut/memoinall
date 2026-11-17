/* 메모 공유 파일 — PC 앱(memoinall/exchange.py)과 같은 형식을 읽고 쓴다.
 *
 *   평문:  {"format":"memoinall/memos","version":1,"memos":[{uid,title,body,created_at,updated_at,tags,origin}]}
 *   암호:  {"format":"memoinall/sealed", kdf: scrypt(N=2^15,r=8,p=1), cipher: AES-256-GCM, nonce, data}
 *
 * 휴대폰 백업은 같은 평문 형식에 "schedules" 와 "backup":"memoinall/mobile-backup" 표시를 더 담는다.
 * 휴대폰은 이 표시가 있는 파일만 되살린다 — PC 의 공유 파일로 PC 메모가 휴대폰에 들어오지 않게 하려는 것이다.
 * PC는 모르는 항목을 무시하므로 휴대폰 백업 파일을 PC 에 넣어도 메모는 그대로 들어간다.
 *
 * scrypt 는 브라우저에 없어서 여기서 구현한다(RFC 7914). PBKDF2 는 WebCrypto 를 쓴다.
 */
(function (root) {
  "use strict";

  const FORMAT = "memoinall/memos", VERSION = 1;
  const SEALED_FORMAT = "memoinall/sealed", SEALED_VERSION = 1;
  const BACKUP_KIND = "memoinall/mobile-backup";
  const SCRYPT = { n: 2 ** 15, r: 8, p: 1 };
  const SCRYPT_MAX_N = 2 ** 20;
  const MAX_BYTES = 50_000_000;

  class ExchangeError extends Error {}
  class PasswordRequired extends ExchangeError {}

  const utf8 = new TextEncoder();

  // ------------------------------------------------------------- 텍스트 (PC와 같은 규칙)
  // importers.clean — 공백·빈 줄을 PC와 똑같이 맞춰야 같은 메모의 uid 가 같아진다.
  function clean(text) {
    if (!text) return "";
    let t = String(text).replace(/\r\n/g, "\n").replace(/\r/g, "\n").replace(/​/g, "");
    t = Array.from(t).filter((ch) => ch === "\n" || ch === "\t" || ch.codePointAt(0) >= 32).join("");
    t = t.replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n");
    return t.trim();
  }

  // textutil.title_from
  function titleFrom(text, limit = 60) {
    for (const raw of String(text || "").split(/\r\n|\r|\n/)) {
      const line = raw.trim().replace(/^(?:[-*•]\s+|\[[ xX]\]\s*|#{1,6}\s+)+/, "").trim();
      if (line) {
        const chars = Array.from(line);
        return chars.slice(0, limit).join("") + (chars.length > limit ? "…" : "");
      }
    }
    return "(빈 메모)";
  }

  async function sha256Hex(text) {
    const digest = await crypto.subtle.digest("SHA-256", utf8.encode(text));
    return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
  }

  // exchange.content_uid
  async function contentUid(createdAt, body) {
    return (await sha256Hex(`${createdAt || ""}\n${clean(body)}`)).slice(0, 16);
  }

  // datetime.now().replace(microsecond=0).isoformat() — 시간대 없는 현지 시각
  function nowIso(d = new Date()) {
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }

  // ------------------------------------------------------------- scrypt (RFC 7914)
  function salsa20_8(B, x) {
    let x0 = B[0], x1 = B[1], x2 = B[2], x3 = B[3], x4 = B[4], x5 = B[5], x6 = B[6], x7 = B[7],
      x8 = B[8], x9 = B[9], x10 = B[10], x11 = B[11], x12 = B[12], x13 = B[13], x14 = B[14], x15 = B[15], u;
    for (let i = 0; i < 8; i += 2) {
      u = x0 + x12; x4 ^= (u << 7) | (u >>> 25); u = x4 + x0; x8 ^= (u << 9) | (u >>> 23);
      u = x8 + x4; x12 ^= (u << 13) | (u >>> 19); u = x12 + x8; x0 ^= (u << 18) | (u >>> 14);
      u = x5 + x1; x9 ^= (u << 7) | (u >>> 25); u = x9 + x5; x13 ^= (u << 9) | (u >>> 23);
      u = x13 + x9; x1 ^= (u << 13) | (u >>> 19); u = x1 + x13; x5 ^= (u << 18) | (u >>> 14);
      u = x10 + x6; x14 ^= (u << 7) | (u >>> 25); u = x14 + x10; x2 ^= (u << 9) | (u >>> 23);
      u = x2 + x14; x6 ^= (u << 13) | (u >>> 19); u = x6 + x2; x10 ^= (u << 18) | (u >>> 14);
      u = x15 + x11; x3 ^= (u << 7) | (u >>> 25); u = x3 + x15; x7 ^= (u << 9) | (u >>> 23);
      u = x7 + x3; x11 ^= (u << 13) | (u >>> 19); u = x11 + x7; x15 ^= (u << 18) | (u >>> 14);
      u = x0 + x3; x1 ^= (u << 7) | (u >>> 25); u = x1 + x0; x2 ^= (u << 9) | (u >>> 23);
      u = x2 + x1; x3 ^= (u << 13) | (u >>> 19); u = x3 + x2; x0 ^= (u << 18) | (u >>> 14);
      u = x5 + x4; x6 ^= (u << 7) | (u >>> 25); u = x6 + x5; x7 ^= (u << 9) | (u >>> 23);
      u = x7 + x6; x4 ^= (u << 13) | (u >>> 19); u = x4 + x7; x5 ^= (u << 18) | (u >>> 14);
      u = x10 + x9; x11 ^= (u << 7) | (u >>> 25); u = x11 + x10; x8 ^= (u << 9) | (u >>> 23);
      u = x8 + x11; x9 ^= (u << 13) | (u >>> 19); u = x9 + x8; x10 ^= (u << 18) | (u >>> 14);
      u = x15 + x14; x12 ^= (u << 7) | (u >>> 25); u = x12 + x15; x13 ^= (u << 9) | (u >>> 23);
      u = x13 + x12; x14 ^= (u << 13) | (u >>> 19); u = x14 + x13; x15 ^= (u << 18) | (u >>> 14);
    }
    x[0] = B[0] + x0; x[1] = B[1] + x1; x[2] = B[2] + x2; x[3] = B[3] + x3;
    x[4] = B[4] + x4; x[5] = B[5] + x5; x[6] = B[6] + x6; x[7] = B[7] + x7;
    x[8] = B[8] + x8; x[9] = B[9] + x9; x[10] = B[10] + x10; x[11] = B[11] + x11;
    x[12] = B[12] + x12; x[13] = B[13] + x13; x[14] = B[14] + x14; x[15] = B[15] + x15;
  }

  // BlockMix: B(32r 워드) → Y. 짝수 블록을 앞에, 홀수 블록을 뒤에 둔다.
  function blockMix(B, Y, r, X, T) {
    X.set(B.subarray((2 * r - 1) * 16, 2 * r * 16));
    for (let i = 0; i < 2 * r; i++) {
      for (let k = 0; k < 16; k++) T[k] = X[k] ^ B[i * 16 + k];
      salsa20_8(T, X);
      Y.set(X, ((i & 1) ? r + (i >> 1) : (i >> 1)) * 16);
    }
  }

  function roMix(B, n, r) {
    const words = 32 * r;
    const V = new Uint32Array(words * n);
    let X = new Uint32Array(B), Y = new Uint32Array(words);
    const tmpX = new Uint32Array(16), tmpT = new Uint32Array(16);
    for (let i = 0; i < n; i++) {
      V.set(X, i * words);
      blockMix(X, Y, r, tmpX, tmpT);
      [X, Y] = [Y, X];
    }
    for (let i = 0; i < n; i++) {
      const j = X[(2 * r - 1) * 16] & (n - 1);
      for (let k = 0; k < words; k++) X[k] ^= V[j * words + k];
      blockMix(X, Y, r, tmpX, tmpT);
      [X, Y] = [Y, X];
    }
    B.set(X);
  }

  async function pbkdf2(password, salt, length) {
    const key = await crypto.subtle.importKey("raw", password, "PBKDF2", false, ["deriveBits"]);
    const bits = await crypto.subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt, iterations: 1 }, key, length * 8);
    return new Uint8Array(bits);
  }

  function toWords(bytes) {
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const out = new Uint32Array(bytes.length / 4);
    for (let i = 0; i < out.length; i++) out[i] = view.getUint32(i * 4, true);
    return out;
  }
  function toBytes(words) {
    const out = new Uint8Array(words.length * 4), view = new DataView(out.buffer);
    for (let i = 0; i < words.length; i++) view.setUint32(i * 4, words[i], true);
    return out;
  }

  async function scrypt(password, salt, n, r, p, length) {
    const B = await pbkdf2(password, salt, p * 128 * r);
    for (let i = 0; i < p; i++) {
      const block = toWords(B.subarray(i * 128 * r, (i + 1) * 128 * r));
      roMix(block, n, r);
      B.set(toBytes(block), i * 128 * r);
    }
    return pbkdf2(password, B, length);
  }

  // ------------------------------------------------------------- 봉투
  function b64(bytes) {
    let s = "";
    for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(s);
  }
  function unb64(value) {
    const s = atob(String(value));
    const out = new Uint8Array(s.length);
    for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
    return out;
  }

  async function aesKey(password, salt, n, r, p) {
    // 같은 한글 비밀번호도 기기에 따라 자모가 풀려(NFD) 들어올 수 있다 — PC와 같이 NFC 로 맞춘다.
    const secret = utf8.encode(String(password).normalize("NFC"));
    const raw = await scrypt(secret, salt, n, r, p, 32);
    return crypto.subtle.importKey("raw", raw, "AES-GCM", false, ["encrypt", "decrypt"]);
  }

  async function seal(payload, password) {
    if (!password) return payload;
    const salt = crypto.getRandomValues(new Uint8Array(16)), nonce = crypto.getRandomValues(new Uint8Array(12));
    const key = await aesKey(password, salt, SCRYPT.n, SCRYPT.r, SCRYPT.p);
    const data = new Uint8Array(await crypto.subtle.encrypt(
      { name: "AES-GCM", iv: nonce, additionalData: utf8.encode(SEALED_FORMAT) }, key, utf8.encode(JSON.stringify(payload))));
    return {
      format: SEALED_FORMAT, version: SEALED_VERSION, count: payload.count,
      kdf: { name: "scrypt", n: SCRYPT.n, r: SCRYPT.r, p: SCRYPT.p, salt: b64(salt) },
      cipher: "AES-256-GCM", nonce: b64(nonce), data: b64(data),
    };
  }

  const isSealed = (data) => !!data && typeof data === "object" && data.format === SEALED_FORMAT;

  async function unseal(envelope, password) {
    if (!password) throw new PasswordRequired("암호화된 메모입니다 — 보낸 사람에게 받은 비밀번호를 입력하세요.");
    const version = parseInt(envelope.version, 10) || 0;
    if (version > SEALED_VERSION) throw new ExchangeError(`더 새로운 형식의 암호화 파일입니다 (version ${version}). 앱을 최신으로 올려주세요.`);
    let n, r, p, salt, nonce, data;
    try {
      const kdf = envelope.kdf;
      n = +kdf.n; r = +kdf.r; p = +kdf.p;
      salt = unb64(kdf.salt); nonce = unb64(envelope.nonce); data = unb64(envelope.data);
      if (kdf.name !== "scrypt" || envelope.cipher !== "AES-256-GCM") throw new ExchangeError("지원하지 않는 암호화 방식입니다. 앱을 최신으로 올려주세요.");
    } catch (err) {
      if (err instanceof ExchangeError) throw err;
      throw new ExchangeError("암호화된 메모가 손상됐습니다 — 다시 받아주세요.");
    }
    // 받은 파일이 비용을 부풀려 휴대폰을 멈추게 두지 않는다.
    if (!(Number.isInteger(n) && n >= 2 && n <= SCRYPT_MAX_N && (n & (n - 1)) === 0 && r >= 1 && r <= 32 && p >= 1 && p <= 16)) {
      throw new ExchangeError("암호화된 메모가 손상됐습니다 — 다시 받아주세요.");
    }
    const key = await aesKey(password, salt, n, r, p);
    try {
      const plain = await crypto.subtle.decrypt({ name: "AES-GCM", iv: nonce, additionalData: utf8.encode(SEALED_FORMAT) }, key, data);
      return new TextDecoder().decode(plain);
    } catch (_) {
      throw new ExchangeError("비밀번호가 맞지 않거나 내용이 손상됐습니다.");
    }
  }

  // ------------------------------------------------------------- 꾸러미
  async function build(memos, { note = "", schedules = null, backup = false } = {}) {
    const items = [];
    for (const m of memos) {
      items.push({
        uid: await contentUid(m.created_at, m.body),
        title: titleFrom(m.body),
        body: m.body,
        created_at: m.created_at,
        updated_at: m.updated_at || m.created_at,
        tags: [...(m.tags || [])],
        origin: m.origin || "mobile",
      });
    }
    const pack = { format: FORMAT, version: VERSION, exported_at: nowIso(), note: String(note || "").trim(), count: items.length, memos: items };
    if (backup) pack.backup = BACKUP_KIND;
    if (schedules) pack.schedules = schedules.map((e) => ({
      title: e.title, date: e.date, time: e.time || "", note: e.note || "", tags: [...(e.tags || [])],
      done: !!e.done, created_at: e.created_at, updated_at: e.updated_at || e.created_at,
    }));
    return pack;
  }

  function loadJson(text) {
    try { return JSON.parse(String(text).replace(/^\uFEFF/, "")); }
    catch (_) { throw new ExchangeError("메모 파일 형식이 아닙니다 — JSON을 읽지 못했습니다."); }
  }

  // 받은 내용을 검증해 꾸러미로. 암호화돼 있으면 password 로 연다.
  async function parse(text, password = "") {
    if (String(text).length > MAX_BYTES) throw new ExchangeError("파일이 너무 큽니다.");
    let data = loadJson(text);
    if (isSealed(data)) data = loadJson(await unseal(data, password));
    if (!data || typeof data !== "object" || !("memos" in data)) throw new ExchangeError("메모 파일 형식이 아닙니다 — memos 항목이 없습니다.");
    if (data.format != null && data.format !== FORMAT) throw new ExchangeError(`다른 프로그램의 파일로 보입니다 (format=${data.format}).`);
    if (!Array.isArray(data.memos)) throw new ExchangeError("메모 파일이 손상됐습니다 — memos 가 목록이 아닙니다.");
    const version = parseInt(data.version, 10) || VERSION;
    if (version > VERSION) throw new ExchangeError(`더 새로운 형식의 파일입니다 (version ${version}). 앱을 최신으로 올려주세요.`);
    return data;
  }

  // 화면에 보일 받은 메모 목록. 깨진 항목은 버리지 않고 이유와 함께 넘긴다.
  async function items(pack) {
    const out = [];
    for (const raw of pack.memos) {
      const body = clean(raw && typeof raw.body === "string" ? raw.body : "");
      const created = raw && typeof raw.created_at === "string" && raw.created_at ? raw.created_at : nowIso();
      const tags = Array.isArray(raw && raw.tags) ? raw.tags.map((t) => String(t).replace(/^#/, "").trim()).filter(Boolean) : [];
      out.push({
        uid: (raw && typeof raw.uid === "string" && raw.uid.trim()) || await contentUid(created, body),
        body, created_at: created,
        updated_at: (raw && typeof raw.updated_at === "string" && raw.updated_at) || created,
        tags: [...new Set(tags)].slice(0, 30), empty: !body,
        origin: raw && typeof raw.origin === "string" ? raw.origin : "",
      });
    }
    return out;
  }

  // 이 휴대폰 앱이 만든 백업인지. PC 의 공유 파일에는 이 표시가 없다.
  const isBackup = (pack) => !!pack && pack.backup === BACKUP_KIND;

  root.Exchange = {
    FORMAT, SEALED_FORMAT, BACKUP_KIND, SCRYPT, ExchangeError, PasswordRequired,
    clean, titleFrom, contentUid, nowIso, scrypt, seal, unseal, isSealed, isBackup, build, parse, items,
  };
})(typeof window !== "undefined" ? window : globalThis);
