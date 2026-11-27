// 요청문 안의 선택은 검색 결과의 공유·문서 선택과 독립적이다.
function editAskSources(editor, handoff, initial, question) {
  const label = memo => memo.file_label || memo.title;
  const selected = state.selected.size ? new Map(state.selected) : new Map(initial.sources.map(m => [m.id, label(m)]));
  const filters = {tags: [...state.tags], person: state.person, since: state.since, until: state.until};
  let revision = 0, searchVersion = 0, offset = 0, matches = [], searchTimer;
  let previewOpen = false;
  const limit = 15;
  editor.innerHTML = `<div class="row"><strong data-count></strong><span class="spacer"></span>
    <button class="btn" type="button" data-toggle aria-expanded="false" aria-controls="askFinder">메모 추가</button></div>
    <div class="ask-source-list" data-selected></div>
    <div id="askFinder" hidden>
      <input type="search" aria-label="추가할 메모 검색" placeholder="추가할 메모 검색" style="width:100%">
      <div class="ask-source-list" data-matches></div>
      <div class="row"><button class="btn ghost" type="button" data-prev>이전</button>
        <span class="status" data-page></span><button class="btn ghost" type="button" data-next>다음</button></div>
    </div>`;
  const find = s => editor.querySelector(s);
  const finder = find("#askFinder"), input = find("input"), toggle = find("[data-toggle]");
  const selectedList = find("[data-selected]"), results = find("[data-matches]");

  function paintSelection() {
    find("[data-count]").textContent = `근거 메모 ${selected.size}건`;
    selectedList.innerHTML = [...selected].map(([id, name]) => `<div class="ask-source-row">
      <button class="btn ghost" type="button" data-view="${id}">M${id} ${esc(name)}</button>
      <button class="btn ghost" type="button" data-remove="${id}" aria-label="${esc(name)} 빼기">빼기</button></div>`).join("");
    results.querySelectorAll("[data-add]").forEach(b => {
      const picked = selected.has(+b.dataset.add);
      b.disabled = picked;
      b.textContent = picked ? "추가됨" : "추가";
    });
  }
  function showPack(pack) {
    if (!pack.sources.length) {
      handoff.textContent = pack.dropped.length ? "분량 한도 안에 담을 수 있는 근거가 없습니다. 메모 선택을 조정하세요." : "메모 추가를 눌러 요청문에 담을 근거를 고르세요.";
      return;
    }
    showCopilotPrompt(handoff, pack);
    handoff.querySelector("details").open = previewOpen;
  }
  async function updatePrompt() {
    const version = ++revision;
    const details = handoff.querySelector("details");
    if (details) previewOpen = details.open;
    paintSelection();
    if (!selected.size) { showPack({sources: [], dropped: []}); return; }
    handoff.textContent = "선택한 메모로 요청문을 갱신하는 중…";
    try {
      const pack = await api("/api/ask", {method: "POST", body: {
        ...filters, q: question, prompt_only: true, memo_ids: [...selected.keys()],
      }});
      if (editor.isConnected && version === revision) showPack(pack);
    } catch (error) {
      if (!editor.isConnected || version !== revision) return;
      handoff.innerHTML = `<span role="alert">${esc(error.message)}</span> <button class="btn" type="button">다시 시도</button>`;
      handoff.querySelector("button").onclick = updatePrompt;
    }
  }
  function searchMemos(delay = 0) {
    const version = ++searchVersion;
    clearTimeout(searchTimer);
    matches = [];
    results.textContent = "찾는 중…";
    find("[data-prev]").disabled = find("[data-next]").disabled = true;
    find("[data-page]").textContent = "";
    searchTimer = setTimeout(async () => {
      if (!editor.isConnected || finder.hidden) return;
      try {
        const data = await api("/api/generate/memos?" + qs({q: input.value.trim(), limit, offset}));
        if (!editor.isConnected || version !== searchVersion) return;
        matches = data.items;
        results.innerHTML = matches.map(m => `<div class="ask-source-row"><span>M${m.id} ${esc(label(m))}</span>
          <button class="btn" type="button" data-add="${m.id}" aria-label="${esc(label(m))} 추가"></button></div>`).join("") || "조건에 맞는 메모가 없습니다.";
        find("[data-prev]").disabled = offset === 0;
        find("[data-next]").disabled = !data.has_more;
        find("[data-page]").textContent = matches.length ? `${offset + 1}–${offset + matches.length}건` : "";
        paintSelection();
      } catch (error) {
        if (editor.isConnected && version === searchVersion) results.textContent = error.message;
      }
    }, delay);
  }
  toggle.onclick = () => {
    finder.hidden = !finder.hidden;
    toggle.setAttribute("aria-expanded", String(!finder.hidden));
    toggle.textContent = finder.hidden ? "메모 추가" : "추가 목록 닫기";
    if (!finder.hidden) { input.focus(); searchMemos(); }
    else { ++searchVersion; clearTimeout(searchTimer); }
  };
  input.oninput = () => { offset = 0; searchMemos(180); };
  find("[data-prev]").onclick = () => { offset = Math.max(0, offset - limit); searchMemos(); };
  find("[data-next]").onclick = () => { offset += limit; searchMemos(); };
  results.onclick = event => {
    const button = event.target.closest("[data-add]");
    if (!button || button.disabled) return;
    if (selected.size >= 1000) { toast("한 번에 1,000건까지 선택할 수 있습니다"); return; }
    const memo = matches.find(m => m.id === +button.dataset.add);
    selected.set(memo.id, label(memo));
    updatePrompt();
  };
  selectedList.onclick = event => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.dataset.view) { openMemo(+button.dataset.view); return; }
    selected.delete(+button.dataset.remove);
    updatePrompt();
    (selectedList.querySelector("[data-remove]") || toggle).focus();
  };
  paintSelection();
  showPack(initial);
}
