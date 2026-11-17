// Schedule tags and explicit, reviewed calendar-file sharing.
function scheduleTagOptions(selected) {
  const names = [...new Set([...(todoBoard.tagChoices || []).map((tag) => tag.name), ...(selected ? [selected] : [])])].sort();
  return '<option value="">모든 태그</option>' + names.map((name) => `<option value="${esc(name)}" ${name === selected ? "selected" : ""}>#${esc(name)}</option>`).join("");
}

function scheduleTagPills(tags, interactive = true) {
  return (tags || []).map((tag) => {
    const color = [...tag].reduce((n, c) => n + c.codePointAt(0), 0) % 5;
    return interactive ? `<button type="button" class="schedule-tag tag-${color}" data-schedule-filter="${esc(tag)}" title="이 태그의 일정 보기">#${esc(tag)}</button>` : `<span class="schedule-tag tag-${color}">#${esc(tag)}</span>`;
  }).join("");
}

function scheduleItemTags(item) {
  const id = item.external ? item.id : `todo-${item.id}`;
  return `<div class="schedule-tags">${scheduleTagPills(item.tags)}<button type="button" class="schedule-tag-edit" data-schedule-tags="${id}" aria-label="${esc(item.title)} 태그 편집">${todoIcon("tag")} ${item.tags?.length ? "편집" : "태그 추가"}</button></div>`;
}

function scheduleTagField(id, tags = []) {
  return `<div class="field"><label for="${id}">태그 (선택)</label><input id="${id}" maxlength="1000" value="${esc(tags.join(", "))}" placeholder="예: 업무, 팀프로젝트, 개인"><p class="todo-help">쉼표로 구분해 최대 20개. 띄어쓰기는 밑줄로 바뀝니다.</p></div>`;
}

function scheduleReadTags(id) {
  return document.getElementById(id).value.split(",").map((value) => value.trim()).filter(Boolean);
}

function scheduleToolbar() {
  return `<div class="schedule-toolbar"><div class="schedule-filter"><label for="scheduleTagFilter">${todoIcon("tag")} 태그</label><select id="scheduleTagFilter">${scheduleTagOptions(todoBoard.tag)}</select>${todoBoard.tag ? '<button class="btn ghost" id="scheduleClearTag" type="button">필터 해제</button>' : ""}</div><button class="btn" type="button" id="scheduleShareOpen">${todoIcon("share")} 일정 공유</button></div>`;
}

function bindSchedule() {
  const filter = (tag) => { todoBoard.tag = tag || null; todoBoard.offset = 0; refreshTodoBoard(); };
  $("#scheduleTagFilter").onchange = (event) => filter(event.target.value);
  if ($("#scheduleClearTag")) $("#scheduleClearTag").onclick = () => filter(null);
  document.querySelectorAll("[data-schedule-filter]").forEach((button) => { button.onclick = () => filter(button.dataset.scheduleFilter); });
  document.querySelectorAll("[data-schedule-tags]").forEach((button) => {
    button.onclick = () => {
      const key = button.dataset.scheduleTags;
      const item = todoBoard.items.find((item) => (item.external ? item.id : `todo-${item.id}`) === key);
      if (item) openScheduleTags(item, key);
    };
  });
  $("#scheduleShareOpen").onclick = openScheduleShare;
}

function openScheduleTags(item, key) {
  const dialog = calendarDialog("scheduleTagsDialog", "일정 태그 편집");
  dialog.innerHTML = `<form id="scheduleTagsForm"><div class="dlg-h"><strong>일정 태그 편집</strong></div><div class="dlg-b"><p class="schedule-dialog-title">${esc(item.title)}</p>${scheduleTagField("scheduleTagsInput", item.tags)}
    <div class="schedule-tag-suggestions">${(todoBoard.tagChoices || []).map((tag) => `<button type="button" class="chip" data-tag-suggestion="${esc(tag.name)}">+ #${esc(tag.name)}</button>`).join("")}</div>
    <p class="todo-help">${item.external ? "이 앱에 붙인 태그는 원본 캘린더를 새로고침해도 유지됩니다." : item.tags_inherited ? "메모의 태그를 가져왔습니다. 저장하면 이 일정의 태그를 따로 관리합니다." : "이 일정의 태그만 바뀝니다."}</p><p id="scheduleTagsError" class="todo-error" role="alert"></p></div>
    <div class="dlg-f"><button class="btn" type="button" id="scheduleTagsClose">닫기</button><button class="btn primary" type="submit">저장</button></div></form>`;
  $("#scheduleTagsClose").onclick = () => dialog.close();
  $("#scheduleTagsForm").oninput = () => { calendarEntry.dirty = true; };
  dialog.querySelectorAll("[data-tag-suggestion]").forEach((button) => {
    button.onclick = () => {
      $("#scheduleTagsInput").value = [...new Set([...scheduleReadTags("scheduleTagsInput"), button.dataset.tagSuggestion])].join(", ");
      calendarEntry.dirty = true;
    };
  });
  $("#scheduleTagsForm").onsubmit = async (event) => {
    event.preventDefault();
    if (calendarEntry.busy) return;
    const tags = scheduleReadTags("scheduleTagsInput");
    calendarEntry.busy = true;
    dialog.querySelectorAll("button,input").forEach((el) => { el.disabled = true; });
    $("#scheduleTagsError").textContent = "";
    try {
      await api(`/api/schedule/items/${key}/tags`, { method: "PATCH", body: { tags } });
      dialog.close();
      await refreshTodoBoard();
      toast("태그를 저장했습니다.");
    } catch (error) { $("#scheduleTagsError").textContent = error.message; }
    finally { calendarEntry.busy = false; dialog.querySelectorAll("button,input").forEach((el) => { el.disabled = false; }); }
  };
  dialog.showModal();
  $("#scheduleTagsInput").focus();
}

function openScheduleShare() {
  const dialog = calendarDialog("scheduleShareDialog", "일정 파일 공유");
  const first = todoCalendar.month + "-01";
  const last = new Date(first + "T12:00:00");
  last.setMonth(last.getMonth() + 1, 0);
  let plan = null;
  dialog.innerHTML = `<div class="dlg-h"><strong>${todoIcon("share")} 일정 공유</strong><button class="btn ghost" type="button" id="scheduleShareClose">닫기</button></div><div class="dlg-b">
    <div class="schedule-share-intro"><span class="schedule-share-step">1</span><div><strong>보낼 일정만 골라 주세요</strong><p>달력 파일(.ics)을 저장해 메신저나 메일로 전달할 수 있습니다. 서버나 계정 연결 없이 사용할 수 있습니다.</p></div></div>
    <form id="scheduleShareForm"><div class="calendar-form-dates"><div class="field"><label for="scheduleShareStart">시작일</label><input id="scheduleShareStart" type="date" value="${first}" min="1970-01-01" max="2100-12-31" required></div><div class="field"><label for="scheduleShareEnd">종료일 (최대 1년)</label><input id="scheduleShareEnd" type="date" value="${todoCalendarISO(last)}" min="1970-01-01" max="2100-12-31" required></div></div>
    <div class="field"><label for="scheduleShareTag">공유할 태그</label><select id="scheduleShareTag">${scheduleTagOptions(todoBoard.tag)}</select></div>
    <div class="schedule-share-options"><label><input type="checkbox" id="scheduleShareExternal"> 가져온 외부 일정도 포함</label><label><input type="checkbox" id="scheduleShareDetails" disabled> 외부 일정의 장소·상세 내용도 포함</label></div>
    <p class="todo-help">일정 제목·날짜·태그가 공유됩니다. 메모 원문은 포함하지 않습니다. 기간에 걸친 일정은 전체 기간으로 공유합니다.</p><button class="btn primary" type="submit" id="scheduleSharePreviewRun">공유할 일정 확인</button></form>
    <p id="scheduleShareError" class="todo-error" role="alert"></p><p id="scheduleShareStatus" class="todo-help" role="status"></p><div id="scheduleSharePreview"></div>
    <details class="schedule-share-guide"><summary>받은 파일은 어떻게 사용하나요?</summary><p>이 앱에서는 ‘일정 → 외부 캘린더 → 일정 파일(.ics)’에서 가져올 수 있습니다. 구글·네이버 등 .ics 가져오기를 지원하는 캘린더에서도 열 수 있습니다.</p><p>파일에는 저장 시점의 일정이 담깁니다. 이후 변경 사항을 전달하려면 새 파일을 보내 주세요. 이 앱에서는 같은 캘린더 이름으로 다시 가져오면 갱신됩니다.</p></details></div>`;
  const selected = () => [...dialog.querySelectorAll("[data-share-key]:checked")].map((el) => el.dataset.shareKey);
  const updateCount = () => {
    const count = selected().length;
    if ($("#scheduleShareCount")) $("#scheduleShareCount").textContent = `${count}개 선택`;
    if ($("#scheduleShareSave")) $("#scheduleShareSave").disabled = calendarEntry.busy || !count;
  };
  const reset = () => {
    plan = null; calendarEntry.dirty = true;
    $("#scheduleSharePreview").innerHTML = "";
    $("#scheduleShareStatus").textContent = "";
    if (!$("#scheduleShareExternal").checked) $("#scheduleShareDetails").checked = false;
    $("#scheduleShareDetails").disabled = !$("#scheduleShareExternal").checked;
  };
  $("#scheduleShareForm").oninput = reset;
  $("#scheduleShareForm").onchange = reset;
  $("#scheduleShareClose").onclick = () => dialog.close();
  const run = async (action) => {
    if (calendarEntry.busy) return;
    calendarEntry.busy = true;
    dialog.querySelectorAll("button,input,select").forEach((el) => { el.disabled = true; });
    $("#scheduleShareError").textContent = "";
    try { await action(); }
    catch (error) { $("#scheduleShareError").textContent = error.message; $("#scheduleShareStatus").textContent = ""; }
    finally {
      calendarEntry.busy = false;
      dialog.querySelectorAll("button,input,select").forEach((el) => { el.disabled = false; });
      $("#scheduleShareDetails").disabled = !$("#scheduleShareExternal").checked;
      updateCount();
    }
  };
  $("#scheduleShareForm").onsubmit = (event) => {
    event.preventDefault();
    const options = { start: $("#scheduleShareStart").value, end: $("#scheduleShareEnd").value, tag: $("#scheduleShareTag").value || null,
      include_external: $("#scheduleShareExternal").checked, include_details: $("#scheduleShareDetails").checked };
    run(async () => {
      plan = null; $("#scheduleSharePreview").innerHTML = "";
      $("#scheduleShareStatus").textContent = "공유할 일정을 불러오고 있습니다…";
      plan = await api("/api/schedule/share/preview", { method: "POST", body: options });
      $("#scheduleShareStatus").textContent = `공유 가능한 일정 ${plan.total}개${plan.undated ? ` · 날짜 없는 일 ${plan.undated}개는 제외` : ""}. 완료·제외·확인 필요 항목은 포함하지 않습니다.`;
      $("#scheduleSharePreview").innerHTML = `<section class="schedule-share-selection"><div class="schedule-share-intro"><span class="schedule-share-step">2</span><div><strong>내용을 확인하고 파일로 저장하세요</strong><p>선택한 항목만 파일에 담깁니다.</p></div></div>
        <div class="row"><strong id="scheduleShareCount"></strong><button class="btn ghost" type="button" id="scheduleShareAll">전체 선택</button><button class="btn ghost" type="button" id="scheduleShareNone">선택 해제</button></div>
        <div class="schedule-share-list">${plan.items.map((item) => `<label class="schedule-share-item"><input type="checkbox" data-share-key="${esc(item.key)}" checked><span><strong>${esc(item.title)}</strong><small>${esc(item.kind)} · ${item.start_day}${item.end_day !== item.start_day ? ` ~ ${item.end_day}` : ""}${item.start_time && !item.all_day ? ` · ${esc(new Date(item.start_time).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" }))}` : ""}</small><span class="schedule-tags">${scheduleTagPills(item.tags, false)}</span>${item.location ? `<small>장소: ${esc(item.location)}</small>` : ""}${item.description ? `<small class="schedule-share-description">${esc(item.description)}</small>` : ""}</span></label>`).join("") || '<p class="todo-help">조건에 맞는 일정이 없습니다. 기간이나 태그를 바꿔 보세요.</p>'}</div>
        <button class="btn primary" type="button" id="scheduleShareSave">${todoIcon("share")} 선택한 일정 파일 저장</button></section>`;
      dialog.querySelectorAll("[data-share-key]").forEach((el) => { el.onchange = () => { calendarEntry.dirty = true; updateCount(); }; });
      const checkAll = (value) => { dialog.querySelectorAll("[data-share-key]").forEach((el) => { el.checked = value; }); updateCount(); };
      $("#scheduleShareAll").onclick = () => checkAll(true);
      $("#scheduleShareNone").onclick = () => checkAll(false);
      $("#scheduleShareSave").onclick = () => {
        const keys = selected(), planId = plan.plan_id;
        run(async () => {
          const file = await api(`/api/schedule/share/${planId}/export`, { method: "POST", body: { selected: keys } });
          if (window.pywebview?.api?.save) {
            const saved = await window.pywebview.api.save(file.content, file.filename);
            if (!saved) { $("#scheduleShareStatus").textContent = "파일을 저장하지 않았습니다. 다시 저장할 수 있습니다."; return; }
          } else {
            const url = URL.createObjectURL(new Blob([file.content], { type: "text/calendar;charset=utf-8" }));
            const link = document.createElement("a"); link.href = url; link.download = file.filename; link.click();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
          }
          calendarEntry.dirty = false;
          $("#scheduleShareStatus").textContent = `${file.count}개 일정을 파일로 저장했습니다. 이 파일을 공유할 사람에게 전달해 주세요.`;
        });
      };
      updateCount();
    });
  };
  dialog.showModal();
}
