// Schedule tags and explicit, reviewed calendar-file sharing.
function scheduleTagOptions(names, selected) {
  return names.map((name) => `<option value="${esc(name)}" ${name === selected ? "selected" : ""}>#${esc(name)}</option>`).join("");
}

function scheduleTagPills(tags, interactive = true) {
  return (tags || []).map((tag) => {
    const definition = todoBoard.registeredTags.find((entry) => entry.name === tag);
    const colorClass = definition ? `registered-tag t-${esc(definition.color)}` : `tag-${[...tag].reduce((n, c) => n + c.codePointAt(0), 0) % 5}`;
    return interactive ? `<button type="button" class="schedule-tag ${colorClass}" data-schedule-filter="${esc(tag)}" title="이 태그의 일정 보기">#${esc(tag)}</button>` : `<span class="schedule-tag ${colorClass}">#${esc(tag)}</span>`;
  }).join("");
}

function scheduleItemTags(item) {
  const id = item.external ? item.id : `todo-${item.id}`;
  return `<div class="schedule-tags">${scheduleTagPills(item.tags)}<button type="button" class="schedule-tag-edit" data-schedule-tags="${id}" aria-label="${esc(item.title)} 태그 편집">${todoIcon("tag")} ${item.tags?.length ? "편집" : "태그 추가"}</button></div>`;
}

function scheduleTagField(id, selected, label = "내 태그 (선택)", registered = todoBoard.registeredTags) {
  const existing = selected.filter((name) => !registered.some((tag) => tag.name === name));
  const chip = (tag) => tagChip(tag, selected.includes(tag.name), `aria-pressed="${selected.includes(tag.name)}"`);
  return `<div class="field schedule-tag-field" id="${id}" role="group" aria-labelledby="${id}Label">
    <strong id="${id}Label">${label}</strong>
    ${registered.length ? `<div class="tagrow">${registered.map(chip).join("")}</div>` : '<p class="todo-help">설정 → 내 태그에서 태그를 만들면 여기에 표시됩니다.</p>'}
    ${existing.length ? `<span>이 일정에 붙어 있는 기타 태그</span><div class="tagrow">${existing.map((name) => chip({ name })).join("")}</div>` : ""}
  </div>`;
}

function bindScheduleTagField(id) {
  const field = document.getElementById(id);
  field.onclick = (event) => {
    const chip = event.target.closest("button[data-tag]");
    if (!chip) return;
    if (!chip.classList.contains("on") && scheduleReadTags(id).length >= 20) {
      toast("일정 태그는 최대 20개까지 선택할 수 있습니다.");
      return;
    }
    chip.setAttribute("aria-pressed", chip.classList.toggle("on"));
    calendarEntry.dirty = true;
    field.dispatchEvent(new Event("input", { bubbles: true }));
  };
}

function scheduleReadTags(id) {
  return [...document.getElementById(id).querySelectorAll('[data-tag][aria-pressed="true"]')].map((chip) => chip.dataset.tag);
}

function scheduleToolbar() {
  const registered = todoBoard.registeredTags.map((tag) => tag.name);
  const names = new Set(todoBoard.tagChoices.map((tag) => tag.name));
  if (todoBoard.tag) names.add(todoBoard.tag);
  const others = [...names].filter((name) => !registered.includes(name)).sort();
  return `<div class="schedule-toolbar"><div class="schedule-filter"><label for="scheduleTagFilter">${todoIcon("tag")} 태그</label><select id="scheduleTagFilter"><option value="">모든 태그</option>
    ${registered.length ? `<optgroup label="내 태그">${scheduleTagOptions(registered, todoBoard.tag)}</optgroup>` : ""}
    ${others.length ? `<optgroup label="기타 태그">${scheduleTagOptions(others, todoBoard.tag)}</optgroup>` : ""}</select>${todoBoard.tag ? '<button class="btn ghost" id="scheduleClearTag" type="button">필터 해제</button>' : ""}</div><button class="btn" type="button" id="scheduleShareOpen">${todoIcon("share")} 일정 공유</button></div>`;
}

function bindSchedule() {
  const filter = (tag) => { todoBoard.tag = tag || null; todoBoard.offset = 0; todoCalendar.memoOffset = 0; refreshTodoBoard(); };
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
    <p class="todo-help">${item.external ? "이 앱에 붙인 태그는 원본 캘린더를 새로고침해도 유지됩니다." : item.tags_inherited ? "메모의 태그를 가져왔습니다. 저장하면 이 일정의 태그를 따로 관리합니다." : "이 일정의 태그만 바뀝니다."}</p><p id="scheduleTagsError" class="todo-error" role="alert"></p></div>
    <div class="dlg-f"><button class="btn" type="button" id="scheduleTagsClose">닫기</button><button class="btn primary" type="submit">저장</button></div></form>`;
  $("#scheduleTagsClose").onclick = () => dialog.close();
  bindScheduleTagField("scheduleTagsInput");
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
  dialog.querySelector("[data-tag]")?.focus();
}

function openScheduleShare() {
  const dialog = calendarDialog("scheduleShareDialog", "일정 파일 공유");
  const first = todoCalendar.month + "-01";
  const last = new Date(first + "T12:00:00");
  last.setMonth(last.getMonth() + 1, 0);
  const initialTags = todoBoard.registeredTags.some((tag) => tag.name === todoBoard.tag) ? [todoBoard.tag] : [];
  let plan = null;
  dialog.innerHTML = `<div class="dlg-h"><strong>${todoIcon("share")} 일정 공유</strong><button class="btn ghost" type="button" id="scheduleShareClose">닫기</button></div><div class="dlg-b">
    <div class="schedule-share-intro"><span class="schedule-share-step">1</span><div><strong>보낼 일정만 골라 주세요</strong><p>달력 파일(.ics)을 저장해 메신저나 메일로 전달하거나, Teams로 바로 보낼 수 있습니다. 서버나 계정 연결 없이 사용할 수 있습니다.</p></div></div>
    <form id="scheduleShareForm"><div class="calendar-form-dates"><div class="field"><label for="scheduleShareStart">시작일</label><input id="scheduleShareStart" type="date" value="${first}" min="1970-01-01" max="2100-12-31" required></div><div class="field"><label for="scheduleShareEnd">종료일 (최대 1년)</label><input id="scheduleShareEnd" type="date" value="${todoCalendarISO(last)}" min="1970-01-01" max="2100-12-31" required></div></div>
    ${scheduleTagField("scheduleShareTag", initialTags, "공유할 내 태그")}
    <div class="schedule-share-options"><label><input type="checkbox" id="scheduleShareWholePeriod" ${!todoBoard.tag && !todoBoard.registeredTags.length ? "checked" : ""}> 기간 내 전체 일정</label></div>
    <p class="todo-help">태그를 여러 개 고르면 그중 하나라도 붙은 일정을 공유합니다.</p>
    <div class="schedule-share-options"><label><input type="checkbox" id="scheduleShareExternal"> 가져온 외부 일정도 포함</label><label><input type="checkbox" id="scheduleShareDetails" disabled> 외부 일정의 장소·상세 내용도 포함</label></div>
    <p class="todo-help">일정 제목·날짜·태그가 공유됩니다. 메모 원문은 포함하지 않습니다. 기간에 걸친 일정은 전체 기간으로 공유합니다.</p><button class="btn primary" type="submit" id="scheduleSharePreviewRun">공유할 일정 확인</button></form>
    <p id="scheduleShareError" class="todo-error" role="alert"></p><p id="scheduleShareStatus" class="todo-help" role="status"></p><div id="scheduleSharePreview"></div>
    <details class="schedule-share-guide"><summary>받은 파일은 어떻게 사용하나요?</summary><p>이 앱에서는 ‘일정 → 외부 캘린더 → 일정 파일(.ics)’에서 가져올 수 있습니다. 구글·네이버 등 .ics 가져오기를 지원하는 캘린더에서도 열 수 있습니다.</p><p>파일에는 저장 시점의 일정이 담깁니다. 이후 변경 사항을 전달하려면 새 파일을 보내 주세요. 이 앱에서는 같은 캘린더 이름으로 다시 가져오면 갱신됩니다.</p></details></div>`;
  const selected = () => [...dialog.querySelectorAll("[data-share-key]:checked")].map((el) => el.dataset.shareKey);
  const updateCount = () => {
    const count = selected().length;
    if ($("#scheduleShareCount")) $("#scheduleShareCount").textContent = `${count}개 선택`;
    if ($("#scheduleShareSave")) $("#scheduleShareSave").disabled = calendarEntry.busy || !count;
    if ($("#scheduleShareTeams")) $("#scheduleShareTeams").disabled = calendarEntry.busy || !count;
  };
  const reset = () => {
    plan = null; calendarEntry.dirty = true;
    $("#scheduleShareError").textContent = "";
    $("#scheduleSharePreview").innerHTML = "";
    $("#scheduleShareStatus").textContent = "";
    if (!$("#scheduleShareExternal").checked) $("#scheduleShareDetails").checked = false;
    $("#scheduleShareDetails").disabled = !$("#scheduleShareExternal").checked;
  };
  bindScheduleTagField("scheduleShareTag");
  $("#scheduleShareForm").oninput = (event) => {
    if (event.target.id === "scheduleShareTag") $("#scheduleShareWholePeriod").checked = false;
    if (event.target.id === "scheduleShareWholePeriod" && event.target.checked) {
      $("#scheduleShareTag").querySelectorAll('[aria-pressed="true"]').forEach((chip) => {
        chip.classList.remove("on");
        chip.setAttribute("aria-pressed", "false");
      });
    }
    reset();
  };
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
    const tags = scheduleReadTags("scheduleShareTag");
    if (!tags.length && !$("#scheduleShareWholePeriod").checked) {
      $("#scheduleShareError").textContent = "공유할 내 태그 또는 ‘기간 내 전체 일정’을 선택해 주세요.";
      return;
    }
    const options = { start: $("#scheduleShareStart").value, end: $("#scheduleShareEnd").value, tags,
      include_external: $("#scheduleShareExternal").checked, include_details: $("#scheduleShareDetails").checked };
    run(async () => {
      plan = null; $("#scheduleSharePreview").innerHTML = "";
      $("#scheduleShareStatus").textContent = "공유할 일정을 불러오고 있습니다…";
      plan = await api("/api/schedule/share/preview", { method: "POST", body: options });
      $("#scheduleShareStatus").textContent = `공유 가능한 일정 ${plan.total}개${plan.undated ? ` · 날짜 없는 일 ${plan.undated}개는 제외` : ""}. 완료·제외·확인 필요 항목은 포함하지 않습니다.`;
      $("#scheduleSharePreview").innerHTML = `<section class="schedule-share-selection"><div class="schedule-share-intro"><span class="schedule-share-step">2</span><div><strong>내용을 확인하고 파일로 저장하거나 Teams로 보내세요</strong><p>선택한 항목만 파일에 담깁니다. Teams로 보내면 일정 목록 글이 함께 갑니다.</p></div></div>
        <div class="row"><strong id="scheduleShareCount"></strong><button class="btn ghost" type="button" id="scheduleShareAll">전체 선택</button><button class="btn ghost" type="button" id="scheduleShareNone">선택 해제</button></div>
        <div class="schedule-share-list">${plan.items.map((item) => `<label class="schedule-share-item"><input type="checkbox" data-share-key="${esc(item.key)}" checked><span><strong>${esc(item.title)}</strong><small>${esc(item.kind)} · ${item.start_day}${item.end_day !== item.start_day ? ` ~ ${item.end_day}` : ""}${item.start_time && !item.all_day ? ` · ${esc(new Date(item.start_time).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" }))}` : ""}</small><span class="schedule-tags">${scheduleTagPills(item.tags, false)}</span>${item.location ? `<small>장소: ${esc(item.location)}</small>` : ""}${item.description ? `<small class="schedule-share-description">${esc(item.description)}</small>` : ""}</span></label>`).join("") || '<p class="todo-help">조건에 맞는 일정이 없습니다. 기간이나 태그를 바꿔 보세요.</p>'}</div>
        <div class="row schedule-share-actions"><button class="btn primary" type="button" id="scheduleShareSave">${todoIcon("share")} 선택한 일정 파일 저장</button>
          <button class="btn" type="button" id="scheduleShareTeams">Teams로 공유</button></div>
        <div id="scheduleShareTeamsOut" class="todo-help" aria-live="polite"></div></section>`;
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
      // 고른 일정을 .ics 파일로 만들어 Teams로. 받는 사람이 파일을 안 열어도 알 수 있게 일정 목록 글을 함께 보낸다.
      $("#scheduleShareTeams").onclick = () => {
        const keys = selected(), planId = plan.plan_id, out = $("#scheduleShareTeamsOut");
        const chosen = keys.join("\n");
        run(async () => {
          out.textContent = "Teams로 보낼 일정 파일을 준비하는 중…";
          try {
            const file = await api(`/api/schedule/share/${planId}/export`, { method: "POST", body: { selected: keys } });
            const stillSelected = () => out.isConnected && plan?.plan_id === planId && selected().join("\n") === chosen;
            await shareExportFile(out, { content: file.content, filename: file.filename, text: file.summary }, stillSelected);
            calendarEntry.dirty = false;
          } catch (error) { out.textContent = ""; throw error; }
        });
      };
      updateCount();
    });
  };
  dialog.showModal();
}
