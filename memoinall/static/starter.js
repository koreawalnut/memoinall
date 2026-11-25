function setupStarterTour() {
  const dialog = $("#starterTour"), tip = $("#tourTip");
  const steps = [
    {tab: "import", title: "기존 메모 가져오기",
     text: "파일 선택을 눌러 문서를 미리 보세요. PC에 저장된 스티커 메모도 여기서 가져올 수 있어요."},
    {tab: "settings", title: "내 태그 1개 정하기",
     text: "이름을 입력하고 ‘만들기’를 눌러 보세요. 만든 태그는 메모를 쓸 때 바로 고를 수 있어요."},
  ];
  let index = 0, anchor, control, generation = 0;
  let sticky = false, fileSelected = false, moving = false;
  const observer = new ResizeObserver(position);
  const blocked = new Set();

  function release() {
    observer.disconnect();
    anchor?.classList.remove("tour-work-area");
    for (const element of blocked) element.inert = false;
    blocked.clear();
    if (control) {
      const ids = (control.getAttribute("aria-describedby") || "").split(/\s+/).filter(id => id && id !== "tourText");
      if (ids.length) control.setAttribute("aria-describedby", ids.join(" "));
      else control.removeAttribute("aria-describedby");
    }
    anchor = control = null;
  }

  function bindTarget() {
    let region, input;
    if (index === 0) {
      region = sticky ? $("#stickySource") : $("#chooseDocument");
      if (!sticky && fileSelected && (docImport.preview || docImport.error)) region = $("#documentPanel");
      input = sticky ? region?.querySelector('[data-src-preview]:enabled') || region
        : region?.querySelector("[data-document-part]:enabled") || $("#chooseDocument");
    } else {
      region = $("#tourTagField");
      input = $("#newTag");
    }
    if (!region || !input) return false;
    if (region === anchor && input === control) return true;
    release();
    anchor = region;
    control = input;
    if (anchor.matches("#documentPanel, #stickySource")) anchor.classList.add("tour-work-area");
    // Keep the actual highlighted controls and the tutorial available to keyboard and screen reader users.
    for (let node = anchor; node && node !== document.body; node = node.parentElement) {
      for (const sibling of node.parentElement.children) {
        if (sibling === node || sibling === dialog || sibling.inert || sibling.matches("script, style, link, #toast, #documentFile")) continue;
        sibling.inert = true;
        blocked.add(sibling);
      }
    }
    $("#tourText").textContent = index === 0 && sticky
      ? "이 PC에서 발견한 스티커 메모를 미리 보세요. 사용 가능 여부도 이 영역에서 확인할 수 있어요."
      : index === 0 && fileSelected && docImport.preview ? "가져올 부분을 확인하고 ‘메모로 저장’을 누르세요. 마치면 다음 단계로 이동하세요."
      : steps[index].text;
    control.setAttribute("aria-describedby", [control.getAttribute("aria-describedby"), "tourText"].filter(Boolean).join(" "));
    anchor.scrollIntoView({block: innerHeight < 480 ? "start" : "center", inline: "nearest", behavior: "instant"});
    control.focus({preventScroll: true});
    if (anchor.classList.contains("tour-work-area")) control.scrollIntoView({block: "nearest", inline: "nearest"});
    observer.observe(anchor);
    observer.observe(tip);
    position();
    return true;
  }
  function position() {
    if (!dialog.open || !anchor?.isConnected) return;
    const width = dialog.clientWidth, height = dialog.clientHeight;
    let rect = anchor.getBoundingClientRect();
    if (rect.bottom > height || rect.top < 0) {
      anchor.scrollIntoView({block: "center", inline: "nearest", behavior: "instant"});
      rect = anchor.getBoundingClientRect();
    }
    const left = Math.max(0, rect.left - 4), top = Math.max(0, rect.top - 4);
    const right = Math.min(width, rect.right + 4), bottom = Math.min(height, rect.bottom + 4);
    for (const [side, value] of Object.entries({left, top, right, bottom})) {
      dialog.style.setProperty("--" + side, value + "px");
    }
    const below = height - bottom - 22, above = top - 22;
    const placeBelow = below >= tip.scrollHeight + 2 || below >= above;
    tip.style.maxHeight = Math.max(80, placeBelow ? below : above) + "px";
    tip.style.left = Math.max(12, Math.min(left, width - tip.offsetWidth - 12)) + "px";
    tip.style.top = Math.max(8, placeBelow ? bottom + 10 : top - tip.offsetHeight - 10) + "px";
  }


  async function show(i) {
    const token = ++generation;
    moving = true;
    index = i;
    const step = steps[i];
    const buttons = [$("#tourNext"), $("#tourBack"), $("#tourImportSwitch")];
    buttons.forEach(button => { button.disabled = true; });
    try {
      if (i === 0) imp.section = "import";
      if (state.tab !== step.tab) await goTab(step.tab);
      else if (i === 0) paintExchangeSection();
      if (token !== generation) return;
      $("#tourTitle").textContent = step.title;
      $("#tourProgress").textContent = (i + 1) + " / " + steps.length + "단계";
      $("#tourImportSwitch").hidden = i !== 0;
      $("#tourImportSwitch").textContent = sticky ? "파일 가져오기 보기" : "스티커 메모 보기";
      $("#tourBack").hidden = i === 0;
      $("#tourNext").textContent = i === steps.length - 1 ? "마치기" : "다음";
      if (!dialog.open) dialog.show();
      if (!bindTarget()) throw new Error("튜토리얼 대상을 찾지 못했습니다.");
    } catch {
      if (token === generation) {
        end();
        toast("튜토리얼 화면을 불러오지 못했습니다. 설정에서 다시 열어주세요.");
      }
    } finally {
      if (token === generation) {
        moving = false;
        buttons.forEach(button => { button.disabled = false; });
      }
    }
  }

  function open() {
    if (dialog.open || moving) return;
    sticky = false;
    fileSelected = false;
    return show(0);
  }

  function end(completed = false) {
    ++generation;
    moving = false;
    const lastControl = control;
    release();
    dialog.close();
    if (completed) {
      goTab("feed");
      $("#capture").focus();
    } else if (lastControl?.isConnected) {
      lastControl.focus({preventScroll: true});
    }
    api("/api/starter", {method: "PUT", body: {visible: false}, keepalive: true})
      .catch(() => toast("튜토리얼 종료 상태를 저장하지 못했습니다."));
  }

  $("#tourImportSwitch").onclick = () => { sticky = !sticky; show(0); };
  $("#tourBack").onclick = () => show(index - 1);
  $("#tourNext").onclick = () => index === steps.length - 1 ? end(true) : show(index + 1);
  $("#tourClose").onclick = () => end();
  document.addEventListener("keydown", event => {
    if (!dialog.open || event.isComposing) return;
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      end();
    }
  }, true);
  document.addEventListener("change", event => {
    if (dialog.open && index === 0 && event.target.id === "documentFile" && event.target.files.length) fileSelected = true;
  }, true);
  window.addEventListener("resize", position);
  window.addEventListener("scroll", position, true);
  return {open, refresh() {
    if (!dialog.open || moving) return;
    if (state.tab !== steps[index].tab || !bindTarget()) end();
  }, async firstRun() {
    try {
      const s = await api("/api/starter");
      if (s.visible && state.tab === "feed" && !document.querySelector("dialog[open]")) await open();
    } catch { /* The main UI remains usable if onboarding state is unavailable. */ }
  }};
}
