function feedDocumentScope() {
  return {feed_query: state.q.trim(), tags: [...state.tags], person: state.person, since: state.since, until: state.until};
}

async function useFeedResults() {
  if (gen.busy) return;
  const tab = state.tab, version = renderVersion, scope = feedDocumentScope();
  gen.busy = true;
  setGenBusy(true);
  $("#documentFromSearch").disabled = true;
  $("#documentFromSearch").textContent = "검색 결과를 모으는 중…";
  if (tab === "generate") genProgress("검색 결과를 모으는 중입니다…");
  try {
    const ids = tab === "generate" && state.selected.size ? [...state.selected.keys()]
      : (await api("/api/search/result-ids?" + qs({q: scope.feed_query}))).ids;
    if (state.tab !== tab || version !== renderVersion || (tab === "feed" && state.selected.size)) return;
    if (!ids.length) throw new Error("이 조건에 맞는 메모가 없습니다. 검색 조건을 바꿔주세요.");
    gen.busy = false;
    if (tab === "generate") {
      changeGenScope(scope, ids);
      await planGenerate();
    } else {
      await startGenerate(ids, "이 검색 결과를 바탕으로 업무 내용을 정리해줘", scope);
    }
  } catch (error) {
    if (tab === "generate" && state.tab === tab && version === renderVersion) {
      gen.busy = false;
      changeGenScope(scope, []);
      genProgress(error.message);
    } else if (state.tab === tab) toast(error.message);
  } finally {
    gen.busy = false;
    setGenBusy(false);
    $("#documentFromSearch").textContent = "검색 결과 전체로 문서 만들기";
    if (state.tab === "feed") paintFeedTools();
  }
}

function applyFeedSearch(filters) {
  Object.assign(state, {q: filters.q || "", tags: filters.tags || [], person: filters.person || null,
    since: filters.since || "", until: filters.until || "", savedPeriod: filters.period || null,
    selecting: false, offset: 0});
  clearTimeout(searchTimer);
  state.selected.clear();
  $("#q").value = state.q;
  $("#filterSince").value = state.since;
  $("#filterUntil").value = state.until;
  $("#savedSearches").open = false;
  goTab("feed");
  loadSidebar().catch((error) => toast(error.message));
}

let savedSearchLoad = 0;
async function loadSavedSearches() {
  const version = ++savedSearchLoad;
  const {items} = await api("/api/saved-searches");
  if (version !== savedSearchLoad) return;
  const host = $("#savedSearchList");
  $("#savedSearches").hidden = !items.length;
  host.replaceChildren();
  for (const item of items) {
    const row = document.createElement("div");
    row.className = "row";
    const run = document.createElement("button"), remove = document.createElement("button");
    run.type = remove.type = "button";
    run.className = "btn";
    remove.className = "btn ghost";
    run.textContent = item.name;
    remove.textContent = "삭제";
    remove.setAttribute("aria-label", item.name + " 저장된 검색 삭제");
    run.onclick = async () => {
      const current = ++savedSearchLoad, view = renderVersion;
      try {
        const filters = await api("/api/saved-searches/" + item.id);
        if (current === savedSearchLoad && view === renderVersion) applyFeedSearch(filters);
      } catch (error) { toast(error.message); }
    };
    remove.onclick = async () => {
      if (!confirm("‘" + item.name + "’ 저장된 검색을 삭제할까요?")) return;
      try { await api("/api/saved-searches/" + item.id, {method: "DELETE"}); await loadSavedSearches(); }
      catch (error) { toast(error.message); }
    };
    row.append(run, remove);
    host.append(row);
  }
}

function bindSearchTools() {
  const dialog = $("#saveSearchDialog"), form = $("#saveSearchForm");
  let filters;
  $("#documentFromSearch").onclick = useFeedResults;
  $("#saveSearch").onclick = () => {
    filters = {q: state.q.trim(), tags: [...state.tags], person: state.person, since: state.since, until: state.until};
    const parts = [filters.q, ...filters.tags.map((tag) => "#" + tag), filters.person && "@" + filters.person].filter(Boolean);
    const dates = [filters.since && filters.since + "부터", filters.until && filters.until + "까지"].filter(Boolean);
    $("#presetSummary").textContent = [...parts, ...dates].join(" · ");
    $("#presetName").value = (parts.join(" · ") || "기간별 메모").slice(0, 100);
    $("#presetPeriod").value = state.savedPeriod || (dates.length ? "custom" : parts.length ? "all" : "this_week");
    $("#presetPeriod option[value=custom]").disabled = !dates.length;
    $("#presetError").textContent = "";
    dialog.showModal();
    $("#presetName").select();
  };
  $("#cancelPreset").onclick = () => dialog.close();
  form.onsubmit = async (event) => {
    event.preventDefault();
    const submit = form.querySelector("[type=submit]");
    if (submit.disabled) return;
    submit.disabled = true;
    try {
      await api("/api/saved-searches", {method: "POST", body: {...filters, name: $("#presetName").value, period: $("#presetPeriod").value}});
      dialog.close();
      await loadSavedSearches();
      toast("검색 조건을 저장했습니다");
    } catch (error) { if (dialog.open) $("#presetError").textContent = error.message; else toast(error.message); }
    finally { submit.disabled = false; }
  };
  loadSavedSearches().catch((error) => toast(error.message));
}
