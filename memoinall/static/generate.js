// Drafts live on the server because desktop ports change on restart.
const gen = { instruction: "", format: "auto", budget: 6000, queries: null, plan: null, output: null,
  busy: false, memoIds: null, scope: null, review: null, showSources: false, initialized: false,
  editing: false, body: "", savedDraft: "", draftSaving: false, draftTask: Promise.resolve(), draftTimer: null,
  error: "", followup: "", historyOffset: 0, historyQuery: "", finderVersion: 0, oldVersion: null,
  tagChoices: [], formats: [], periods: {}, periodDay: "", collection: null };
// 취합 자료로 정리할 때의 시작 문구. 요청문 아래에는 이 세 가지만 둔다.
const GEN_COLLECTION_STARTERS = [
  { label: "현황 요약", format: "brief", text: "취합 자료의 현황을 요약해줘. 전체 합계와 묶음별 차이, 확인이 필요한 점을 함께 알려줘." },
  { label: "확인할 내용 찾기", format: "list", text: "취합 자료에서 확인할 내용을 찾아줘. 빈칸·값 확인 필요·중복 같은 확인 사항과 후속 확인이 필요한 행을 정리해줘." },
  { label: "보고서 초안", format: "report", text: "취합 자료를 바탕으로 보고서 초안을 써줘. 집계표를 근거로 현황, 확인 사항, 후속 할 일을 정리해줘." },
];

function genToday() { const d = new Date(); return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`; }
function genPeriod() { return gen.scope?.period || (gen.scope?.since || gen.scope?.until ? "custom" : "all"); }
function genPeriodOptions(selected = genPeriod()) {
  return Object.entries(gen.periods).map(([key, label]) => `<option value="${key}" ${key === selected ? "selected" : ""}>${esc(label)}</option>`).join("");
}
function paintGenPeriod() {
  if (!$("#genPeriod")) return;
  $("#genPeriod").value = genPeriod(); $("#genDates").hidden = genPeriod() !== "custom";
  $("#genSince").value = gen.scope?.since?.slice(0, 10) || ""; $("#genUntil").value = gen.scope?.until?.slice(0, 10) || "";
}
function refreshGenDay() {
  const day = genToday(), changed = gen.periodDay && gen.periodDay !== day;
  gen.periodDay = day;
  if (changed && !["all", "custom"].includes(genPeriod())) {
    gen.review = gen.memoIds = null; ++gen.finderVersion;
    invalidatePlan(true); renderPlanPanel();
  }
}

async function startGenerate(ids, instruction) {
  if (gen.busy) { toast("진행 중인 정리가 끝난 뒤 다시 선택하세요"); return; }
  gen.busy = true; setGenBusy(true);
  try { await flushGenDraft(); } catch { toast("수정 내용을 보관하지 못했습니다. 다시 시도해 주세요"); return; }
  finally { gen.busy = false; setGenBusy(false); }
  Object.assign(gen, { instruction, memoIds: ids, scope: null, queries: null, plan: null, output: null, collection: null,
    review: null, body: "", savedDraft: "", editing: false, initialized: true, error: "", followup: "", oldVersion: null, periodDay: genToday() });
  await goTab("generate");
}

// 취합 자료의 한 버전을 고정해 정리한다. 메모 검색·태그 범위는 쓰지 않는다.
async function startCollectionGenerate(collection, instruction = "") {
  if (gen.busy) { toast("진행 중인 정리가 끝난 뒤 다시 선택하세요"); return; }
  gen.busy = true; setGenBusy(true);
  try { await flushGenDraft(); } catch { toast("수정 내용을 보관하지 못했습니다. 다시 시도해 주세요"); return; }
  finally { gen.busy = false; setGenBusy(false); }
  Object.assign(gen, { instruction, format: "auto", memoIds: null, scope: null, queries: null, plan: null, output: null,
    review: null, body: "", savedDraft: "", editing: false, initialized: true, error: "", followup: "", oldVersion: null,
    periodDay: genToday(), collection });
  await goTab("generate");
}

function genCollectionLabel(c = gen.collection) {
  return `사용할 자료: ${c.title} · 파일 ${c.files}개 · ${colNum(c.rows)}행 (${c.version}번째 취합)`
    + (c.group_by ? ` · ${c.group_by}별${c.measure ? " " + c.measure : ""} 집계` : "");
}

function genScopeLabel() {
  if (gen.collection) return genCollectionLabel();
  const count = gen.review ? gen.review.selected.size : gen.memoIds?.length;
  const s = gen.scope;
  const relative = s?.period && !["all", "custom"].includes(s.period);
  const filters = s ? [...(s.tags || []).map((t) => "#" + t), s.person ? "@" + s.person : "",
    relative ? gen.periods[s.period] : "", !relative && s.since ? s.since + "부터" : "",
    !relative && s.until ? s.until + "까지" : ""].filter(Boolean) : [];
  return [filters.length ? "찾을 범위: " + filters.join(" · ") : "",
    count !== undefined ? `선택한 메모 ${count}건만 사용` : ""].filter(Boolean).join(" — ") || "전체 메모에서 필요한 내용을 찾습니다";
}

function changeGenScope(scope) {
  if (gen.busy) return;
  gen.scope = scope; gen.memoIds = gen.review = null; gen.periodDay = genToday();
  ++gen.finderVersion;
  invalidatePlan(true); renderPlanPanel(); paintGenTags(); paintGenPeriod(); setGenBusy(false);
  genProgress("범위를 바꿨습니다. 메모 고르기로 대상을 확인하거나 바로 정리할 수 있습니다.");
}

function paintGenTags() {
  const host = $("#genTagChoices");
  if (!host) return;
  const focused = document.activeElement?.dataset.genTag;
  const selected = gen.scope?.tags || [], query = ($("#genTagSearch").value || "").trim().replace(/^#/, "").toLowerCase();
  $("#genTagCount").textContent = selected.length ? `${selected.length}개 선택` : "선택 안 함";
  $("#genTagClear").hidden = !selected.length;
  $("#genTagClear").disabled = gen.busy;
  $("#genSelectedTags").innerHTML = selected.map((name) => `<button type="button" class="tagchip on t-slate" data-gen-remove-tag="${esc(name)}" aria-label="#${esc(name)} 조건 해제" ${gen.busy ? "disabled" : ""}>#${esc(name)} ×</button>`).join("");
  const choices = gen.tagChoices.filter((t) => t.name.toLowerCase().includes(query));
  host.innerHTML = choices.slice(0, 40).map((t) => tagChip(t, selected.includes(t.name),
    `data-gen-tag="${esc(t.name)}" aria-pressed="${selected.includes(t.name)}" ${gen.busy ? "disabled" : ""}`)).join("") ||
    `<span class="status">${gen.tagChoices.length ? "일치하는 태그가 없습니다." : "아직 메모에 사용한 태그가 없습니다."}</span>`;
  $("#genTagMore").textContent = choices.length > 40 ? `태그 ${choices.length}개 중 40개 표시 · 이름으로 검색하면 나머지도 고를 수 있습니다.` : "";
  document.querySelectorAll("[data-gen-tag], [data-gen-remove-tag]").forEach((b) => {
    b.onclick = () => {
      const name = b.dataset.genTag || b.dataset.genRemoveTag;
      changeGenScope({ ...gen.scope, tags: selected.includes(name) ? selected.filter((t) => t !== name) : [...selected, name] });
    };
  });
  if (focused) host.querySelector(`[data-gen-tag="${CSS.escape(focused)}"]`)?.focus();
}

function genMemosUrl(query = "", offset = 0) {
  const s = gen.scope || {}, params = new URLSearchParams({ q: query, offset, limit: 20 });
  if (s.tags?.length) params.set("tags", s.tags.join(","));
  for (const key of ["person", "since", "until", "period"]) if (s[key]) params.set(key, s[key]);
  return `/api/generate/memos?${params}`;
}

async function renderGenerate(version = renderVersion) {
  const [{ formats, llm_ready, periods }, history, tagList] = await Promise.all([
    api("/api/generate/formats"), api("/api/generate/history"), api("/api/generate/tags"),
  ]);
  if (version !== renderVersion) return;
  gen.tagChoices = tagList.items;
  gen.formats = formats; gen.periods = periods;
  if (!gen.initialized) {
    gen.initialized = true;
    if (history.items.length) {
      const r = await api(`/api/generate/history/${history.items[0].id}`);
      if (version !== renderVersion) { gen.initialized = false; return; }
      adoptGenResult(r); gen.instruction = r.instruction; gen.format = r.format; gen.scope = r.scope || null;
      gen.collection = r.collection || null;
    }
  }
  const usingCollection = !!gen.collection;
  $("#view").innerHTML = `
    <details class="panel" id="genHistory"><summary>이전 결과</summary>
      <div class="row" style="margin:12px 0"><input type="search" id="genHistorySearch" aria-label="이전 결과 찾기" placeholder="이전에 요청한 내용으로 찾기" style="flex:1" /></div>
      <div id="genHistoryList"></div><div class="row" style="margin-top:8px"><button class="btn" id="genHistoryPrev">이전</button><button class="btn" id="genHistoryNext">다음</button></div>
    </details>
    <div class="panel">
      <div class="row"><h3 style="margin:0">무엇을 정리할까요?</h3><span class="spacer"></span><button class="btn ghost" id="genNew">새로 정리</button></div>
      <div class="desc" style="margin-top:10px">원하는 내용을 적으면 메모를 찾아 정리합니다. 만든 결과는 자동으로 보관됩니다.
        ${llm_ready ? "" : '<br>AI를 연결하면 바로 정리할 수 있어요. <a href="#" id="genSettings">AI 연결하기</a>'}</div>
      ${workflowBarHTML()}
      <textarea class="gen-input" id="genInput" aria-label="정리할 내용" placeholder="${usingCollection ? "예) 부서별 신청금액과 미처리 내역을 정리해줘" : "예) 이번 주 업무를 팀 공유용으로 정리해줘"}">${esc(gen.instruction)}</textarea>
      ${usingCollection ? `<div class="row" style="margin:10px 0" id="genStarters">${GEN_COLLECTION_STARTERS.map((s, i) => `<button class="btn ghost" data-gen-starter="${i}">${esc(s.label)}</button>`).join("")}</div>
      <div class="row gen-collection" style="margin:10px 0"><span class="status">숫자는 앱이 포함 행 전체로 계산한 값만 AI에 전달합니다. 원문 행은 읽을 분량 안에서 일부만 실을 수 있습니다.</span>
        <span class="spacer"></span><button class="btn ghost" id="genCollectionView">취합표 보기</button><button class="btn ghost" id="genCollectionClear">메모로 정리하기</button></div>` : ""}
      <details class="capture-options" id="genTagFilter" style="margin:12px 0" ${gen.scope?.tags?.length ? "open" : ""} ${usingCollection ? "hidden" : ""}>
        <summary>태그로 범위 좁히기 · <span class="meta" id="genTagCount"></span></summary>
        <div class="note" style="margin-top:8px">여러 개를 고르면 <b>그 태그가 모두 붙은 메모만</b> 찾습니다. 태그를 바꾸면 참고할 메모를 다시 고릅니다.</div>
        <div class="tagrow" id="genSelectedTags" style="margin-top:8px"></div>
        <div class="row" style="margin:10px 0"><input type="search" id="genTagSearch" aria-label="범위를 좁힐 태그 찾기" placeholder="태그 이름으로 찾기" style="flex:1;min-width:0" /><button class="btn ghost" id="genTagClear">태그 해제</button></div>
        <div class="tagrow" id="genTagChoices" style="max-height:180px;overflow:auto;padding:2px"></div>
        <div class="note" id="genTagMore"></div>
      </details>
      <div class="row" style="margin:10px 0"><label for="genFormat">형식</label><select id="genFormat">${formats.map((f) => `<option value="${f.key}" ${gen.format === f.key ? "selected" : ""}>${esc(f.label)}</option>`).join("")}</select>
        <span class="spacer"></span><button class="btn" id="genPlan" ${usingCollection ? "hidden" : ""}>메모 고르기</button><button class="btn primary" id="genRun">${llm_ready ? "정리하기" : "요청문 만들기"}</button></div>
      <div class="note" id="genScope">${esc(genScopeLabel())}</div>
      <details class="capture-options" style="margin-top:12px" id="genScopeOptions" ${usingCollection ? "hidden" : ""}><summary>찾을 범위 바꾸기</summary>
        <div class="row" style="margin:10px 0"><button class="btn" id="genAll">전체 메모에서 찾기</button><button class="btn" id="genFiltered">현재 검색 범위로 찾기</button></div>
        <div class="row" style="margin:10px 0"><label for="genPeriod">기간</label><select id="genPeriod">${genPeriodOptions()}</select></div>
        <div class="row" id="genDates" style="margin:10px 0"><input type="date" id="genSince" aria-label="정리할 작성 시작일" /><span>~</span><input type="date" id="genUntil" aria-label="정리할 작성 종료일" /></div>
        <label for="genBudget" class="status">읽을 분량</label> <select id="genBudget"><option value="6000">기본</option><option value="12000">넉넉히</option><option value="24000">많이</option></select>
        <div class="note">분량을 늘리면 처리 시간과 AI 사용량이 늘어날 수 있습니다.</div>
      </details><div class="status" id="genProgress" aria-live="polite"></div>
    </div><div id="genPlanOut"></div><div id="genOut"></div>`;
  $("#genBudget").value = String(gen.budget);
  $("#genInput").oninput = (e) => { gen.instruction = e.target.value; invalidatePlan(true); };
  $("#genInput").onkeydown = (e) => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); runGenerate(); } };
  $("#genFormat").onchange = (e) => { gen.format = e.target.value; invalidatePlan(); };
  $("#genBudget").onchange = (e) => { gen.budget = +e.target.value; invalidatePlan(); };
  $("#genPeriod").onchange = (e) => changeGenScope({ ...gen.scope, period: e.target.value,
    since: e.target.value === "custom" ? $("#genSince").value || null : null,
    until: e.target.value === "custom" ? $("#genUntil").value || null : null });
  $("#genSince").onchange = $("#genUntil").onchange = () => changeGenScope({ ...gen.scope, period: "custom",
    since: $("#genSince").value || null, until: $("#genUntil").value || null });
  $("#genTagSearch").oninput = paintGenTags;
  $("#genTagClear").onclick = () => changeGenScope({ ...gen.scope, tags: [] });
  $("#genAll").onclick = () => changeGenScope(null);
  $("#genFiltered").onclick = () => {
    changeGenScope({ tags: [...state.tags], person: state.person, since: state.since, until: state.until });
  };
  $("#genPlan").onclick = planGenerate; $("#genRun").onclick = runGenerate;
  $("#genNew").onclick = () => startGenerate(null, "");
  document.querySelectorAll("[data-gen-starter]").forEach((b) => {
    b.onclick = () => {
      const s = GEN_COLLECTION_STARTERS[+b.dataset.genStarter];
      gen.instruction = s.text; gen.format = s.format;
      $("#genInput").value = s.text; $("#genFormat").value = s.format; invalidatePlan(true); $("#genInput").focus();
    };
  });
  if ($("#genCollectionView")) $("#genCollectionView").onclick = () => colOpen(gen.collection.id, gen.collection.version);
  if ($("#genCollectionClear")) $("#genCollectionClear").onclick = () => { if (!gen.busy) startGenerate(null, gen.instruction); };
  if ($("#genSettings")) $("#genSettings").onclick = (e) => { e.preventDefault(); goTab("settings"); };
  let historyTimer;
  $("#genHistorySearch").oninput = (e) => {
    gen.historyQuery = e.target.value; gen.historyOffset = 0;
    clearTimeout(historyTimer); historyTimer = setTimeout(loadGenHistory, 250);
  };
  $("#genHistoryPrev").onclick = () => { gen.historyOffset = Math.max(0, gen.historyOffset - 20); loadGenHistory(); };
  $("#genHistoryNext").onclick = () => { gen.historyOffset += 20; loadGenHistory(); };
  gen.historyOffset = 0; gen.historyQuery = ""; paintGenHistory(history);
  bindWorkflows(); paintGenPeriod(); paintGenTags(); renderPlanPanel(); renderGenOutput(); setGenBusy(gen.busy);
}

function setGenBusy(busy) {
  const empty = gen.review && !gen.review.selected.size;
  document.querySelectorAll("#genInput, #genFormat, #genBudget, #genPlan, #genRun, #genAll, #genFiltered, #genRun2, #genNew, #genEdit, #genDone, #genMemo, #genRefine, #genFollowup, #genOldSelect, #genRestore, #genBody, #genTagSearch, #genTagClear, [data-gen-tag], [data-gen-remove-tag], [data-gen-quick], [data-gen-history], #genPlanOut input, #genPlanOut button")
    .forEach((el) => { el.disabled = busy || (["genRun", "genRun2"].includes(el.id) && empty); });
  document.querySelectorAll("#workflowBar button, #workflowBar select, #genPeriod, #genSince, #genUntil").forEach((el) => { el.disabled = busy; });
  paintWorkflowBar();
  if (!busy) paintGenPicker();
}
function genProgress(text = "") { if ($("#genProgress")) $("#genProgress").textContent = text; }
function invalidatePlan(reset = false) {
  gen.plan = null;
  if (reset) gen.queries = null;
  if ($("#genScope")) $("#genScope").textContent = genScopeLabel();
  updateGenPromptVisibility();
}
function genPayload() {
  if (gen.collection) {
    const c = gen.collection;
    return { instruction: gen.instruction.trim(), format: gen.format, budget: gen.budget,
      collection: { id: c.id, version: c.version, group_by: c.group_by || null, measure: c.measure || null } };
  }
  const items = gen.review?.items.filter((m) => gen.review.selected.has(m.key));
  return { instruction: gen.instruction.trim(), format: gen.format, budget: gen.budget,
    queries: gen.queries, memo_ids: items ? items.map((m) => m.id) : gen.memoIds,
    ...(items ? { source_revisions: Object.fromEntries(items.map((m) => [m.id, m.revision])) } : {}), ...(gen.scope || {}) };
}

async function planGenerate() {
  if (gen.busy) return;
  refreshGenDay();
  gen.busy = true; setGenBusy(true); genProgress("참고할 메모를 찾는 중입니다…");
  try {
    const payload = genPayload(); delete payload.source_revisions;
    let plan;
    if (!payload.instruction && payload.memo_ids === null) {
      const list = await api(genMemosUrl());
      plan = { candidates: list.items, sources: [], dropped: [], queries: [] };
    } else {
      plan = await api("/api/generate/plan", { method: "POST", body: { ...payload, instruction: payload.instruction || "선택한 메모 정리" } });
    }
    const incoming = plan.candidates || [], previous = gen.review;
    const merged = new Map((previous?.items || []).map((m) => [m.key, m]));
    incoming.forEach((m) => merged.set(m.key, m));
    gen.review = { items: [...merged.values()], selected: previous ? previous.selected : new Set(payload.instruction || payload.memo_ids !== null ? incoming.map((m) => m.key) : []) };
    gen.plan = plan; gen.queries = plan.queries || []; gen.showSources = true;
    gen.planKey = JSON.stringify(genPayload());
    renderPlanPanel(); genProgress(!incoming.length && gen.scope?.tags?.length ? "고른 태그에 맞는 메모를 찾지 못했습니다. 태그를 바꾸거나 해제해 주세요." : "");
  } catch (e) { genProgress(e.message); }
  finally { gen.busy = false; setGenBusy(false); }
}

function renderPlanPanel() {
  if (!$("#genPlanOut")) return;
  if ($("#genScope")) $("#genScope").textContent = genScopeLabel();
  if (gen.collection) {
    // 메모를 고르지 않는다. AI 에 넘길 집계·원문을 그대로 보여 주고 복사할 수 있게만 한다.
    const p = gen.plan;
    $("#genPlanOut").innerHTML = p?.prompt ? `<details class="panel" id="genReferences" ${gen.showSources ? "open" : ""}>
      <summary>AI에 전달할 내용</summary><div class="note" style="margin-top:10px">앱이 포함 행 전체로 계산한 집계와 일부 원문 행입니다. AI를 연결하지 않았다면 복사해서 다른 AI에 붙여 넣을 수 있습니다.</div>
      <div id="genPromptChanged" class="note">요청을 바꿨습니다. 요청문 복사를 누르면 현재 요청으로 다시 만듭니다.</div>
      <pre id="genPromptText" class="pack">${esc(p.prompt)}</pre><button class="btn" id="copyGenPrompt">요청문 복사</button></details>` : "";
    if ($("#genReferences")) {
      $("#genReferences").ontoggle = (e) => { if (e.target.isConnected) gen.showSources = e.target.open; };
      $("#copyGenPrompt").onclick = copySelectedGenPrompt;
    }
    updateGenPromptVisibility();
    return;
  }
  if (!gen.review) { $("#genPlanOut").innerHTML = ""; return; }
  const p = gen.plan;
  $("#genPlanOut").innerHTML = `<details class="panel" id="genReferences" ${gen.showSources ? "open" : ""}>
    <summary>참고할 메모 <span id="genRefCount"></span></summary>
    <div class="note" style="margin-top:10px">체크한 메모로만 정리합니다. 본문을 확인하거나 필요한 메모를 더 골라보세요.</div>
    ${p?.dropped?.length ? `<div class="note" style="color:var(--warn)">자료가 많아 ${p.dropped.length}건은 정리에 포함되지 않았습니다. 선택을 줄이거나 읽을 분량을 늘려주세요.</div>` : ""}
    ${p?.sources?.some((s) => s.included_as !== "full") ? '<div class="note" style="color:var(--warn)">긴 메모는 일부 내용만 반영됩니다.</div>' : ""}
    <div id="genPicker"></div>
    <details id="genAddPanel" style="margin-top:12px"><summary>메모 더 찾기</summary>
      <div class="row" style="margin:10px 0"><input id="genFind" type="search" placeholder="찾을 내용을 입력하세요" aria-label="추가할 메모 찾기" style="flex:1" /></div>
      <div id="genFound"></div><div class="row"><button class="btn" id="genFindPrev">이전</button><button class="btn" id="genFindNext">다음</button></div>
    </details>
    <div class="row" style="margin-top:12px"><span class="spacer"></span><button class="btn primary" id="genRun2">선택한 메모로 정리</button></div>
    <details style="margin-top:10px"><summary class="status">AI에 전달할 내용</summary><div id="genPromptChanged" class="note">현재 선택으로 요청문을 다시 만듭니다.</div><pre id="genPromptText" class="pack">${esc(p?.prompt || "")}</pre><button class="btn" id="copyGenPrompt">요청문 복사</button></details>
    </details>`;
  $("#genReferences").ontoggle = (e) => { if (e.target.isConnected) gen.showSources = e.target.open; };
  $("#genRun2").onclick = runGenerate;
  $("#copyGenPrompt").onclick = copySelectedGenPrompt;
  let offset = 0, timer;
  $("#genAddPanel").ontoggle = (e) => { if (e.target.open) findGenMemos(offset); };
  $("#genFind").oninput = () => { offset = 0; ++gen.finderVersion; clearTimeout(timer); timer = setTimeout(() => findGenMemos(offset), 250); };
  $("#genFindPrev").onclick = () => { offset = Math.max(0, offset - 20); findGenMemos(offset); };
  $("#genFindNext").onclick = () => { offset += 20; findGenMemos(offset); };
  paintGenPicker();
}
function paintGenPicker() {
  if (!$("#genPicker") || !gen.review) return;
  memoPicker($("#genPicker"), gen.review, () => {
    $("#genRefCount").textContent = `${gen.review.selected.size}건`;
    $("#genScope").textContent = genScopeLabel();
    $("#genRun2").disabled = $("#genRun").disabled = gen.busy || !gen.review.selected.size;
    updateGenPromptVisibility();
  });
}
function updateGenPromptVisibility() {
  const current = gen.plan?.prompt && gen.planKey === JSON.stringify(genPayload());
  if ($("#genPromptText")) $("#genPromptText").hidden = !current;
  if ($("#genPromptChanged")) $("#genPromptChanged").hidden = !!current;
}
async function copySelectedGenPrompt() {
  if (gen.busy) return;
  refreshGenDay();
  if (!gen.instruction.trim()) { toast("정리할 내용을 먼저 적어주세요"); return; }
  gen.busy = true; setGenBusy(true);
  try {
    const payload = genPayload();
    const plan = await api("/api/generate/plan", { method: "POST", body: payload });
    gen.plan = plan; gen.planKey = JSON.stringify(payload);
    if ($("#genPromptText")) $("#genPromptText").textContent = plan.prompt;
    updateGenPromptVisibility();
    await navigator.clipboard.writeText(plan.prompt); toast("선택한 메모로 만든 요청문을 복사했습니다");
  } catch (e) { genProgress(e.message); }
  finally { gen.busy = false; setGenBusy(false); }
}
async function findGenMemos(offset) {
  refreshGenDay();
  const host = $("#genFound"), query = $("#genFind")?.value || "", version = ++gen.finderVersion;
  if (!host) return;
  host.textContent = "찾는 중…";
  try {
    const r = await api(genMemosUrl(query, offset));
    if (!host.isConnected || version !== gen.finderVersion) return;
    host.innerHTML = r.items.map((m, i) => `<div class="src"><div class="src-body"><b>${esc(m.title)}</b><div class="meta">${esc((m.created_at || "").slice(0, 10))}</div><details><summary class="status">본문 확인</summary><pre class="pack">${esc(m.body)}</pre></details></div><button class="btn" data-gen-add="${i}">${gen.review.selected.has(m.key) ? "선택됨" : "추가"}</button></div>`).join("") || '<div class="status">찾은 메모가 없습니다.</div>';
    host.querySelectorAll("[data-gen-add]").forEach((b) => {
      const m = r.items[+b.dataset.genAdd]; b.disabled = gen.busy || gen.review.selected.has(m.key);
      b.onclick = () => {
        if (gen.review.items.length >= 1000 && !gen.review.items.some((x) => x.id === m.id)) { toast("한 번에 1,000건까지 고를 수 있습니다"); return; }
        const at = gen.review.items.findIndex((x) => x.id === m.id);
        at < 0 ? gen.review.items.push(m) : gen.review.items.splice(at, 1, m);
        gen.review.selected.add(m.key); b.textContent = "선택됨"; b.disabled = true; paintGenPicker();
      };
    });
    $("#genFindPrev").disabled = gen.busy || offset === 0;
    $("#genFindNext").disabled = gen.busy || !r.has_more;
  } catch (e) { if (host.isConnected && version === gen.finderVersion) host.textContent = e.message; }
}

async function runGenerate() {
  if (!gen.instruction.trim()) { toast("정리할 내용을 먼저 적어주세요"); $("#genInput")?.focus(); return; }
  if (gen.busy) return;
  refreshGenDay();
  gen.busy = true; setGenBusy(true); genProgress("메모를 읽고 정리하는 중입니다…");
  try {
    await flushGenDraft();
    const r = await api("/api/generate", { method: "POST", body: genPayload() });
    if (r.output) {
      adoptGenResult(r); gen.showSources = false; gen.plan = r; gen.queries = r.queries || [];
      if (!gen.review && !gen.collection) gen.review = { items: r.candidates || [], selected: new Set((r.candidates || []).map((m) => m.key)) };
      gen.planKey = JSON.stringify(genPayload());
      if (state.tab === "generate") { renderPlanPanel(); renderGenOutput(); loadGenHistory(); }
      genProgress();
    } else {
      gen.plan = r;
      if (!gen.review && !gen.collection) gen.review = { items: r.candidates || [], selected: new Set((r.candidates || []).map((m) => m.key)) };
      gen.planKey = JSON.stringify(genPayload());
      gen.showSources = true; renderPlanPanel(); genProgress(r.reason || "정리한 내용이 없습니다.");
    }
  } catch (e) { genProgress(e.message); }
  finally { gen.busy = false; setGenBusy(false); }
}

function adoptGenResult(r) {
  gen.output = r; gen.body = r.draft_body ?? r.output ?? ""; gen.savedDraft = gen.body;
  gen.editing = r.draft_body !== null && r.draft_body !== undefined;
  gen.error = r.refine_error || ""; gen.oldVersion = null;
}
function genDirty() { return gen.draftSaving || gen.body !== gen.savedDraft; }
function draftStatus() {
  const el = $("#genDraftStatus");
  if (el) el.textContent = gen.error || (genDirty() ? "수정 내용을 보관하는 중입니다…" : "이전 결과에 자동으로 보관됩니다");
}
async function flushGenDraft() {
  clearTimeout(gen.draftTimer);
  const r = gen.output;
  if (!r?.history_id) return;
  const body = gen.body, id = r.history_id, version = r.version;
  gen.draftTask = gen.draftTask.catch(() => {}).then(async () => {
    if (body === gen.savedDraft && gen.output?.history_id === id && gen.output?.version === version) return;
    gen.draftSaving = true; draftStatus();
    try {
      await api(`/api/generate/history/${id}/draft`, { method: "PUT", body: { version, body } });
      if (gen.output?.history_id === id && gen.output.version === version) { gen.savedDraft = body; gen.error = ""; }
    } catch (e) { gen.error = "수정 내용을 보관하지 못했습니다: " + e.message; throw e; }
    finally { gen.draftSaving = false; draftStatus(); }
  });
  return gen.draftTask;
}

// 목록 번호처럼 숫자 근거가 아닌 것은 빼고, 취합 집계에 없는 숫자만 골라 보여 준다.
function genNumberKey(text) {
  let t = text.replace(/,/g, "").replace(/^\+/, "");
  const negative = t.startsWith("-");
  if (negative) t = t.slice(1);
  let [whole, frac = ""] = t.split(".");
  whole = whole.replace(/^0+(?=\d)/, ""); frac = frac.replace(/0+$/, "");
  const out = (whole || "0") + (frac ? "." + frac : "");
  return (negative && out !== "0" ? "-" : "") + out;
}
function genUnverifiedNumbers(text, known) {
  const set = new Set(known || []), out = [];
  for (const m of text.matchAll(/(?<![\w.])[+-]?\d[\d,]*(?:\.\d+)?/g)) {
    const lineStart = text.lastIndexOf("\n", m.index - 1) + 1;
    if (!text.slice(lineStart, m.index).trim() && /^[.)]/.test(text.slice(m.index + m[0].length))) continue;
    const key = genNumberKey(m[0].replace(/,$/, ""));
    if (!set.has(key) && !out.includes(m[0])) out.push(m[0]);
  }
  return out;
}
function genNumberCheck(r) {
  if (!r.collection?.known_numbers) return "";
  const unknown = genUnverifiedNumbers(gen.body, r.collection.known_numbers);
  return unknown.length
    ? `<div class="note" id="genNumberCheck" style="color:var(--warn)">취합 집계에서 확인되지 않은 숫자 ${unknown.length}개: ${esc(unknown.slice(0, 10).join(", "))}${unknown.length > 10 ? " 외" : ""}. AI가 새로 계산했거나 잘못 옮긴 값일 수 있으니 취합표와 비교해 주세요.</div>`
    : '<div class="note" id="genNumberCheck">본문의 숫자는 모두 취합 집계·원문에 있는 값입니다.</div>';
}

function renderGenOutput() {
  const host = $("#genOut"), r = gen.output;
  if (!host) return;
  if (!r || (!r.output && !r.history_id)) { host.innerHTML = ""; return; }
  const ids = new Set((r.sources || []).map((s) => s.id));
  const content = esc(gen.body).replace(/\[M(\d+)\]/g, (text, id) => ids.has(+id)
    ? `<a href="#" data-output-source="${id}" title="참고한 메모 열기">${text}</a>` : text)
    .replace(/\[C(\d+)\]/g, (text, id) => r.collection && +id === r.collection.id
      ? `<a href="#" data-output-collection="${id}" title="취합표 열기">${text}</a>` : text);
  const used = r.collection ? `${esc(genCollectionLabel(r.collection))} · 출처를 누르면 취합표가 열립니다`
    : `참고한 메모 ${(r.sources || []).length}건 · 출처를 누르면 원문이 열립니다`;
  const basis = r.collection
    ? `<details style="margin-top:12px"><summary>사용한 취합 자료 (${r.collection.version}번째 취합 당시 근거)</summary><pre class="pack">${esc(r.collection.material || "")}</pre></details>`
    : `<details style="margin-top:12px"><summary>참고했던 메모</summary>${(r.sources || []).map((s) => `<details style="margin:8px 0"><summary>${esc(s.title)} · ${esc((s.created_at || "").slice(0, 10))}</summary><pre class="pack">${esc(s.text)}</pre></details>`).join("")}</details>`;
  host.innerHTML = `<div class="panel">
    <div class="row"><h3 style="margin:0">정리한 내용</h3><span class="spacer"></span>
      <button class="btn" id="genEdit" ${gen.editing ? "hidden" : ""}>직접 수정</button><button class="btn" id="genDone" ${gen.editing ? "" : "hidden"}>수정 완료</button>
      <button class="btn primary" id="genMemo">${r.memo_id && gen.body === r.output ? "저장한 메모 열기" : "메모로 저장"}</button></div>
    <div class="note" id="genDraftStatus" aria-live="polite" style="margin:9px 0"></div>
    ${gen.editing ? `<textarea id="genBody" aria-label="정리한 내용 수정" maxlength="200000" style="width:100%;min-height:300px;font:inherit;line-height:1.7;padding:12px;background:var(--panel-2);border:1px solid var(--line);border-radius:8px">${esc(gen.body)}</textarea>` : `<pre class="pack" style="max-height:none">${content}</pre>`}
    ${genNumberCheck(r)}
    <div class="row" style="margin:10px 0"><button class="btn ghost" id="copyOut">복사</button><button class="btn ghost" id="dlOut">파일로 저장</button><span class="meta" style="white-space:normal">${used}</span></div>
    <div style="border-top:1px solid var(--line);padding-top:14px;margin-top:14px">
      <label for="genFollowup"><b>어떻게 바꿀까요?</b></label>
      <div class="row" style="margin:8px 0">${["더 짧게", "보고용으로", "표로 정리"].map((s) => `<button class="btn ghost" data-gen-quick="${s}">${s}</button>`).join("")}</div>
      <div class="row"><input id="genFollowup" aria-label="다듬을 내용" maxlength="2000" placeholder="예) 결정 사항만 남기고 더 짧게 해줘" value="${esc(gen.followup)}" style="flex:1;padding:10px;border:1px solid var(--line);border-radius:7px;background:var(--panel-2)" /><button class="btn" id="genRefine">다듬기</button></div>
      <div class="note">현재 내용과 참고했던 메모로 다듬습니다. 이전 내용도 남겨둡니다.</div>
    </div>
    <details style="margin-top:16px"><summary>이전 내용 보기</summary><select id="genOldSelect" aria-label="이전 내용 선택" style="max-width:100%;margin:10px 0"><option value="">확인할 내용을 고르세요</option>${(r.versions || []).map((v) => `<option value="${v.version}">${v.version} · ${esc(({ generated:"처음 정리", edited:"직접 수정", refined:"AI로 다듬기", restored:"되돌린 내용" })[v.kind] || "정리")} · ${esc(v.request || fmt(v.created_at))}</option>`).join("")}</select><div id="genOldPreview"></div></details>
    ${basis}
  </div>`;
  host.querySelectorAll("[data-output-source]").forEach((a) => { a.onclick = (e) => { e.preventDefault(); openMemo(+a.dataset.outputSource); }; });
  host.querySelectorAll("[data-output-collection]").forEach((a) => { a.onclick = (e) => { e.preventDefault(); colOpen(r.collection.id, r.collection.version); }; });
  $("#genEdit").onclick = () => { gen.editing = true; renderGenOutput(); $("#genBody").focus(); };
  $("#genDone").onclick = () => genAction("save");
  $("#genMemo").onclick = () => r.memo_id && gen.body === r.output ? openMemo(r.memo_id) : genAction("memo");
  if ($("#genBody")) $("#genBody").oninput = (e) => {
    gen.body = e.target.value; gen.error = ""; draftStatus();
    if ($("#genNumberCheck")) $("#genNumberCheck").outerHTML = genNumberCheck(r);
    $("#genMemo").textContent = r.memo_id && gen.body === r.output ? "저장한 메모 열기" : "메모로 저장";
    clearTimeout(gen.draftTimer); gen.draftTimer = setTimeout(() => flushGenDraft().catch(() => {}), 400);
  };
  $("#genFollowup").oninput = (e) => { gen.followup = e.target.value; };
  $("#genFollowup").onkeydown = (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); genAction("refine"); } };
  host.querySelectorAll("[data-gen-quick]").forEach((b) => { b.onclick = () => { gen.followup = b.dataset.genQuick; $("#genFollowup").value = gen.followup; $("#genFollowup").focus(); }; });
  $("#genRefine").onclick = () => genAction("refine");
  $("#copyOut").onclick = () => navigator.clipboard.writeText(gen.body).then(() => toast("복사했습니다"));
  $("#dlOut").onclick = downloadGenOutput; $("#genOldSelect").onchange = showGenVersion;
  draftStatus();
}

async function genAction(action) {
  if (gen.busy || !gen.output?.history_id) return;
  if (!gen.body.trim()) { toast("내용을 입력하세요"); return; }
  if (action === "refine" && !gen.followup.trim()) { $("#genFollowup").focus(); toast("바꾸고 싶은 내용을 적어주세요"); return; }
  gen.busy = true; setGenBusy(true); gen.error = "";
  const initial = gen.output;
  genProgress(action === "refine" ? "요청에 맞게 다듬는 중입니다…" : "저장하는 중입니다…");
  try {
    await flushGenDraft();
    const endpoint = action === "refine" ? "refine" : "save";
    const r = await api(`/api/generate/history/${initial.history_id}/${endpoint}`, { method: "POST", body: { version: initial.version, body: gen.body, request: gen.followup.trim() } });
    adoptGenResult(r);
    if (action === "memo") {
      const saved = await api(`/api/generate/history/${r.history_id}/memo`, { method: "POST", body: { version: r.version } });
      gen.output.memo_id = saved.memo_id; loadSidebar(); loadTags(); toast("내 메모에 저장했습니다");
    }
    if (action === "refine" && !r.refine_error) gen.followup = "";
    if (state.tab === "generate") { renderGenOutput(); loadGenHistory(); }
    genProgress(r.refine_error || "");
  } catch (e) { gen.error = e.message; draftStatus(); genProgress(e.message); }
  finally { gen.busy = false; setGenBusy(false); }
}

async function showGenVersion() {
  const target = $("#genOldPreview"), number = +$("#genOldSelect").value, id = gen.output.history_id;
  target.innerHTML = ""; gen.oldVersion = null;
  if (!number) return;
  try {
    const old = await api(`/api/generate/history/${id}?version=${number}`);
    if (!target.isConnected || +$("#genOldSelect").value !== number || gen.output.history_id !== id) return;
    gen.oldVersion = old;
    target.innerHTML = `<pre class="pack">${esc(old.output)}</pre><button class="btn" id="genRestore">이 내용으로 되돌리기</button>`;
    $("#genRestore").disabled = gen.busy; $("#genRestore").onclick = restoreGenVersion;
  } catch (e) { if (target.isConnected) target.textContent = e.message; }
}
async function restoreGenVersion() {
  if (gen.busy || !gen.oldVersion) return;
  const old = gen.oldVersion, current = gen.output;
  gen.busy = true; setGenBusy(true);
  try {
    await flushGenDraft();
    const r = await api(`/api/generate/history/${current.history_id}/restore`, { method: "POST", body: { version: current.version, target_version: old.version } });
    adoptGenResult(r);
    if (state.tab === "generate") { renderGenOutput(); loadGenHistory(); }
    toast("선택한 내용으로 되돌렸습니다. 바꾸기 전 내용도 남아 있습니다");
  } catch (e) { genProgress(e.message); }
  finally { gen.busy = false; setGenBusy(false); }
}
async function downloadGenOutput() {
  const name = `memoinall-${gen.output.instruction.slice(0, 20).replace(/[^\w가-힣]+/g, "_")}.md`;
  if (window.pywebview?.api?.save) { const ok = await window.pywebview.api.save(gen.body, name); toast(ok ? "저장했습니다" : "저장을 취소했습니다"); return; }
  const a = document.createElement("a"), url = URL.createObjectURL(new Blob([gen.body], { type: "text/markdown;charset=utf-8" }));
  a.href = url; a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function paintGenHistory(r) {
  const target = $("#genHistoryList");
  if (!target) return;
  target.innerHTML = r.items.map((h) => `<button class="btn ghost" data-gen-history="${h.id}" style="display:block;text-align:left;white-space:normal;margin-bottom:7px">${esc(h.title)}<span class="meta"> · ${esc(fmt(h.updated_at))}${h.has_draft ? " · 수정 중" : ""}</span></button>`).join("") || '<div class="status">아직 보관된 결과가 없습니다.</div>';
  target.querySelectorAll("[data-gen-history]").forEach((b) => { b.disabled = gen.busy; b.onclick = () => openGenHistory(+b.dataset.genHistory); });
  $("#genHistoryPrev").disabled = gen.historyOffset === 0; $("#genHistoryNext").disabled = !r.has_more;
}
async function loadGenHistory() {
  const target = $("#genHistoryList"), q = gen.historyQuery, offset = gen.historyOffset;
  if (!target) return;
  try {
    const r = await api(`/api/generate/history?${new URLSearchParams({ q, offset })}`);
    if (target.isConnected && q === gen.historyQuery && offset === gen.historyOffset) paintGenHistory(r);
  } catch (e) { if (target.isConnected) target.textContent = "이전 결과를 불러오지 못했습니다: " + e.message; }
}
async function openGenHistory(id) {
  if (gen.busy) return;
  gen.busy = true; setGenBusy(true);
  try {
    await flushGenDraft();
    const r = await api(`/api/generate/history/${id}`);
    adoptGenResult(r); gen.instruction = r.instruction; gen.format = r.format;
    gen.memoIds = null; gen.review = gen.plan = gen.queries = null; gen.scope = r.scope || null; gen.followup = "";
    gen.collection = r.collection || null;
    if (state.tab === "generate") await render();
  } catch (e) { genProgress(e.message); }
  finally { gen.busy = false; setGenBusy(false); }
}
