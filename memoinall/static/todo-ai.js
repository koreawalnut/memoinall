// AI is invoked only by the user's button; changing tabs never starts a call.
const todoAi = { days: 30, busy: false, result: null, selected: new Set(), error: "", applied: false, undone: false };
const todoAiFields = { title: "할 일", status: "상태", due: "마감일", review_on: "다시 볼 날짜" };

function todoAiToolbar(info) {
  return `<div class="todo-ai-tools row">
    <label for="todoAiDays">할 일·날짜를 찾을 기간</label>
    <select id="todoAiDays" ${todoAi.busy ? "disabled" : ""}>${[7, 30, 90].map((days) => `<option value="${days}" ${todoAi.days === days ? "selected" : ""}>최근 ${days}일</option>`).join("")}</select>
    <button type="button" class="btn primary" id="todoAiRun" ${!info?.ready || todoAi.busy ? "disabled" : ""}>${todoAi.busy ? "제안을 찾는 중…" : "할 일·날짜 제안받기"}</button>
    ${!info?.ready ? '<button type="button" class="btn ghost" id="todoAiSettings">AI 설정</button>' : ""}
    <p class="todo-help todo-ai-description">${info?.ready ? `${esc(info.provider)} · ${esc(info.model)}. 버튼을 누르면 선택한 기간의 최신 메모 최대 30건을 보내 정리합니다. 결과를 확인한 뒤 적용할 수 있습니다.` : esc(info?.reason || "AI 설정을 확인해 주세요.")}</p>
  </div>`;
}

function todoAiValue(key, value) {
  return esc(key === "status" ? todoStatuses[value] : value || "미정");
}

function todoAiPanel() {
  const result = todoAi.result;
  if (!result && !todoAi.busy && !todoAi.error) return "";
  const content = result ? `<p class="todo-help">${esc(result.provider)} · ${esc(result.model)} · ${result.scope.since}~${result.scope.until} 메모 ${result.scope.memo_count}건 분석
    ${result.scope.omitted_memos ? ` · ${result.scope.omitted_memos}건은 이번 분석에서 제외` : ""}${result.scope.partial_memos ? ` · 긴 메모 ${result.scope.partial_memos}건은 일부만 분석` : ""}</p>
    ${result.rejected ? `<p class="todo-help">근거나 형식을 확인할 수 없는 제안 ${result.rejected}건은 제외했습니다.</p>` : ""}
    ${result.suggestions.length ? `<p class="todo-help">직접 수정한 값은 유지됩니다. 완료·제외 제안은 확인 후 직접 선택해 주세요.</p>
      <div class="todo-ai-proposals">${result.suggestions.map((item) => `<article class="todo-ai-proposal" data-ai-proposal="${item.index}">
        <label class="todo-ai-select"><input type="checkbox" data-ai-select="${item.index}" ${todoAi.selected.has(item.index) ? "checked" : ""} ${todoAi.busy || todoAi.applied ? "disabled" : ""}>
          <strong>${item.todo_id === null ? "새 할 일 · " : "변경 · "}${esc(item.after.title)}</strong></label>
        <dl>${Object.keys(item.changes).map((key) => `<div><dt>${todoAiFields[key]}</dt><dd>${item.before ? `<span class="todo-ai-before">${todoAiValue(key, item.before[key])}</span> → ` : ""}${todoAiValue(key, item.after[key])}</dd></div>`).join("")}</dl>
        <p class="todo-help">${esc(item.reason)}</p>
        ${item.protected_fields.length ? `<p class="todo-help">직접 정한 ${item.protected_fields.map((key) => todoAiFields[key]).join("·")} 유지</p>` : ""}
        <details class="todo-evidence"><summary>근거 메모 ${item.evidence.length}건</summary>${item.evidence.map((ref) => `<blockquote>${esc(ref.quote)}</blockquote><button type="button" class="btn ghost" data-ai-memo="${ref.memo_id}">${esc(ref.title)} · 원문 열기</button>`).join("")}</details>
      </article>`).join("")}</div>` : '<p class="todo-help">추가하거나 바꿀 할 일을 찾지 못했습니다. 기존 목록은 그대로 유지됩니다.</p>'}
    <div class="row todo-ai-footer">${todoAi.applied ? (todoAi.undone ? '<span role="status">AI 적용을 취소했습니다.</span>' : '<span role="status">선택한 제안을 적용했습니다.</span><button type="button" class="btn" id="todoAiUndo">적용 취소</button>') : (result.suggestions.length ? `<button type="button" class="btn primary" id="todoAiApply" ${todoAi.busy || !todoAi.selected.size ? "disabled" : ""}>선택한 ${todoAi.selected.size}건 적용</button>` : "")}
      <button type="button" class="btn ghost" id="todoAiClose" ${todoAi.busy ? "disabled" : ""}>결과 닫기</button></div>` : "";
  return `<section class="todo-ai-panel" aria-label="AI 할 일 정리 결과">
    <h3>AI 정리 결과</h3>${todoAi.busy ? '<p role="status" class="todo-help">처리 중입니다. 분석 결과는 이 화면에 보관되며, 선택해 적용하기 전에는 할 일이 바뀌지 않습니다.</p>' : ""}
    ${todoAi.error ? `<p id="todoAiError" class="todo-error" role="alert">${esc(todoAi.error)}</p>` : ""}${content}</section>`;
}

function bindTodoAi() {
  $("#todoAiDays").onchange = (event) => { todoAi.days = +event.target.value; };
  $("#todoAiRun").onclick = runTodoAi;
  if ($("#todoAiSettings")) $("#todoAiSettings").onclick = () => goTab("settings");
  document.querySelectorAll("[data-ai-select]").forEach((checkbox) => {
    checkbox.onchange = () => {
      const index = +checkbox.dataset.aiSelect;
      if (checkbox.checked) todoAi.selected.add(index); else todoAi.selected.delete(index);
      $("#todoAiApply").disabled = !todoAi.selected.size || todoAi.busy;
      $("#todoAiApply").textContent = `선택한 ${todoAi.selected.size}건 적용`;
    };
  });
  document.querySelectorAll("[data-ai-memo]").forEach((button) => { button.onclick = () => openMemo(+button.dataset.aiMemo); });
  if ($("#todoAiApply")) $("#todoAiApply").onclick = applyTodoAi;
  if ($("#todoAiUndo")) {
    $("#todoAiUndo").disabled = todoAi.busy;
    $("#todoAiUndo").onclick = undoTodoAi;
  }
  if ($("#todoAiClose")) $("#todoAiClose").onclick = () => {
    if (todoAi.busy) return;
    todoAi.result = null; todoAi.error = ""; render();
  };
}

async function todoAiOperation(action) {
  if (todoAi.busy || todoBoard.busy) return;
  todoAi.busy = true; todoAi.error = "";
  // Paint the pending state before the network request; keep previous results on failure.
  if (state.tab === "todos") await render();
  try { await action(); }
  catch (error) { todoAi.error = error.message; }
  finally {
    todoAi.busy = false;
    if (state.tab === "todos") await render();
    loadSidebar().catch(() => {});
  }
}

function runTodoAi() {
  const days = todoAi.days, today = todoLocalDate();
  return todoAiOperation(async () => {
    const result = await api("/api/todos/ai/preview", { method: "POST", body: { days, today } });
    todoAi.result = result;
    todoAi.applied = false; todoAi.undone = false;
    todoAi.selected = new Set(result.suggestions.filter((item) => !["done", "dismissed"].includes(item.after.status)).map((item) => item.index));
  });
}

function applyTodoAi() {
  if (!todoAi.result || todoAi.applied || !todoAi.selected.size) return;
  return todoAiOperation(async () => {
    await api(`/api/todos/ai/${todoAi.result.id}/apply`, { method: "POST", body: { selected: [...todoAi.selected] } });
    todoAi.applied = true;
    todoBoard.undo = null;
  });
}

function undoTodoAi() {
  if (!todoAi.result || !todoAi.applied || todoAi.undone) return;
  return todoAiOperation(async () => {
    await api(`/api/todos/ai/${todoAi.result.id}/undo`, { method: "POST", body: {} });
    todoAi.undone = true;
    todoBoard.undo = null;
  });
}
