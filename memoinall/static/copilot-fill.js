// Called inside the dedicated Edge WebView2 window. Only fill an empty chat composer.
(function fillCopilotDraft(prompt, origins) {
  function trusted(page) {
    return origins.includes(page.location.origin) && /^\/(?:chat\/?)?$/.test(page.location.pathname);
  }
  if (!trusted(window)) return "waiting";
  const selector = 'textarea, [contenteditable="true"], [contenteditable="plaintext-only"]';
  function composer(doc) {
    for (const el of doc.querySelectorAll(selector)) {
      if (!el.getClientRects().length || el.disabled || el.readOnly || el.getAttribute("aria-disabled") === "true") continue;
      const hint = ["aria-label", "placeholder", "data-placeholder", "aria-placeholder"].map(k => el.getAttribute(k) || "").join(" ");
      if (["m365-chat-editor-target-element", "userInput"].includes(el.id) ||
          (/copilot|코파일럿|메시지|message|무엇이든 물어|ask anything/i.test(hint) && !/search|검색/i.test(hint))) return el;
    }
    for (const frame of doc.querySelectorAll("iframe")) {
      const child = frame.contentDocument;
      if (child && child.location.origin === doc.location.origin) {
        const found = composer(child);
        if (found) return found;
      }
    }
    return null;
  }
  const editor = composer(document);
  if (!editor) return "waiting";
  const doc = editor.ownerDocument;
  const value = () => (editor.value ?? editor.innerText ?? "").replace(/\r\n/g, "\n");
  if (value().trim()) return "occupied";
  editor.focus();
  if (editor.isContentEditable) {
    // Rich editors can handle plain-text paste themselves. The OS clipboard stays untouched.
    const data = new DataTransfer();
    data.setData("text/plain", prompt);
    const paste = new ClipboardEvent("paste", {clipboardData: data, bubbles: true, cancelable: true});
    if (editor.dispatchEvent(paste) && !value().trim()) {
      const lines = doc.createElement("div");
      for (const [index, line] of prompt.split("\n").entries()) {
        if (index) lines.append(doc.createElement("br"));
        lines.append(doc.createTextNode(line));
      }
      doc.execCommand("insertHTML", false, lines.innerHTML);
    }
  } else {
    doc.execCommand("insertText", false, prompt);
  }
  if (value().trimEnd() === prompt.trimEnd()) return "filled";
  return "manual";
})
