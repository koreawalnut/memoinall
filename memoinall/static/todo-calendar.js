const todoCalendar = { month: null, selected: null, focusDay: null, memoOffset: 0 };
const todoCalendarKinds = { due: "마감", review: "다시 보기", scheduled: "예정", range: "기간 중", external: "외부 일정" };

function todoIcon(name) {
  const paths = {
    calendar: '<rect x="3" y="5" width="18" height="16" rx="3"/><path d="M16 3v4M8 3v4M3 11h18M8 15h2M14 15h2"/>',
    list: '<path d="M9 6h12M9 12h12M9 18h12M3 6h.01M3 12h.01M3 18h.01"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    left: '<path d="m14 6-6 6 6 6"/>',
    right: '<path d="m10 6 6 6-6 6"/>',
    import: '<path d="M12 3v12m-4-4 4 4 4-4M5 16v4h14v-4"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1 1m12 12 1 1M5 19l1-1M18 6l1-1"/>',
    sparkles: '<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5ZM20 2v4m-2-2h4"/>',
    check: '<path d="m7 12 3 3 7-7"/><circle cx="12" cy="12" r="9"/>',
    tag: '<path d="M20 13 13 20a2 2 0 0 1-3 0L3 13V3h10l7 7a2 2 0 0 1 0 3Z"/><circle cx="7.5" cy="7.5" r="1"/>',
    share: '<path d="M12 16V3m-4 4 4-4 4 4M5 13v7h14v-7"/>',
  };
  return `<svg class="todo-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.calendar}</svg>`;
}

function todoCalendarInit() {
  if (!todoCalendar.month) todoCalendar.month = todoLocalDate().slice(0, 7);
  if (!todoCalendar.selected) todoCalendar.selected = todoLocalDate();
}

function todoLayoutButtons() {
  return `<div class="todo-layout-toggle" role="group" aria-label="일정 보기 방식">
    <button type="button" class="btn ${todoBoard.layout === "calendar" ? "on" : ""}" data-todo-layout="calendar" aria-pressed="${todoBoard.layout === "calendar"}">${todoIcon("calendar")} 달력</button>
    <button type="button" class="btn ${todoBoard.layout === "list" ? "on" : ""}" data-todo-layout="list" aria-pressed="${todoBoard.layout === "list"}">${todoIcon("list")} 목록</button></div>`;
}

function todoCalendarHTML(data) {
  const monthTitle = `${+data.month.slice(0, 4)}년 ${+data.month.slice(5)}월`;
  const cell = (day) => {
    const selected = day.date === data.selected;
    const kindCounts = Object.entries(todoCalendarKinds).map(([kind, label]) => ({ kind, label,
      count: day.events.filter((event) => event.kinds.includes(kind)).length })).filter((item) => item.count);
    const summary = kindCounts.map((item) => `${item.label} ${item.count}건`).join(", ");
    const mobileKinds = kindCounts.slice(0, 2);
    const otherCount = day.events.filter((event) => !mobileKinds.some((item) => event.kinds.includes(item.kind))).length;
    return `<td class="${day.in_month ? "" : "outside-month"}"><button type="button" class="todo-calendar-day ${selected ? "selected" : ""} ${day.is_today ? "is-today" : ""}"
      data-calendar-day="${day.date}" aria-label="${day.date}, ${day.count ? summary : "일정 없음"}, 작성한 메모 ${day.memo_count}건, Enter로 일정 추가" aria-pressed="${selected}" ${day.is_today ? 'aria-current="date"' : ""} tabindex="${selected ? "0" : "-1"}">
      ${day.memo_count ? `<span class="todo-memo-mark" aria-hidden="true" title="이날 작성한 메모 ${day.memo_count}건">▤</span>` : ""}
      <span class="todo-calendar-number"><span class="todo-calendar-date-number">${+day.date.slice(8)}</span>${day.is_today ? '<span class="todo-calendar-today-label">오늘</span>' : ""}</span>
      <span class="todo-calendar-events">${day.events.slice(0, 2).map((event) => `<span class="todo-calendar-event ${event.overdue ? "overdue" : event.kinds[0]}" title="${esc(event.time_label ? event.time_label + " · " + event.title : event.title)}"><b>${event.kinds[0] === "external" ? esc(event.time_label?.split("–")[0] || "일정") : todoCalendarKinds[event.kinds[0]]}</b> ${esc(event.title)}</span>`).join("")}</span>
      <span class="todo-calendar-dots">${mobileKinds.map(({kind, label, count}) => `<span class="${kind}">${kind === "review" ? "확인" : kind === "external" ? "외부" : label} ${count}</span>`).join("")}${otherCount ? `<span class="todo-calendar-dots-more">+${otherCount}건</span>` : ""}</span>
      ${day.count > 2 ? `<span class="todo-calendar-more">+${day.count - 2}건</span>` : ""}
    </button></td>`;
  };
  const weeks = [];
  for (let index = 0; index < data.days.length; index += 7) weeks.push(`<tr>${data.days.slice(index, index + 7).map(cell).join("")}</tr>`);
  return `<section class="todo-calendar-section" aria-label="일정 달력">
    <div class="todo-calendar-entry"><div class="todo-calendar-month-summary"><span>${todoIcon("check")} 이번 달 내 일정 <b>${data.month_count}</b></span><span class="external">외부 일정 <b>${data.external_count || 0}</b></span></div><div class="todo-calendar-entry-actions"><button type="button" class="btn" id="todoCalendarImport">${todoIcon("import")} 외부 캘린더${data.sources?.length ? ` <span class="todo-connection-count">${data.sources.length}</span>` : ""}</button><button type="button" class="btn primary" id="todoCalendarAdd">${todoIcon("plus")} 일정 추가</button></div></div>
    <div class="todo-calendar-layout"><div class="todo-calendar-month-panel">
    <div class="todo-calendar-toolbar"><div><h3 id="todoCalendarMonth">${monthTitle}</h3><span class="todo-help">이번 달 ${data.month_count}개 할 일${data.external_count ? ` · 외부 일정 ${data.external_count}개` : ""}</span></div>
      <div class="row"><button class="btn" type="button" id="todoMonthPrev" aria-label="이전 달">${todoIcon("left")}</button><button class="btn" type="button" id="todoCalendarToday">오늘</button><button class="btn" type="button" id="todoMonthNext" aria-label="다음 달">${todoIcon("right")}</button></div></div>
    <div class="todo-calendar-legend" aria-label="달력 범례">${Object.entries(todoCalendarKinds).map(([kind, label]) => `<span class="${kind}"><i aria-hidden="true"></i>${label}</span>`).join("")}<span><b aria-hidden="true">▤</b> 작성한 메모</span></div>
    <div class="todo-calendar-grid-wrap"><table class="todo-calendar-table" aria-labelledby="todoCalendarMonth"><thead><tr>${["일", "월", "화", "수", "목", "금", "토"].map((day) => `<th scope="col">${day}</th>`).join("")}</tr></thead><tbody>${weeks.join("")}</tbody></table></div>
    <div class="todo-calendar-notices">
      ${data.counts.today ? `<button class="btn ghost ${data.overdue_count ? "todo-calendar-late" : ""}" data-todo-view="today">오늘 챙길 일 ${data.counts.today}건${data.overdue_count ? ` · 기한 지난 일 ${data.overdue_count}건` : ""}</button>` : ""}
      <button class="btn ghost" data-todo-view="unscheduled">일정 미정 ${data.counts.unscheduled}건</button>
      ${data.undated_waiting ? `<button class="btn ghost" data-todo-view="waiting">날짜 없는 대기 ${data.undated_waiting}건</button>` : ""}
      <button class="btn ghost" data-todo-view="review">확인 필요 ${data.counts.review}건</button>
    </div>
    <p class="todo-calendar-footnote">날짜를 누르면 그날의 일정과 작성한 메모를 보고, <b>두 번 누르거나 Enter</b>를 누르면 바로 일정을 추가합니다. ‘기간 중’은 매일 반복하는 일이 아닙니다.</p>
    </div>
    <section class="todo-calendar-agenda" aria-labelledby="todoCalendarDayTitle">
      <div class="todo-calendar-agenda-head"><span class="todo-agenda-date" aria-hidden="true">${+data.selected.slice(8)}</span><div><p class="todo-agenda-eyebrow">선택한 날짜</p><h3 class="todo-group-title" id="todoCalendarDayTitle">${todoDate(data.selected)}${data.selected === data.today ? " · 오늘" : ""} <span>일정 ${data.total}건</span></h3></div></div>
      ${calendarDayMemos(data.day_memos)}
      <button type="button" class="btn ghost" id="todoCalendarDayAdd">${todoIcon("plus")} 이 날짜에 일정 추가</button>
      ${data.items.length ? `<div class="todo-list">${data.items.map((item) => `<div class="todo-calendar-agenda-item agenda-${item.calendar_kinds[0]}"><div class="todo-calendar-agenda-kinds">${item.calendar_kinds.map((kind) => `<span class="${kind}">${todoCalendarKinds[kind]}</span>`).join("")}</div>${item.external ? externalCalendarCard(item) : todoCard(item, data.today)}</div>`).join("")}</div>` : `<div class="todo-agenda-empty"><span>${todoIcon("calendar")}</span><strong>예정된 일정이 없어요</strong><p>이날 하고 싶은 일을<br>가볍게 적어 두세요.</p></div>`}
      <div class="row todo-pager" ${data.total <= 50 ? "hidden" : ""}><button class="btn" id="todoPrev" ${data.offset ? "" : "disabled"}>이전</button><span class="hint">${data.total ? `${data.offset + 1}–${data.offset + data.items.length} / ${data.total}건` : "0건"}</span><button class="btn" id="todoNext" ${data.has_more ? "" : "disabled"}>다음</button></div>
      <p class="todo-agenda-history">완료한 일은 <button type="button" data-todo-view="done">완료 목록</button>에서 다시 볼 수 있어요.</p>
    </section>
    </div>
  </section>`;
}

function calendarDayMemos({items, total, offset, limit}) {
  return `<section class="calendar-day-memos" aria-labelledby="calendarMemoTitle">
    <h4 id="calendarMemoTitle">이날 작성한 메모 <span>${total}건</span></h4>
    ${items.map((memo) => `<button type="button" class="calendar-memo-row" data-todo-memo="${memo.id}"><time>${esc(memo.time)}</time><span>${esc(memo.title || "제목 없는 메모")}</span></button>`).join("")}
    ${total > limit ? `<div class="row todo-pager"><button type="button" class="btn" id="calendarMemoPrev" ${offset ? "" : "disabled"}>이전 메모</button>
      <span class="hint">${offset + 1}–${offset + items.length} / ${total}건</span>
      <button type="button" class="btn" id="calendarMemoNext" ${offset + limit < total ? "" : "disabled"}>다음 메모</button></div>` : ""}
    </section>`;
}

function todoCalendarISO(value) {
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
}

function selectTodoCalendarDay(day, focus = false) {
  todoCalendar.selected = day;
  todoCalendar.month = day.slice(0, 7);
  todoCalendar.focusDay = focus ? day : null;
  todoCalendar.memoOffset = 0;
  todoBoard.offset = 0;
  return refreshTodoBoard();
}

document.addEventListener("dblclick", (event) => {
  const button = event.target.closest?.("[data-calendar-day]");
  if (button) openTodoCalendarEntry(button.dataset.calendarDay);
});

// 그 날짜로 일정 추가 창을 연다. 이미 열려 있으면 다시 열지 않는다.
function openTodoCalendarEntry(day) {
  if (document.getElementById("todoCreate")?.open) return;
  if (day !== todoCalendar.selected) selectTodoCalendarDay(day, true);
  openTodoCreate(day);
}

function moveTodoCalendarMonth(delta) {
  const day = +todoCalendar.selected.slice(8);
  const month = new Date(todoCalendar.month + "-01T12:00:00");
  month.setMonth(month.getMonth() + delta);
  const last = new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate();
  month.setDate(Math.min(day, last));
  return selectTodoCalendarDay(todoCalendarISO(month));
}

function bindTodoCalendar(memos) {
  for (const [id, delta] of [["calendarMemoPrev", -1], ["calendarMemoNext", 1]]) {
    const button = $("#" + id);
    if (button) button.onclick = () => {
      todoCalendar.memoOffset = memos.offset + delta * memos.limit;
      refreshTodoBoard();
    };
  }
  $("#todoCalendarAdd").onclick = () => openTodoCreate(todoCalendar.selected);
  $("#todoCalendarDayAdd").onclick = () => openTodoCreate(todoCalendar.selected);
  $("#todoCalendarImport").onclick = () => openCalendarImport();
  $("#todoMonthPrev").onclick = () => moveTodoCalendarMonth(-1);
  $("#todoMonthNext").onclick = () => moveTodoCalendarMonth(1);
  $("#todoCalendarToday").onclick = () => selectTodoCalendarDay(todoLocalDate());
  // 한 번 누르면 날짜만 고르고(오른쪽에 그날 일정·메모), 두 번 누르거나 Enter 면 바로 일정 추가 창을 연다.
  // 두 번째 클릭은 브라우저가 세는 클릭 수(detail)로 안다. 이미 고른 날짜를 다시 눌러도 다시 그리지 않아야
  // 두 번째 클릭이 같은 버튼에 닿는다. 혹시 끊기면 아래 문서 전체의 dblclick 이 받는다.
  document.querySelectorAll("[data-calendar-day]").forEach((button) => {
    button.onclick = (event) => {
      const day = button.dataset.calendarDay;
      if (event.detail >= 2) { openTodoCalendarEntry(day); return; }
      if (day !== todoCalendar.selected) selectTodoCalendarDay(day, true);
    };
    button.onkeydown = (event) => {
      if (event.key === "Enter") {
        event.preventDefault();               // 버튼 기본 동작(클릭 = 선택)을 막고 바로 입력
        openTodoCalendarEntry(button.dataset.calendarDay);
        return;
      }
      const steps = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 };
      if (!(event.key in steps)) return;
      event.preventDefault();
      const value = new Date(button.dataset.calendarDay + "T12:00:00");
      value.setDate(value.getDate() + steps[event.key]);
      selectTodoCalendarDay(todoCalendarISO(value), true);
    };
  });
  if (todoCalendar.focusDay) {
    document.querySelector(`[data-calendar-day="${todoCalendar.focusDay}"]`)?.focus({ preventScroll: true });
    todoCalendar.focusDay = null;
  }
}
