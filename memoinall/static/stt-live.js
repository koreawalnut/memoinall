// The local server owns the Google connection and API key.
async function openLiveMic(onText, onFailure) {
  const url = new URL("/api/stt/live", location.href);
  url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(url);
  let ready, rejectReady, completed, connected = false, closed = false, ending = false;
  const handshake = new Promise((resolve, reject) => { ready = resolve; rejectReady = reject; });
  const finished = new Promise((resolve) => { completed = resolve; });
  function close(message = "") {
    if (closed) return;
    closed = true;
    socket.close();
    completed(message);
    if (!connected) rejectReady(new Error(message || "Live 연결을 열지 못했습니다."));
    else if (message && !ending) onFailure(message);
  }
  socket.onclose = socket.onerror = () => close("Live 연결이 끊겼습니다. 네트워크와 Gemini 설정을 확인하세요.");
  socket.onmessage = ({data}) => {
    if (closed) return;
    const result = JSON.parse(data);
    if (result.error) close(result.error);
    else if (result.ready) { connected = true; ready(); }
    else if (result.done) close();
    else onText(result);
  };
  const timer = setTimeout(() => close("Live 연결 시간이 초과됐습니다."), 20000);
  try { await handshake; } finally { clearTimeout(timer); }
  return {
    send(data) {
      if (closed) return;
      if (socket.bufferedAmount > 160000) { close("음성 전송이 지연되어 녹음을 마쳤습니다."); return; }
      socket.send(data);
    },
    async finish() {
      ending = true;
      if (!closed) socket.send("finish");
      const timeout = setTimeout(() => close("Live 받아쓰기 마무리 시간이 초과됐습니다."), 15000);
      try { return await finished; } finally { clearTimeout(timeout); }
    },
    cancel() { ending = true; close(); },
  };
}
