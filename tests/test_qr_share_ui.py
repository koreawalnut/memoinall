"""PC 앱의 QR 받기 (휴대폰 → PC 한 방향).  python tests/test_qr_share_ui.py

- 휴대폰이 띄운 QR(여러 장)을 Chromium 가짜 웹캠으로 찍어 받는다 → 기존 '받은 메모' 미리보기·가져오기.
- PC 는 메모를 QR 로 만들지 않는다(사내 메모가 휴대폰으로 나가는 길이 없다). 그리는 QR 은 설치 주소뿐이다.
- 비밀번호로 잠근 휴대폰 QR 은 비밀번호를 넣어야 열린다.
Node 가 없으면 건너뛴다(QR 영상을 만들 때 실제 qrshare.js 를 Node 로 돌린다).
"""
import json
import os
import random
import re
import sys
import tempfile
from pathlib import Path

TMP = tempfile.TemporaryDirectory(prefix="memoinall-qr-ui-", ignore_cleanup_errors=True)
os.environ["MEMOINALL_HOME"] = TMP.name
os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

from memoinall import exchange, store, todos  # noqa: E402
from memoinall.api import app  # noqa: E402
from qr_helpers import camera_args, node_available, run_qrshare, write_camera_video  # noqa: E402

WORDS = ("내일 오후 결제 시스템 점검 회의 보고서 금요일 서버 로그 확인 고객사 요청 배포 일정 변경 검토 예산 승인 견적서 "
         "계약 담당자 연락 회신 대기 자료 정리 공유 다음주 장애 원인 분석 재발 방지 대책 테스트 환경 데이터 이관 백업").split()


def main():
    if not node_available():
        print("SKIP node 가 없어 QR 영상을 만들 수 없습니다")
        return
    rnd = random.Random(3)
    phone_memos = [
        {"uid": f"phone-{i}", "title": "", "body": f"휴대폰 메모 {i}\n" + " ".join(rnd.choice(WORDS) for _ in range(260)),
         "created_at": f"2026-11-1{i}T08:00:00", "updated_at": f"2026-11-1{i}T08:00:00", "tags": ["현장"], "origin": "mobile"}
        for i in range(1, 4)
    ]
    pack = {"format": exchange.FORMAT, "version": 1, "count": 3, "note": "", "memos": phone_memos}
    qr = run_qrshare(text=json.dumps(pack, ensure_ascii=False))
    assert len(qr["parts"]) >= 2, len(qr["parts"])
    video = Path(TMP.name) / "camera.y4m"
    write_camera_video(video, qr["matrices"])

    errors = []
    with TestClient(app) as client, sync_playwright() as pw:
        store.add_memo("PC 메모: 결제 점검 회의 정리", enqueue_enrich=False)
        store.add_memo("PC 메모: 보고서 마감 금요일", enqueue_enrich=False)
        browser = pw.chromium.launch(headless=True, args=camera_args(video))
        page = browser.new_page(viewport={"width": 1280, "height": 920})
        page.on("pageerror", lambda e: errors.append(str(e)))

        def serve(route):
            request = route.request
            path = request.url.split("127.0.0.1:8787", 1)[-1]
            sent = {"content-type": request.headers["content-type"]} if "content-type" in request.headers else {}
            response = client.request(request.method, path, content=request.post_data_buffer, headers=sent)
            headers = {k: v for k, v in response.headers.items() if k not in {"content-length", "content-encoding"}}
            route.fulfill(status=response.status_code, headers=headers, body=response.content)

        page.route("http://127.0.0.1:8787/**", serve)
        client.put("/api/starter", json={"visible": False})
        page.goto("http://127.0.0.1:8787/")
        page.locator('[data-tab="import"]').click()
        page.locator('[data-exchange-tab="share"]').click()
        expect(page.locator("#sharedFile")).to_be_visible()
        expect(page.locator("#sharedQr")).to_be_hidden()
        expect(page.locator("#mobileInstall")).to_be_hidden()
        page.get_by_role("button", name="휴대폰 QR·설치").click()
        expect(page.locator("#sharedFile")).to_be_hidden()
        expect(page.locator("#mobileInstall")).to_be_visible()
        expect(page.locator('[data-exchange-section="mobile"] h3').first).to_have_text("휴대폰 앱 설치")
        page.locator("#mobileInstall").focus()
        page.keyboard.press("Tab")
        expect(page.locator("#sharedQr")).to_be_focused()

        # 휴대폰이 띄운 QR 여러 장을 웹캠으로 찍어 받는다
        page.locator("#sharedQr").click()
        dlg = page.locator("#qrDlg")
        expect(dlg).to_be_visible()
        expect(dlg.locator("video")).to_be_visible()
        picker = page.locator("#impOut-shared")
        expect(picker.locator("[data-pick]")).to_have_count(3, timeout=30000)
        expect(dlg).to_be_hidden()
        expect(picker).to_be_visible()
        expect(page.locator("#sharedQrStatus")).not_to_contain_text("실패")
        assert page.evaluate("typeof jsQR") == "function"  # 필요할 때 불러왔다
        page.locator('[data-src-run="shared"]').click()
        expect(picker).to_contain_text("가져오기 완료")
        got = sorted(m["body"].split("\n")[0] for m in store.list_memos() if m["source"] == "shared")
        assert got == ["휴대폰 메모 1", "휴대폰 메모 2", "휴대폰 메모 3"], got
        print(f"OK {len(qr['parts'])} QR codes from the phone are read by webcam and imported after review")

        # 같은 QR 을 다시 찍으면 이미 있는 메모로 표시된다
        page.locator("#sharedQr").click()
        expect(picker.locator("[data-pick]")).to_have_count(3, timeout=30000)
        expect(picker.locator("[data-pick]:checked")).to_have_count(0)
        print("OK scanning the same QR again marks every memo as already imported")

        # 한 방향: PC 는 메모를 QR 로 만들지 않는다 (사내 메모가 휴대폰으로 나가는 길을 두지 않는다)
        page.locator('[data-exchange-tab="share"]').click()
        expect(picker).to_be_hidden()
        page.locator("#expPreview").click()
        expect(page.locator("#expReview [data-picker-count]")).to_contain_text("5건 선택")
        assert page.locator("#expQr").count() == 0
        assert page.evaluate("typeof qrcode") == "undefined"      # QR 을 그리는 라이브러리 자체를 싣지 않는다
        page.locator('[data-exchange-tab="mobile"]').click()
        expect(page.locator("#sharedQr")).to_be_visible()
        print("OK one way only: the PC has no 'show as QR' and does not even load a QR drawing library")

        # 휴대폰 앱 설치 QR — 설치 주소만 담는다. 찍기 창에서도 바로 열 수 있다.
        read_canvas = """(sel) => { const c = document.querySelector(sel);
          const img = c.getContext('2d').getImageData(0, 0, c.width, c.height);
          const hit = jsQR(img.data, img.width, img.height); return hit && hit.data; }"""
        page.locator("#sharedQr").click()
        expect(dlg.locator("video")).to_be_visible()
        page.locator("#qrToInstall").click()
        expect(dlg.locator("#installQr")).to_be_visible()
        expect(dlg.locator("video")).to_have_count(0)                  # 카메라는 꺼진다
        default_url = "https://kbiwalnut.github.io/mobile/"
        assert page.evaluate(read_canvas, "#installQr") == default_url
        expect(dlg).to_contain_text("메모는 들어 있지 않습니다")
        expect(page.locator("#installUrl")).to_have_text(default_url)
        dlg.locator("#installUrlEdit summary").click()
        page.locator("#installUrlInput").fill("http://intranet/mobile/")
        page.locator("#installUrlForm button[type=submit]").click()
        expect(page.locator("#installUrlNote")).to_contain_text("https://")
        page.locator("#installUrlInput").fill("https://example.org/m/")
        page.locator("#installUrlForm button[type=submit]").click()
        expect(page.locator("#installUrl")).to_have_text("https://example.org/m/")
        assert page.evaluate(read_canvas, "#installQr") == "https://example.org/m/"
        page.locator("#installUrlReset").click()
        expect(page.locator("#installUrl")).to_have_text(default_url)
        page.locator("#qrDlgClose").click()
        page.locator("#mobileInstall").click()
        expect(dlg.locator("#installQr")).to_be_visible()
        assert page.evaluate(read_canvas, "#installQr") == default_url
        page.locator("#qrDlgClose").click()
        assert client.put("/api/settings", json={"values": {"mobile.url": "javascript:alert(1)"}}).status_code == 400
        assert page.locator("#expQr").count() == 0
        print("OK install QR holds only the app address, opens from the scan window too, and the address can be changed (https only)")

        # 휴대폰이 비밀번호로 잠가 보낸 QR 도 같은 비밀번호 칸으로 연다
        body = "잠근 휴대폰 메모: 현장 배관 점검 결과와 보수 일정 협의 내용"
        locked = exchange.seal({**pack, "memos": [{**phone_memos[0], "uid": "phone-locked", "body": body}], "count": 1}, "현장 비번")
        qr2 = run_qrshare(text=json.dumps(locked, ensure_ascii=False))
        assert len(qr2["parts"]) == 1
        page.evaluate("""async (part) => {
          const c = new QrShare.Collector(); c.add(part);
          await loadShared(async () => c.text(), "QR로 받은 메모", { what: "QR" });
        }""", qr2["parts"][0])
        expect(page.locator("#sharedQrStatus")).to_contain_text("잠긴 메모")
        page.locator("#impSharedPw").fill("현장 비번")
        page.locator("#impSharedPw").press("Enter")
        expect(picker.locator("[data-pick]")).to_have_count(1)
        page.locator('[data-src-run="shared"]').click()
        expect(picker).to_contain_text("가져오기 완료")
        assert any(body in m["body"] for m in store.list_memos()), [m["body"][:20] for m in store.list_memos()]
        print("OK a password-locked phone QR asks for the password and then imports")

        page.locator('[data-exchange-tab="share"]').click()
        expect(page.locator("#sharedFileStatus")).not_to_contain_text("QR")
        file_pack = exchange.seal(pack, "파일 비번")
        page.locator("#sharedFile").set_input_files({
            "name": "동료.json", "mimeType": "application/json",
            "buffer": json.dumps(file_pack).encode(),
        })
        expect(page.locator("#sharedFileStatus")).to_contain_text("동료.json")
        expect(page.locator("#impSharedPw")).to_be_focused()
        expect(page.locator("#impSharedPw")).to_have_value("")
        page.locator("#impSharedPw").fill("파일 비번")
        page.locator("#impSharedPw").press("Enter")
        expect(picker.locator("[data-pick]")).to_have_count(3)
        expect(picker).to_be_visible()
        print("OK file review works independently after receiving a locked QR")

        assert not errors, errors
        browser.close()

        # 휴대폰 일정 QR (비밀번호로 잠금) — 웹캠으로 찍으면 일정 화면으로 가서 PC 일정에 들어간다.
        # 휴대폰 exchange.js 처럼 잠근 봉투 겉에 kind 를 붙인다.
        schedules = {"format": "memoinall/schedules", "version": 1, "count": 2, "schedules": [
            {"uid": "ev-1", "title": "고객사 방문", "date": "2026-12-03", "time": "10:30", "note": "견적서 지참",
             "tags": ["고객사"], "done": False, "created_at": "2026-11-18T09:00:00"},
            {"uid": "ev-2", "title": "안전 교육", "date": "2026-12-05", "time": "", "note": "", "tags": [], "done": False,
             "created_at": "2026-11-18T09:01:00"}]}
        envelope = {**exchange.seal(schedules, "일정 비번"), "kind": "schedules"}
        qr3 = run_qrshare(text=json.dumps(envelope, ensure_ascii=False))
        video2 = Path(TMP.name) / "camera-schedules.y4m"
        write_camera_video(video2, qr3["matrices"])
        browser = pw.chromium.launch(headless=True, args=camera_args(video2))
        page = browser.new_page(viewport={"width": 1280, "height": 920})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.route("http://127.0.0.1:8787/**", serve)
        page.goto("http://127.0.0.1:8787/")
        page.locator('[data-tab="import"]').click()
        page.locator('[data-exchange-tab="mobile"]').click()
        expect(page.locator("h3", has_text="휴대폰 메모·일정 QR로 받기")).to_be_visible()
        page.locator("#sharedQr").click()
        dlg = page.locator("#qrDlg")
        expect(page.locator("#schedPw")).to_be_visible(timeout=30000)
        expect(page.locator("#qrDlgTitle")).to_have_text("휴대폰 일정 받기")
        expect(dlg.locator("video")).to_have_count(0)
        page.locator("#schedPw").fill("틀린 비번")
        page.locator("#schedPw").press("Enter")
        expect(dlg).to_contain_text("비밀번호")
        page.locator("#schedPw").fill("일정 비번")
        page.locator("#schedPw").press("Enter")
        expect(dlg.locator("[data-sched-key]")).to_have_count(2)
        expect(dlg).to_contain_text("2026-12-03 10:30")
        expect(dlg).to_contain_text("#고객사")
        dlg.locator("[data-sched-key]").nth(1).uncheck()
        expect(page.locator("#schedApply")).to_have_text("일정 1건 가져오기")
        page.locator("#schedApply").click()
        expect(dlg).to_contain_text("일정 1건을 추가했습니다")
        expect(dlg.locator("[data-sched-key]").nth(0)).to_be_disabled()      # 방금 넣은 것은 이미 있음
        expect(dlg.locator("[data-sched-key]").nth(0).locator("xpath=..")).to_contain_text("이미 있음")
        visit = next((t for t in todos.board(limit=50)["items"] if t["title"] == "10:30 고객사 방문"), None)
        assert visit and visit["scheduled_for"] == "2026-12-03" and visit["tags"] == ["고객사"], visit
        assert not any(t["title"] == "안전 교육" for t in todos.board(limit=50)["items"])
        page.locator("#schedOpen").click()
        expect(dlg).to_be_hidden()
        expect(page.locator('[data-tab="todos"]')).to_have_class(re.compile(r"\bon\b"))
        print(f"OK a locked schedule QR from the phone ({len(qr3['parts'])} QR) is read by webcam, "
              "opens with the password, and only the checked schedule is added to PC schedules")

        assert not errors, errors
        browser.close()


if __name__ == "__main__":
    main()
