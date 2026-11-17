/* memoinall 모바일 — 메모·일정을 이 휴대폰 안(IndexedDB)에만 둔다.
 * PC 앱과는 연결되지 않고, QR·파일(PC와 같은 공유 형식)로만 주고받는다.
 * AI 정리는 사용자가 넣은 API 키로 휴대폰에서 해당 회사에 직접 요청한다.
 */
"use strict";

const APP_VERSION = "__BUILD__";
const X = window.Exchange;
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const uniq = (a) => [...new Set(a)];

// ================================================================ 저장소
const DB_NAME = "memoinall", DB_VERSION = 1;
let dbOpen = null;
function db() {
  if (!dbOpen) {
    dbOpen = new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = () => {
        const d = req.result;
        if (!d.objectStoreNames.contains("memos")) d.createObjectStore("memos", { keyPath: "id", autoIncrement: true });
        if (!d.objectStoreNames.contains("events")) d.createObjectStore("events", { keyPath: "id", autoIncrement: true });
        if (!d.objectStoreNames.contains("kv")) d.createObjectStore("kv", { keyPath: "key" });
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
      req.onblocked = () => reject(new Error("다른 창에서 앱이 열려 있습니다 — 그 창을 닫고 다시 여세요."));
    });
  }
  return dbOpen;
}
const done = (r) => new Promise((resolve, reject) => { r.onsuccess = () => resolve(r.result); r.onerror = () => reject(r.error); });

// 쓰기는 트랜잭션이 끝까지 저장된 뒤에 돌려준다.
async function write(name, fn) {
  const d = await db();
  return new Promise((resolve, reject) => {
    const t = d.transaction(name, "readwrite");
    const out = fn(t.objectStore(name));
    t.oncomplete = () => resolve(out);
    t.onerror = () => reject(t.error);
    t.onabort = () => reject(t.error || new Error("저장하지 못했습니다."));
  });
}
const idb = {
  async all(name) { return done((await db()).transaction(name).objectStore(name).getAll()); },
  async get(name, key) { return done((await db()).transaction(name).objectStore(name).get(key)); },
  async put(name, value) {
    const keys = await write(name, (s) => { const r = s.put(value); const box = {}; r.onsuccess = () => { box.key = r.result; }; return box; });
    return keys.key;
  },
  async putMany(name, values) {
    const box = await write(name, (s) => {
      const keys = [];
      values.forEach((v, i) => { const r = s.put(v); r.onsuccess = () => { keys[i] = r.result; }; });
      return keys;
    });
    return box;
  },
  async del(name, keys) { await write(name, (s) => { [].concat(keys).forEach((k) => s.delete(k)); }); },
  async clear(name) { await write(name, (s) => { s.clear(); }); },
};
const kv = {
  async get(key, fallback = null) { const row = await idb.get("kv", key); return row ? row.value : fallback; },
  async set(key, value) { await idb.put("kv", { key, value }); },
};

const local = {
  get(key) { try { return localStorage.getItem(key); } catch (_) { return null; } },
  set(key, value) { try { value == null ? localStorage.removeItem(key) : localStorage.setItem(key, value); } catch (_) { /* 저장 못 해도 동작엔 지장 없음 */ } },
};

// ================================================================ 상태
const PROVIDERS = {
  gemini: { label: "Gemini", company: "Google", models: ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.5-flash-lite"],
    help: "Google AI Studio(aistudio.google.com)에서 키를 만들 수 있습니다." },
  openai: { label: "ChatGPT", company: "OpenAI", models: ["gpt-5-mini", "gpt-5", "gpt-5-nano"],
    help: "platform.openai.com의 API keys에서 만듭니다. 쓴 만큼 요금이 나갑니다." },
  anthropic: { label: "Claude", company: "Anthropic", models: ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"],
    help: "console.anthropic.com 에서 만듭니다. 쓴 만큼 요금이 나갑니다." },
};
const S = {
  memos: [], events: [], ai: { provider: "gemini", keys: {}, models: {} }, lastBackup: null,
  tab: "memos", q: "", tag: "", shown: 50, selecting: false, picked: new Set(),
  month: null, day: null,
};

// ================================================================ 날짜
const pad = (n) => String(n).padStart(2, "0");
const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const todayStr = () => ymd(new Date());
const parseYmd = (s) => { const [y, m, d] = String(s).split("-").map(Number); return new Date(y, m - 1, d); };
const DOW = ["일", "월", "화", "수", "목", "금", "토"];
const dayLabel = (s) => { const d = parseYmd(s); return `${d.getMonth() + 1}월 ${d.getDate()}일 (${DOW[d.getDay()]})`; };
function whenLabel(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return String(iso || "");
  const day = ymd(d), hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (day === todayStr()) return `오늘 ${hm}`;
  const y = new Date(); y.setDate(y.getDate() - 1);
  if (day === ymd(y)) return `어제 ${hm}`;
  if (d.getFullYear() === new Date().getFullYear()) return `${d.getMonth() + 1}월 ${d.getDate()}일`;
  return `${d.getFullYear()}. ${d.getMonth() + 1}. ${d.getDate()}.`;
}
const fullLabel = (iso) => { const d = new Date(iso); return isNaN(d) ? String(iso || "") : `${ymd(d)} ${pad(d.getHours())}:${pad(d.getMinutes())}`; };
const stamp = () => { const d = new Date(); return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}`; };
function ago(iso) {
  const days = Math.floor((Date.now() - new Date(iso)) / 86400000);
  return days <= 0 ? "오늘" : days === 1 ? "어제" : `${days}일 전`;
}

// ================================================================ 공통 화면 도구
let toastTimer = null;
function toast(message, ms = 2800) {
  const t = $("#toast");
  t.textContent = message;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, ms);
}

// 시트는 뒤로 가기(안드로이드 뒤로 버튼·스와이프)로 닫히게 기록을 하나씩 쌓는다.
// 닫자마자 다른 시트를 열면(메모 → AI 설정 열기 등) 뒤로 가기가 아직 처리되기 전이라,
// 그때 쌓은 기록이 뒤로 가기에 같이 지워진다. 그래서 뒤로 가기가 끝난 다음에 쌓는다.
const sheets = [];
let skipPop = 0, pendingPush = 0;
function openSheet(dlg) {
  if (dlg.open) return;
  dlg.showModal();
  sheets.push(dlg);
  if (skipPop) pendingPush++;
  else history.pushState({ sheet: sheets.length }, "");
}
function closeSheet(dlg, { fromHistory = false, force = false } = {}) {
  if (!dlg.open) return;
  if (!force && dlg.beforeClose && !dlg.beforeClose()) {
    if (fromHistory) history.pushState({ sheet: sheets.length }, "");
    return;
  }
  dlg.close();
  const i = sheets.lastIndexOf(dlg);
  if (i >= 0) sheets.splice(i, 1);
  if (!fromHistory) { skipPop++; history.back(); }
  if (dlg.onClosed) dlg.onClosed();
}
window.addEventListener("popstate", () => {
  if (skipPop) {
    skipPop--;
    if (!skipPop) for (; pendingPush; pendingPush--) history.pushState({ sheet: sheets.length }, "");
    return;
  }
  const top = sheets[sheets.length - 1];
  if (top) closeSheet(top, { fromHistory: true });
});
$$("dialog.sheet").forEach((dlg) => {
  dlg.addEventListener("cancel", (e) => { e.preventDefault(); closeSheet(dlg); });
  dlg.addEventListener("click", (e) => { if (e.target === dlg) closeSheet(dlg); });
  $$("[data-close]", dlg).forEach((b) => { b.onclick = () => closeSheet(dlg); });
});

function download(name, text) {
  const url = URL.createObjectURL(new Blob([text], { type: "application/json" }));
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

// ================================================================ 태그·검색
const TAG_RE = /(^|[\s(\[,])#([\p{L}\p{N}_\-]{1,30})/gu;
const hashtags = (text) => uniq(Array.from(String(text || "").matchAll(TAG_RE), (m) => m[2]));
const normTag = (t) => String(t || "").trim().replace(/^#+/, "").replace(/\s+/g, "").slice(0, 30);
const parseTags = (text) => uniq(String(text || "").split(/[\s,]+/).map(normTag).filter(Boolean));
const terms = (q) => String(q || "").toLowerCase().split(/\s+/).filter(Boolean);
const reEsc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

function highlight(text, ts) {
  if (!ts.length) return esc(text);
  const re = new RegExp("(" + ts.map(reEsc).join("|") + ")", "gi");
  return String(text).split(re).map((part, i) => (i % 2 ? `<mark>${esc(part)}</mark>` : esc(part))).join("");
}
function matches(m, ts) {
  if (S.tag && !m.tags.includes(S.tag)) return false;
  if (!ts.length) return true;
  const hay = (m.body + "\n" + m.tags.map((t) => "#" + t).join(" ")).toLowerCase();
  return ts.every((t) => hay.includes(t));
}
const visibleMemos = () => { const ts = terms(S.q); return S.memos.filter((m) => matches(m, ts)); };

function snippet(m, ts) {
  const lines = m.body.split("\n");
  const first = lines.findIndex((l) => l.trim());
  let rest = lines.slice(first + 1).join(" ").replace(/\s+/g, " ").trim();
  if (ts.length) {
    const low = rest.toLowerCase();
    const at = Math.min(...ts.map((t) => { const i = low.indexOf(t); return i < 0 ? Infinity : i; }));
    if (at !== Infinity && at > 40) rest = "…" + rest.slice(at - 30);
  }
  return rest.slice(0, 180);
}

// ================================================================ 메모
function sortMemos() {
  S.memos.sort((a, b) => (b.created_at || "").localeCompare(a.created_at || "") || b.id - a.id);
}
function fixMemo(m) {
  m.tags = Array.isArray(m.tags) ? m.tags : [];
  m.body = String(m.body || "");
  return m;
}

async function addMemo(body, extra = {}) {
  const now = X.nowIso();
  const memo = { body, created_at: now, updated_at: now, origin: "mobile", ...extra };
  memo.tags = uniq([...(extra.tags || []), ...hashtags(body)]);
  memo.id = await idb.put("memos", memo);
  S.memos.push(memo);
  sortMemos();
  askPersist();
  return memo;
}

function renderTagChips() {
  const counts = new Map();
  S.memos.forEach((m) => m.tags.forEach((t) => counts.set(t, (counts.get(t) || 0) + 1)));
  const tags = [...counts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 40).map(([t]) => t);
  if (S.tag && !tags.includes(S.tag)) tags.unshift(S.tag);
  const box = $("#tagChips");
  box.hidden = !tags.length;
  box.innerHTML = tags.length ? `<button class="chip" type="button" data-tag="" aria-pressed="${!S.tag}">전체</button>` +
    tags.map((t) => `<button class="chip" type="button" data-tag="${esc(t)}" aria-pressed="${S.tag === t}">#${esc(t)}</button>`).join("") : "";
}

function renderMemos() {
  const ts = terms(S.q);
  const rows = visibleMemos();
  const list = $("#memoList");
  const filtering = !!(ts.length || S.tag);
  $("#listInfo").textContent = filtering ? `찾은 메모 ${rows.length}건` : "";
  if (!S.memos.length) {
    list.innerHTML = '<div class="empty">아직 메모가 없습니다.<br>위에 적고 저장하세요.</div>';
  } else if (!rows.length) {
    list.innerHTML = '<div class="empty">찾는 메모가 없습니다.</div>';
  } else {
    list.innerHTML = rows.slice(0, S.shown).map((m) => {
      const picked = S.picked.has(m.id), snip = snippet(m, ts);
      return `<div class="memo${picked ? " picked" : ""}" role="button" tabindex="0" data-id="${m.id}"${S.selecting ? ` aria-pressed="${picked}"` : ""}>
        ${S.selecting ? `<input type="checkbox" tabindex="-1" aria-hidden="true" ${picked ? "checked" : ""} />` : ""}
        <div class="body">
          <div class="title">${highlight(X.titleFrom(m.body), ts)}</div>
          ${snip ? `<div class="snip">${highlight(snip, ts)}</div>` : ""}
          <div class="meta"><span>${esc(whenLabel(m.created_at))}</span>${m.origin === "ai" ? '<span class="badge">AI 정리</span>' : ""}${m.tags.map((t) => `<span class="tag">#${highlight(t, ts)}</span>`).join("")}</div>
        </div></div>`;
    }).join("");
  }
  const more = $("#more");
  more.hidden = rows.length <= S.shown;
  more.textContent = `더 보기 (${rows.length - S.shown}건 더)`;
  if (S.tab === "memos") $("#pageSub").textContent = S.memos.length ? `${S.memos.length}건` : "";
  paintSelection();
}

function renderAllMemos() { renderTagChips(); renderMemos(); }

// ---- 새 메모
const capture = $("#capture");
// 음성 입력은 휴대폰 키보드의 받아쓰기를 그대로 쓴다. 빈 칸일 때 그 사실을 알려 준다.
const VOICE_HINT = "키보드의 🎤로 말해서 입력할 수 있어요";
const draftNote = (text) => { $("#draftNote").textContent = text || VOICE_HINT; };
function captureChanged() {
  $("#saveMemo").disabled = !capture.value.trim();
  capture.style.height = "auto";
  capture.style.height = Math.min(capture.scrollHeight + 4, window.innerHeight * 0.5) + "px";
}
let draftTimer = null;
capture.addEventListener("input", () => {
  captureChanged();
  clearTimeout(draftTimer);
  draftTimer = setTimeout(() => {
    local.set("memoinall.draft", capture.value.trim() ? capture.value : null);
    draftNote(capture.value.trim() ? "작성 중인 내용은 자동 보관됩니다" : "");
  }, 400);
});
capture.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); saveCapture(); } });
$("#saveMemo").onclick = saveCapture;
async function saveCapture() {
  const body = X.clean(capture.value);
  if (!body || capture.readOnly) return;
  // 저장이 끝나면 입력칸을 비운다. 그사이 새로 친 글이 같이 지워지지 않게 잠깐 잠근다.
  $("#saveMemo").disabled = true;
  capture.readOnly = true;
  try {
    await addMemo(body);
    capture.readOnly = false;
    capture.value = "";
    clearTimeout(draftTimer);
    local.set("memoinall.draft", null);
    draftNote("");
    captureChanged();
    renderAllMemos();
    toast("저장했습니다");
  } catch (err) {
    capture.readOnly = false;
    toast("저장하지 못했습니다: " + err.message, 5000);
    captureChanged();
  }
}

// ---- 검색·태그
let searchTimer = null;
$("#q").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { S.q = e.target.value; S.shown = 50; renderMemos(); }, 120);
});
$("#tagChips").onclick = (e) => {
  const chip = e.target.closest("[data-tag]");
  if (!chip) return;
  S.tag = S.tag === chip.dataset.tag ? "" : chip.dataset.tag;
  S.shown = 50;
  renderAllMemos();
};
$("#more").onclick = () => { S.shown += 50; renderMemos(); };

// ---- 목록 누르기
function cardAction(card) {
  const id = +card.dataset.id;
  if (S.selecting) {
    S.picked.has(id) ? S.picked.delete(id) : S.picked.add(id);
    renderMemos();
    const again = $(`.memo[data-id="${id}"]`);
    if (again) again.focus({ preventScroll: true });
  } else openMemo(id);
}
$("#memoList").addEventListener("click", (e) => { const card = e.target.closest(".memo"); if (card) cardAction(card); });
$("#memoList").addEventListener("keydown", (e) => {
  const card = e.target.closest(".memo");
  if (card && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); cardAction(card); }
});

// ---- 고르기
function startSelecting() { S.selecting = true; S.picked.clear(); renderMemos(); }
function stopSelecting() { S.selecting = false; S.picked.clear(); renderMemos(); }
function paintSelection() {
  document.body.classList.toggle("selecting", S.selecting);
  $("#selbar").hidden = !S.selecting;
  $("#selectToggle").textContent = S.selecting ? "선택 끝" : "선택";
  $("#selectToggle").setAttribute("aria-pressed", S.selecting);
  const n = S.picked.size;
  $("#selCount").textContent = `${n}건 선택`;
  ["#selSend", "#selAi", "#selDel"].forEach((s) => { $(s).disabled = !n; });
  const shown = visibleMemos();
  $("#selAll").textContent = shown.length && shown.every((m) => S.picked.has(m.id)) ? "전체 해제" : "전체";
}
$("#selectToggle").onclick = () => (S.selecting ? stopSelecting() : startSelecting());
$("#selDone").onclick = stopSelecting;
$("#selAll").onclick = () => {
  const shown = visibleMemos();
  if (shown.every((m) => S.picked.has(m.id))) shown.forEach((m) => S.picked.delete(m.id));
  else shown.forEach((m) => S.picked.add(m.id));
  renderMemos();
};
const pickedIds = () => S.memos.filter((m) => S.picked.has(m.id)).map((m) => m.id);
$("#selSend").onclick = () => openSend(pickedIds());
$("#selAi").onclick = () => openAi(pickedIds());
$("#selDel").onclick = async () => {
  const ids = pickedIds();
  if (!ids.length || !confirm(`메모 ${ids.length}건을 지울까요? 되돌릴 수 없습니다.`)) return;
  await idb.del("memos", ids);
  S.memos = S.memos.filter((m) => !ids.includes(m.id));
  stopSelecting();
  renderAllMemos();
  toast(`${ids.length}건을 지웠습니다`);
};

// ---- 메모 보기·고치기
const memoDlg = $("#memoDlg");
let editing = null;   // { id, tags }
const findMemo = (id) => S.memos.find((m) => m.id === id);
function openMemo(id) {
  const m = findMemo(id);
  if (!m) return;
  editing = { id, tags: [...m.tags] };
  $("#memoBody").value = m.body;
  $("#memoMeta").textContent = `작성 ${fullLabel(m.created_at)}` +
    (m.updated_at && m.updated_at !== m.created_at ? ` · 수정 ${fullLabel(m.updated_at)}` : "") +
    (m.origin === "ai" ? " · AI 정리" : "");
  paintTagEdit();
  openSheet(memoDlg);
}
function memoDirty() {
  const m = editing && findMemo(editing.id);
  if (!m) return false;
  return X.clean($("#memoBody").value) !== m.body || editing.tags.join("\n") !== m.tags.join("\n");
}
memoDlg.beforeClose = () => !memoDirty() || confirm("고친 내용을 저장하지 않고 닫을까요?");
memoDlg.onClosed = () => { editing = null; };
function paintTagEdit() {
  $("#memoTags").innerHTML = editing.tags.map((t, i) =>
    `<span class="chip">#${esc(t)}<button class="btn ghost small" type="button" data-untag="${i}" aria-label="${esc(t)} 태그 빼기" style="min-height:28px;padding:0 4px">×</button></span>`).join("") +
    `<input id="tagInput" placeholder="태그 추가" aria-label="태그 추가" enterkeyhint="done" autocomplete="off" />`;
  const input = $("#tagInput");
  // 다시 그리면 입력칸이 빠지면서 blur 가 한 번 더 온다 — 값을 먼저 비워 두 번 넣지 않게 한다.
  const commit = () => {
    const add = parseTags(input.value);
    input.value = "";
    if (!add.length || !input.isConnected) return false;
    editing.tags = uniq([...editing.tags, ...add]);
    paintTagEdit();
    $("#tagInput").focus();
    return true;
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === ",") { e.preventDefault(); commit(); } });
  input.addEventListener("blur", () => { if (input.value.trim()) commit(); });
}
$("#memoTags").addEventListener("click", (e) => {
  const b = e.target.closest("[data-untag]");
  if (!b) return;
  editing.tags.splice(+b.dataset.untag, 1);
  paintTagEdit();
});
$("#memoSave").onclick = async () => {
  const m = editing && findMemo(editing.id);
  if (!m) return;
  const tagInput = $("#tagInput");
  if (tagInput && tagInput.value.trim()) editing.tags = uniq([...editing.tags, ...parseTags(tagInput.value)]);
  const body = X.clean($("#memoBody").value);
  if (!body) { toast("내용이 비었습니다 — 지우려면 삭제를 누르세요"); return; }
  const before = hashtags(m.body);
  const added = hashtags(body).filter((t) => !before.includes(t));
  const next = { ...m, body, tags: uniq([...editing.tags, ...added]), updated_at: body !== m.body || editing.tags.join() !== m.tags.join() ? X.nowIso() : m.updated_at };
  try {
    await idb.put("memos", next);
    Object.assign(m, next);
    closeSheet(memoDlg, { force: true });
    renderAllMemos();
    toast("저장했습니다");
  } catch (err) { toast("저장하지 못했습니다: " + err.message, 5000); }
};
$("#memoDel").onclick = async () => {
  if (!editing || !confirm("이 메모를 지울까요? 되돌릴 수 없습니다.")) return;
  const id = editing.id;
  await idb.del("memos", id);
  S.memos = S.memos.filter((m) => m.id !== id);
  closeSheet(memoDlg, { force: true });
  renderAllMemos();
  toast("지웠습니다");
};
$("#memoSend").onclick = () => editing && openSend([editing.id]);
$("#memoAi").onclick = () => editing && openAi([editing.id]);

// ================================================================ PC로 보내기·백업
// 주고받기는 휴대폰 → PC 한 방향뿐이다. PC로는 화면의 QR 로만 보낸다 — 네트워크·외부 서버(메신저·메일)를
// 거치지 않는다. 파일은 이 휴대폰의 백업용으로만 만든다.
const sendDlg = $("#sendDlg");
let sending = null;   // { ids, backup }
let qrView = null, wake = null;
function openSend(ids, { backup = false } = {}) {
  if (!ids.length && !backup) { toast("보낼 메모를 고르세요"); return; }
  sending = { ids, backup };
  $("#sendTitle").textContent = backup ? `백업 — 메모 ${S.memos.length}건 · 일정 ${S.events.length}건` : `PC로 보내기 — 메모 ${ids.length}건`;
  $("#sendPw").value = "";
  $("#sendPw").placeholder = backup ? "정하면 되살릴 때 이 비밀번호가 필요합니다" : "정하면 PC에서 열 때 이 비밀번호가 필요합니다";
  $("#sendOut").innerHTML = "";
  $("#sendQr").hidden = backup;
  $("#sendFile").hidden = !backup;
  paintPwNote();
  openSheet(sendDlg);
}
function paintPwNote() {
  const pw = !!$("#sendPw").value, backup = !!(sending && sending.backup);
  $("#sendPwNote").innerHTML = pw
    ? (backup ? "되살릴 때 이 비밀번호가 필요합니다. 잊으면 아무도 열 수 없습니다."
      : "PC에서 QR을 찍은 뒤 이 비밀번호를 넣어야 열립니다. 비밀번호는 화면과 따로 알려주세요.")
    : `<span class="warn">${backup ? "비밀번호 없이 저장하면 파일을 손에 넣은 누구나 내용을 읽을 수 있습니다."
      : "비밀번호 없이 보내면 이 화면을 찍은 누구나 내용을 읽을 수 있습니다."}</span>`;
}
$("#sendPw").addEventListener("input", paintPwNote);
sendDlg.onClosed = () => { stopQr(); sending = null; };
function stopQr() {
  if (qrView) qrView.stop();
  qrView = null;
  if (wake) wake.release().catch(() => {});
  wake = null;
}

async function sendText() {
  const pw = $("#sendPw").value;
  const memos = sending.backup ? S.memos.slice().reverse() : sending.ids.map(findMemo).filter(Boolean);
  const pack = await X.build(memos, sending.backup ? { note: "memoinall 휴대폰 백업", schedules: S.events, backup: true } : {});
  return { text: JSON.stringify(await X.seal(pack, pw), null, 2), count: memos.length, locked: !!pw };
}
function sendBusy(on, message = "") {
  ["#sendQr", "#sendFile"].forEach((s) => { $(s).disabled = on; });
  if (message) $("#sendOut").innerHTML = `<div class="muted">${esc(message)}</div>`;
}
$("#sendQr").onclick = async () => {
  stopQr();
  sendBusy(true, $("#sendPw").value ? "암호화하는 중…" : "QR 만드는 중…");
  try {
    const { text, count } = await sendText();
    const parts = await QrShare.encode(text);
    $("#sendOut").innerHTML = `<div class="note" style="margin-bottom:10px">PC 앱의 <b>가져오기·공유 → 동료와 주고받기 → QR 찍기</b>를 누르고
      이 화면을 웹캠에 비추세요.${parts.length > 1 ? ` QR <b>${parts.length}장</b>이 자동으로 넘어가니 그대로 두면 됩니다.` : ""}</div>
      <div id="qrHere"></div>
      <div class="muted" style="margin-top:8px;text-align:center">메모 ${count}건${$("#sendPw").value ? " · 암호화됨" : ""}</div>`;
    qrView = QrShare.show($("#qrHere"), parts);
    try { if (navigator.wakeLock) wake = await navigator.wakeLock.request("screen"); } catch (_) { /* 화면 켜짐 유지는 선택 */ }
  } catch (err) {
    $("#sendOut").innerHTML = `<div class="err">${esc(err.message)}</div>`;
  } finally { sendBusy(false); }
};
$("#sendFile").onclick = async () => {
  sendBusy(true, $("#sendPw").value ? "암호화하는 중…" : "만드는 중…");
  try {
    const { text, locked } = await sendText();
    const name = `memoinall-백업-${stamp()}.json`;
    download(name, text);
    S.lastBackup = X.nowIso();
    await kv.set("lastBackup", S.lastBackup);
    paintBackupInfo();
    $("#sendOut").innerHTML = `<div class="note"><b>${esc(name)}</b> 파일로 저장했습니다.
      되살릴 때는 <b>백업에서 되살리기</b>로 이 파일을 고르세요.${locked ? "" : '<br><span class="warn">암호화하지 않은 파일입니다.</span>'}</div>`;
  } catch (err) {
    $("#sendOut").innerHTML = `<div class="err">${esc(err.message)}</div>`;
  } finally { if (sending) sendBusy(false); }
};

$("#pickToSend").onclick = () => { switchTab("memos"); startSelecting(); toast("보낼 메모를 고르고 아래의 PC로 보내기를 누르세요"); };
$("#backupBtn").onclick = () => openSend([], { backup: true });
function paintBackupInfo() {
  $("#backupInfo").innerHTML = S.lastBackup
    ? `마지막 백업: ${esc(fullLabel(S.lastBackup))} (${ago(S.lastBackup)})`
    : S.memos.length ? '<span class="warn">아직 백업한 적이 없습니다.</span>' : "";
}

// ================================================================ 백업에서 되살리기
// 이 앱이 만든 백업만 받는다. PC 의 공유 파일은 받지 않는다 — PC 메모가 휴대폰으로 나오는 길을 만들지 않기 위해서다.
const importDlg = $("#importDlg");
$("#restoreFile").addEventListener("change", async (e) => {
  const input = e.target, file = input.files && input.files[0];
  input.value = "";
  if (!file) return;
  if (file.size > 50_000_000) { toast("50MB 이하의 파일을 고르세요"); return; }
  try { restore(await file.text(), file.name); }
  catch (err) { toast("파일을 읽지 못했습니다: " + err.message, 5000); }
});

let incoming = null;
const uidCache = new Map();
async function knownUids() {
  const set = new Set();
  for (const m of S.memos) {
    const key = `${m.id}:${m.updated_at}:${m.body.length}`;
    if (!uidCache.has(key)) uidCache.set(key, await X.contentUid(m.created_at, m.body));
    set.add(uidCache.get(key));
  }
  return set;
}
const eventKey = (e) => `${e.title.trim()}|${e.date}|${e.time || ""}`;
function packSchedules(pack) {
  return (Array.isArray(pack.schedules) ? pack.schedules : [])
    .filter((e) => e && typeof e.title === "string" && e.title.trim() && /^\d{4}-\d{2}-\d{2}$/.test(e.date))
    .map((e) => ({
      title: e.title.trim().slice(0, 300), date: e.date, time: /^\d{2}:\d{2}$/.test(e.time || "") ? e.time : "",
      note: typeof e.note === "string" ? e.note : "", tags: Array.isArray(e.tags) ? uniq(e.tags.map(normTag).filter(Boolean)) : [],
      done: !!e.done, created_at: typeof e.created_at === "string" ? e.created_at : X.nowIso(),
      updated_at: typeof e.updated_at === "string" ? e.updated_at : X.nowIso(),
    }));
}

async function restore(text, label) {
  let sealed = false;
  try { sealed = X.isSealed(JSON.parse(String(text).replace(/^\uFEFF/, ""))); } catch (_) { /* parse 에서 알려준다 */ }
  incoming = { text, label, sealed, password: "", pack: null, items: [], schedules: [], error: "", picked: new Set(), withEvents: true };
  if (!sealed) await openIncoming();
  paintImport();
  openSheet(importDlg);
  if (sealed) setTimeout(() => $("#impPw") && $("#impPw").focus(), 50);
}
async function openIncoming() {
  try {
    const pack = await X.parse(incoming.text, incoming.password);
    if (!X.isBackup(pack)) {
      throw new X.ExchangeError("이 앱이 만든 백업 파일이 아닙니다. 사내 정보 보호를 위해 PC의 메모는 휴대폰으로 가져올 수 없습니다.");
    }
    const items = await X.items(pack);
    const known = await knownUids();
    const seen = new Set();
    for (const it of items) {
      const uid = await X.contentUid(it.created_at, it.body);
      it.dup = known.has(uid) || seen.has(uid);
      seen.add(uid);
    }
    const have = new Set(S.events.map(eventKey));
    const schedules = packSchedules(pack).map((e) => ({ ...e, dup: have.has(eventKey(e)) }));
    Object.assign(incoming, { pack, items, schedules, error: "",
      picked: new Set(items.map((it, i) => (!it.dup && !it.empty ? i : -1)).filter((i) => i >= 0)) });
  } catch (err) {
    incoming.pack = null;
    incoming.error = err instanceof X.PasswordRequired ? "" : err.message;
  }
}
function paintImport() {
  const body = $("#importBody"), inc = incoming;
  let html = "";
  if (inc.sealed && !inc.pack && !/백업 파일이 아닙니다/.test(inc.error)) {
    html += `<div class="note">암호화된 백업입니다. 백업할 때 정한 비밀번호를 넣으세요.</div>
      <form id="impPwForm" class="row" style="margin-top:10px;flex-wrap:nowrap">
        <input id="impPw" type="password" autocomplete="off" aria-label="비밀번호" placeholder="비밀번호" style="flex:1" />
        <button class="btn primary" type="submit">열기</button></form>`;
  }
  if (inc.error) html += `<div class="err" style="margin-top:10px">${esc(inc.error)}</div>`;
  if (inc.pack) {
    const dups = inc.items.filter((it) => it.dup).length;
    html += `<div class="note">${esc(inc.label)} · 메모 ${inc.items.length}건${inc.pack.exported_at ? ` · ${esc(fullLabel(inc.pack.exported_at))} 백업` : ""}${dups ? ` · 이미 있는 ${dups}건은 빼 두었습니다` : ""}</div>`;
    if (inc.items.length) {
      html += `<div class="row" style="margin:10px 0 2px"><button class="btn small" type="button" id="impAll">모두 선택</button>
        <button class="btn small" type="button" id="impNone">선택 해제</button></div>`;
      html += inc.items.map((it, i) => `<label class="imp-item">
        <input type="checkbox" data-imp="${i}" ${inc.picked.has(i) ? "checked" : ""} ${it.empty ? "disabled" : ""} />
        <div style="min-width:0"><div class="t"><b>${esc(X.titleFrom(it.body))}</b></div>
          <div class="muted">${esc(String(it.created_at).slice(0, 10))}${it.tags.length ? " · " + it.tags.map((t) => "#" + esc(t)).join(" ") : ""}${it.dup ? ' · <span class="warn">이미 있음</span>' : ""}${it.empty ? " · 빈 메모" : ""}</div>
          <div class="muted t" style="font-size:13.5px">${esc(it.body.split("\n").slice(1).join(" ").slice(0, 120))}</div></div></label>`).join("");
    }
    if (inc.schedules.length) {
      const fresh = inc.schedules.filter((e) => !e.dup).length;
      html += `<label class="imp-item" style="border:0;margin-top:6px"><input type="checkbox" id="impEvents" ${inc.withEvents && fresh ? "checked" : ""} ${fresh ? "" : "disabled"} />
        <div>일정 ${inc.schedules.length}건도 되살리기${fresh < inc.schedules.length ? ` <span class="muted">(이미 있는 ${inc.schedules.length - fresh}건 제외)</span>` : ""}</div></label>`;
    }
  }
  body.innerHTML = html;
  const form = $("#impPwForm");
  if (form) form.onsubmit = async (e) => {
    e.preventDefault();
    inc.password = $("#impPw").value;
    if (!inc.password) return;
    form.querySelector("button").disabled = true;
    form.querySelector("button").textContent = "여는 중…";
    await openIncoming();
    paintImport();
    if (!inc.pack) setTimeout(() => $("#impPw") && $("#impPw").focus(), 30);
  };
  $$("[data-imp]", body).forEach((c) => { c.onchange = () => { c.checked ? inc.picked.add(+c.dataset.imp) : inc.picked.delete(+c.dataset.imp); paintImportButton(); }; });
  if ($("#impAll")) $("#impAll").onclick = () => { inc.items.forEach((it, i) => { if (!it.empty) inc.picked.add(i); }); paintImport(); };
  if ($("#impNone")) $("#impNone").onclick = () => { inc.picked.clear(); paintImport(); };
  if ($("#impEvents")) $("#impEvents").onchange = (e) => { inc.withEvents = e.target.checked; paintImportButton(); };
  paintImportButton();
}
function importCounts() {
  const inc = incoming;
  const events = inc.pack && inc.withEvents ? inc.schedules.filter((e) => !e.dup).length : 0;
  return { memos: inc.pack ? inc.picked.size : 0, events };
}
function paintImportButton() {
  const { memos, events } = importCounts();
  const b = $("#importRun");
  b.disabled = !(memos || events);
  b.textContent = memos || events
    ? [memos ? `메모 ${memos}건` : "", events ? `일정 ${events}건` : ""].filter(Boolean).join(" · ") + " 되살리기"
    : "되살리기";
}
$("#importRun").onclick = async () => {
  const inc = incoming;
  if (!inc || !inc.pack) return;
  const { events } = importCounts();
  const memos = [...inc.picked].sort((a, b) => a - b).map((i) => inc.items[i]).map((it) => ({
    body: it.body, created_at: it.created_at, updated_at: it.updated_at, tags: it.tags,
    origin: it.origin === "ai" ? "ai" : "mobile",
  }));
  const evs = events ? inc.schedules.filter((e) => !e.dup).map(({ dup, ...e }) => e) : [];
  $("#importRun").disabled = true;
  try {
    if (memos.length) {
      const keys = await idb.putMany("memos", memos);
      memos.forEach((m, i) => { m.id = keys[i]; S.memos.push(m); });
      sortMemos();
    }
    if (evs.length) {
      const keys = await idb.putMany("events", evs);
      evs.forEach((e, i) => { e.id = keys[i]; S.events.push(e); });
    }
    askPersist();
    closeSheet(importDlg, { force: true });
    renderAllMemos();
    renderCal();
    toast([memos.length ? `메모 ${memos.length}건` : "", evs.length ? `일정 ${evs.length}건` : ""].filter(Boolean).join(" · ") + "을 되살렸습니다");
    if (memos.length) switchTab("memos");
  } catch (err) {
    toast("되살리지 못했습니다: " + err.message, 5000);
    paintImportButton();
  }
};
importDlg.onClosed = () => { incoming = null; };

// ================================================================ AI 로 정리
const PRESETS = [
  ["핵심 요약", "핵심 내용을 5줄 이내로 요약해줘."],
  ["할 일 정리", "해야 할 일을 마감일·담당자와 함께 목록으로 정리해줘. 끝난 일은 빼줘."],
  ["결정·미결", "결정된 사항과 아직 정해지지 않은 문제를 나눠서 정리해줘."],
  ["보고서 초안", "상사에게 보고할 짧은 보고서 초안으로 정리해줘. 배경, 진행 상황, 이슈, 다음 할 일 순서로."],
];
const SYSTEM = "당신은 사용자의 업무 메모를 정리하는 비서입니다. 주어진 메모에 있는 내용만 근거로 한국어로 답하세요. " +
  "메모에 없는 사실은 지어내지 말고, 근거가 된 메모 번호를 [1]처럼 붙이세요. 읽기 쉬운 문장과 목록으로 짧게 쓰세요.";
const MAX_AI_CHARS = 120000;

function aiConfig(source = S.ai) {
  const provider = PROVIDERS[source.provider] ? source.provider : "gemini";
  return { provider, key: (source.keys || {})[provider] || "", model: (source.models || {})[provider] || PROVIDERS[provider].models[0] };
}

function aiError(status, message) {
  const m = String(message || "").slice(0, 300);
  if (status === 400 && /api key|API_KEY/i.test(m)) return `API 키가 맞지 않습니다 (${m})`;
  if (status === 401 || status === 403) return `API 키가 맞지 않거나 권한이 없습니다 (${m})`;
  if (status === 404) return `모델 이름을 확인하세요 (${m})`;
  if (status === 429) return `요청 한도를 넘었거나 크레딧이 부족합니다 — 잠시 뒤 다시 시도하세요 (${m})`;
  return `AI 요청 실패 (${status}): ${m}`;
}

async function callAi(cfg, system, prompt, signal) {
  if (!cfg.key) throw new Error("API 키가 없습니다 — 설정에서 넣어주세요.");
  let url, body;
  const headers = { "content-type": "application/json" };
  if (cfg.provider === "gemini") {
    url = `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(cfg.model)}:generateContent`;
    headers["x-goog-api-key"] = cfg.key;
    body = { systemInstruction: { parts: [{ text: system }] }, contents: [{ role: "user", parts: [{ text: prompt }] }] };
  } else if (cfg.provider === "openai") {
    url = "https://api.openai.com/v1/chat/completions";
    headers.authorization = `Bearer ${cfg.key}`;
    body = { model: cfg.model, messages: [{ role: "system", content: system }, { role: "user", content: prompt }] };
  } else {
    url = "https://api.anthropic.com/v1/messages";
    headers["x-api-key"] = cfg.key;
    headers["anthropic-version"] = "2023-06-01";
    headers["anthropic-dangerous-direct-browser-access"] = "true";
    body = { model: cfg.model, max_tokens: 8000, system, messages: [{ role: "user", content: prompt }] };
  }
  // abort(이유) 로 멈추면 fetch 는 AbortError 가 아니라 그 이유를 그대로 던진다 — 멈춘 건 하나로 맞춘다.
  const aborted = () => Object.assign(new Error("멈췄습니다."), { name: "AbortError" });
  let res;
  try { res = await fetch(url, { method: "POST", headers, body: JSON.stringify(body), signal }); }
  catch (err) {
    if (signal && signal.aborted) throw aborted();
    throw new Error(navigator.onLine === false ? "인터넷에 연결되어 있지 않습니다." : "AI 서버에 연결하지 못했습니다 — 인터넷 연결을 확인하세요.");
  }
  let data = null;
  try { data = await res.json(); } catch (_) { if (signal && signal.aborted) throw aborted(); }
  if (!res.ok) throw new Error(aiError(res.status, (data && data.error && (data.error.message || data.error.type)) || (data && data.message) || res.statusText));
  let text = "";
  if (cfg.provider === "gemini") {
    const c = data && data.candidates && data.candidates[0];
    text = ((c && c.content && c.content.parts) || []).filter((p) => !p.thought).map((p) => p.text || "").join("");
    if (!text.trim()) {
      const why = (data && data.promptFeedback && data.promptFeedback.blockReason) || (c && c.finishReason);
      throw new Error(why === "SAFETY" || why === "PROHIBITED_CONTENT" ? "AI가 안전 정책 때문에 답하지 않았습니다." : `AI가 빈 답을 보냈습니다${why ? ` (${why})` : ""}.`);
    }
  } else if (cfg.provider === "openai") {
    const msg = data && data.choices && data.choices[0] && data.choices[0].message;
    text = (msg && msg.content) || "";
    if (!text.trim()) throw new Error(msg && msg.refusal ? `AI가 답하지 않았습니다: ${msg.refusal}` : "AI가 빈 답을 보냈습니다.");
  } else {
    text = ((data && data.content) || []).filter((b) => b.type === "text").map((b) => b.text).join("");
    if (!text.trim()) throw new Error(data && data.stop_reason === "refusal" ? "AI가 답하지 않았습니다." : "AI가 빈 답을 보냈습니다.");
  }
  return text.trim();
}

function aiPrompt(ask, memos) {
  return `요청: ${ask}\n오늘: ${todayStr()}\n\n메모 ${memos.length}건:\n\n` + memos.map((m, i) =>
    `[${i + 1}] ${String(m.created_at).slice(0, 10)}${m.tags.length ? " · " + m.tags.map((t) => "#" + t).join(" ") : ""}\n${m.body}`).join("\n\n---\n\n");
}
// 굵게·제목 정도만 살려서 보인다. 복사·저장은 원문 그대로.
function lightMarkdown(text) {
  return esc(text)
    .replace(/^#{1,6}\s+(.+)$/gm, "<b>$1</b>")
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
}

const aiDlg = $("#aiDlg");
let aiJob = null;   // { ids, abort, result }
function openAi(ids) {
  if (!ids.length) { toast("정리할 메모를 고르세요"); return; }
  aiJob = { ids, abort: null, result: null };
  const cfg = aiConfig(), p = PROVIDERS[cfg.provider];
  $("#aiTitle").textContent = `메모 ${ids.length}건 AI로 정리`;
  $("#aiWho").innerHTML = cfg.key
    ? `<b>${esc(p.label)}</b> · ${esc(cfg.model)}로 보냅니다. 보내면 고른 메모가 ${esc(p.company)}로 전송됩니다.`
    : `AI 연결이 아직 없습니다. <button class="btn small" type="button" id="aiGoSettings">설정 열기</button>`;
  if ($("#aiGoSettings")) $("#aiGoSettings").onclick = () => { closeAll(); switchTab("settings"); $("#aiKey").focus(); };
  $("#aiPresets").innerHTML = PRESETS.map(([label], i) => `<button class="chip" type="button" data-preset="${i}" aria-pressed="${i === 0}">${esc(label)}</button>`).join("");
  $("#aiAsk").value = PRESETS[0][1];
  $("#aiResult").hidden = true;
  $("#aiState").textContent = "";
  $("#aiState").className = "muted";
  $("#aiRun").textContent = "보내기";
  $("#aiRun").disabled = !cfg.key;
  openSheet(aiDlg);
}
// 여러 시트를 한 번에 닫을 때는 뒤로 가기도 한 번에(history.go(-n)) 한다 — back() 을 연달아
// 부르면 브라우저가 하나로 합쳐 버려 기록 수가 어긋난다.
function closeAll() {
  const n = sheets.length;
  [...sheets].reverse().forEach((d) => closeSheet(d, { force: true, fromHistory: true }));
  if (n) { skipPop++; history.go(-n); }
}
$("#aiPresets").onclick = (e) => {
  const b = e.target.closest("[data-preset]");
  if (!b) return;
  $$("[data-preset]").forEach((x) => x.setAttribute("aria-pressed", x === b));
  $("#aiAsk").value = PRESETS[+b.dataset.preset][1];
};
$("#aiAsk").addEventListener("input", () => $$("[data-preset]").forEach((x) => x.setAttribute("aria-pressed", "false")));
aiDlg.onClosed = () => { if (aiJob && aiJob.abort) aiJob.abort.abort(); aiJob = null; };
$("#aiRun").onclick = async () => {
  const job = aiJob;
  if (!job) return;
  if (job.abort) { job.abort.abort(); return; }
  const ask = $("#aiAsk").value.trim();
  if (!ask) { $("#aiAsk").focus(); return; }
  const memos = job.ids.map(findMemo).filter(Boolean);
  const prompt = aiPrompt(ask, memos);
  const state = $("#aiState");
  if (prompt.length > MAX_AI_CHARS) {
    state.className = "err";
    state.textContent = `메모가 너무 깁니다 (${prompt.length.toLocaleString()}자) — ${MAX_AI_CHARS.toLocaleString()}자 이하로 줄여서 고르세요.`;
    return;
  }
  job.abort = new AbortController();
  const timeout = setTimeout(() => job.abort && job.abort.abort("timeout"), 180000);
  const started = Date.now();
  const tick = setInterval(() => { state.textContent = `정리하는 중… ${Math.round((Date.now() - started) / 1000)}초`; }, 1000);
  state.className = "muted";
  state.textContent = "정리하는 중…";
  $("#aiRun").textContent = "멈추기";
  $("#aiResult").hidden = true;
  try {
    const text = await callAi(aiConfig(), SYSTEM, prompt, job.abort.signal);
    if (aiJob !== job) return;
    job.result = { ask, text };
    $("#aiText").innerHTML = lightMarkdown(text);
    $("#aiResult").hidden = false;
    state.textContent = `${Math.round((Date.now() - started) / 1000)}초 걸렸습니다`;
  } catch (err) {
    if (aiJob !== job) return;
    state.className = err.name === "AbortError" ? "muted" : "err";
    state.textContent = err.name === "AbortError" ? (job.abort.signal.reason === "timeout" ? "응답이 너무 늦어 멈췄습니다." : "멈췄습니다.") : err.message;
  } finally {
    clearTimeout(timeout);
    clearInterval(tick);
    if (aiJob === job) { job.abort = null; $("#aiRun").textContent = "다시 보내기"; }
  }
};
$("#aiCopy").onclick = async () => {
  if (!aiJob || !aiJob.result) return;
  try { await navigator.clipboard.writeText(aiJob.result.text); toast("복사했습니다"); }
  catch (_) { toast("복사하지 못했습니다 — 글을 길게 눌러 복사하세요"); }
};
$("#aiKeep").onclick = async () => {
  if (!aiJob || !aiJob.result) return;
  const { ask, text } = aiJob.result;
  await addMemo(`AI 정리: ${ask}\n\n${text}`, { origin: "ai", tags: ["AI정리"] });
  renderAllMemos();
  toast("메모로 저장했습니다");
  $("#aiKeep").disabled = true;
  setTimeout(() => { $("#aiKeep").disabled = false; }, 1500);
};

// ================================================================ 일정
function renderCal() {
  if (!S.month) return;
  const y = S.month.getFullYear(), mo = S.month.getMonth();
  $("#calTitle").textContent = `${y}년 ${mo + 1}월`;
  const today = todayStr();
  const byDay = new Map();
  S.events.forEach((e) => { if (!byDay.has(e.date)) byDay.set(e.date, []); byDay.get(e.date).push(e); });
  const start = new Date(y, mo, 1 - new Date(y, mo, 1).getDay());
  let html = DOW.map((d) => `<div class="dow" role="columnheader">${d}</div>`).join("");
  for (let i = 0; i < 42; i++) {
    const d = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i), key = ymd(d);
    const evs = byDay.get(key) || [];
    const cls = ["day", d.getMonth() !== mo ? "out" : "", key === today ? "today" : "", key === S.day ? "sel" : "",
      d.getDay() === 0 ? "sun" : "", d.getDay() === 6 ? "sat" : ""].filter(Boolean).join(" ");
    html += `<button class="${cls}" type="button" data-day="${key}" aria-label="${dayLabel(key)}${evs.length ? `, 일정 ${evs.length}건` : ""}"${key === S.day ? ' aria-current="date"' : ""}>
      <span class="n">${d.getDate()}</span><span class="dots">${evs.slice(0, 3).map((e) => `<i class="${e.done ? "done" : ""}"></i>`).join("")}</span></button>`;
  }
  $("#cal").innerHTML = html;
  $("#dayTitle").textContent = dayLabel(S.day) + (S.day === today ? " · 오늘" : "");
  const sortEv = (a, b) => a.date.localeCompare(b.date) || (a.time || "").localeCompare(b.time || "") || a.id - b.id;
  const day = (byDay.get(S.day) || []).slice().sort(sortEv);
  $("#dayList").innerHTML = day.length ? day.map((e) => eventRow(e, false)).join("") : '<div class="empty" style="padding:16px">일정이 없습니다.</div>';
  const upcoming = S.events.filter((e) => e.date >= today && !e.done).sort(sortEv).slice(0, 12);
  $("#upcoming").innerHTML = upcoming.length ? upcoming.map((e) => eventRow(e, true)).join("") : '<div class="empty" style="padding:16px">남은 일정이 없습니다.</div>';
  const overdue = S.events.filter((e) => e.date < today && !e.done).sort(sortEv);
  $("#overdueBox").hidden = !overdue.length;
  $("#overdue").innerHTML = overdue.map((e) => eventRow(e, true)).join("");
}
function eventRow(e, withDate) {
  const when = [withDate ? dayLabel(e.date) : "", e.time || (withDate ? "" : "종일")].filter(Boolean).join(" ");
  return `<div class="event${e.done ? " done" : ""}" data-ev="${e.id}">
    <input type="checkbox" aria-label="${esc(e.title)} 끝냄" ${e.done ? "checked" : ""} />
    <button class="what" type="button"><div class="t">${esc(e.title)}</div>
      <div class="when">${esc(when)}${e.tags.length ? " · " + e.tags.map((t) => "#" + esc(t)).join(" ") : ""}${e.note ? " · 메모 있음" : ""}</div></button></div>`;
}
$("#cal").onclick = (e) => {
  const b = e.target.closest("[data-day]");
  if (!b) return;
  S.day = b.dataset.day;
  const d = parseYmd(S.day);
  if (d.getMonth() !== S.month.getMonth() || d.getFullYear() !== S.month.getFullYear()) S.month = new Date(d.getFullYear(), d.getMonth(), 1);
  renderCal();
};
function shiftMonth(step) {
  S.month = new Date(S.month.getFullYear(), S.month.getMonth() + step, 1);
  const now = new Date();
  S.day = S.month.getFullYear() === now.getFullYear() && S.month.getMonth() === now.getMonth() ? todayStr() : ymd(S.month);
  renderCal();
}
$("#calPrev").onclick = () => shiftMonth(-1);
$("#calNext").onclick = () => shiftMonth(1);
$("#calToday").onclick = () => { const n = new Date(); S.month = new Date(n.getFullYear(), n.getMonth(), 1); S.day = todayStr(); renderCal(); };
["#dayList", "#upcoming", "#overdue"].forEach((sel) => {
  $(sel).addEventListener("change", async (e) => {
    const row = e.target.closest("[data-ev]");
    if (!row || e.target.type !== "checkbox") return;
    const ev = S.events.find((x) => x.id === +row.dataset.ev);
    if (!ev) return;
    const next = { ...ev, done: e.target.checked, updated_at: X.nowIso() };
    try { await idb.put("events", next); Object.assign(ev, next); }
    catch (err) { toast("저장하지 못했습니다: " + err.message, 5000); }
    renderCal();
  });
  $(sel).addEventListener("click", (e) => {
    const what = e.target.closest(".what");
    if (what) openEvent(+what.closest("[data-ev]").dataset.ev);
  });
});

const eventDlg = $("#eventDlg");
let editingEvent = null;
function openEvent(id, date) {
  const ev = id ? S.events.find((x) => x.id === id) : null;
  editingEvent = ev ? ev.id : null;
  $("#eventTitle").textContent = ev ? "일정 고치기" : "일정 추가";
  $("#evTitle").value = ev ? ev.title : "";
  $("#evDate").value = ev ? ev.date : (date || todayStr());
  $("#evTime").value = ev ? ev.time || "" : "";
  $("#evTags").value = ev ? ev.tags.map((t) => "#" + t).join(" ") : "";
  $("#evNote").value = ev ? ev.note || "" : "";
  $("#evErr").textContent = "";
  $("#evDel").hidden = !ev;
  openSheet(eventDlg);
  if (!ev) setTimeout(() => $("#evTitle").focus(), 60);
}
$("#addEvent").onclick = () => openEvent(null, S.day);
$("#evSave").onclick = async () => {
  const title = $("#evTitle").value.trim(), date = $("#evDate").value;
  if (!title) { $("#evErr").textContent = "할 일·일정을 적어주세요."; $("#evTitle").focus(); return; }
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) { $("#evErr").textContent = "날짜를 고르세요."; $("#evDate").focus(); return; }
  const old = editingEvent ? S.events.find((x) => x.id === editingEvent) : null;
  const now = X.nowIso();
  const ev = {
    ...(old || { done: false, created_at: now }),
    title: title.slice(0, 300), date, time: $("#evTime").value || "", tags: parseTags($("#evTags").value),
    note: $("#evNote").value.trim(), updated_at: now,
  };
  try {
    ev.id = await idb.put("events", ev);
    if (old) Object.assign(old, ev); else S.events.push(ev);
    S.day = date;
    S.month = new Date(parseYmd(date).getFullYear(), parseYmd(date).getMonth(), 1);
    askPersist();
    closeSheet(eventDlg, { force: true });
    renderCal();
    toast(old ? "고쳤습니다" : `${dayLabel(date)}에 넣었습니다`);
  } catch (err) { $("#evErr").textContent = "저장하지 못했습니다: " + err.message; }
};
$("#evDel").onclick = async () => {
  if (!editingEvent || !confirm("이 일정을 지울까요?")) return;
  await idb.del("events", editingEvent);
  S.events = S.events.filter((x) => x.id !== editingEvent);
  closeSheet(eventDlg, { force: true });
  renderCal();
  toast("지웠습니다");
};
eventDlg.onClosed = () => { editingEvent = null; };

// ================================================================ 설정
let aiDraft = null;   // 저장 전까지 서비스별 키·모델을 따로 들고 있는다
function paintAiSettings() {
  aiDraft = JSON.parse(JSON.stringify(S.ai));
  aiDraft.keys = aiDraft.keys || {};
  aiDraft.models = aiDraft.models || {};
  $("#aiProvider").value = aiConfig(aiDraft).provider;
  fillAiFields();
}
function fillAiFields() {
  const cfg = aiConfig(aiDraft), p = PROVIDERS[cfg.provider];
  $("#aiKey").value = cfg.key;
  $("#aiModel").value = cfg.model;
  $("#aiModels").innerHTML = p.models.map((m) => `<option value="${esc(m)}"></option>`).join("");
  $("#aiKeyHelp").textContent = p.help;
  $("#aiOut").textContent = "";
  $("#aiOut").className = "note";
}
function stashAiFields() {
  const p = aiDraft.provider;
  aiDraft.keys[p] = $("#aiKey").value.trim();
  aiDraft.models[p] = $("#aiModel").value.trim() || PROVIDERS[p].models[0];
}
$("#aiProvider").onchange = () => { stashAiFields(); aiDraft.provider = $("#aiProvider").value; fillAiFields(); };
$("#aiSave").onclick = async () => {
  stashAiFields();
  S.ai = JSON.parse(JSON.stringify(aiDraft));
  await kv.set("ai", S.ai);
  $("#aiOut").textContent = `저장했습니다 — AI로 정리는 ${PROVIDERS[S.ai.provider].label}로 보냅니다.`;
};
$("#aiTest").onclick = async () => {
  stashAiFields();
  const out = $("#aiOut");
  out.className = "note";
  out.textContent = "확인하는 중…";
  $("#aiTest").disabled = true;
  try {
    const reply = await callAi(aiConfig(aiDraft), "짧게 답하세요.", "연결 확인입니다. OK 라고만 답하세요.");
    out.textContent = `연결됐습니다 — 답: ${reply.slice(0, 40)}. 저장을 눌러야 적용됩니다.`;
  } catch (err) { out.className = "note err"; out.textContent = err.message; }
  finally { $("#aiTest").disabled = false; }
};

let installPrompt = null;
window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  installPrompt = e;
  paintInstallBanner();
  if (S.tab === "settings") paintAppInfo();
});
window.addEventListener("appinstalled", () => { installPrompt = null; local.set("memoinall.installHidden", "1"); paintInstallBanner(); });
const standalone = () => matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
const isIos = () => /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
// 카카오톡·네이버·인스타그램 등 앱 안의 브라우저는 홈 화면 설치를 지원하지 않는다.
const inAppBrowser = () => /KAKAOTALK|NAVER\(inapp|Instagram|FBAN|FBAV|\bLine\//i.test(navigator.userAgent);

// 브라우저로 열었을 때 맨 위에 설치 방법을 보여준다. 설치한 앱으로 열었거나 닫았으면 숨긴다.
function paintInstallBanner() {
  const box = $("#installBanner");
  if (standalone() || local.get("memoinall.installHidden") === "1") { box.hidden = true; return; }
  box.hidden = false;
  const inApp = inAppBrowser();
  $("#installNow").hidden = !installPrompt || inApp;
  $("#installHow").innerHTML = inApp
    ? "지금은 다른 앱 안의 브라우저로 열려 있어 설치할 수 없습니다. 오른쪽 위 메뉴에서 <b>다른 브라우저로 열기</b>를 누르세요."
    : installPrompt ? "아래 버튼을 누르면 바로 설치됩니다. 설치하면 인터넷 없이도 열립니다."
    : isIos() ? "Safari 아래쪽의 공유 버튼 → <b>홈 화면에 추가</b>를 누르세요."
    : "브라우저 메뉴(⋮) → <b>앱 설치</b> 또는 <b>홈 화면에 추가</b>를 누르세요.";
}
$("#installNow").onclick = async () => {
  if (!installPrompt) return;
  const prompt = installPrompt;
  prompt.prompt();
  let choice = null;
  try { choice = await prompt.userChoice; } catch (_) { /* 브라우저가 알려주지 않으면 그대로 둔다 */ }
  installPrompt = null;
  if (choice && choice.outcome === "accepted") {
    local.set("memoinall.installHidden", "1");
    toast("설치했습니다 — 홈 화면의 memoinall 아이콘으로 여세요", 5000);
  }
  paintInstallBanner();
};
$("#installClose").onclick = () => { local.set("memoinall.installHidden", "1"); paintInstallBanner(); };

async function paintAppInfo() {
  let storage = "확인할 수 없음", persisted = null;
  try {
    if (navigator.storage && navigator.storage.estimate) {
      const est = await navigator.storage.estimate();
      storage = `${(est.usage / 1048576).toFixed(1)}MB 사용`;
    }
    if (navigator.storage && navigator.storage.persisted) persisted = await navigator.storage.persisted();
  } catch (_) { /* 지원 안 함 */ }
  const install = standalone() ? "홈 화면 앱으로 실행 중"
    : installPrompt ? '<button class="btn small primary" type="button" id="installBtn">홈 화면에 설치</button>'
    : isIos() ? "Safari의 공유 버튼 → <b>홈 화면에 추가</b>" : "브라우저 메뉴 → <b>앱 설치</b> 또는 <b>홈 화면에 추가</b>";
  $("#appInfo").innerHTML = `
    <div class="stat"><span>메모</span><b>${S.memos.length}건</b></div>
    <div class="stat"><span>일정</span><b>${S.events.length}건</b></div>
    <div class="stat"><span>저장 공간</span><span>${esc(storage)}</span></div>
    <div class="stat"><span>자동 삭제 방지</span><span>${persisted === true ? "켜짐" : persisted === false ? '<button class="btn small" type="button" id="persistBtn">켜기</button>' : "확인할 수 없음"}</span></div>
    <div class="stat"><span>설치</span><span style="text-align:right">${install}</span></div>
    <div class="stat"><span>버전</span><span class="muted">${esc(APP_VERSION)}</span></div>
    <div class="note" style="margin-top:8px">이 앱은 PC 앱과 연결되지 않습니다. 메모는 이 휴대폰에만 있고, 주고받기는 QR·파일로만 합니다.</div>`;
  if ($("#installBtn")) $("#installBtn").onclick = async () => { installPrompt.prompt(); await installPrompt.userChoice; installPrompt = null; paintAppInfo(); };
  if ($("#persistBtn")) $("#persistBtn").onclick = async () => {
    const ok = await navigator.storage.persist();
    toast(ok ? "브라우저가 이 앱의 데이터를 임의로 지우지 않습니다" : "브라우저가 허락하지 않았습니다 — 홈 화면에 설치하면 대개 허락됩니다", 4000);
    paintAppInfo();
  };
}
let persistAsked = false;
async function askPersist() {
  if (persistAsked || !navigator.storage || !navigator.storage.persist) return;
  persistAsked = true;
  try { if (!(await navigator.storage.persisted())) await navigator.storage.persist(); } catch (_) { /* 선택 사항 */ }
}
$("#wipe").onclick = async () => {
  if (!confirm(`이 휴대폰의 메모 ${S.memos.length}건과 일정 ${S.events.length}건을 모두 지울까요?`)) return;
  if (!confirm("정말 지울까요? 백업 파일이 없으면 되살릴 수 없습니다.")) return;
  await Promise.all([idb.clear("memos"), idb.clear("events")]);
  await kv.set("lastBackup", null);
  local.set("memoinall.draft", null);
  S.memos = []; S.events = []; S.lastBackup = null;
  renderAllMemos(); renderCal(); paintAppInfo(); paintBackupInfo();
  toast("모두 지웠습니다");
};

// ================================================================ 탭
const TITLES = { memos: "메모", cal: "일정", share: "보내기·백업", settings: "설정" };
function switchTab(name) {
  if (name !== "memos" && S.selecting) stopSelecting();
  S.tab = name;
  $$("main > section").forEach((s) => { s.hidden = s.dataset.tab !== name; });
  $$("nav.tabs [data-go]").forEach((b) => b.setAttribute("aria-selected", b.dataset.go === name));
  $("#pageTitle").textContent = TITLES[name];
  $("#pageSub").textContent = name === "memos" && S.memos.length ? `${S.memos.length}건` : "";
  if (name === "cal") renderCal();
  if (name === "share") paintBackupInfo();
  if (name === "settings") { paintAiSettings(); paintAppInfo(); }
  window.scrollTo(0, 0);
}
$$("nav.tabs [data-go]").forEach((b) => { b.onclick = () => switchTab(b.dataset.go); });

function paintOnline() { $("#offline").hidden = navigator.onLine !== false; }
window.addEventListener("online", paintOnline);
window.addEventListener("offline", paintOnline);

// ================================================================ 시작
async function init() {
  const now = new Date();
  S.month = new Date(now.getFullYear(), now.getMonth(), 1);
  S.day = todayStr();
  try {
    const [memos, events, ai, lastBackup] = await Promise.all([idb.all("memos"), idb.all("events"), kv.get("ai"), kv.get("lastBackup")]);
    S.memos = memos.map(fixMemo);
    S.events = events.map((e) => ({ ...e, tags: Array.isArray(e.tags) ? e.tags : [] }));
    if (ai) S.ai = { provider: ai.provider || "gemini", keys: ai.keys || {}, models: ai.models || {} };
    S.lastBackup = lastBackup;
  } catch (err) {
    $("#memoList").innerHTML = `<div class="empty err">저장소를 열지 못했습니다: ${esc(err.message)}<br>개인 정보 보호(시크릿) 모드에서는 저장되지 않을 수 있습니다.</div>`;
    return;
  }
  sortMemos();
  const draft = local.get("memoinall.draft");
  if (draft) capture.value = draft;
  draftNote(draft ? "작성 중이던 내용을 되살렸습니다" : "");
  captureChanged();
  renderAllMemos();
  paintOnline();
  paintBackupInfo();
  paintInstallBanner();
}

if ("serviceWorker" in navigator && (location.protocol === "https:" || ["localhost", "127.0.0.1"].includes(location.hostname))) {
  let controlled = !!navigator.serviceWorker.controller;
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (controlled) toast("새 버전을 받았습니다 — 앱을 다시 열면 적용됩니다", 5000);
    controlled = true;
  });
  window.addEventListener("load", () => { navigator.serviceWorker.register("sw.js").catch(() => {}); });
}

init();
