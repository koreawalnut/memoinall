// Schedules open in the calendar; list view remains available for task follow-up.
const todoBoard = { view: "today", layout: "calendar", offset: 0, items: [], undo: null, busy: false, date: null, tag: null, tagChoices: [] };
const todoViews = [
  ["today", "오늘 챙길 일"], ["upcoming", "다가오는 일"], ["waiting", "기다리는 일"],
  ["unscheduled", "일정 미정"], ["review", "확인 필요"], ["done", "완료"], ["dismissed", "제외"],
];
const todoStatuses = { open: "할 일", waiting: "기다리는 중", review: "확인 필요", done: "완료", dismissed: "제외" };

function todoLocalDate(delta = 0) {
  const value = new Date();
  value.setDate(value.getDate() + delta);
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
}

function todoDate(value) {
  if (!value) return "";
  const date = new Date(value + "T12:00:00");
  return `${date.getMonth() + 1}/${date.getDate()}(${["일", "월", "화", "수", "목", "금", "토"][date.getDay()]})`;
}

function todoCard(item, today) {
  const history = ["done", "dismissed"].includes(item.status);
  const badges = [];
  if (item.origin === "manual") badges.push('<span class="badge">직접 추가</span>');
  if (item.origin === "ai" || item.ai_sources?.length) badges.push('<span class="badge">AI 정리</span>');
  if (item.status !== "open") badges.push(`<span class="badge">${todoStatuses[item.status]}</span>`);
  if (item.due) badges.push(`<span class="badge ${item.overdue ? "todo-overdue" : "todo-deadline"}">${item.overdue ? "기한 지남 · " : item.due === today ? "오늘 마감 · " : "마감 "}${todoDate(item.due)}</span>`);
  if (item.review_on) badges.push(`<span class="badge">${item.review_on <= today ? "다시 볼 날 · " : "다시 보기 "}${todoDate(item.review_on)}</span>`);
  if (item.scheduled_for) {
    const range = item.schedule_end && item.schedule_end !== item.scheduled_for;
    badges.push(`<span class="badge">예정 ${todoDate(item.scheduled_for)}${range ? "~" + todoDate(item.schedule_end) : ""}${range ? " 중" : ""}</span>`);
  }
  if (item.date_label && (!item.scheduled_for || item.date_label.includes("기한 미정") || item.date_label.includes("확인 필요"))) badges.push(`<span class="badge">${esc(item.date_label)}</span>`);
  const button = (action, text) => `<button type="button" class="btn ghost" data-todo-action="${action}" data-id="${item.id}">${text}</button>`;
  const actions = history ? button("restore", "다시 열기") :
    (item.status === "review" ? button("accept", "내 할 일로") : (item.group === "today" ? "" : button("today", "오늘 보기")) + button("tomorrow", "내일 다시 보기")) +
    button("edit", "수정·날짜") + button("dismiss", "할 일 아님");
  return `<article class="todo" data-task-id="${item.id}">
    <input type="checkbox" data-todo-action="complete" data-id="${item.id}" aria-label="${esc(item.title)} 완료" ${item.status === "done" ? "checked" : ""} ${item.status === "dismissed" ? "disabled" : ""}/>
    <div class="todo-body">
      <div class="todo-title ${history ? "todo-history-title" : ""}">${esc(item.title)}</div>
      <div class="badges">${badges.join("")}</div>
      ${scheduleItemTags(item)}
      ${item.reason && !history ? `<p class="todo-reason">${esc(item.reason)}</p>` : ""}
      ${item.source_changed ? '<p class="todo-reason">근거 메모가 변경되었습니다. 원문을 확인하거나 AI로 다시 정리해 주세요.</p>' : ""}
      <details class="todo-evidence"><summary>${item.origin === "manual" ? "등록 내용 보기" : "메모 근거 보기"}</summary>
        <blockquote>${esc(item.text)}</blockquote>
        <button class="btn ghost" type="button" data-todo-memo="${item.memo_id}">${esc(item.memo_title)} · 원문 열기</button>
        ${(item.ai_sources || []).filter((source) => source.memo_id !== item.memo_id).map((source) => `<blockquote>${esc(source.quote)}</blockquote>${source.missing ? '<span>삭제된 메모</span>' : `<button type="button" class="btn ghost" data-todo-memo="${source.memo_id}">${esc(source.title)} · 관련 원문 열기</button>`}`).join("")}
      </details>
      <div class="todo-actions">${actions}</div>
    </div>
  </article>`;
}

async function renderTodoBoard(version) {
  const calendar = todoBoard.layout === "calendar";
  todoCalendarInit();
  const params = new URLSearchParams({ ...(calendar ? { month: todoCalendar.month, selected: todoCalendar.selected } : { view: todoBoard.view }), offset: todoBoard.offset, limit: 50, today: todoLocalDate() });
  if (todoBoard.tag) params.set("tag", todoBoard.tag);
  const data = await api(`/api/todos${calendar ? "/calendar" : ""}?${params}`);
  if (version !== renderVersion || state.tab !== "todos") return;
  if (!data.items.length && todoBoard.offset > 0) {
    todoBoard.offset = 0;
    return renderTodoBoard(version);
  }
  todoBoard.items = data.items;
  todoBoard.date = data.today;
  todoBoard.tagChoices = data.tag_choices || [];
  todoBoard.registeredTags = data.registered_tags;
  const empty = {
    today: "오늘 챙길 일은 없습니다. 날짜가 없는 일은 ‘일정 미정’에서 오늘 볼 일로 고를 수 있습니다.",
    upcoming: "앞으로 예정된 일이 없습니다. 날짜가 적힌 메모는 여기에 자동으로 모입니다.",
    waiting: "기다리는 일이 없습니다. 회신 요청이나 선행 조건이 있는 일은 여기에 모입니다.",
    unscheduled: "일정이 정해지지 않은 일이 없습니다.",
    review: "확인이 필요한 항목이 없습니다.",
    done: "완료한 일이 없습니다. 완료 체크한 항목은 여기에서 다시 열 수 있습니다.",
    dismissed: "제외한 항목이 없습니다. ‘할 일 아님’으로 제외한 항목은 여기에서 다시 열 수 있습니다.",
  };
  $("#view").innerHTML = `<section class="todo-board ${calendar ? "calendar-layout" : "list-layout"}" aria-label="일정 보드">
    <div class="todo-overview"><div class="todo-overview-head"><div class="todo-today-summary"><span>${todoIcon("sun")} ${todoDate(data.today)}</span><strong>오늘 챙길 일 ${data.counts.today}건</strong></div>${todoLayoutButtons()}</div></div>
    ${scheduleToolbar()}
    ${todoBoard.undo ? `<div class="todo-undo" role="status">${esc(todoBoard.undo.message)} <button class="btn ghost" id="todoUndo">되돌리기</button></div>` : ""}
    <p id="todoError" class="todo-error" role="alert" hidden></p>
    ${calendar ? todoCalendarHTML(data) : `<div class="todo-filters" role="group" aria-label="할 일 분류">${todoViews.map(([key, label]) =>
      `<button type="button" class="chip ${todoBoard.view === key ? "on" : ""}" data-todo-view="${key}" aria-pressed="${todoBoard.view === key}">${label} <span class="n">${data.counts[key]}</span></button>`).join("")}</div>
    <h3 class="todo-group-title">${todoViews.find(([key]) => key === todoBoard.view)[1]} <span>${data.total}건</span></h3>
    ${todoBoard.view === "review" && data.total ? '<p class="todo-help">다른 사람의 일이나 가져온 자료일 수 있습니다. 내가 챙길 일만 선택해 주세요.</p>' : ""}
    ${data.items.length ? `<div class="todo-list">${data.items.map((item) => todoCard(item, data.today)).join("")}</div>` : `<div class="empty">${empty[todoBoard.view]}<br><span class="todo-example">예: “금요일까지 교육계획안 보내야 함”</span></div>`}
    <div class="row todo-pager"><button class="btn" id="todoPrev" ${todoBoard.offset ? "" : "disabled"}>이전</button>
      <span class="hint">${data.total ? `${todoBoard.offset + 1}–${todoBoard.offset + data.items.length} / ${data.total}건` : "0건"}</span>
      <button class="btn" id="todoNext" ${data.has_more ? "" : "disabled"}>다음</button></div>
    <p class="todo-help">‘내일 다시 보기’를 눌러도 마감일은 유지됩니다. 오늘 마감이거나 기한을 넘긴 일은 오늘 목록에 계속 표시됩니다.</p>`}
    <section class="todo-assist" aria-label="선택적 AI 정리"><div class="todo-assist-heading">${todoIcon("sparkles")} <span>메모에서 놓친 할 일도 찾아보세요</span><span class="todo-optional">선택 기능</span></div>${todoAiToolbar(data.ai)}${todoAiPanel()}</section>
  </section>`;
  document.querySelectorAll("[data-todo-layout]").forEach((button) => {
    button.onclick = () => { todoBoard.layout = button.dataset.todoLayout; todoBoard.offset = 0; render(); };
  });
  document.querySelectorAll("[data-todo-view]").forEach((button) => {
    button.onclick = () => { todoBoard.layout = "list"; todoBoard.view = button.dataset.todoView; todoBoard.offset = 0; render(); };
  });
  $("#todoPrev").onclick = () => { todoBoard.offset = Math.max(0, todoBoard.offset - 50); render(); };
  $("#todoNext").onclick = () => { todoBoard.offset += 50; render(); };
  document.querySelectorAll("[data-todo-memo]").forEach((button) => {
    button.onclick = () => openMemo(+button.dataset.todoMemo);
  });
  document.querySelectorAll("[data-todo-action]").forEach((control) => {
    const action = async () => {
      const item = todoBoard.items.find((candidate) => candidate.id === +control.dataset.id);
      if (control.dataset.todoAction === "edit") return openTodoEditor(item);
      const actions = {
        complete: [{ status: control.checked ? "done" : "open" }, control.checked ? "완료했습니다." : "다시 열었습니다."],
        today: [{ review_on: todoLocalDate() }, "오늘 챙길 일에 추가했습니다."],
        tomorrow: [{ review_on: todoLocalDate(1) }, "내일 다시 볼 날짜를 저장했습니다. 마감일은 그대로 유지됩니다."],
        dismiss: [{ status: "dismissed" }, "할 일에서 제외했습니다."],
        accept: [{ status: "open" }, "내 할 일로 등록했습니다."],
        restore: [{ status: "open" }, "할 일을 다시 열었습니다."],
      };
      const [payload, message] = actions[control.dataset.todoAction];
      await changeTodo(item, payload, message);
    };
    if (control.type === "checkbox") control.onchange = action;
    else control.onclick = action;
  });
  if ($("#todoUndo")) $("#todoUndo").onclick = undoTodo;
  if (calendar) bindTodoCalendar();
  bindTodoAi();
  bindSchedule();
}

function todoError(error) {
  if ($("#todoError")) { $("#todoError").hidden = false; $("#todoError").textContent = error.message; }
  toast(error.message);
}

async function refreshTodoBoard() {
  if (state.tab !== "todos") return;
  const version = ++renderVersion;
  const focusId = document.activeElement?.id;
  try {
    // Keep the old board's height while fetching so choosing a calendar day
    // doesn't collapse the page and jump the user back to the top.
    await renderTodoBoard(version);
    if (version === renderVersion && focusId) document.getElementById(focusId)?.focus({ preventScroll: true });
  } catch (error) { if (version === renderVersion) todoError(error); }
}

async function changeTodo(item, payload, message) {
  if (todoBoard.busy) return false;
  todoBoard.busy = true;
  document.querySelectorAll("[data-todo-action], #todoUndo").forEach((el) => { el.disabled = true; });
  try {
    await api(`/api/todos/${item.id}`, { method: "PATCH", body: payload });
    const keys = [...new Set([...Object.keys(payload).filter((key) => key !== "reset"), ...(payload.reset || [])])];
    const undo = { reset: keys.filter((key) => !item.manual_fields.includes(key)) };
    for (const key of keys.filter((key) => item.manual_fields.includes(key))) undo[key] = item[key];
    todoBoard.undo = { id: item.id, payload: undo, message };
    if (state.tab === "todos") await refreshTodoBoard();
    loadSidebar().catch(() => {});
    return true;
  } catch (error) {
    todoError(error);
    const check = document.querySelector(`[data-task-id="${item.id}"] input[type="checkbox"]`);
    if (check) check.checked = item.status === "done";
    return false;
  } finally {
    todoBoard.busy = false;
    document.querySelectorAll("[data-todo-action], #todoUndo").forEach((el) => {
      el.disabled = el.type === "checkbox" && todoBoard.items.find((item) => item.id === +el.dataset.id)?.status === "dismissed";
    });
  }
}

async function undoTodo() {
  if (todoBoard.busy || !todoBoard.undo) return;
  todoBoard.busy = true;
  $("#todoUndo").disabled = true;
  try {
    const undo = todoBoard.undo;
    await api(`/api/todos/${undo.id}`, { method: "PATCH", body: undo.payload });
    todoBoard.undo = null;
    if (state.tab === "todos") await refreshTodoBoard();
    loadSidebar().catch(() => {});
  } catch (error) { todoError(error); }
  finally { todoBoard.busy = false; if ($("#todoUndo")) $("#todoUndo").disabled = false; }
}

function openTodoEditor(item) {
  if (todoBoard.busy) return;
  let dialog = $("#todoEditor");
  if (!dialog) { dialog = document.createElement("dialog"); dialog.id = "todoEditor"; document.body.append(dialog); }
  dialog.innerHTML = `<form id="todoEditForm">
    <div class="dlg-h"><strong id="todoEditorTitle">일정 수정</strong></div>
    <div class="dlg-b">
      <div class="field"><label for="todoTitle">할 일</label><input id="todoTitle" required maxlength="500" value="${esc(item.title)}"></div>
      <div class="field"><label for="todoStatus">상태</label><select id="todoStatus">${Object.entries(todoStatuses).map(([key, label]) => `<option value="${key}" ${item.status === key ? "selected" : ""}>${label}</option>`).join("")}</select></div>
      <div class="calendar-form-dates"><div class="field"><label for="todoPlanned">예정일</label><input id="todoPlanned" type="date" value="${item.scheduled_for || ""}"></div>
      <div class="field"><label for="todoPlannedEnd">예정 종료일 (선택)</label><input id="todoPlannedEnd" type="date" value="${item.schedule_end || ""}"></div></div>
      <div class="field"><label for="todoDue">마감일</label><input id="todoDue" type="date" value="${item.due || ""}"></div>
      <div class="field"><label for="todoReview">다시 볼 날짜</label><input id="todoReview" type="date" value="${item.review_on || ""}"></div>
      <p class="todo-help">마감일은 끝내야 하는 날, 다시 볼 날짜는 이 일을 챙길 날입니다. 비워 두어도 됩니다.</p>
      <p id="todoEditError" class="todo-error" role="alert"></p>
    </div>
    <div class="dlg-f"><button type="button" class="btn ghost" id="todoReset" ${item.manual_fields.length ? "" : "disabled"}>${item.origin === "manual" ? "처음 등록값으로 되돌리기" : item.ai_fields?.length ? "AI 정리값으로 되돌리기" : "메모에서 다시 읽기"}</button>
      <button type="button" class="btn" id="todoCancel">닫기</button><button type="submit" class="btn primary">저장</button></div>
  </form>`;
  dialog.setAttribute("aria-labelledby", "todoEditorTitle");
  let saving = false;
  dialog.oncancel = (event) => { if (saving) event.preventDefault(); };
  $("#todoCancel").onclick = () => { if (!saving) dialog.close(); };
  const save = async (payload) => {
    if (saving) return;
    saving = true;
    dialog.querySelectorAll("button").forEach((button) => { button.disabled = true; });
    const ok = await changeTodo(item, payload, "할 일을 수정했습니다.");
    saving = false;
    if (ok) dialog.close();
    else {
      $("#todoEditError").textContent = "저장하지 못했습니다. 입력한 내용은 그대로 남아 있습니다. 다시 시도해 주세요.";
      dialog.querySelectorAll("button").forEach((button) => { button.disabled = false; });
      $("#todoReset").disabled = !item.manual_fields.length;
    }
  };
  $("#todoEditForm").onsubmit = async (event) => {
    event.preventDefault();
    const values = { title: $("#todoTitle").value.trim(), status: $("#todoStatus").value,
      scheduled_for: $("#todoPlanned").value || null, schedule_end: $("#todoPlannedEnd").value || null,
      due: $("#todoDue").value || null, review_on: $("#todoReview").value || null };
    const payload = Object.fromEntries(Object.entries(values).filter(([key, value]) => value !== item[key]));
    if (!Object.keys(payload).length) { dialog.close(); return; }
    await save(payload);
  };
  $("#todoReset").onclick = () => save({ reset: ["title", "status", "due", "review_on", "scheduled_for", "schedule_end"] });
  dialog.showModal();
}

function refreshTodoDay() {
  if (typeof state !== "undefined" && state.tab === "todos" && todoBoard.date && todoBoard.date !== todoLocalDate()
      && !todoBoard.busy && !document.querySelector("dialog[open]")) {
    if (todoCalendar.selected === todoBoard.date) {
      todoCalendar.selected = todoLocalDate();
      todoCalendar.month = todoLocalDate().slice(0, 7);
      todoBoard.offset = 0;
    }
    render();
  }
}
window.addEventListener("focus", refreshTodoDay);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshTodoDay(); });
setInterval(refreshTodoDay, 60000);
