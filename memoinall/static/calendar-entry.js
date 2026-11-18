const calendarEntry = { busy: false, dirty: false };

function externalCalendarCard(item) {
  return `<article class="external-calendar-card" data-external-event="${item.id}">
    <div class="todo-title">${esc(item.title)}</div>
    <div class="badges"><span class="badge">${esc(item.source_name)}</span><span class="badge">${esc(item.time_label)}</span>
      ${item.start_day !== item.end_day ? `<span class="badge">${todoDate(item.start_day)}–${todoDate(item.end_day)}</span>` : ""}</div>
    ${scheduleItemTags(item)}
    ${item.location ? `<p>${esc(item.location)}</p>` : ""}
    <details class="todo-evidence"><summary>일정 내용</summary><p>${esc(item.timezone)} 기준 · 외부 캘린더에서 가져온 일정</p><blockquote>${esc(item.description || "추가 내용이 없습니다.")}</blockquote></details>
  </article>`;
}

function calendarDialog(id, title) {
  let dialog = document.getElementById(id);
  if (!dialog) { dialog = document.createElement("dialog"); dialog.id = id; document.body.append(dialog); }
  dialog.setAttribute("aria-label", title);
  dialog.oncancel = (event) => { if (calendarEntry.busy) event.preventDefault(); };
  dialog.onclose = () => { calendarEntry.dirty = false; };
  return dialog;
}

function openTodoCreate(day, fromDayCell = false, memoBody = null) {
  const fromMemo = memoBody !== null;
  const heading = fromMemo ? "메모를 일정으로 등록" : "일정 추가";
  const dialog = calendarDialog("todoCreate", heading);
  if (fromDayCell) {
    // Refreshing the agenda replaces the day button that opened this dialog.
    dialog.addEventListener("close", () => {
      document.querySelector(`[data-calendar-day="${todoCalendar.selected}"]`)?.focus({ preventScroll: true });
    }, { once: true });
  }
  const requestId = crypto.randomUUID();
  const registered = fromMemo ? myTags : todoBoard.registeredTags;
  const memoTags = new Set(fromMemo ? bodyTags(memoBody).map((name) => name.toLowerCase()) : []);
  const selectedTags = registered.filter((tag) => fromMemo ? memoTags.has(tag.name.toLowerCase()) : tag.name === todoBoard.tag).map((tag) => tag.name).slice(0, 20);
  dialog.innerHTML = `<form id="todoCreateForm"><div class="dlg-h"><strong>${heading}</strong></div><div class="dlg-b">
    <p class="todo-help">${fromMemo ? "예정일을 선택하세요. 작성한 메모 전체가 일정에 연결됩니다." : "선택한 날짜에 일정을 바로 등록합니다."}</p>
    <div class="field"><label for="todoCreateTitle">일정 이름</label><input id="todoCreateTitle" required maxlength="500" placeholder="예: 교육계획안 보내기" ${fromMemo ? "" : "autofocus"}></div>
    <div class="calendar-form-dates"><div class="field"><label for="todoCreateDate">예정일</label><input id="todoCreateDate" type="date" required value="${day}" ${fromMemo ? "autofocus" : ""}></div>
    <div class="field"><label for="todoCreateDue">마감일 (선택)</label><input id="todoCreateDue" type="date"></div></div>
    <p class="todo-help">예정일은 할 날짜, 마감일은 끝내야 하는 날짜입니다. 등록 내용은 메모에도 함께 저장됩니다.</p>
    ${scheduleTagField("todoCreateTags", selectedTags, "내 태그 (선택)", registered)}
    <p id="todoCreateError" class="todo-error" role="alert"></p></div>
    <div class="dlg-f"><button class="btn" type="button" id="todoCreateCancel">닫기</button><button class="btn primary" type="submit" id="todoCreateSave">${fromMemo ? "등록" : "추가"}</button></div></form>`;
  if (fromMemo) $("#todoCreateTitle").value = [...memoBody.trim().split(/\r?\n/, 1)[0]].slice(0, 500).join("");
  $("#todoCreateCancel").onclick = () => dialog.close();
  bindScheduleTagField("todoCreateTags");
  $("#todoCreateForm").oninput = () => { calendarEntry.dirty = true; };
  $("#todoCreateForm").onsubmit = async (event) => {
    event.preventDefault();
    if (calendarEntry.busy) return;
    const planned = $("#todoCreateDate").value;
    const payload = { title: $("#todoCreateTitle").value.trim(), scheduled_for: planned,
      due: $("#todoCreateDue").value || null, request_id: requestId, tags: scheduleReadTags("todoCreateTags") };
    calendarEntry.busy = true;
    dialog.querySelectorAll("button,input").forEach((el) => { el.disabled = true; });
    $("#todoCreateError").textContent = "";
    try {
      if (fromMemo) {
        payload.memo_body = memoBody;
        await persistDraft().catch(() => {});
      }
      await api("/api/todos", { method: "POST", body: payload });
      if (!payload.tags.includes(todoBoard.tag)) todoBoard.tag = null;
      dialog.close();
      if (fromMemo) {
        if ($("#capture").value === memoBody) {
          $("#capture").value = draftSaved = "";
          localDraft("");
          $("#captureHint").textContent = "Ctrl+Enter 저장 · 형식은 신경 쓰지 마세요";
        }
        paintTagRows();
        updateCaptureSchedule();
        state.offset = 0;
        await Promise.allSettled([render(), loadSidebar(), loadTags()]);
        $("#capture").focus();
      }
      await selectTodoCalendarDay(planned, fromDayCell);
      toast(fromMemo ? `${todoDate(planned)} 일정으로 등록했습니다.` : "일정을 추가했습니다.");
    } catch (error) { $("#todoCreateError").textContent = error.message; }
    finally {
      calendarEntry.busy = false;
      dialog.querySelectorAll("button,input").forEach((el) => { el.disabled = false; });
    }
  };
  dialog.showModal();
}

async function openCalendarImport() {
  const dialog = calendarDialog("calendarImport", "외부 캘린더 가져오기");
  const first = todoCalendar.month + "-01";
  const last = new Date(first + "T12:00:00");
  last.setFullYear(last.getFullYear() + 1);
  last.setDate(last.getDate() - 1);
  dialog.innerHTML = `<div class="dlg-h"><strong>외부 캘린더 가져오기</strong><button type="button" class="btn ghost" id="calendarImportClose">닫기</button></div>
    <div class="dlg-b"><p class="todo-help">가져온 일정은 달력에 함께 표시됩니다. 이곳의 변경 사항은 네이버·구글에 전송되지 않습니다.</p>
      <details class="calendar-import-guide"><summary>네이버·구글에서 가져오는 방법</summary>
        <p><b>네이버:</b> PC 캘린더의 캘린더명 옆 메뉴 → 가져오기/내보내기(백업) → 내보내기에서 .ics 파일을 저장하세요. <a href="https://help.naver.com/service/5620/contents/18401?lang=ko&osType=PC" target="_blank" rel="noopener noreferrer">네이버 안내</a></p>
        <p><b>구글:</b> 설정 → 내 캘린더의 설정 → 캘린더 통합 → ‘iCal 형식의 비공개 주소’를 복사하세요. 파일로 내보내기도 가능합니다. ZIP 파일은 압축을 풀고 .ics 파일을 선택하세요. <a href="https://support.google.com/calendar/answer/37648?hl=ko" target="_blank" rel="noopener noreferrer">구글 안내</a></p>
        <p>다른 서비스도 .ics 파일이나 HTTPS iCal 주소를 지원하면 가져올 수 있습니다. 비공개 주소는 이 앱에 저장되며, 다른 사람에게 공유하지 마세요.</p>
      </details>
      <form id="calendarImportForm">
        <div class="calendar-import-method" role="group" aria-label="가져오기 방식"><label><input type="radio" name="calendarKind" value="file" checked> 일정 파일 (.ics)</label><label><input type="radio" name="calendarKind" value="url"> iCal 주소 연결</label></div>
        <div class="field"><label for="calendarName">캘린더 이름</label><input id="calendarName" maxlength="80" required placeholder="예: 네이버 개인 일정"></div>
        <div class="field" id="calendarFileField"><label for="calendarFile">일정 파일 (최대 2MB)</label><input type="file" id="calendarFile" accept=".ics,text/calendar" required></div>
        <div class="field" id="calendarUrlField" hidden><label for="calendarUrl">iCal 주소</label><input type="password" id="calendarUrl" autocomplete="off" placeholder="https://…/basic.ics"><p class="todo-help">주소를 등록한 뒤 ‘새로고침’을 누르면 변경 내용을 가져옵니다.</p></div>
        <div class="calendar-form-dates"><div class="field"><label for="calendarStart">가져올 시작일</label><input type="date" id="calendarStart" value="${first}" required min="1970-01-01" max="2100-12-31"></div>
        <div class="field"><label for="calendarEnd">종료일 (최대 1년)</label><input type="date" id="calendarEnd" value="${todoCalendarISO(last)}" required min="1970-01-01" max="2100-12-31"></div></div>
        <div class="field"><label for="calendarTimezone">표시 시간대</label><input id="calendarTimezone" required value="${esc(Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Seoul")}"></div>
        <p class="todo-help">같은 이름으로 다시 가져오면 해당 캘린더의 저장된 일정을 새 기간·내용으로 교체합니다. 반복 일정도 선택한 기간 안에서 가져옵니다.</p>
        <button type="submit" class="btn" id="calendarPreviewRun">미리보기</button>
      </form>
      <p id="calendarImportError" class="todo-error" role="alert"></p><p id="calendarImportStatus" class="todo-help" role="status"></p>
      <div id="calendarPreview"></div>
      <section class="calendar-sources" aria-label="가져온 캘린더"><h3>가져온 캘린더</h3><div id="calendarSources">불러오는 중…</div></section>
    </div>`;
  let plan = null, refreshId = null;
  const kind = () => dialog.querySelector('[name="calendarKind"]:checked').value;
  const method = () => {
    const file = kind() === "file";
    $("#calendarFileField").hidden = !file;
    $("#calendarUrlField").hidden = file;
    $("#calendarFile").required = file;
    $("#calendarUrl").required = !file && !refreshId;
    $("#calendarUrl").placeholder = refreshId ? "등록된 주소를 사용합니다" : "https://…/basic.ics";
  };
  const reset = (event) => { plan = null; if (!["calendarStart", "calendarEnd", "calendarTimezone"].includes(event?.target?.id)) refreshId = null; $("#calendarPreview").innerHTML = ""; $("#calendarImportStatus").textContent = ""; calendarEntry.dirty = true; method(); };
  $("#calendarImportForm").oninput = reset;
  $("#calendarImportForm").onchange = (event) => { reset(event); if (!$("#calendarName").value && $("#calendarFile").files[0]) $("#calendarName").value = $("#calendarFile").files[0].name.replace(/\.ics$/i, "").slice(0, 80); };
  $("#calendarImportClose").onclick = () => dialog.close();
  const run = async (fn) => {
    if (calendarEntry.busy) return;
    calendarEntry.busy = true;
    dialog.querySelectorAll("button,input").forEach((el) => { el.disabled = true; });
    $("#calendarImportError").textContent = "";
    $("#calendarImportStatus").textContent = "일정을 처리하고 있습니다…";
    try { await fn(); }
    catch (error) { $("#calendarImportError").textContent = error.message; $("#calendarImportStatus").textContent = ""; }
    finally { calendarEntry.busy = false; dialog.querySelectorAll("button,input").forEach((el) => { el.disabled = false; }); }
  };
  const showPreview = (result) => {
    plan = result;
    $("#calendarImportStatus").textContent = "아직 저장되지 않았습니다. 변경 내용을 확인한 뒤 적용해 주세요.";
    const c = result.counts;
    $("#calendarPreview").innerHTML = `<section class="calendar-preview"><h3>${esc(result.name)} · ${result.total}개 일정</h3>
      <p>${result.range_start} ~ ${result.range_end} · ${esc(result.timezone)}</p>
      <div class="badges"><span class="badge">추가 ${c.added}</span><span class="badge">변경 ${c.updated}</span><span class="badge">유지 ${c.unchanged}</span><span class="badge ${c.removed ? "todo-error" : ""}">삭제 ${c.removed}</span></div>
      ${c.removed ? '<p class="todo-error">새 파일·주소에 없거나 가져올 기간에서 빠진 기존 외부 일정은 삭제됩니다.</p>' : ""}
      ${result.ignored ? `<p class="todo-help">일정이 아닌 할 일·저널 ${result.ignored}개는 가져오지 않습니다.</p>` : ""}
      ${result.sample.length ? `<ul class="calendar-preview-list">${result.sample.map((event) => `<li><span>${event.start_day} · ${esc(event.time_label)}</span><strong>${esc(event.title)}</strong></li>`).join("")}</ul>` : '<p>선택한 기간에 일정이 없습니다. 기간과 파일을 확인해 주세요.</p>'}
      ${result.total > result.sample.length ? '<p class="todo-help">앞의 30건만 미리 표시합니다. 적용하면 전체 일정을 가져옵니다.</p>' : ""}
      <button class="btn primary" type="button" id="calendarApply">${c.removed && !result.total ? "변경 적용 (외부 일정 비우기)" : "달력에 적용"}</button></section>`;
    $("#calendarApply").onclick = () => run(async () => {
      const result = await api(`/api/calendars/plans/${plan.plan_id}/apply`, { method: "POST" });
      plan = null; calendarEntry.dirty = false;
      $("#calendarPreview").innerHTML = "";
      $("#calendarUrl").value = "";
      $("#calendarImportStatus").textContent = `${result.count}개 일정을 달력에 반영했습니다.`;
      await loadSources();
      await refreshTodoBoard();
    });
  };
  const loadSources = async () => {
    const data = await api("/api/calendars");
    $("#calendarSources").innerHTML = data.items.length ? data.items.map((source) => `<div class="calendar-source"><div><strong>${esc(source.name)}</strong><p class="todo-help">${source.kind === "url" ? "주소 연결" : "파일"} · ${source.count}개 · ${source.range_start} ~ ${source.range_end}</p><p class="todo-help">최근 반영 ${esc(source.updated_at.slice(0, 16).replace("T", " "))}</p></div><div class="row"><button class="btn" data-calendar-refresh="${source.id}">${source.kind === "url" ? "새로고침" : "새 파일로 갱신"}</button><button class="btn ghost" data-calendar-remove="${source.id}">연결 해제</button></div></div>`).join("") : '<p class="todo-help">가져온 캘린더가 없습니다.</p>';
    dialog.querySelectorAll("[data-calendar-refresh]").forEach((button) => { button.onclick = () => {
      const source = data.items.find((s) => s.id === +button.dataset.calendarRefresh);
      reset();
      $("#calendarName").value = source.name;
      $("#calendarStart").value = source.range_start;
      $("#calendarEnd").value = source.range_end;
      $("#calendarTimezone").value = source.timezone;
      dialog.querySelector(`[name="calendarKind"][value="${source.kind}"]`).checked = true;
      refreshId = source.kind === "url" ? source.id : null;
      method();
      if (refreshId) run(async () => showPreview(await api(`/api/calendars/${refreshId}/preview`, { method: "POST" })));
      else { $("#calendarFile").value = ""; $("#calendarFile").focus(); $("#calendarImportStatus").textContent = "새 .ics 파일을 선택한 뒤 미리보기를 눌러 주세요."; }
    }; });
    dialog.querySelectorAll("[data-calendar-remove]").forEach((button) => { button.onclick = () => {
      const source = data.items.find((s) => s.id === +button.dataset.calendarRemove);
      if (!confirm(`‘${source.name}’ 연결과 가져온 외부 일정 ${source.count}개를 이 앱에서 지울까요? 네이버·구글의 원본 일정과 할 일은 유지됩니다.`)) return;
      run(async () => {
        await api(`/api/calendars/${source.id}`, { method: "DELETE" });
        reset(); calendarEntry.dirty = false;
        await loadSources(); await refreshTodoBoard();
        $("#calendarImportStatus").textContent = "연결을 해제했습니다.";
      });
    }; });
  };
  $("#calendarImportForm").onsubmit = (event) => {
    event.preventDefault();
    run(async () => {
      plan = null; $("#calendarPreview").innerHTML = "";
      if (refreshId) return showPreview(await api(`/api/calendars/${refreshId}/preview`, { method: "POST", body: {
        range_start: $("#calendarStart").value, range_end: $("#calendarEnd").value, timezone: $("#calendarTimezone").value.trim() } }));
      const payload = { name: $("#calendarName").value.trim(), kind: kind(), range_start: $("#calendarStart").value,
        range_end: $("#calendarEnd").value, timezone: $("#calendarTimezone").value.trim() };
      if (payload.kind === "file") {
        const file = $("#calendarFile").files[0];
        if (!file || !file.name.toLowerCase().endsWith(".ics")) throw new Error(".ics 일정 파일을 선택해 주세요. ZIP 파일은 먼저 압축을 풀어 주세요.");
        if (file.size > 2 * 1024 * 1024) throw new Error("일정 파일은 2MB 이하만 가져올 수 있습니다.");
        try { payload.content = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer()); }
        catch { throw new Error("파일 문자를 읽지 못했습니다. UTF-8 형식의 .ics 파일로 다시 내보내 주세요."); }
      } else payload.url = $("#calendarUrl").value.trim();
      showPreview(await api("/api/calendars/preview", { method: "POST", body: payload }));
    });
  };
  dialog.showModal();
  try { await loadSources(); } catch (error) { $("#calendarImportError").textContent = error.message; $("#calendarSources").textContent = "목록을 불러오지 못했습니다."; }
}
