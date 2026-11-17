"""휴대폰 앱(PWA).  python tests/test_mobile_ui.py

build_mobile.py 로 만든 폴더를 localhost 에 띄우고 휴대폰 크기 Chromium 으로 실제로 써 본다.
- 메모 쓰기·초안·검색·태그·고치기·뒤로 가기, 새로 열어도 남아 있는지(IndexedDB)
- 일정 넣기·끝냄·다른 달
- PC로 보내기: QR(여러 장·비밀번호)을 화면에서 읽어 PC 형식으로 열리는지. 파일·공유 시트로는 못 보낸다
- 한 방향: PC 의 공유 파일은 되살리기로도 들어오지 않는다. 카메라로 받는 기능 자체가 없다
- AI 정리: Gemini·ChatGPT·Claude 요청 모양과 오류 문구 (가짜 응답, 실제 키는 쓰지 않음)
- 백업 → 전부 지우기 → 되살리기, 오프라인에서 다시 열기
"""
import functools
import http.server
import json
import mimetypes
import re
import sys
import tempfile
import threading
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import expect, sync_playwright  # noqa: E402

import build_mobile  # noqa: E402
from memoinall import exchange  # noqa: E402

TMP = tempfile.TemporaryDirectory(prefix="memoinall-mobile-ui-", ignore_cleanup_errors=True)
JSQR = ROOT / "memoinall" / "static" / "vendor" / "jsQR.js"   # 화면의 QR 을 읽어 보는 데만 쓴다(앱에는 없다)
AI_HOSTS = re.compile(r"https://(generativelanguage\.googleapis\.com|api\.openai\.com|api\.anthropic\.com)/.*")
CORS = {"access-control-allow-origin": "*", "access-control-allow-methods": "POST, OPTIONS",
        "access-control-allow-headers": "content-type, x-goog-api-key, authorization, x-api-key, anthropic-version, anthropic-dangerous-direct-browser-access"}

READ_QR = """async () => {
  const box = document.querySelector('#sendOut');
  const read = () => { const c = box.querySelector('.qrs-code');
    const img = c.getContext('2d').getImageData(0, 0, c.width, c.height);
    const hit = jsQR(img.data, img.width, img.height); return hit && hit.data; };
  const next = box.querySelector('[aria-label="다음 QR"]');
  const total = next ? +box.querySelector('.qrs-count').textContent.split('/')[1] : 1;
  const parts = [];
  for (let i = 0; i < total; i++) { parts.push(read()); if (next) next.click(); }
  return { parts, total, text: await QrShare.decode(parts) };
}"""


def serve(folder: Path):
    mimetypes.add_type("application/manifest+json", ".webmanifest")

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(folder)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://localhost:{srv.server_address[1]}/"


def main():
    site = Path(TMP.name) / "site"
    info = build_mobile.build(site)
    srv, base = serve(site)

    errors, answers, ai_calls = [], [], []
    ai_reply = {"status": 200}

    def on_dialog(d):
        (d.accept if (answers.pop(0) if answers else True) else d.dismiss)()

    def fake_ai(route):
        req = route.request
        if req.method == "OPTIONS":
            route.fulfill(status=204, headers=CORS)
            return
        ai_calls.append({"url": req.url, "headers": req.headers, "body": json.loads(req.post_data or "{}")})
        if ai_reply["status"] != 200:
            route.fulfill(status=ai_reply["status"], headers={**CORS, "content-type": "application/json"},
                          body=json.dumps({"error": {"message": "API key not valid. Please pass a valid API key."}}))
            return
        text = "할 일:\n1. **보고서** 금요일까지 보내기 [2]\n2. 서버 로그 확인 [1]"
        if "googleapis" in req.url:
            body = {"candidates": [{"content": {"parts": [{"text": "생각 중", "thought": True}, {"text": text}]}, "finishReason": "STOP"}]}
        elif "openai" in req.url:
            body = {"choices": [{"message": {"role": "assistant", "content": "OK"}}]}
        else:
            body = {"content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn"}
        route.fulfill(status=200, headers={**CORS, "content-type": "application/json"}, body=json.dumps(body, ensure_ascii=False))

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 360, "height": 780}, device_scale_factor=2, is_mobile=True,
                                  has_touch=True, locale="ko-KR", accept_downloads=True)
        ctx.route(AI_HOSTS, fake_ai)
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", on_dialog)
        memos, capture = page.locator("#memoList .memo"), page.locator("#capture")

        def fits():
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "horizontal scroll"

        def tab(name):
            page.locator(f'nav.tabs [data-go="{name}"]').click()

        def toast(text):
            expect(page.locator("#toast")).to_contain_text(text)

        page.goto(base)
        expect(page.locator("#memoList .empty")).to_contain_text("아직 메모가 없습니다")
        fits()

        # ---------------------------------------------------------- 설치 안내
        banner = page.locator("#installBanner")
        expect(banner).to_be_visible()
        expect(page.locator("#installHow")).to_contain_text("앱 설치")
        expect(page.locator("#installNow")).to_be_hidden()
        page.evaluate("""() => { window.__prompted = 0;
          const e = new Event('beforeinstallprompt', { cancelable: true });
          e.prompt = () => { window.__prompted++; };
          e.userChoice = Promise.resolve({ outcome: 'dismissed' });
          window.dispatchEvent(e); }""")
        expect(page.locator("#installNow")).to_be_visible()
        expect(page.locator("#installHow")).to_contain_text("바로 설치")
        page.locator("#installNow").click()
        expect(page.locator("#installNow")).to_be_hidden()          # 한 번 쓴 설치 요청은 다시 쓸 수 없다
        assert page.evaluate("window.__prompted") == 1
        expect(banner).to_be_visible()                               # 설치를 취소했으니 안내는 남는다
        page.locator("#installClose").click()
        expect(banner).to_be_hidden()
        page.reload()
        expect(page.locator("#capture")).to_be_visible()
        expect(banner).to_be_hidden()                                # 닫은 안내는 다시 나오지 않는다
        kakao = browser.new_context(viewport={"width": 360, "height": 780}, is_mobile=True, has_touch=True,
                                    user_agent="Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/131 Mobile KAKAOTALK 10.9.0")
        kp = kakao.new_page()
        kp.goto(base)
        expect(kp.locator("#installHow")).to_contain_text("다른 브라우저로 열기")
        kakao.close()
        iphone = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                                     user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 Version/17.5 Mobile/15E148 Safari/604.1")
        ip = iphone.new_page()
        ip.goto(base)
        expect(ip.locator("#installHow")).to_contain_text("홈 화면에 추가")
        iphone.close()
        print("OK install banner: one-tap install where the browser allows it, Safari and in-app-browser instructions, stays closed once closed")

        # ---------------------------------------------------------- 메모
        capture.fill("내일 오후 3시 결제 시스템 점검 회의 #회의 #결제\n자료는 김과장이 준비")
        expect(page.locator("#draftNote")).to_contain_text("자동 보관")
        page.reload()
        expect(capture).to_have_value(re.compile("결제 시스템 점검 회의"))
        expect(page.locator("#draftNote")).to_contain_text("되살렸습니다")
        page.locator("#saveMemo").click()
        toast("저장했습니다")
        expect(capture).to_have_value("")
        capture.fill("금요일까지 보고서 보내기 #보고서")
        capture.press("Control+Enter")
        capture.fill("서버 로그 확인하기")
        page.locator("#saveMemo").click()
        expect(memos).to_have_count(3)
        expect(page.locator("#tagChips .chip")).to_have_count(4)   # 전체 + 3
        expect(memos.first).to_contain_text("서버 로그")              # 새 것이 위
        page.locator("#q").fill("결제 회의")
        expect(memos).to_have_count(1)
        assert set(memos.first.locator("mark").all_text_contents()) == {"결제", "회의"}
        expect(page.locator("#listInfo")).to_contain_text("찾은 메모 1건")
        page.locator("#q").fill("")
        page.locator('#tagChips [data-tag="보고서"]').click()
        expect(memos).to_have_count(1)
        page.locator('#tagChips [data-tag="보고서"]').click()
        expect(memos).to_have_count(3)
        print("OK memos: draft survives reload, #tags become chips, search needs every word, tag filter toggles")

        memos.filter(has_text="서버 로그").click()
        dlg = page.locator("#memoDlg")
        expect(dlg).to_be_visible()
        page.locator("#memoBody").fill("서버 로그 확인하기 #서버")
        page.locator("#tagInput").fill("점검")
        page.locator("#tagInput").press("Enter")
        page.locator("#memoSave").click()
        expect(dlg).to_be_hidden()
        card = memos.filter(has_text="서버 로그")
        expect(card.locator(".tag")).to_have_text(["#점검", "#서버"])
        card.click()
        page.locator('[data-untag="0"]').click()
        answers.extend([False, True])      # 저장 안 하고 닫을까요? → 아니요, 다시 → 예
        dlg.locator("[data-close]").click()
        expect(dlg).to_be_visible()
        dlg.locator("[data-close]").click()
        expect(dlg).to_be_hidden()
        expect(card.locator(".tag")).to_have_count(2)
        card.click()
        expect(dlg).to_be_visible()
        url = page.url
        page.go_back()                      # 휴대폰 뒤로 가기
        expect(dlg).to_be_hidden()
        assert page.url == url
        page.reload()
        expect(memos).to_have_count(3)
        expect(memos.filter(has_text="서버 로그").locator(".tag")).to_have_count(2)
        print("OK editing: tags added/removed, unsaved changes ask first, phone back closes the sheet, all kept after reload")

        # ---------------------------------------------------------- 일정
        tab("cal")
        today = date.today()
        page.locator("#addEvent").click()
        page.locator("#evTitle").fill("결제 시스템 점검 회의")
        page.locator("#evTime").fill("15:00")
        page.locator("#evTags").fill("#회의")
        page.locator("#evSave").click()
        expect(page.locator("#dayList .event")).to_have_count(1)
        expect(page.locator("#dayList")).to_contain_text("15:00")
        expect(page.locator(f'[data-day="{today.isoformat()}"] .dots i')).to_have_count(1)
        expect(page.locator("#upcoming")).to_contain_text("결제 시스템 점검 회의")
        page.locator("#dayList .event input").check()
        expect(page.locator("#dayList .event.done")).to_have_count(1)
        expect(page.locator("#upcoming .event")).to_have_count(0)
        later = (today.replace(day=1) + timedelta(days=40)).replace(day=12)
        page.locator("#addEvent").click()
        page.locator("#evTitle").fill("고객사 방문")
        page.locator("#evDate").fill(later.isoformat())
        page.locator("#evSave").click()
        expect(page.locator("#calTitle")).to_have_text(f"{later.year}년 {later.month}월")
        expect(page.locator(f'[data-day="{later.isoformat()}"] .dots i')).to_have_count(1)
        page.locator("#calToday").click()
        expect(page.locator("#calTitle")).to_have_text(f"{today.year}년 {today.month}월")
        page.locator("#addEvent").click()
        page.locator("#evSave").click()
        expect(page.locator("#evErr")).to_contain_text("적어주세요")
        page.go_back()
        expect(page.locator("#eventDlg")).to_be_hidden()
        fits()
        print("OK schedule: add with time and tag, dot on the day, mark done, jump to another month, title required")

        # ---------------------------------------------------------- 보내기 (QR·파일)
        tab("memos")
        page.locator("#selectToggle").click()
        expect(page.locator("#selbar")).to_be_visible()
        expect(page.locator("#selSend")).to_be_disabled()
        memos.filter(has_text="보고서").click()
        memos.filter(has_text="서버 로그").click()
        expect(page.locator("#selCount")).to_have_text("2건 선택")
        page.locator("#selSend").click()
        send = page.locator("#sendDlg")
        expect(send).to_contain_text("PC로 보내기 — 메모 2건")
        expect(page.locator("#sendPwNote")).to_contain_text("누구나")
        # PC로는 QR 로만 보낸다 — 파일 저장·공유 시트(메신저·메일)는 없다
        expect(page.locator("#sendFile")).to_be_hidden()
        assert page.locator("#sendShare").count() == 0
        page.locator("#sendQr").click()
        expect(send.locator(".qrs-code")).to_be_visible()
        expect(send).to_contain_text("QR 찍기")
        page.add_script_tag(path=str(JSQR))
        r = page.evaluate(READ_QR)
        packet = exchange.parse(r["text"])
        assert sorted(m["body"] for m in packet["memos"]) == ["금요일까지 보고서 보내기 #보고서", "서버 로그 확인하기 #서버"]
        assert packet["memos"][0]["tags"] and packet["memos"][0]["origin"] == "mobile" and "backup" not in packet
        page.locator("#sendPw").fill("폰 비번")
        expect(page.locator("#sendPwNote")).to_contain_text("이 비밀번호를 넣어야")
        page.locator("#sendQr").click()
        expect(send).to_contain_text("암호화됨")
        r = page.evaluate(READ_QR)
        assert exchange.parse(r["text"], "폰 비번")["count"] == 2
        page.go_back()
        expect(send).to_be_hidden()
        expect(page.locator("#selbar")).to_be_visible()
        print("OK sending to PC: QR only (no file or share sheet); read off the screen it opens on the PC, plain and locked")

        page.locator("#selDone").click()
        page.evaluate("""async () => { for (let i = 0; i < 25; i++)
          await addMemo('대량 메모 ' + i + ' ' + Array.from({length: 60}, (_, k) => '현장점검배관누수보수일정협의자재발주'.slice((i * 7 + k) % 14, (i * 7 + k) % 14 + 2 + k % 3)).join(' '));
          renderAllMemos(); }""")
        page.locator("#q").fill("대량")
        expect(memos).to_have_count(25)
        page.locator("#selectToggle").click()
        page.locator("#selAll").click()
        expect(page.locator("#selCount")).to_have_text("25건 선택")
        page.locator("#selSend").click()
        page.locator("#sendQr").click()
        expect(send.locator(".qrs-count")).to_contain_text("1 / ")
        total = int(send.locator(".qrs-count").text_content().split("/")[1])
        assert total > 1, total
        page.wait_for_timeout(1300)
        expect(send.locator(".qrs-count")).not_to_contain_text("1 / ")     # 저절로 넘어간다
        r = page.evaluate(READ_QR)
        assert exchange.parse(r["text"])["count"] == 25
        page.go_back()
        answers.append(True)
        page.locator("#selDel").click()
        toast("25건을 지웠습니다")
        expect(page.locator("#selbar")).to_be_hidden()
        page.locator("#q").fill("")
        expect(memos).to_have_count(3)
        print(f"OK 25 memos go out as {total} auto-advancing QR codes and come back whole; bulk delete asks first")

        # ---------------------------------------------------------- 한 방향: PC 메모는 휴대폰으로 들어오지 않는다
        tab("share")
        fits()
        expect(page.locator("#tab-share")).to_contain_text("휴대폰 → PC 한 방향")
        for gone in ("#scanBtn", "#scanDlg", "#openFile"):
            assert page.locator(gone).count() == 0, gone
        assert page.evaluate("typeof jsQR === 'function' && !document.querySelector('script[src*=\"jsQR\"]')")
        from_pc = {"format": exchange.FORMAT, "version": 1, "count": 1, "note": "", "memos": [
            {"uid": "pc-a", "body": "PC 메모: 예산 승인 요청", "created_at": "2026-09-01T10:00:00", "tags": ["예산"]}]}
        plain = Path(TMP.name) / "from-pc.json"
        plain.write_text(exchange.dumps(from_pc), encoding="utf-8")
        locked = Path(TMP.name) / "from-pc-locked.json"
        locked.write_text(exchange.dumps(exchange.seal(from_pc, "회사 비번")), encoding="utf-8")
        imp = page.locator("#importDlg")
        page.locator("#restoreFile").set_input_files(str(plain))
        expect(imp).to_contain_text("백업 파일이 아닙니다")
        expect(page.locator("#importRun")).to_be_disabled()
        page.go_back()
        page.locator("#restoreFile").set_input_files(str(locked))
        expect(imp).to_contain_text("암호화된 백업")
        page.locator("#impPw").fill("회사 비번")
        page.locator("#impPw").press("Enter")
        expect(imp).to_contain_text("백업 파일이 아닙니다")     # 열어도 PC 파일이면 받지 않는다
        expect(page.locator("#impPw")).to_have_count(0)
        expect(page.locator("#importRun")).to_be_disabled()
        page.go_back()
        expect(imp).to_be_hidden()
        expect(memos).to_have_count(3)
        assert page.evaluate("history.state") is None
        print("OK one way only: no QR scanning on the phone, and PC share files (plain or locked) are refused by restore")

        # ---------------------------------------------------------- AI 정리
        tab("settings")
        fits()
        expect(page.locator("#aiModel")).to_have_value("gemini-2.5-flash")
        page.locator("#aiKey").fill("g-key")
        page.locator("#aiSave").click()
        expect(page.locator("#aiOut")).to_contain_text("Gemini로 보냅니다")
        tab("memos")
        page.locator("#selectToggle").click()
        memos.filter(has_text="보고서").click()
        memos.filter(has_text="서버 로그").click()
        page.locator("#selAi").click()
        ai = page.locator("#aiDlg")
        expect(page.locator("#aiWho")).to_contain_text("Google로 전송")
        page.locator('[data-preset="1"]').click()
        expect(page.locator("#aiAsk")).to_have_value(re.compile("해야 할 일"))
        page.locator("#aiRun").click()
        expect(page.locator("#aiText")).to_contain_text("서버 로그 확인")
        expect(page.locator("#aiText b")).to_have_text("보고서")
        assert "생각 중" not in page.locator("#aiText").text_content()
        call = ai_calls[-1]
        assert call["url"].endswith("/models/gemini-2.5-flash:generateContent"), call["url"]
        assert call["headers"]["x-goog-api-key"] == "g-key"
        prompt = call["body"]["contents"][0]["parts"][0]["text"]
        assert "해야 할 일" in prompt and "서버 로그 확인하기" in prompt and "금요일까지 보고서" in prompt
        assert "근거" in call["body"]["systemInstruction"]["parts"][0]["text"]
        page.locator("#aiKeep").click()
        toast("메모로 저장했습니다")
        ai_reply["status"] = 400
        page.locator("#aiRun").click()
        expect(page.locator("#aiState")).to_contain_text("API 키가 맞지 않습니다")
        ai_reply["status"] = 200
        page.go_back()
        expect(ai).to_be_hidden()
        page.locator("#selDone").click()
        expect(memos.filter(has_text="AI 정리:").locator(".badge")).to_have_text("AI 정리")
        expect(memos.filter(has_text="AI 정리:").locator(".tag")).to_have_text(["#AI정리"])

        tab("settings")
        page.locator("#aiProvider").select_option("openai")
        expect(page.locator("#aiModel")).to_have_value("gpt-5-mini")
        page.locator("#aiKey").fill("o-key")
        page.locator("#aiTest").click()
        expect(page.locator("#aiOut")).to_contain_text("연결됐습니다")
        assert ai_calls[-1]["url"] == "https://api.openai.com/v1/chat/completions"
        assert ai_calls[-1]["headers"]["authorization"] == "Bearer o-key"
        page.locator("#aiProvider").select_option("anthropic")
        expect(page.locator("#aiModel")).to_have_value("claude-opus-5-5")
        page.locator("#aiKey").fill("a-key")
        page.locator("#aiTest").click()
        expect(page.locator("#aiOut")).to_contain_text("연결됐습니다")
        h = ai_calls[-1]["headers"]
        assert h["x-api-key"] == "a-key" and h["anthropic-dangerous-direct-browser-access"] == "true" and h["anthropic-version"]
        assert ai_calls[-1]["body"]["model"] == "claude-opus-5-5" and ai_calls[-1]["body"]["max_tokens"] > 0
        page.locator("#aiProvider").select_option("gemini")
        expect(page.locator("#aiKey")).to_have_value("g-key")        # 서비스마다 키를 따로 기억
        print("OK AI: Gemini request carries the memos and key, thoughts hidden, result saved as a memo; "
              "bad key explained; ChatGPT and Claude request shapes checked")

        # ---------------------------------------------------------- 백업 → 지우기 → 되살리기
        tab("share")
        expect(page.locator("#backupInfo")).to_contain_text("아직 백업한 적이 없습니다")
        page.locator("#backupBtn").click()
        expect(send).to_contain_text("백업 — 메모 4건 · 일정 2건")
        expect(page.locator("#sendQr")).to_be_hidden()
        with page.expect_download() as dl:
            page.locator("#sendFile").click()
        backup = Path(TMP.name) / "backup.json"
        dl.value.save_as(str(backup))
        data = exchange.parse(backup.read_text(encoding="utf-8"))
        assert data["count"] == 4 and len(data["schedules"]) == 2 and data["backup"] == "memoinall/mobile-backup"
        page.go_back()
        expect(page.locator("#backupInfo")).to_contain_text("마지막 백업")
        tab("settings")
        expect(page.locator("#appInfo")).to_contain_text("4건")
        answers.extend([True, True])
        page.locator("#wipe").click()
        toast("모두 지웠습니다")
        tab("memos")
        expect(memos).to_have_count(0)
        tab("share")
        page.locator("#restoreFile").set_input_files(str(backup))
        expect(imp.locator("[data-imp]:checked")).to_have_count(4)
        expect(page.locator("#impEvents")).to_be_checked()
        expect(page.locator("#importRun")).to_have_text("메모 4건 · 일정 2건 되살리기")
        page.locator("#importRun").click()
        toast("되살렸습니다")
        expect(memos).to_have_count(4)
        expect(memos.filter(has_text="서버 로그 확인하기").locator(".badge")).to_have_count(0)    # 내가 쓴 메모는 그대로
        expect(memos.filter(has_text="AI 정리:").locator(".badge")).to_have_text("AI 정리")
        tab("cal")
        expect(page.locator(f'[data-day="{today.isoformat()}"] .dots i.done')).to_have_count(1)
        print("OK backup holds memos and schedules; after wiping, restoring brings both back with their origin")

        # ---------------------------------------------------------- 오프라인
        page.wait_for_function("navigator.serviceWorker && navigator.serviceWorker.controller !== null", timeout=15000)
        ctx.set_offline(True)
        page.reload()
        tab("memos")
        expect(memos).to_have_count(4)
        expect(page.locator("#offline")).to_be_visible()
        ctx.set_offline(False)
        assert page.evaluate("APP_VERSION") == info["version"]
        print(f"OK offline: the installed app opens without network (version {info['version']})")

        assert not errors, errors
        browser.close()
    srv.shutdown()


if __name__ == "__main__":
    main()
