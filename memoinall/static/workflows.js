// Saved task settings stay out of the main flow until the user opens this row.
const workflowState = { items: [], selected: "", editing: null, baseline: "", busy: false };

function workflowBarHTML() {
  return `<details class="capture-options" id="workflowBar" style="margin:10px 0">
    <summary>자주 하는 업무</summary>
    <div class="row" style="margin-top:10px"><select id="workflowChoice" aria-label="불러올 업무" style="flex:1;min-width:0"></select><button class="btn" id="workflowUse">불러오기</button></div>
    <div class="row" style="margin-top:6px"><button class="btn ghost" id="workflowSaveCurrent">현재 설정 저장</button><button class="btn ghost" id="workflowManage">관리·공유</button></div>
    <div class="note" id="workflowStatus" role="status"></div>
  </details>`;
}

function paintWorkflowBar() {
  const select = $("#workflowChoice");
  if (!select) return;
  select.innerHTML = `<option value="">${workflowState.items.length ? "불러올 업무 선택" : "저장한 업무가 없습니다"}</option>` +
    workflowState.items.map((w) => `<option value="${w.id}">${esc(w.name)}</option>`).join("");
  select.value = workflowState.selected;
  if (!select.value) workflowState.selected = "";
  $("#workflowUse").disabled = gen.busy || workflowState.busy || !select.value;
}

async function loadWorkflows() {
  const data = await api("/api/generate/workflows");
  workflowState.items = data.items; paintWorkflowBar();
}

function bindWorkflows() {
  paintWorkflowBar();
  $("#workflowBar").ontoggle = async (e) => {
    if (!e.target.open) return;
    try { await loadWorkflows(); if ($("#workflowStatus")) $("#workflowStatus").textContent = ""; }
    catch (err) { if ($("#workflowStatus")) $("#workflowStatus").textContent = err.message; }
  };
  $("#workflowChoice").onchange = (e) => { workflowState.selected = e.target.value; paintWorkflowBar(); };
  $("#workflowUse").onclick = useWorkflow;
  $("#workflowSaveCurrent").onclick = () => openWorkflowDialog(true);
  $("#workflowManage").onclick = () => openWorkflowDialog(false);
}

function currentWorkflow() {
  const s = gen.scope || {};
  return { name: gen.instruction.trim().split("\n")[0].slice(0, 40), instruction: gen.instruction,
    format: gen.format, tags: [...(s.tags || [])], period: genPeriod(), since: s.since || null,
    until: s.until || null, person: s.person || null, budget: gen.budget };
}

async function useWorkflow() {
  if (gen.busy || !workflowState.selected) return;
  gen.busy = true; setGenBusy(true);
  try {
    const w = await api(`/api/generate/workflows/${workflowState.selected}`);
    await flushGenDraft();
    ++gen.finderVersion;
    Object.assign(gen, { instruction: w.instruction, format: w.format, budget: w.budget,
      scope: { tags: [...w.tags], period: w.period, since: w.since, until: w.until, person: w.person },
      memoIds: null, review: null, plan: null, queries: null, output: null, body: "", savedDraft: "",
      editing: false, initialized: true, error: "", followup: "", oldVersion: null, periodDay: genToday() });
    await render();
    const missing = w.tags.filter((t) => !gen.tagChoices.some((x) => x.name === t));
    genProgress(missing.length ? `이 PC에 없는 태그: ${missing.map((t) => "#" + t).join(", ")} · 필요한 태그로 바꿔 주세요.` :
      `‘${w.name}’ 설정을 불러왔습니다. 정리하기를 누르면 실행합니다.`);
  } catch (e) { genProgress(e.message); }
  finally { gen.busy = false; setGenBusy(false); }
}

function workflowFields() {
  return { name: $("#wfName").value, instruction: $("#wfInstruction").value, format: $("#wfFormat").value,
    tags: $("#wfTags").value.split(/[,\n]/).map((t) => t.trim()).filter(Boolean), period: $("#wfPeriod").value,
    since: $("#wfPeriod").value === "custom" ? $("#wfSince").value || null : null,
    until: $("#wfPeriod").value === "custom" ? $("#wfUntil").value || null : null,
    person: $("#wfPerson").value.trim() || null, budget: +$("#wfBudget").value };
}

function workflowDirty() { return JSON.stringify(workflowFields()) !== workflowState.baseline; }
function allowWorkflowDiscard() { return !workflowDirty() || confirm("수정한 업무 설정을 저장하지 않고 계속할까요?"); }
function workflowMessage(text) { $("#wfMessage").textContent = text; }
function setWorkflowBusy(busy) {
  workflowState.busy = busy;
  $("#workflowDialog")?.querySelectorAll("input,select,textarea,button").forEach((el) => { el.disabled = busy; });
  if ($("#wfDelete")) $("#wfDelete").disabled = busy || !workflowState.editing;
  paintWorkflowBar();
}

function paintWorkflowForm(w, editing = null) {
  workflowState.editing = editing;
  $("#wfSelect").value = editing ? String(editing.id) : "";
  $("#wfName").value = w.name || ""; $("#wfInstruction").value = w.instruction || "";
  $("#wfFormat").value = w.format || "auto"; $("#wfTags").value = (w.tags || []).join(", ");
  $("#wfPeriod").value = w.period || "all"; $("#wfSince").value = w.since || ""; $("#wfUntil").value = w.until || "";
  $("#wfPerson").value = w.person || ""; $("#wfBudget").value = String(w.budget || 6000);
  $("#wfDates").hidden = $("#wfPeriod").value !== "custom";
  $("#wfDelete").hidden = !editing;
  $("#wfSave").textContent = editing ? "변경 저장" : "업무 저장";
  workflowState.baseline = JSON.stringify(workflowFields());
  workflowMessage(""); paintWorkflowTags();
}

function paintWorkflowTags() {
  const selected = workflowFields().tags.map((t) => t.replace(/^#+/, ""));
  const query = $("#wfTagSearch").value.trim().replace(/^#/, "").toLowerCase();
  $("#wfTagChoices").innerHTML = gen.tagChoices.filter((t) => t.name.toLowerCase().includes(query)).slice(0, 40)
    .map((t) => tagChip(t, selected.includes(t.name), `data-wf-tag="${esc(t.name)}" aria-pressed="${selected.includes(t.name)}"`)).join("");
  $("#wfTagChoices").querySelectorAll("[data-wf-tag]").forEach((b) => {
    b.onclick = () => {
      const name = b.dataset.wfTag;
      $("#wfTags").value = (selected.includes(name) ? selected.filter((t) => t !== name) : [...selected, name]).join(", ");
      paintWorkflowTags();
    };
  });
}

async function openWorkflowDialog(fresh) {
  if (gen.busy || workflowState.busy) return;
  workflowState.busy = true;
  try { await loadWorkflows(); } catch (e) { toast(e.message); return; }
  finally { workflowState.busy = false; }
  if (state.tab !== "generate") return;
  let dialog = $("#workflowDialog");
  if (!dialog) { dialog = document.createElement("dialog"); dialog.id = "workflowDialog"; document.body.append(dialog); }
  dialog.setAttribute("aria-labelledby", "wfTitle");
  dialog.innerHTML = `<div class="dlg-h"><strong id="wfTitle">자주 하는 업무</strong><button class="btn ghost" id="wfImport">받은 업무 가져오기</button><input id="wfFile" type="file" accept=".json" hidden /></div>
    <div class="dlg-b">
      <div class="field"><label for="wfSelect">저장한 업무</label><select id="wfSelect"><option value="">새 업무</option>${workflowState.items.map((w) => `<option value="${w.id}">${esc(w.name)}</option>`).join("")}</select></div>
      <div class="field"><label for="wfName">업무 이름</label><input id="wfName" maxlength="80" placeholder="예) 우리 팀 주간보고" /></div>
      <div class="field"><label for="wfInstruction">정리할 내용</label><textarea id="wfInstruction" maxlength="10000" placeholder="어떤 내용을 어떻게 정리할까요?"></textarea></div>
      <div class="field"><label for="wfTags">태그</label><div><input id="wfTags" placeholder="예) 업무, 프로젝트A" /><div class="note">쉼표로 구분합니다. 고른 태그가 모두 붙은 메모만 찾습니다.</div>
        <details class="capture-options"><summary>사용한 태그에서 고르기</summary><input type="search" id="wfTagSearch" aria-label="업무에 사용할 태그 찾기" placeholder="태그 이름으로 찾기" style="margin:8px 0" /><div class="tagrow" id="wfTagChoices"></div></details></div></div>
      <div class="field"><label for="wfPeriod">기간</label><div><select id="wfPeriod">${genPeriodOptions()}</select><div class="row" id="wfDates" style="margin-top:8px"><input type="date" id="wfSince" aria-label="업무 시작일" /><span>~</span><input type="date" id="wfUntil" aria-label="업무 종료일" /></div><div class="note">이번 주·지난주·이번 달·최근 30일은 실행하는 날을 기준으로 계산합니다.</div></div></div>
      <div class="field"><label for="wfFormat">형식</label><select id="wfFormat">${gen.formats.map((f) => `<option value="${f.key}">${esc(f.label)}</option>`).join("")}</select></div>
      <details class="capture-options"><summary>추가 조건</summary><div class="field" style="margin-top:10px"><label for="wfPerson">사람</label><input id="wfPerson" maxlength="100" placeholder="전체 (비워 두기)" /></div><div class="field"><label for="wfBudget">읽을 분량</label><select id="wfBudget"><option value="6000">기본</option><option value="12000">넉넉히</option><option value="24000">많이</option></select></div></details>
      <div class="note" style="margin-top:10px">다음에도 쓸 요청과 검색 조건을 저장합니다. 체크한 메모 목록은 저장하지 않습니다.</div>
      <div class="note">공유 파일에는 위 업무 설정만 담기며, 메모 본문과 AI 연결 설정은 포함하지 않습니다.</div>
      <div class="status" id="wfMessage" role="status" aria-live="polite"></div>
    </div><div class="dlg-f"><button class="btn ghost" id="wfDelete">삭제</button><button class="btn" id="wfExport">파일로 공유</button><span class="spacer"></span><button class="btn ghost" id="wfClose">닫기</button><button class="btn primary" id="wfSave">업무 저장</button></div>`;
  const w = !fresh && workflowState.items.find((x) => String(x.id) === workflowState.selected);
  paintWorkflowForm(w || currentWorkflow(), w || null);
  $("#wfSelect").onchange = (e) => {
    const id = e.target.value;
    if (!allowWorkflowDiscard()) { e.target.value = workflowState.editing?.id || ""; return; }
    const selected = workflowState.items.find((x) => String(x.id) === id);
    paintWorkflowForm(selected || currentWorkflow(), selected || null);
  };
  $("#wfPeriod").onchange = () => { $("#wfDates").hidden = $("#wfPeriod").value !== "custom"; };
  $("#wfTags").oninput = $("#wfTagSearch").oninput = paintWorkflowTags;
  $("#wfSave").onclick = saveWorkflow;
  $("#wfDelete").onclick = deleteWorkflow;
  $("#wfExport").onclick = exportWorkflow;
  $("#wfImport").onclick = () => $("#wfFile").click();
  $("#wfFile").onchange = importWorkflow;
  const close = () => { if (!workflowState.busy && allowWorkflowDiscard()) dialog.close(); };
  $("#wfClose").onclick = close;
  dialog.oncancel = (e) => { e.preventDefault(); close(); };
  dialog.showModal(); $("#wfName").focus();
}

async function saveWorkflow() {
  if (workflowState.busy) return;
  const data = workflowFields(), editing = workflowState.editing;
  setWorkflowBusy(true); workflowMessage("저장하는 중입니다…");
  try {
    const saved = await api(editing ? `/api/generate/workflows/${editing.id}` : "/api/generate/workflows",
      { method: editing ? "PUT" : "POST", body: editing ? { settings: data, version: editing.version } : data });
    workflowState.items = [saved, ...workflowState.items.filter((w) => w.id !== saved.id)];
    workflowState.selected = String(saved.id);
    $("#workflowDialog").close(); toast("업무를 저장했습니다. 목록에서 불러와 사용할 수 있습니다");
  } catch (e) { workflowMessage(e.message); }
  finally { setWorkflowBusy(false); }
}

async function deleteWorkflow() {
  const w = workflowState.editing;
  if (workflowState.busy || !w || !confirm(`‘${w.name}’ 업무 설정을 삭제할까요?`)) return;
  setWorkflowBusy(true);
  try {
    await api(`/api/generate/workflows/${w.id}?version=${w.version}`, { method: "DELETE" });
    workflowState.items = workflowState.items.filter((x) => x.id !== w.id);
    $("#workflowDialog").close(); toast("업무 설정을 삭제했습니다");
  } catch (e) { workflowMessage(e.message); }
  finally { setWorkflowBusy(false); }
}

async function exportWorkflow() {
  if (workflowState.busy) return;
  const data = workflowFields(); setWorkflowBusy(true);
  try {
    const file = await api("/api/generate/workflows/export", { method: "POST", body: data });
    if (window.pywebview?.api?.save) {
      const saved = await window.pywebview.api.save(file.content, file.filename);
      workflowMessage(saved ? "업무 설정 파일을 저장했습니다." : "파일 저장을 취소했습니다.");
    } else {
      const url = URL.createObjectURL(new Blob([file.content], { type: "application/json;charset=utf-8" }));
      const link = document.createElement("a"); link.href = url; link.download = file.filename; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000); workflowMessage("업무 설정 파일을 저장했습니다.");
    }
  } catch (e) { workflowMessage(e.message); }
  finally { setWorkflowBusy(false); }
}

async function importWorkflow() {
  const file = $("#wfFile").files?.[0]; $("#wfFile").value = "";
  if (!file || workflowState.busy) return;
  if (file.size > 100000) { workflowMessage("업무 설정 파일은 100KB 이하로 선택하세요."); return; }
  setWorkflowBusy(true);
  try {
    const result = await api("/api/generate/workflows/preview", { method: "POST", body: { content: await file.text() } });
    if (!allowWorkflowDiscard()) return;
    paintWorkflowForm(result.workflow);
    workflowMessage("파일의 내용을 확인하고 업무 저장을 누르세요." + (result.missing_tags.length ?
      ` 이 PC에 없는 태그: ${result.missing_tags.map((t) => "#" + t).join(", ")} · 내 태그로 바꿀 수 있습니다.` : ""));
  } catch (e) { workflowMessage(e.message); }
  finally { setWorkflowBusy(false); }
}
