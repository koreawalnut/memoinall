"""음성 입력(STT) 서버 쪽.  python tests/test_stt.py

모델을 실제로 받거나 돌리지 않는다. 엔진 호출부만 바꿔 끼우고
설정·상태·WAV 검사·모델 받기·Gemini 요청 형태를 확인한다.
"""

from __future__ import annotations

import base64
import asyncio
import json
from contextlib import asynccontextmanager
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

TMP = tempfile.mkdtemp(prefix="memoinall-stt-")
os.environ["MEMOINALL_HOME"] = TMP
os.environ["MEMOINALL_DISABLE_ST"] = "1"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from memoinall import config, db, store, stt, stt_live  # noqa: E402
from memoinall.api import app  # noqa: E402
from memoinall.providers.openai_compat import OpenAICompatProvider  # noqa: E402

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {extra}")


def wav_bytes(seconds=1.0, rate=16000, channels=1, amplitude=0.3) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    pcm = (np.sin(2 * np.pi * 440 * t) * amplitude * 32767).astype("<i2")
    if channels == 2:
        pcm = np.repeat(pcm, 2)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def post_wav(c, data: bytes):
    return c.post("/api/stt/transcribe", content=data, headers={"Content-Type": "audio/wav"})


class FakeWhisper:
    calls: list = []

    def __init__(self, path, device, compute_type):
        FakeWhisper.calls.append(("load", Path(path).name, device, compute_type))

    def transcribe(self, pcm, **kwargs):
        FakeWhisper.calls.append(("run", len(pcm), kwargs))
        return iter([SimpleNamespace(text=" 내일 회의는 3시입니다. "), SimpleNamespace(text=" "),
                     SimpleNamespace(text="보고서를 보내야 합니다.")]), None


class FakeCompletions:
    def __init__(self, replies):
        self.replies = list(replies)
        self.sent: list[dict] = []

    def create(self, **kwargs):
        self.sent.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        content, finish = reply
        message = SimpleNamespace(content=content, refusal=None, model_extra={})
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)], model="gemini")


def fake_client(completions):
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def main() -> int:
    assert config.DB_PATH.resolve() == (Path(TMP) / "memo.db").resolve()
    with patch.object(store, "start_worker"), TestClient(app) as c:
        # ---------------------------------------------------------------- 기본값
        with patch.object(stt, "local_package_available", return_value=True):
            s = c.get("/api/stt/status").json()
        check("기본 엔진은 로컬 Whisper small", s["engine"] == "local" and s["local_model"] == "small", s)
        check("모델이 없으면 받기 안내", s["state"] == "needs_download" and not s["ready"]
              and "약 486MB" in s["message"], s["message"])
        check("모델 목록과 크기", [m["name"] for m in s["local_models"]] == ["small", "large-v3-turbo"]
              and s["local_models"][1]["size"] == "1.6GB", s["local_models"])
        check("모델 위치는 사용자 폴더", s["models_dir"].startswith(TMP), s["models_dir"])

        with patch.object(stt, "local_package_available", return_value=False):
            s = c.get("/api/stt/status").json()
        check("패키지가 없으면 설치·Gemini 안내", s["state"] == "missing_package"
              and "pip install faster-whisper" in s["message"] and "Gemini" in s["message"], s["message"])

        # ---------------------------------------------------------------- 설정
        r = c.put("/api/settings", json={"values": {"stt.engine": "whisper.cpp"}})
        check("모르는 엔진 거부", r.status_code == 400, r.status_code)
        r = c.put("/api/settings", json={"values": {"stt.local.model": "huge"}})
        check("모르는 모델 거부", r.status_code == 400, r.status_code)

        c.put("/api/settings", json={"values": {"stt.engine": "gemini", "stt.gemini.model": "gemini-2.5-flash-lite"}})
        s = c.get("/api/stt/status").json()
        check("Gemini 를 고르고 키가 없으면 키 안내", s["engine"] == "gemini" and s["state"] == "needs_key", s)
        check("LLM 프로바이더는 그대로", c.get("/api/settings").json()["provider"] == "anthropic")

        c.put("/api/settings", json={"values": {"llm.gemini.api_key": "AIzaSyTESTKEY1234567890"}})
        s = c.get("/api/stt/status").json()
        check("LLM 연결과 같은 Gemini 키를 쓴다", s["ready"] and s["gemini_key"] and "Google" in s["message"], s)
        settings_view = c.get("/api/settings").json()["settings"]
        check("키는 가려서 내보낸다", "TESTKEY" not in settings_view["llm.gemini.api_key"]["value"])

        c.put("/api/settings", json={"values": {"stt.gemini.model": stt.LIVE_MODEL}})
        check("기존 Live 모델 설정 인식", c.get("/api/stt/status").json()["gemini_transport"] == "live")
        frames = []

        @asynccontextmanager
        async def fake_connect(url, **options):
            assert url == stt_live.URL and options["additional_headers"]["x-goog-api-key"]
            replies = asyncio.Queue()

            async def send(raw):
                frame = json.loads(raw)
                frames.append(frame)
                if "setup" in frame:
                    await replies.put('{"setupComplete":{}}')
                elif "audio" in frame["realtimeInput"]:
                    await replies.put(json.dumps({"serverContent": {"interimInputTranscription": {"text": "중간"}}}))
                elif "activityEnd" in frame["realtimeInput"]:
                    await replies.put(json.dumps({"serverContent": {"inputTranscription": {"text": "확정"}}}))

            yield SimpleNamespace(send=send, recv=replies.get)

        with patch.object(stt_live, "connect", fake_connect):
            check("Live 연결 테스트", c.post("/api/stt/live/test", json={"api_key": "test"}).json()["ok"])
            with c.websocket_connect("/api/stt/live", headers={"origin": "http://testserver"}) as ws:
                assert ws.receive_json() == {"ready": True}
                ws.send_text("start")
                ws.send_bytes(b"\x01\x02" * 1600)
                assert ws.receive_json() == {"interim": "중간"}
                ws.send_text("end")
                ws.send_text("finish")
                assert ws.receive_json() == {"final": "확정"}
                assert ws.receive_json() == {"done": True}
            check("Live 한국어 설정·PCM 전송", frames[0]["setup"]["inputAudioTranscription"]["languageCodes"] == ["ko-KR"]
                  and base64.b64decode(frames[-2]["realtimeInput"]["audio"]["data"]) == b"\x01\x02" * 1600)
        check("Live 설정은 기본 HTTP 전송을 거부", post_wav(c, wav_bytes()).status_code == 400)
        c.put("/api/settings", json={"values": {"stt.gemini.transport": "basic", "stt.gemini.model": "gemini-2.5-flash-lite"}})

        # ---------------------------------------------------------------- Gemini 요청 형태
        completions = FakeCompletions([("  내일 회의는 3시입니다.  ", "stop")])
        with patch.object(OpenAICompatProvider, "client", lambda self: fake_client(completions)):
            data = wav_bytes(1.2)
            r = post_wav(c, data)
        body = r.json()
        sent = completions.sent[0] if completions.sent else {}
        parts = sent.get("messages", [{}])[0].get("content", [])
        audio = next((p for p in parts if p.get("type") == "input_audio"), {})
        check("Gemini 변환 결과", r.status_code == 200 and body["text"] == "내일 회의는 3시입니다."
              and body["engine"] == "gemini" and body["model"] == "gemini-2.5-flash-lite", body)
        check("음성은 WAV base64 로 보낸다", audio.get("input_audio", {}).get("format") == "wav"
              and base64.b64decode(audio["input_audio"]["data"]) == data)
        check("받아쓰기는 추론을 끈다", sent.get("reasoning_effort") == "none", sent.get("reasoning_effort"))
        check("받아쓰기 지시를 함께 보낸다", any(p.get("type") == "text" and "받아 적" in p["text"] for p in parts))

        completions = FakeCompletions([RuntimeError("400 Invalid reasoning_effort"), ("확인했습니다", "stop")])
        with patch.object(OpenAICompatProvider, "client", lambda self: fake_client(completions)):
            r = post_wav(c, wav_bytes(1.0))
        check("추론을 못 끄는 모델은 빼고 다시 보낸다", r.status_code == 200 and r.json()["text"] == "확인했습니다"
              and "reasoning_effort" not in completions.sent[1], [k for k in completions.sent[-1]])

        completions = FakeCompletions([("", "stop")])
        with patch.object(OpenAICompatProvider, "client", lambda self: fake_client(completions)):
            r = post_wav(c, wav_bytes(1.0))
        check("말소리가 없다는 빈 답은 오류가 아니다", r.status_code == 200 and r.json()["text"] == "", r.text)

        completions = FakeCompletions([RuntimeError("403 API key not valid")])
        with patch.object(OpenAICompatProvider, "client", lambda self: fake_client(completions)):
            r = post_wav(c, wav_bytes(1.0))
        check("Gemini 오류는 이유와 함께 502", r.status_code == 502 and "API key not valid" in r.json()["detail"], r.text)

        # ---------------------------------------------------------------- 로컬 변환
        c.put("/api/settings", json={"values": {"stt.engine": "local"}})
        FakeWhisper.calls = []
        with patch.object(stt, "local_package_available", return_value=True), \
             patch.object(stt, "is_downloaded", return_value=True), \
             patch.object(stt, "_import_whisper_model", return_value=FakeWhisper):
            check("준비되면 상태가 ready", c.get("/api/stt/status").json()["ready"])
            check("녹음 시작 신호", c.post("/api/stt/prepare").json() == {"ok": True})
            for _ in range(50):
                if stt._loaded:
                    break
                time.sleep(0.05)
            r = post_wav(c, wav_bytes(2.0))
            r2 = post_wav(c, wav_bytes(1.0))
            r3 = c.post("/api/stt/transcribe?partial=1", content=wav_bytes(1.0), headers={"Content-Type": "audio/wav"})
        body = r.json()
        runs = [x for x in FakeWhisper.calls if x[0] == "run"]
        loads = [x for x in FakeWhisper.calls if x[0] == "load"]
        check("로컬 변환 결과를 이어 붙인다", r.status_code == 200
              and body["text"] == "내일 회의는 3시입니다. 보고서를 보내야 합니다.", body)
        check("녹음 길이를 돌려준다", body["seconds"] == 2.0 and body["engine"] == "local", body)
        check("모델은 미리 한 번만 올린다", loads == [("load", "small", "cpu", "int8")] and r2.status_code == 200, loads)
        check("한국어 고정 · 무음 구간 제거", runs and runs[0][1] == 32000 and runs[0][2].get("language") == "ko"
              and runs[0][2].get("vad_filter") is True, runs[:1])
        check("확정본은 정확하게(빔 5), 말하는 중 초안은 빠르게(빔 1)", r3.status_code == 200 and len(runs) == 3
              and runs[0][2].get("beam_size") == 5 and runs[2][2].get("beam_size") == 1, [x[2] for x in runs])
        stt._loaded = None

        # ---------------------------------------------------------------- WAV 검사
        FakeWhisper.calls = []
        with patch.object(stt, "local_package_available", return_value=True), \
             patch.object(stt, "is_downloaded", return_value=True), \
             patch.object(stt, "_import_whisper_model", return_value=FakeWhisper):
            r = post_wav(c, wav_bytes(1.0, amplitude=0.0))
            check("무음이면 모델을 돌리지 않고 무음이라고 알린다", r.status_code == 200 and r.json()["text"] == ""
                  and r.json()["silent"] is True
                  and not [x for x in FakeWhisper.calls if x[0] == "run"], FakeWhisper.calls)
            check("소리가 있으면 무음이 아니다", post_wav(c, wav_bytes(1.0)).json()["silent"] is False)
            r = post_wav(c, b"not a wav file")
            check("WAV 가 아니면 400", r.status_code == 400 and "읽지 못했습니다" in r.json()["detail"], r.text)
            r = post_wav(c, wav_bytes(1.0, rate=44100))
            check("16kHz 가 아니면 400", r.status_code == 400 and "16kHz" in r.json()["detail"], r.text)
            r = post_wav(c, wav_bytes(1.0, channels=2))
            check("스테레오면 400", r.status_code == 400, r.status_code)
            r = post_wav(c, wav_bytes(0.2))
            check("너무 짧으면 다시 하라고 안내", r.status_code == 400 and "짧습니다" in r.json()["detail"], r.text)
            r = post_wav(c, b"\0" * (stt.MAX_BYTES + 1))
            check("5분을 넘는 녹음은 413", r.status_code == 413 and "5분" in r.json()["detail"], r.text)
        stt._loaded = None

        # ---------------------------------------------------------------- 모델 받기
        fetched = []

        def fake_fetch(url, dest, progress, label):
            fetched.append(url)
            Path(dest).write_bytes(b"x" * 10)
            progress(label, 10, 10)

        with patch("memoinall.embed_onnx._fetch", fake_fetch), \
             patch.object(stt, "local_package_available", return_value=True):
            r = c.post("/api/stt/download", json={"model": "large-v3-turbo"})
            for _ in range(100):
                if not stt._download_snapshot()["running"]:
                    break
                time.sleep(0.02)
            d = stt.model_dir("large-v3-turbo")
            check("모델 받기", r.status_code == 200 and stt.is_downloaded("large-v3-turbo"), r.text)
            check("저장소의 필요한 파일만 받는다", sorted(Path(u).name for u in fetched)
                  == sorted(stt.LOCAL_MODELS["large-v3-turbo"].files)
                  and all("mobiuslabsgmbh/faster-whisper-large-v3-turbo/resolve/main/" in u for u in fetched), fetched)
            check("반쪽 파일을 남기지 않는다", not list(d.glob("*.part")), list(d.iterdir()))

            fetched.clear()
            c.post("/api/stt/download", json={"model": "large-v3-turbo"})
            time.sleep(0.1)
            check("이미 받은 모델은 다시 받지 않는다", fetched == [], fetched)
            check("모르는 모델 받기 거부", c.post("/api/stt/download", json={"model": "x"}).status_code == 400)

        def broken_fetch(url, dest, progress, label):
            Path(dest).write_bytes(b"half")
            raise OSError("네트워크 끊김")

        with patch("memoinall.embed_onnx._fetch", broken_fetch), \
             patch.object(stt, "local_package_available", return_value=True):
            c.post("/api/stt/download", json={"model": "small"})
            for _ in range(100):
                if not stt._download_snapshot()["running"]:
                    break
                time.sleep(0.02)
            s = c.get("/api/stt/status").json()
        check("받기 실패는 이유와 다시 받기 안내", s["state"] == "needs_download" and "네트워크 끊김" in s["message"]
              and not stt.is_downloaded("small"), s["message"])

    # -------------------------------------------------------------------- PyAV 없이
    # exe 는 PyAV 를 빼고 묶는다. 그래도 faster-whisper 를 import 할 수 있어야 한다.
    if stt.local_package_available():
        probe = (
            "import sys\n"
            "class Hide:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name == 'av' or name.startswith('av.'): raise ModuleNotFoundError(name)\n"
            "sys.meta_path.insert(0, Hide())\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from memoinall import stt\n"
            "cls = stt._import_whisper_model()\n"
            "print(cls.__name__, getattr(sys.modules['av'], '__file__', None))\n"
        )
        env = {**os.environ, "MEMOINALL_HOME": TMP}
        out = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, text=True, env=env, timeout=120)
        check("PyAV 가 없어도 faster-whisper 를 불러온다", out.stdout.strip() == "WhisperModel None", out.stdout + out.stderr)
    else:
        print("  SKIP faster-whisper 미설치 — PyAV 대체 확인 생략")

    print(f"\n통과 {PASS} · 실패 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    try:
        code = main()
    finally:
        db.close()
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
