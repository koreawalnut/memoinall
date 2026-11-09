// 엑셀 폴더 취합: 폴더 선택 → 취합 확인 → 결과 활용.
// 확정 전 작업은 서버 메모리에만 있다. 다른 화면에 다녀와도 이 상태로 이어서 보여 준다.
const col = {
  step: 1, includeSub: false, target: null, job: null, title: "", tags: "",
  rules: { files: {}, sheets: {} }, preview: null, previewVersion: 0, upload: null,
  busy: false, error: "", notices: [], issuesOnly: false, saved: null, view: null, openAdjusted: new Set(),
};
const COL_EXCEL = /\.(xlsx|xlsm|xls)$/i;
const COL_LIMITS = { files: 100, bytes: 200000000, fileBytes: 20000000 };

// Decimal 문자열에 쉼표만 넣는다. Number 로 바꾸면 큰 금액·소수에서 값이 달라질 수 있다.
function colNum(value) {
  if (value === null || value === undefined || value === "") return "-";
  const text = String(value), sign = text.startsWith("-") ? "-" : "";
  const [whole, frac] = text.replace(/^[+-]/, "").split(".");
  return sign + whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (frac ? "." + frac : "");
}
function colSheetRule(key) { return (col.rules.sheets[key] ||= {}); }
function colReset() {
  Object.assign(col, { job: null, preview: null, rules: { files: {}, sheets: {} }, notices: [], error: "", issuesOnly: false, busy: false, openAdjusted: new Set() });
}
async function colDiscardJob() {
  const id = col.job;
  colReset();
  if (id) await api(`/api/collections/jobs/${id}/cancel`, { method: "POST" }).catch(() => {});
}

function openCollect() {
  if (!col.job && !col.upload) { col.step = 1; col.view = null; }
  return goTab("collect");
}

async function renderCollect(version = renderVersion) {
  if (col.step === 3 && col.view) return renderCollectResult(version);
  if (col.step === 2 && col.preview) { paintCollectReview(); return; }
  col.step = 1;
  const saved = await api("/api/collections?limit=30").catch(() => ({ items: [] }));
  if (version !== renderVersion) return;
  col.saved = saved;
  paintCollectPick();
}

function colSteps() {
  const labels = ["폴더 선택", "취합 확인", "결과 활용"];
  return `<div class="row"><a href="#" id="colBack" class="status">← 가져오기·공유</a></div>
    <ol class="col-steps" aria-label="진행 단계">${labels.map((label, i) =>
      `<li ${col.step === i + 1 ? 'aria-current="step"' : ""}>${i + 1}. ${label}</li>`).join("")}</ol>`;
}
function bindColSteps() {
  $("#colBack").onclick = (e) => { e.preventDefault(); imp.section = "import"; goTab("import"); };
}

// ---------------------------------------------------------------- 1. 폴더 선택
function colProgressHTML() {
  const u = col.upload;
  if (!u) return col.error ? `<div class="col-warn">${esc(col.error)}</div>` : "";
  return `<div class="col-progress"><b>${u.total}개 중 ${u.done}개 읽는 중</b>${u.current ? ` · ${esc(u.current)}` : ""}
    <div class="bar"><i style="width:${u.total ? Math.round(u.done / u.total * 100) : 0}%"></i></div>
    <div class="row" style="margin-top:8px"><span class="note">읽는 동안 다른 화면에서 메모를 계속 쓸 수 있습니다.</span><span class="spacer"></span>
    <button class="btn" id="colCancel" ${u.cancelled ? "disabled" : ""}>${u.cancelled ? "취소하는 중…" : "취소"}</button></div></div>`;
}
function colPaintProgress() {
  const host = $("#colProgress");
  if (!host) return;
  host.innerHTML = colProgressHTML();
  if ($("#colCancel")) $("#colCancel").onclick = () => { if (col.upload) { col.upload.cancelled = true; colPaintProgress(); } };
  ["#colPickFolder", "#colPickFiles", "#colSub"].forEach((id) => { if ($(id)) $(id).disabled = !!col.upload; });
}
function colSavedHTML() {
  const items = col.saved?.items || [];
  if (!items.length) return '<div class="status">아직 저장한 취합 자료가 없습니다. 저장하면 메모 목록에도 대표 메모 하나로 보입니다.</div>';
  return items.map((c) => `<button class="btn ghost col-saved" data-col-open="${c.id}"><b>${esc(c.title)}</b>
    <span class="meta">파일 ${c.files}개 · ${colNum(c.rows)}행 · ${c.version}번째 취합 · ${esc(fmt(c.updated_at))}</span></button>`).join("");
}

function paintCollectPick() {
  $("#view").innerHTML = `${colSteps()}
    ${col.target ? `<div class="panel col-target"><b>‘${esc(col.target.title)}’ 다시 취합</b>
      <div class="note">같은 폴더를 선택하세요. 이전에 정한 시트·항목 연결을 먼저 적용하고, 달라진 부분은 다시 확인합니다. 이전 버전과 그 AI 결과는 그대로 남습니다.</div>
      <div class="row" style="margin:8px 0 0"><button class="btn ghost" id="colTargetCancel">새 자료로 취합하기</button></div></div>` : ""}
    <div class="panel col-pick">
      <h3>부서에서 받은 파일을 한 폴더에 모아 주세요</h3>
      <div class="desc">같은 항목을 가진 엑셀(.xlsx·.xlsm·.xls)을 한 표로 모읍니다. 열 순서가 달라도 괜찮고, 항목 이름이 다르면 연결할 수 있어요. 원본 파일은 바꾸지 않습니다.</div>
      <button class="btn primary" id="colPickFolder">폴더 선택</button>
      <input type="file" id="colFolder" webkitdirectory multiple hidden />
      <input type="file" id="colFiles" multiple accept=".xlsx,.xlsm,.xls" hidden />
      <details class="capture-options" style="margin-top:12px"><summary>상세 옵션</summary>
        <label class="col-check" style="margin-top:8px"><input type="checkbox" id="colSub" ${col.includeSub ? "checked" : ""} /> 하위 폴더도 포함</label>
        <div class="note">폴더를 고를 수 없는 환경이면 <a href="#" id="colPickFiles">파일 여러 개 선택</a>으로 고르세요.</div>
        <div class="note">한 번에 파일 100개 · 합계 200MB · 100만 셀까지, 파일당 20MB까지 취합합니다. 엑셀 임시 파일(~$)은 제외합니다.</div>
      </details>
      <div id="colProgress" role="status" aria-live="polite"></div>
    </div>
    <div class="panel"><h3>저장한 취합 자료</h3><div id="colSaved">${colSavedHTML()}</div></div>`;
  bindColSteps();
  $("#colPickFolder").onclick = () => $("#colFolder").click();
  $("#colPickFiles").onclick = (e) => { e.preventDefault(); if (!col.upload) $("#colFiles").click(); };
  $("#colFolder").onchange = (e) => { const list = [...e.target.files]; e.target.value = ""; colChoose(list, true); };
  $("#colFiles").onchange = (e) => { const list = [...e.target.files]; e.target.value = ""; colChoose(list, false); };
  $("#colSub").onchange = (e) => { col.includeSub = e.target.checked; };
  if ($("#colTargetCancel")) $("#colTargetCancel").onclick = () => { col.target = null; paintCollectPick(); };
  document.querySelectorAll("[data-col-open]").forEach((b) => { b.onclick = () => colOpen(+b.dataset.colOpen); });
  colPaintProgress();
}

async function colChoose(list, fromFolder) {
  if (col.upload || col.busy || !list.length) return;
  if (col.job) await colDiscardJob();
  const relative = (f) => {
    const path = (f.webkitRelativePath || f.name).replace(/\\/g, "/");
    return fromFolder && f.webkitRelativePath ? path.split("/").slice(1).join("/") : path;
  };
  const root = fromFolder ? (list[0].webkitRelativePath || "").split("/")[0] : "";
  const picked = [], skipped = [];
  let temp = 0, other = 0, nested = 0;
  for (const file of list) {
    const path = relative(file), name = path.split("/").pop();
    if (!COL_EXCEL.test(name)) { other++; continue; }
    if (name.startsWith("~$")) { temp++; continue; }
    if (fromFolder && path.includes("/") && !col.includeSub) { nested++; continue; }
    if (file.size > COL_LIMITS.fileBytes) { skipped.push({ path, reason: "20MB 초과 · 파일을 나눠 저장하세요" }); continue; }
    picked.push({ file, path });
  }
  col.notices = [
    temp ? `엑셀 임시 파일(~$) ${temp}개는 제외했습니다.` : "",
    other ? `엑셀이 아닌 파일 ${other}개는 찾지 않았습니다.` : "",
    nested ? `하위 폴더의 엑셀 ${nested}개는 제외했습니다. 상세 옵션에서 ‘하위 폴더도 포함’을 켜면 함께 취합합니다.` : "",
  ].filter(Boolean);
  const bytes = picked.reduce((sum, p) => sum + p.file.size, 0);
  col.error = !picked.length && !skipped.length ? ["선택한 곳에 취합할 엑셀 파일이 없습니다.", ...col.notices].join(" ")
    : picked.length + skipped.length > COL_LIMITS.files ? `엑셀 파일이 ${picked.length + skipped.length}개입니다. 한 번에 100개까지 취합할 수 있으니 폴더를 나눠 주세요.`
    : bytes > COL_LIMITS.bytes ? "선택한 파일이 200MB를 넘습니다. 폴더를 나눠 취합해 주세요." : "";
  if (col.error) { colPaintProgress(); return; }
  const today = new Date();
  col.title = col.target?.title || root || `엑셀 취합 ${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
  col.rules = col.target ? JSON.parse(JSON.stringify(col.target.rules || {})) : { files: {}, sheets: {} };
  col.rules.files ||= {}; col.rules.sheets ||= {};
  col.upload = { done: 0, total: picked.length, current: "", cancelled: false };
  colPaintProgress();
  let jobId = null;
  try {
    const job = await api("/api/collections/jobs", { method: "POST", body: { title: col.title, collection_id: col.target?.id ?? null, skipped } });
    jobId = col.job = job.id;
    for (const item of picked) {
      if (col.upload.cancelled) break;
      col.upload.current = item.path; colPaintProgress();
      try {
        await api(`/api/collections/jobs/${job.id}/files?` + new URLSearchParams({ path: item.path }), {
          method: "POST", headers: { "Content-Type": "application/octet-stream" }, rawBody: item.file,
        });
      } catch (e) {
        if (/작업을 찾을 수 없습니다/.test(e.message)) throw e;
        col.notices.push(`${item.path}: ${e.message}`);
      }
      col.upload.done++; colPaintProgress();
    }
    if (col.upload.cancelled) { await colDiscardJob(); col.error = "취합을 취소했습니다. 읽은 내용은 저장하지 않았습니다."; }
    else { await colLoadPreview(); col.step = 2; }
  } catch (e) {
    const message = e.message;
    if (jobId) await colDiscardJob();
    col.error = message;
  } finally {
    col.upload = null;
    if (state.tab === "collect") render(); else if (col.step === 2) toast("엑셀 읽기를 마쳤습니다. 가져오기·공유 → 엑셀 폴더 취합에서 확인하세요");
  }
}

async function colLoadPreview() {
  const version = ++col.previewVersion;
  const p = await api(`/api/collections/jobs/${col.job}/preview`, { method: "POST", body: { rules: col.rules } });
  if (version === col.previewVersion) col.preview = p;
}

// ---------------------------------------------------------------- 2. 취합 확인
function colFileIssues(f) {
  if (f.status !== "read" || !f.selected) return 0;
  return f.sheets.filter((s) => s.selected).reduce((n, s) => n + s.issues.filter((x) => x.blocking).length, 0);
}
function colHint(s) {
  if (s.blocking && s.rows) return `확인 필요 ${s.blocking}개 · 항목을 연결하거나 해당 파일·시트의 선택을 해제해 주세요.`;
  if (!s.rows) return "취합할 파일과 시트를 선택해 주세요.";
  return `선택한 ${s.files}개 파일 · ${colNum(s.rows)}행을 취합합니다.` + (s.excluded || s.failed ? ` 제외하거나 읽지 못한 파일 ${s.excluded + s.failed}개는 결과에 함께 표시됩니다.` : "");
}

function colPreviewTable(s, withAbove = false) {
  const p = s.preview;
  if (!p) return "";
  const cells = (row, tag = "td") => row.cells.map((c) => `<${tag}>${esc(c)}</${tag}>`).join("");
  return `<div class="col-scroll"><table class="col-table"><thead>
    ${withAbove ? p.above.map((r) => `<tr class="col-dim"><td>${r.row}</td>${cells(r)}</tr>`).join("") : ""}
    <tr><th>${esc(s.header_label || s.header_row + "행")}</th>${p.header.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead>
    <tbody>${p.rows.map((r) => `<tr><td class="col-dim">${r.row}</td>${cells(r)}</tr>`).join("") || '<tr><td colspan="99" class="col-dim">아래에 행이 없습니다.</td></tr>'}</tbody></table></div>`;
}

function colColumnsHTML(s) {
  const fields = col.preview.fields;
  const shown = s.columns.filter((c) => c.extra || c.problem || c.mapped || c.auto || c.target === null);
  const missing = s.missing.length ? `<div class="note">이 파일에 없는 항목: ${s.missing.map((m) => `‘${esc(m)}’`).join(", ")} → 빈칸으로 둡니다.</div>` : "";
  return `<div class="col-map">${shown.map((c) => {
    const at = s.columns.indexOf(c), samples = (s.preview?.rows || []).map((r) => r.cells[at]).filter(Boolean).slice(0, 3);
    const k = esc(s.key);
    return `<div class="col-map-row">
      <div class="row" style="margin:0"><span>${esc(c.letter)}열 ‘${esc(c.name)}’${c.problem ? ` <span class="col-warn-text">(${c.problem === "empty" ? "이름 없음" : "이름 중복"})</span>` : ""} →</span>
      <select data-col-map="${k}" data-col="${c.index}" data-focus="map:${k}:${c.index}" aria-label="${esc(c.name)} 열을 연결할 항목">
        <option value="" ${c.target === c.name ? "selected" : ""}>새 항목 ‘${esc(c.name)}’으로 유지</option>
        ${fields.filter((n) => n !== c.name).map((n) => `<option value="${esc(n)}" ${c.target === n ? "selected" : ""}>‘${esc(n)}’에 연결</option>`).join("")}
        <option value="-" ${c.target === null ? "selected" : ""}>이 열은 취합하지 않음</option>
      </select>
      ${c.suggestion && !c.mapped ? `<button class="btn" data-col-suggest="${k}" data-col="${c.index}" data-target="${esc(c.suggestion)}" data-focus="sug:${k}:${c.index}">‘${esc(c.suggestion)}’과 같은 항목으로 사용</button>` : ""}</div>
      ${samples.length ? `<div class="note">예시 값: ${samples.map(esc).join(" / ")}</div>` : ""}
      ${c.unit_warning ? `<div class="col-warn-text">${esc(c.unit_warning)}</div>` : ""}</div>`;
  }).join("")}${missing}</div>`;
}

// 저장을 막는 문제(blocking)만 '확인 필요'로 보여 준다. 나머지는 앱이 알아서 맞춘 내용이라 접어 두고, 필요할 때만 바꾼다.
function colIssueHTML(s, x) {
  const k = esc(s.key), cls = x.blocking ? "col-issue" : "col-issue col-info";
  if (x.kind === "header") {
    return `<div class="${cls}">${x.blocking ? "<b>항목 이름이 있는 행을 확인해 주세요</b>" : ""}<div>${esc(x.message)}</div>${colPreviewTable(s, true)}
      <div class="row" style="margin:8px 0 0"><label for="colHeader-${k}">항목 이름 행</label>
      <input type="number" min="1" id="colHeader-${k}" data-col-header="${k}" data-focus="header:${k}" value="${x.row || ""}" style="width:90px" />
      <button class="btn" data-col-header-ok="${k}" data-focus="headerok:${k}">${x.blocking ? "이 행을 항목 이름으로 사용" : "행 바꾸기"}</button></div></div>`;
  }
  if (x.kind === "note") return `<div class="${cls}">${esc(x.message)}</div>`;
  if (x.kind === "special") {
    return `<div class="${cls}"><div>${esc(x.message)}</div><ul>${x.rows.map((r) => `<li>${r.row}행 · ${esc(r.reason)}${r.text ? ` · ‘${esc(r.text)}’` : ""}</li>`).join("")}
      ${x.count > x.rows.length ? `<li>외 ${x.count - x.rows.length}행</li>` : ""}</ul>
      <div class="row" style="margin:0">${x.included
        ? `<button class="btn" data-col-special="${k}" data-value="exclude" data-focus="spx:${k}">다시 빼기</button>`
        : `<button class="btn" data-col-special="${k}" data-value="include" data-focus="spi:${k}">빼지 말고 일반 행으로 포함</button>`}</div></div>`;
  }
  return `<div class="${cls}">${x.kind === "conflict" ? "<b>항목 연결을 확인해 주세요</b>" : ""}<div>${esc(x.message)}</div>${colColumnsHTML(s)}</div>`;
}

function colSheetHTML(s, multi) {
  const k = esc(s.key);
  const title = `시트 ‘${esc(s.name)}’ · ${colNum(s.selected ? s.rows : s.data_rows)}행 · 항목 이름 ${esc(s.header_label || s.header_row + "행")}`;
  const notes = s.notes.map((n) => `<span class="note"> · ${esc(n)}</span>`).join("");
  const head = multi
    ? `<label class="col-check"><input type="checkbox" data-col-sheet="${k}" data-focus="sheet:${k}" ${s.selected ? "checked" : ""} /> ${title}</label>${notes}`
    : `<div class="note">${title}${notes}</div>`;
  if (!s.selected) return `<div class="col-sheet">${head}</div>`;
  const blocking = s.issues.filter((x) => x.blocking), adjusted = s.issues.filter((x) => !x.blocking);
  const key = s.key, open = col.openAdjusted.has(key);
  return `<div class="col-sheet">${head}${blocking.map((x) => colIssueHTML(s, x)).join("")}
    ${adjusted.length && !col.issuesOnly ? `<details class="capture-options" data-col-adjusted="${esc(key)}" ${open ? "open" : ""}><summary>자동으로 맞춘 내용 ${adjusted.length}개 · 다르면 여기서 바꾸세요</summary>
      ${adjusted.map((x) => colIssueHTML(s, x)).join("")}</details>` : ""}
    <details class="capture-options"><summary>미리보기</summary>${colPreviewTable(s)}</details></div>`;
}

function colFileHTML(f, i) {
  if (f.status !== "read") {
    return `<div class="col-file"><div class="row" style="margin:0"><b>${esc(f.path)}</b><span class="spacer"></span><span class="badge col-badge-warn">읽지 못함</span></div>
      <div class="note">${esc(f.error)} · 결과의 확인 사항에 남습니다.</div></div>`;
  }
  const sheets = f.sheets.filter((s) => !s.hidden && !s.empty);
  const issues = colFileIssues(f), hidden = f.sheets.filter((s) => s.hidden).length, empty = f.sheets.filter((s) => s.empty).length;
  const badge = !f.selected ? '<span class="badge">제외</span>' : issues ? `<span class="badge col-badge-warn">확인 필요 ${issues}</span>` : '<span class="badge col-badge-ok">취합 가능</span>';
  return `<div class="col-file">
    <div class="row" style="margin:0"><label class="col-check"><input type="checkbox" data-col-file="${i}" data-focus="file:${i}" ${f.selected ? "checked" : ""} /> <b>${esc(f.path)}</b></label><span class="spacer"></span>${badge}</div>
    <div class="note">${sheets.length > 1 ? `시트 ${sheets.length}개 중 ${sheets.filter((s) => s.selected).length}개 선택 · ` : ""}${colNum(f.rows)}행${hidden ? ` · 숨김 시트 ${hidden}개 제외` : ""}${empty ? ` · 빈 시트 ${empty}개` : ""}${!sheets.length ? " · 취합할 표가 없습니다" : ""}</div>
    ${f.selected ? sheets.map((s) => colSheetHTML(s, sheets.length > 1)).join("") : ""}</div>`;
}

// 같은 이름의 시트를 파일마다 따로 체크하지 않도록 이름별로 묶는다. 취합에 포함한 파일의 시트만 센다.
function colSheetGroups() {
  const groups = new Map();
  for (const f of col.preview.files) {
    if (f.status !== "read" || !f.selected) continue;
    for (const s of f.sheets) {
      if (s.hidden || s.empty) continue;
      const g = groups.get(s.name) || { name: s.name, keys: [], on: 0 };
      g.keys.push(s.key); g.on += s.selected ? 1 : 0;
      groups.set(s.name, g);
    }
  }
  return [...groups.values()];
}
function colSheetGroupsHTML(groups) {
  const multi = col.preview.files.some((f) => f.status === "read" && f.selected && f.sheets.filter((s) => !s.hidden && !s.empty).length > 1);
  if (!multi) return "";
  return `<div class="col-groups"><b>시트 이름으로 한 번에 선택</b>
    <div class="note">파일마다 시트가 여러 개일 때, 같은 이름의 시트를 모든 파일에서 한 번에 넣거나 뺍니다. 파일별로 바꾸려면 아래 목록에서 고르세요.</div>
    <div class="row" style="margin:8px 0 0">${groups.map((g, i) => `<label class="col-check col-group"><input type="checkbox" data-col-group="${i}" data-focus="group:${i}" ${g.on === g.keys.length ? "checked" : ""} />
      ‘${esc(g.name)}’ <span class="meta">${g.on}/${g.keys.length}개 파일</span></label>`).join("")}</div></div>`;
}

function paintCollectReview(keep = false) {
  const p = col.preview;
  if (!p) return;
  col.groups = colSheetGroups();
  const y = window.scrollY, focus = document.activeElement?.dataset?.focus;
  const s = p.summary;
  const visible = p.files.map((f, i) => [f, i]).filter(([f]) => !col.issuesOnly || colFileIssues(f) || f.status !== "read");
  $("#view").innerHTML = `${colSteps()}
    <div class="panel">
      <div class="row"><label for="colTitle" class="status">자료 이름</label><input id="colTitle" class="col-input" maxlength="100" data-focus="title" value="${esc(col.title)}" /></div>
      <div class="col-summary"><b>${s.files}개 파일 · ${colNum(s.rows)}행</b>${s.blocking ? ` · <span class="col-warn-text">확인 필요 ${s.blocking}개</span>` : ""}${s.failed ? ` · 읽지 못한 파일 ${s.failed}개` : ""}${s.excluded ? ` · 제외 ${s.excluded}개` : ""}${s.adjusted ? ` · <span class="meta">자동으로 맞춘 내용 ${s.adjusted}개</span>` : ""}</div>
      ${col.target ? `<div class="note">‘${esc(col.target.title)}’의 새 버전으로 저장합니다. 이전 버전과 그 AI 결과는 그대로 남습니다.</div>` : ""}
      ${col.notices.map((n) => `<div class="note">${esc(n)}</div>`).join("")}
      ${colSheetGroupsHTML(col.groups)}
      <label class="col-check" style="margin:10px 0"><input type="checkbox" id="colIssuesOnly" data-focus="issuesOnly" ${col.issuesOnly ? "checked" : ""} /> 확인 필요만 보기</label>
      <div id="colFileList">${visible.map(([f, i]) => colFileHTML(f, i)).join("") || '<div class="status">선택한 파일 중 확인이 필요한 항목이 없습니다.</div>'}
        ${p.skipped.map((x) => `<div class="col-file"><div class="row" style="margin:0"><b>${esc(x.path)}</b><span class="spacer"></span><span class="badge col-badge-warn">읽지 않음</span></div><div class="note">${esc(x.reason)} · 결과의 확인 사항에 남습니다.</div></div>`).join("")}</div>
      <details class="capture-options" style="margin-top:12px"><summary>취합 기준</summary>
        <div class="note">시트마다 항목 이름이 있는 행(보통 1행) 아래를 모읍니다 · 같은 항목 이름은 열 순서와 관계없이 합칩니다 · 숨김 시트는 제외하고 숨긴 행·필터로 가린 행은 포함합니다 · 수식은 엑셀에 저장된 계산 결과만 읽습니다 · 매크로와 외부 연결은 실행하지 않습니다.</div></details>
      <div class="row" style="margin-top:12px"><label for="colTags" class="status">태그 (선택)</label><input id="colTags" class="col-input" data-focus="tags" placeholder="예) 11월 신청" value="${esc(col.tags)}" /></div>
      <div id="colHint" class="${s.blocking || !s.rows ? "col-warn" : "note"}" role="status">${esc(colHint(s))}</div>
      ${col.error ? `<div class="col-warn">${esc(col.error)}</div>` : ""}
      <div class="row" style="margin-top:12px"><button class="btn" id="colRestart">다른 폴더 선택</button><span class="spacer"></span>
        <button class="btn primary" id="colCommit" data-focus="commit" ${s.blocking || !s.rows || col.busy ? "disabled" : ""}>${col.busy ? "처리하는 중…" : `${s.files}개 파일 · ${colNum(s.rows)}행 취합하기`}</button></div>
    </div>`;
  bindColSteps();
  bindCollectReview();
  if (keep) window.scrollTo(0, y);
  if (focus) document.querySelector(`[data-focus="${CSS.escape(focus)}"]`)?.focus({ preventScroll: true });
}

async function colUpdate(change) {
  if (col.busy) return;
  change();
  col.busy = true; col.error = "";
  document.querySelectorAll("#view input, #view select, #view button").forEach((el) => { if (el.id !== "colBack") el.disabled = true; });
  try { await colLoadPreview(); }
  catch (e) {
    col.error = e.message;
    if (/작업을 찾을 수 없습니다/.test(e.message)) { colReset(); col.error = e.message; col.step = 1; }
  } finally {
    col.busy = false;
    if (state.tab === "collect") col.step === 2 && col.preview ? paintCollectReview(true) : render();
  }
}

function bindCollectReview() {
  const p = col.preview;
  $("#colTitle").oninput = (e) => { col.title = e.target.value; };
  $("#colTags").oninput = (e) => { col.tags = e.target.value; };
  $("#colIssuesOnly").onchange = (e) => { col.issuesOnly = e.target.checked; paintCollectReview(true); };
  document.querySelectorAll("[data-col-file]").forEach((c) => {
    c.onchange = () => colUpdate(() => { col.rules.files[p.files[+c.dataset.colFile].path] = { selected: c.checked }; });
  });
  document.querySelectorAll("[data-col-group]").forEach((c) => {
    const g = col.groups[+c.dataset.colGroup];
    c.indeterminate = g.on > 0 && g.on < g.keys.length;
    c.onchange = () => colUpdate(() => { g.keys.forEach((key) => { colSheetRule(key).selected = c.checked; }); });
  });
  document.querySelectorAll("[data-col-sheet]").forEach((c) => {
    c.onchange = () => colUpdate(() => { colSheetRule(c.dataset.colSheet).selected = c.checked; });
  });
  const setHeader = (key) => {
    const value = +document.querySelector(`[data-col-header="${CSS.escape(key)}"]`).value;
    if (!Number.isInteger(value) || value < 1) { toast("항목 이름이 있는 행 번호를 입력하세요"); return; }
    colUpdate(() => { const rule = colSheetRule(key); rule.header_row = value; delete rule.confirmed; delete rule.mapping; delete rule.special; });
  };
  document.querySelectorAll("[data-col-header-ok]").forEach((b) => { b.onclick = () => setHeader(b.dataset.colHeaderOk); });
  document.querySelectorAll("[data-col-header]").forEach((input) => {
    input.onkeydown = (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); setHeader(input.dataset.colHeader); } };
  });
  document.querySelectorAll("[data-col-special]").forEach((b) => {
    b.onclick = () => colUpdate(() => { colSheetRule(b.dataset.colSpecial).special = b.dataset.value; });
  });
  document.querySelectorAll("[data-col-map]").forEach((select) => {
    select.onchange = () => colUpdate(() => { (colSheetRule(select.dataset.colMap).mapping ||= {})[select.dataset.col] = select.value; });
  });
  document.querySelectorAll("[data-col-suggest]").forEach((b) => {
    b.onclick = () => colUpdate(() => { (colSheetRule(b.dataset.colSuggest).mapping ||= {})[b.dataset.col] = b.dataset.target; });
  });
  document.querySelectorAll("[data-col-adjusted]").forEach((d) => {
    d.ontoggle = () => { if (d.open) col.openAdjusted.add(d.dataset.colAdjusted); else col.openAdjusted.delete(d.dataset.colAdjusted); };
  });
  $("#colRestart").onclick = async () => {
    if (!confirm("확인한 내용을 버리고 다른 폴더를 선택할까요?")) return;
    await colDiscardJob(); col.step = 1; render();
  };
  $("#colCommit").onclick = colCommit;
}

async function colCommit() {
  if (col.busy || !col.preview) return;
  const title = col.title.trim();
  if (!title) { toast("자료 이름을 입력하세요"); $("#colTitle").focus(); return; }
  const tags = col.tags.split(/[,\s]+/).map((t) => t.replace(/^#/, "").trim()).filter(Boolean);
  col.busy = true; col.error = ""; paintCollectReview(true);
  try {
    const r = await api(`/api/collections/jobs/${col.job}/commit`, {
      method: "POST", body: { rules: col.rules, revision: col.preview.revision, title, tags },
    });
    colReset(); col.target = null; col.tags = "";
    toast(r.version > 1 ? `${r.version}번째 취합으로 저장했습니다` : "취합 자료를 저장했습니다. 메모 목록에도 대표 메모가 생겼습니다");
    loadSidebar().catch(() => {}); loadTags();
    await colOpen(r.collection_id, r.version);
    return;
  } catch (e) {
    col.error = e.message;
    if (/바뀌었습니다/.test(e.message)) await colLoadPreview().catch(() => {});
  } finally { col.busy = false; }
  if (state.tab === "collect" && col.step === 2) paintCollectReview(true);
}

// ---------------------------------------------------------------- 3. 결과 활용
async function colOpen(id, version = null) {
  try {
    const meta = await api(`/api/collections/${id}`);
    col.view = { id, version: version ?? meta.version, meta, summary: null, rows: null, offset: 0, excluded: false,
      groupBy: null, measure: null, source: "", criteriaOpen: false };
    col.step = 3;
  } catch (e) { toast(e.message); return; }
  if (state.tab !== "collect") await goTab("collect"); else await render();
}

async function renderCollectResult(version = renderVersion) {
  const v = col.view;
  const params = new URLSearchParams();
  if (v.groupBy) params.set("group_by", v.groupBy);
  if (v.measure) params.set("measure", v.measure);
  let summary, rows;
  try {
    [summary, rows] = await Promise.all([
      api(`/api/collections/${v.id}/versions/${v.version}?${params}`),
      api(`/api/collections/${v.id}/versions/${v.version}/rows?` + new URLSearchParams({ offset: v.offset, limit: 50, excluded: v.excluded })),
    ]);
  } catch (e) {
    if (version !== renderVersion) return;
    col.view = null; col.step = 1;
    $("#view").innerHTML = `${colSteps()}<div class="empty">${esc(e.message)}</div>`;
    bindColSteps();
    return;
  }
  if (version !== renderVersion) return;
  Object.assign(v, { summary, rows, groupBy: summary.group?.by || null, measure: summary.group?.measure || null });
  paintCollectResult();
}

function colCellHTML(field, text, kind) {
  if (kind === "x") return '<td class="col-dim" title="이 파일에 없는 항목">–</td>';
  if (kind === "e") return `<td class="col-bad" title="셀 오류 · 계산에서 제외">${esc(text)}</td>`;
  if (kind === "f") return '<td class="col-bad" title="엑셀에 계산 결과가 저장되지 않은 수식 · 계산에서 제외">수식 결과 없음</td>';
  if (field.type === "number") return `<td class="num">${kind === "n" ? colNum(text) : esc(text)}</td>`;
  return `<td>${esc(text)}</td>`;
}

function colCriteriaHTML(s) {
  const g = s.group;
  const by = s.choices.group_by, measure = s.choices.measure;
  const groupTable = g ? `<div class="col-scroll"><table class="col-table" aria-label="${esc(g.by)}별 집계"><thead><tr><th>${esc(g.by)}</th><th class="num">건수</th>
      ${g.measure ? `<th class="num">${esc(g.measure)} 합계</th><th class="num">값 확인 필요</th>` : ""}</tr></thead><tbody>
      ${[...g.rows, { ...g.total, total: true }].map((r) => `<tr class="${r.total ? "col-total" : ""}"><td>${esc(r.value)}</td><td class="num">${colNum(r.count)}</td>
        ${g.measure ? `<td class="num">${colNum(r.sum)}</td><td class="num">${r.invalid ? colNum(r.invalid) : ""}</td>` : ""}</tr>`).join("")}</tbody></table></div>
      ${g.more ? `<div class="note">묶음이 많아 상위 ${g.rows.length}개만 표시했습니다. 합계 줄은 전체 기준입니다.</div>` : ""}` : '<div class="note">묶을 수 있는 항목(값의 종류가 30개 이하인 글자 항목)이 없습니다.</div>';
  const numbers = s.numbers.length ? `<div class="col-scroll" style="margin-top:12px"><table class="col-table" aria-label="숫자 항목 집계"><thead><tr><th>항목</th><th class="num">합계</th><th class="num">평균</th><th class="num">최소</th><th class="num">최대</th><th class="num">숫자 확인</th><th class="num">값 확인 필요</th><th class="num">빈칸</th></tr></thead><tbody>
      ${s.numbers.map((n) => `<tr><td>${esc(n.field)}</td><td class="num">${colNum(n.sum)}</td><td class="num">${colNum(n.avg)}</td><td class="num">${colNum(n.min)}</td><td class="num">${colNum(n.max)}</td><td class="num">${colNum(n.valid)}</td><td class="num">${colNum(n.invalid)}</td><td class="num">${colNum(n.blank)}</td></tr>`).join("")}</tbody></table></div>` : "";
  const categories = s.categories.map((c) => `<div class="note">${esc(c.field)}: ${c.top.map((t) => `${esc(t.value)} ${colNum(t.count)}`).join(" · ")}${c.blank ? ` · 빈칸 ${colNum(c.blank)}` : ""}</div>`).join("");
  return `<div class="row" style="margin-top:10px">${by.length ? `<label for="colGroupBy">묶을 항목</label><select id="colGroupBy">${by.map((n) => `<option ${n === g?.by ? "selected" : ""}>${esc(n)}</option>`).join("")}</select>` : ""}
      ${measure.length ? `<label for="colMeasure">계산할 항목</label><select id="colMeasure">${measure.map((n) => `<option ${n === g?.measure ? "selected" : ""}>${esc(n)}</option>`).join("")}</select>` : ""}</div>
    ${groupTable}${numbers}${categories}
    <div class="note">합계·평균은 포함한 모든 행에서 앱이 계산했습니다. 값 확인이 필요한 셀은 0으로 치지 않고 계산에서 뺍니다. 고른 묶음 기준은 AI 정리와 엑셀 저장에도 쓰입니다.</div>`;
}

function paintCollectResult() {
  const v = col.view, s = v.summary, r = v.rows, m = v.meta;
  const left = s.file_list.filter((f) => f.status !== "포함");
  const head = s.numbers[0];
  const last = r.offset + r.items.length;
  $("#view").innerHTML = `${colSteps()}
    <div class="panel">
      <div class="row" style="margin-bottom:4px"><h3 style="margin:0">${esc(s.title)}</h3>
        ${left.length ? `<span class="badge col-badge-warn">일부 파일 취합 · 제외 ${left.length}개</span>` : '<span class="badge col-badge-ok">전체 파일 취합</span>'}
        <span class="spacer"></span>
        ${m.versions.length > 1 ? `<label for="colVersion" class="status">취합 버전</label><select id="colVersion">${m.versions.map((x) => `<option value="${x.version}" ${x.version === v.version ? "selected" : ""}>${x.version}번째 · ${esc(fmt(x.created_at))}${x.version === m.version ? " (최신)" : ""}</option>`).join("")}</select>` : ""}</div>
      <div class="note">${s.files}개 파일 · ${colNum(s.rows)}행${head ? ` · ${esc(head.field)} 합계 ${colNum(head.sum)}` : ""} · ${esc(fmt(s.created_at))} 취합${v.version !== s.current_version ? " · <b>이전 버전입니다</b>" : ""}</div>
      <div class="row" style="margin-top:12px"><button class="btn primary" id="colAi">이 자료를 AI로 정리</button><button class="btn" id="colExport">엑셀로 저장</button>
        ${s.memo_id ? '<button class="btn ghost" id="colMemo">대표 메모 열기</button>' : ""}</div>
      ${s.issues.length ? `<div class="col-issues"><b>확인 사항</b><ul>${s.issues.map((i) => `<li class="${i.level === "warn" ? "col-warn-text" : ""}">${esc(i.text)}</li>`).join("")}</ul></div>` : ""}
      <div class="row" style="margin:16px 0 8px"><b>${v.excluded ? "취합에서 뺀 행" : "합쳐진 표"}</b>
        <span class="meta">${r.total ? `${colNum(r.offset + 1)}–${colNum(last)} / ${colNum(r.total)}행` : "행 없음"}</span><span class="spacer"></span>
        ${v.excluded ? '<button class="btn ghost" id="colShowIncluded">합쳐진 표로 돌아가기</button>' : ""}</div>
      <div class="col-scroll col-main"><table class="col-table" aria-label="${esc(s.title)} ${v.excluded ? "제외한 행" : "취합표"}"><thead><tr>
        ${r.fields.map((f) => `<th class="${f.type === "number" ? "num" : ""}">${esc(f.name)}</th>`).join("")}${v.excluded ? "<th>뺀 이유</th>" : ""}<th>출처</th></tr></thead>
        <tbody>${r.items.map((row, i) => `<tr>${row.cells.map((c, j) => colCellHTML(r.fields[j], c, row.kinds[j])).join("")}
          ${v.excluded ? `<td>${esc(row.reason)}</td>` : ""}<td><button class="btn col-small" data-col-source="${i}">출처 보기</button></td></tr>`).join("")
          || `<tr><td colspan="${r.fields.length + 2}" class="col-dim">${v.excluded ? "뺀 행이 없습니다." : "행이 없습니다."}</td></tr>`}</tbody></table></div>
      <div id="colSource" class="note" role="status">${esc(v.source)}</div>
      ${r.total > 50 ? `<div class="row"><button class="btn" id="colPrev" ${r.offset ? "" : "disabled"}>이전 50행</button><span class="spacer"></span><button class="btn" id="colNext" ${r.has_more ? "" : "disabled"}>다음 50행</button></div>` : ""}
      <details class="capture-options" id="colCriteria" ${v.criteriaOpen ? "open" : ""}><summary>취합 기준 · 묶음 집계${s.group ? ` (${esc(s.group.by)}별${s.group.measure ? " " + esc(s.group.measure) : ""})` : ""}</summary>${colCriteriaHTML(s)}</details>
      <details class="capture-options" id="colMore"><summary>더 보기</summary>
        <div class="row" style="margin-top:10px"><button class="btn" id="colRecollect">같은 폴더로 다시 취합</button><button class="btn" id="colExcluded" ${s.excluded_rows ? "" : "disabled"}>뺀 행 보기 (${colNum(s.excluded_rows)})</button>
          <span class="spacer"></span><button class="btn ghost" id="colDelete" style="color:var(--danger)">취합 자료 삭제</button></div>
        <div class="note">다시 취합하면 새 버전으로 저장합니다. 원본 폴더가 없어도 저장 당시의 표와 집계는 계속 볼 수 있습니다.</div>
        <div class="col-scroll"><table class="col-table" aria-label="파일별 취합 내역"><thead><tr><th>파일</th><th>상태</th><th>시트</th><th class="num">행</th><th>사유</th></tr></thead><tbody>
          ${s.file_list.map((f) => `<tr><td>${esc(f.path)}</td><td>${esc(f.status)}</td><td>${esc(f.sheets.join(", "))}</td><td class="num">${colNum(f.rows)}</td><td>${esc(f.error)}</td></tr>`).join("")}</tbody></table></div>
      </details>
    </div>`;
  bindColSteps();
  $("#colAi").onclick = () => startCollectionGenerate({ id: s.id, version: s.version, title: s.title, files: s.files, rows: s.rows, group_by: v.groupBy, measure: v.measure });
  $("#colExport").onclick = colExport;
  if ($("#colMemo")) $("#colMemo").onclick = () => openMemo(s.memo_id);
  if ($("#colVersion")) $("#colVersion").onchange = (e) => { Object.assign(v, { version: +e.target.value, offset: 0, excluded: false, source: "" }); render(); };
  if ($("#colShowIncluded")) $("#colShowIncluded").onclick = () => { Object.assign(v, { excluded: false, offset: 0, source: "" }); render(); };
  if ($("#colPrev")) $("#colPrev").onclick = () => { v.offset = Math.max(0, v.offset - 50); v.source = ""; render(); };
  if ($("#colNext")) $("#colNext").onclick = () => { v.offset += 50; v.source = ""; render(); };
  document.querySelectorAll("[data-col-source]").forEach((b) => {
    b.onclick = () => {
      const row = r.items[+b.dataset.colSource];
      v.source = `원본 위치: ${row.file} / ${row.sheet} 시트 / ${row.row}행 · 저장 당시의 값을 표시합니다.`;
      $("#colSource").textContent = v.source;
    };
  });
  $("#colCriteria").ontoggle = (e) => { v.criteriaOpen = e.target.open; };
  if ($("#colGroupBy")) $("#colGroupBy").onchange = (e) => { v.groupBy = e.target.value; render(); };
  if ($("#colMeasure")) $("#colMeasure").onchange = (e) => { v.measure = e.target.value; render(); };
  $("#colExcluded").onclick = () => { Object.assign(v, { excluded: true, offset: 0, source: "" }); render(); };
  $("#colRecollect").onclick = () => {
    if (col.upload) { toast("진행 중인 읽기가 끝난 뒤 다시 시도하세요"); return; }
    col.target = { id: s.id, title: s.title, rules: m.rules }; col.view = null; col.step = 1; render();
  };
  $("#colDelete").onclick = async () => {
    if (!confirm(`‘${s.title}’ 취합 자료(모든 버전의 표)를 삭제할까요?\n대표 메모와 이미 만든 AI 결과는 남습니다. 되돌릴 수 없습니다.`)) return;
    try {
      await api(`/api/collections/${s.id}`, { method: "DELETE" });
      toast("취합 자료를 삭제했습니다"); col.view = null; col.step = 1; render();
    } catch (e) { toast("삭제 실패: " + e.message); }
  };
}

async function colExport() {
  const v = col.view, button = $("#colExport");
  const params = new URLSearchParams();
  if (v.groupBy) params.set("group_by", v.groupBy);
  if (v.measure) params.set("measure", v.measure);
  button.disabled = true; button.textContent = "엑셀 만드는 중…";
  try {
    const res = await fetch(`/api/collections/${v.id}/versions/${v.version}/export?${params}`);
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || "엑셀 파일을 만들지 못했습니다.");
    const blob = await res.blob();
    const match = (res.headers.get("content-disposition") || "").match(/filename\*=UTF-8''([^;]+)/i);
    const name = match ? decodeURIComponent(match[1]) : "엑셀취합.xlsx";
    if (window.pywebview?.api?.save_bytes) {
      const encoded = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(",")[1]);
        reader.onerror = () => reject(new Error("파일을 준비하지 못했습니다."));
        reader.readAsDataURL(blob);
      });
      toast(await window.pywebview.api.save_bytes(encoded, name) ? "엑셀로 저장했습니다" : "저장을 취소했습니다");
    } else {
      const a = document.createElement("a"), url = URL.createObjectURL(blob);
      a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast("엑셀 파일을 내려받았습니다");
    }
  } catch (e) { toast(e.message); }
  finally { if (button.isConnected) { button.disabled = false; button.textContent = "엑셀로 저장"; } }
}

// 대표 메모에서 바로 AI 정리로 갈 때. 현재(최신) 버전을 고정해 넘긴다.
async function colStartAi(id) {
  try {
    const meta = await api(`/api/collections/${id}`);
    const latest = meta.versions.find((x) => x.version === meta.version) || { files: 0, rows: 0 };
    await startCollectionGenerate({ id, version: meta.version, title: meta.title, files: latest.files, rows: latest.rows });
  } catch (e) { toast(e.message); }
}
