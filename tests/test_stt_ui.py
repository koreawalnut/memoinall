"""메모 입력칸의 음성 입력(🎤)과 설정 화면.  python tests/test_stt_ui.py

Chromium 의 가짜 마이크로 실제로 소리를 받아, 말하는 도중 입력칸에 글이 나타나는지 확인한다.
음성 인식 엔진만 바꿔 끼운다(모델을 받거나 돌리지 않는다).
"""
import os
import re
import sys
import tempfile
import wave
from pathlib import Path
from unittest.mock import patch

TMP = tempfile.TemporaryDirectory(prefix="memoinall-stt-ui-", ignore_cleanup_errors=True)
os.environ["MEMOINALL_HOME"] = TMP.name
os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

from memoinall import config, db, settings, store, stt  # noqa: E402
from memoinall.api import app  # noqa: E402


def fake_microphone(path: Path) -> None:
    """말 1.5초 + 쉼 1.2초를 되풀이한다. 높낮이가 바뀌는 소리라야 브라우저 잡음 제거가 지우지 않는다."""
    rate, out = 48000, []
    for _ in range(4):
        for i in range(6):
            t = np.arange(int(rate * 0.22)) / rate
            out.append(np.sin(2 * np.pi * (220 + 90 * (i % 5)) * t) * 0.45 * np.hanning(t.size))
            out.append(np.zeros(int(rate * 0.03)))
        out.append(np.zeros(int(rate * 1.2)))
    pcm = (np.concatenate(out) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


WATCH = """() => { window.__seen = []; let last = null;
  setInterval(() => { const v = document.querySelector('#capture').value;
    if (v !== last) { window.__seen.push([document.querySelector('#mic').dataset.state, v]); last = v; } }, 50); }"""


def main():
    assert config.DB_PATH.resolve() == (Path(TMP.name) / "memo.db").resolve()
    errors, dialogs, heard, calls, downloads = [], [], [], [], []
    downloaded = {}
    mic_file = Path(TMP.name) / "mic.wav"
    fake_microphone(mic_file)
    real_transcribe = stt.transcribe

    def record_transcribe(data, *, partial=False):
        calls.append((stt.engine_name(), partial))
        return real_transcribe(data, partial=partial)

    def fake_local(pcm, name, *, partial=False):
        heard.append((len(pcm), partial, name))
        return "말하는 중" if partial else f"문장{sum(1 for h in heard if not h[1])}."

    with patch.object(store, "start_worker"), TestClient(app) as client, sync_playwright() as pw, \
         patch.object(stt, "transcribe", side_effect=record_transcribe), \
         patch.object(stt, "local_package_available", return_value=True), \
         patch.object(stt, "is_downloaded", side_effect=lambda name: downloaded.get(name, False)), \
         patch.object(stt, "_load_local", return_value=None), \
         patch.object(stt, "_transcribe_local", side_effect=fake_local), \
         patch.object(stt, "_transcribe_gemini", return_value="제미나이 문장."), \
         patch.object(stt, "start_download", side_effect=lambda name=None: downloads.append(name)):
        browser = pw.chromium.launch(headless=True, args=[
            "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream",
            f"--use-file-for-fake-audio-capture={mic_file}",
        ])
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("pageerror", lambda e: errors.append(str(e)))

        def answer(route):
            request = route.request
            path = request.url.split("127.0.0.1:8787", 1)[-1]
            sent = {"content-type": request.headers["content-type"]} if "content-type" in request.headers else {}
            response = client.request(request.method, path, content=request.post_data_buffer, headers=sent)
            headers = {k: v for k, v in response.headers.items() if k not in {"content-length", "content-encoding"}}
            route.fulfill(status=response.status_code, headers=headers, body=response.content)

        page.route("http://127.0.0.1:8787/**", answer)
        page.goto("http://127.0.0.1:8787/")
        capture, mic, hint = page.locator("#capture"), page.locator("#mic"), page.locator("#captureHint")
        expect(capture).to_be_enabled()
        expect(mic).to_be_enabled()
        expect(mic).to_have_attribute("aria-label", "음성으로 입력")
        box_mic, box_save = mic.bounding_box(), page.locator("#save").bounding_box()
        assert box_mic["x"] + box_mic["width"] <= box_save["x"], (box_mic, box_save)
        assert abs(box_mic["height"] - box_save["height"]) <= 1, (box_mic, box_save)
        print("OK small mic button sits just left of 'save' at the same height")

        # 모델이 없으면 받을지 묻고, 받는 동안에도 메모를 쓸 수 있다.
        page.once("dialog", lambda d: (dialogs.append(d.message), d.accept()))
        mic.click()
        expect(mic).to_have_attribute("data-state", "idle")
        page.wait_for_timeout(300)
        assert downloads == ["small"], downloads
        assert "small" in dialogs[0] and "약 486MB" in dialogs[0], dialogs
        print("OK first use asks before downloading the model and names its size")

        # 말하는 도중에 글이 나타난다: 초안이 먼저 보이고, 쉼에서 확정된 문장으로 바뀐다.
        downloaded["small"] = True
        capture.fill("회의 메모:")
        page.evaluate(WATCH)
        mic.click()
        expect(mic).to_have_attribute("data-state", "recording")
        expect(mic).to_have_attribute("aria-label", "받아쓰기 끝내기")
        expect(hint).to_contain_text("듣는 중")
        expect(page.locator(".capture")).to_have_class(re.compile(r"\bdictating\b"))
        assert capture.evaluate("el => el.readOnly")
        # 목소리 크기에 따라 버튼 테두리 고리가 커진다. 마우스가 위에 있어도 빨간색이다.
        page.wait_for_function("parseFloat(document.querySelector('#mic').style.getPropertyValue('--lvl')) > 0.2")
        assert mic.evaluate("b => getComputedStyle(b).color === getComputedStyle(b).borderColor")
        expect(capture).to_have_value(re.compile(r"^회의 메모: 문장1\."), timeout=10000)
        expect(mic).to_have_attribute("data-state", "recording")  # 아직 듣는 중인데 이미 확정 문장이 보인다
        mic.click()
        expect(mic).to_have_attribute("data-state", "idle", timeout=10000)
        final = capture.input_value()
        assert re.fullmatch(r"회의 메모: 문장1\.( 문장\d+\.)*", final), final
        seen = page.evaluate("window.__seen")
        assert any(s == "recording" and v.endswith("말하는 중") for s, v in seen), seen
        assert not capture.evaluate("el => el.readOnly")
        expect(page.locator("#toast")).to_contain_text("받아쓴 내용을 확인한 뒤 저장하세요")
        commits = [h for h in heard if not h[1]]
        assert commits and all(16000 * 0.6 <= n <= 16000 * 4 for n, _, _ in commits), commits
        assert client.get("/api/memos").json()["items"] == []
        print("OK text appears while speaking: drafts first, then each pause commits a sentence")

        # 커서가 중간에 있으면 그 자리에, 앞뒤 띄어쓰기를 맞춰 넣는다.
        capture.fill("가나다라")
        capture.evaluate("el => el.setSelectionRange(2, 2)")
        mic.click()
        expect(capture).to_have_value(re.compile(r"^가나 .*문장\d+\. 다라$"), timeout=10000)
        mic.click()
        expect(mic).to_have_attribute("data-state", "idle", timeout=10000)
        middle = capture.input_value()
        assert re.fullmatch(r"가나 문장\d+\.( 문장\d+\.)* 다라", middle), middle
        print("OK text goes in at the cursor with spaces around it")

        # 소리가 아예 안 들어오면 듣는 도중에 바로 알려 주고, 아무것도 넣지 않는다.
        page.evaluate("mic.voiceRms = mic.deadRms = 10")
        mic.click()
        expect(hint).to_contain_text("마이크에서 소리가 들어오지 않습니다", timeout=6000)
        expect(hint).to_contain_text("데스크톱 앱 액세스")
        mic.click()
        expect(mic).to_have_attribute("data-state", "idle")
        expect(hint).to_contain_text("마이크에서 소리가 들어오지 않습니다")
        expect(capture).to_have_value(middle)
        page.evaluate("mic.voiceRms = 0.012; mic.deadRms = 0.002")
        print("OK a silent microphone is reported while listening")

        # Esc 는 🎤와 같이 받아쓰기를 끝내고, 받아쓴 글은 남긴다.
        mic.click()
        expect(capture).not_to_have_value(middle, timeout=10000)
        page.keyboard.press("Escape")
        expect(mic).to_have_attribute("data-state", "idle", timeout=15000)
        page.wait_for_timeout(500)
        kept = capture.input_value()
        assert kept != middle and "문장" in kept, kept
        assert not capture.evaluate("el => el.readOnly")
        print("OK Esc finishes dictation and keeps the text")

        # Gemini 를 골랐는데 키가 없으면 무엇을 하면 되는지 설정으로 잇는다.
        client.put("/api/settings", json={"values": {"stt.engine": "gemini"}})
        mic.click()
        expect(hint).to_contain_text("Gemini API 키가 없습니다")
        hint.get_by_role("link", name="설정 열기").click()
        panel = page.locator("#sttPanel")
        expect(panel).to_be_visible()
        expect(page.locator("#sttEngine")).to_have_value("gemini")
        expect(page.locator("#sttGeminiKey")).to_be_visible()
        expect(page.locator("#sttLocalModel")).to_be_hidden()
        expect(page.locator("#sttState")).to_have_text("○ 준비 필요")
        print("OK missing Gemini key links straight to the voice settings")

        # 방식을 바꾸면 저장 전에도 해당 항목만 보인다. 모델 받기는 선택을 함께 저장한다.
        page.locator("#sttEngine").select_option("local")
        expect(page.locator("#sttLocalModel")).to_be_visible()
        expect(page.locator("#sttGeminiKey")).to_be_hidden()
        expect(page.locator("#sttDownload")).to_be_hidden()  # small 은 받아 둠
        expect(page.locator("#sttLocalNote")).to_contain_text("받아 둔 모델입니다")
        page.locator("#sttLocalModel").select_option("large-v3-turbo")
        expect(page.locator("#sttDownload")).to_be_visible()
        expect(page.locator("#sttLocalNote")).to_contain_text("약 1.6GB")
        page.locator("#sttDownload").click()
        page.wait_for_timeout(300)
        assert downloads[-1] == "large-v3-turbo", downloads
        assert settings.get("stt.engine") == "local" and settings.get("stt.local.model") == "large-v3-turbo"
        print("OK settings switch fields live and 'download' also saves the choice")

        # Gemini 키를 여기서 넣어도 요약에 쓰는 AI 는 그대로다.
        page.locator("#sttEngine").select_option("gemini")
        page.locator("#sttGeminiKey").fill("AIzaSyUiTestKey1234567890")
        page.locator("#sttGeminiModel").fill("gemini-2.5-flash-lite")
        page.locator("#sttSave").click()
        expect(page.locator("#sttState")).to_have_text("● Gemini gemini-2.5-flash-lite 사용 중")
        view = client.get("/api/settings").json()
        assert view["provider"] == "anthropic", view["provider"]
        assert settings.get("llm.gemini.api_key") == "AIzaSyUiTestKey1234567890"
        assert settings.get("stt.gemini.model") == "gemini-2.5-flash-lite"
        expect(page.locator("#sttGeminiKey")).to_have_attribute("placeholder", "AIzaSyU…7890 — 바꾸려면 새로 입력")
        print("OK Gemini key saved from the voice panel without switching the AI provider")

        # Gemini 는 호출마다 요금이 들어 말하는 중 초안을 보내지 않고, 쉼에서만 보낸다.
        page.locator('[data-tab="feed"]').click()
        capture.fill("")
        calls.clear()
        mic.click()
        expect(capture).to_have_value(re.compile(r"제미나이 문장\."), timeout=10000)
        mic.click()
        expect(mic).to_have_attribute("data-state", "idle", timeout=10000)
        assert calls and all(engine == "gemini" and not partial for engine, partial in calls), calls
        print("OK Gemini only gets one request per pause, no drafts")

        page.locator("#moreMenu > summary").click()
        page.locator('[data-tab="settings"]').click()
        page.locator("#sttTransport").select_option("live")
        expect(page.locator("#sttGeminiModel")).to_be_hidden()
        expect(page.locator("#sttLiveField")).to_contain_text("gemini-3.5-transcribe-live")
        page.locator("#sttSave").click()
        expect(page.locator("#sttState")).to_contain_text("gemini-3.5-transcribe-live")
        page.locator('[data-tab="feed"]').click()
        sockets, commands = [], []

        def live_socket(ws):
            sockets.append(ws)
            def receive(frame):
                if isinstance(frame, bytes):
                    assert len(frame) % 2 == 0
                    ws.send('{"interim":"초안"}')
                else:
                    commands.append(frame)
                    if frame == "end":
                        ws.send('{"final":"확정 문장."}')
                    elif frame == "finish":
                        ws.send('{"done":true}')
            ws.on_message(receive)
            ws.send('{"ready":true}')

        page.route_web_socket("**/api/stt/live", live_socket)
        page.reload()  # Playwright installs its WebSocket routing hook on navigation.
        calls.clear()
        capture.fill("메모")
        mic.click()
        expect(capture).to_have_value(re.compile("초안"), timeout=10000)
        mic.click()
        expect(mic).to_have_attribute("data-state", "idle")
        assert capture.input_value() == "메모 " + " ".join(["확정 문장."] * commands.count("end"))
        assert len(sockets) == 1 and commands[-1] == "finish" and not calls
        print("OK one live socket streams PCM, replaces drafts and drains final text on stop")

        # 좁은 창에서도 입력 줄이 넘치지 않는다.
        page.set_viewport_size({"width": 390, "height": 844})
        expect(mic).to_be_visible()
        for sel in ("#mic", "#save"):
            b = page.locator(sel).bounding_box()
            assert b["x"] >= 0 and b["x"] + b["width"] <= 390, (sel, b)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        print("OK capture bar fits a phone-width window")

        assert not errors, errors
        browser.close()


if __name__ == "__main__":
    try:
        main()
    finally:
        db.close()
