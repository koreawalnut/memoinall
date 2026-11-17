"""PC 앱의 QR 받기 (휴대폰 → PC 한 방향).  python tests/test_qr_share_ui.py

- 휴대폰이 띄운 QR(여러 장)을 Chromium 가짜 웹캠으로 찍어 받는다 → 기존 '받은 메모' 미리보기·가져오기.
- PC 는 QR 을 만들지 않는다(사내 메모가 휴대폰으로 나가는 길이 없다).
- 비밀번호로 잠근 휴대폰 QR 은 비밀번호를 넣어야 열린다.
Node 가 없으면 건너뛴다(QR 영상을 만들 때 실제 qrshare.js 를 Node 로 돌린다).
"""
import json
import os
import random
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

from memoinall import exchange, store  # noqa: E402
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
        page.goto("http://127.0.0.1:8787/")
        page.locator('[data-tab="import"]').click()
        page.locator('[data-exchange-tab="share"]').click()

        # 휴대폰이 띄운 QR 여러 장을 웹캠으로 찍어 받는다
        page.locator("#sharedQr").click()
        dlg = page.locator("#qrDlg")
        expect(dlg).to_be_visible()
        expect(dlg.locator("video")).to_be_visible()
        picker = page.locator("#impOut-shared")
        expect(picker.locator("[data-pick]")).to_have_count(3, timeout=30000)
        expect(dlg).to_be_hidden()
        expect(page.locator("#sharedFileStatus")).not_to_contain_text("실패")
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

        # 한 방향: PC 는 QR 을 만들지 않는다 (사내 메모가 휴대폰으로 나가는 길을 두지 않는다)
        page.locator("#expPreview").click()
        expect(page.locator("#expReview [data-picker-count]")).to_contain_text("5건 선택")
        assert page.locator("#expQr").count() == 0
        assert page.evaluate("typeof qrcode") == "undefined"      # QR 을 그리는 라이브러리 자체를 싣지 않는다
        expect(page.locator("#sharedQr")).to_be_visible()
        print("OK one way only: the PC has no 'show as QR' and does not even load a QR drawing library")

        # 휴대폰이 비밀번호로 잠가 보낸 QR 도 같은 비밀번호 칸으로 연다
        body = "잠근 휴대폰 메모: 현장 배관 점검 결과와 보수 일정 협의 내용"
        locked = exchange.seal({**pack, "memos": [{**phone_memos[0], "uid": "phone-locked", "body": body}], "count": 1}, "현장 비번")
        qr2 = run_qrshare(text=json.dumps(locked, ensure_ascii=False))
        assert len(qr2["parts"]) == 1
        page.evaluate("""async (part) => {
          const c = new QrShare.Collector(); c.add(part);
          await loadShared(async () => c.text(), "QR로 받은 메모", { what: "QR" });
        }""", qr2["parts"][0])
        expect(page.locator("#sharedFileStatus")).to_contain_text("암호화된 파일입니다")
        page.locator("#impSharedPw").fill("현장 비번")
        page.locator("#impSharedPw").press("Enter")
        expect(picker.locator("[data-pick]")).to_have_count(1)
        page.locator('[data-src-run="shared"]').click()
        expect(picker).to_contain_text("가져오기 완료")
        assert any(body in m["body"] for m in store.list_memos()), [m["body"][:20] for m in store.list_memos()]
        print("OK a password-locked phone QR asks for the password and then imports")

        assert not errors, errors
        browser.close()


if __name__ == "__main__":
    main()
