"""화면 테마(화이트·다크): 처음 시작할 때 고르기 → 튜토리얼, 설정 맨 아래에서 바꾸기, 깜빡임 없는 적용.
python tests/test_theme_ui.py
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

TMP = tempfile.TemporaryDirectory(prefix="memoinall-theme-ui-", ignore_cleanup_errors=True)
os.environ["MEMOINALL_HOME"] = TMP.name
os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402
from memoinall import db, settings, store  # noqa: E402
from memoinall.api import app  # noqa: E402

ARTIFACTS = Path(__file__).resolve().parents[1] / "artifacts" / "ux-review"
LIGHT_BG, DARK_BG = "rgb(246, 247, 249)", "rgb(15, 17, 21)"


def main():
    errors = []
    with patch.object(store, "start_worker"), TestClient(app) as client, sync_playwright() as pw:
        store.add_memo("테마 확인용 메모 #업무", enqueue_enrich=False)
        assert client.get("/api/starter").json()["visible"]            # 새 프로필: 시작 안내가 켜져 있다
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(locale="ko-KR", color_scheme="light", viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))

        def serve(route):
            request = route.request
            path = request.url.split("127.0.0.1:8787", 1)[-1]
            sent = {"content-type": request.headers["content-type"]} if "content-type" in request.headers else {}
            response = client.request(request.method, path, content=request.post_data_buffer, headers=sent)
            headers = {k: v for k, v in response.headers.items() if k not in {"content-length", "content-encoding"}}
            route.fulfill(status=response.status_code, headers=headers, body=response.content)

        page.route("http://127.0.0.1:8787/**", serve)
        background = lambda: page.evaluate("getComputedStyle(document.body).backgroundColor")  # noqa: E731
        theme = lambda: page.evaluate("document.documentElement.dataset.theme")                # noqa: E731

        # ---- 처음 시작: 테마부터 고르고, 고른 뒤에 튜토리얼
        page.goto("http://127.0.0.1:8787/")
        chooser = page.locator("#themeChooser")
        expect(chooser).to_be_visible()
        expect(page.locator("#starterTour")).not_to_be_visible()           # 테마를 고르기 전에는 튜토리얼을 열지 않는다
        light, dark = chooser.locator('[data-theme-option="light"]'), chooser.locator('[data-theme-option="dark"]')
        expect(light).to_have_attribute("aria-checked", "true")             # 처음엔 지금 보이는 색(Windows 설정)
        expect(light).to_be_focused()
        assert theme() == "light" and background() == LIGHT_BG
        dark.click()                                                          # 누르면 바로 미리 보기
        expect(dark).to_have_attribute("aria-checked", "true")
        assert theme() == "dark" and background() == DARK_BG
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(ARTIFACTS / "theme-chooser-dark.png"))
        dark.press("ArrowLeft")                                               # 화살표로도 고른다
        expect(light).to_have_attribute("aria-checked", "true")
        expect(light).to_be_focused()
        light.press("ArrowRight")
        assert theme() == "dark"
        assert settings.theme() == ""                                         # 아직 저장 전
        page.locator("#themeChooserDone").click()
        expect(chooser).not_to_be_visible()
        expect(page.locator("#starterTour")).to_be_visible()                 # 고른 뒤 튜토리얼
        assert settings.theme() == "dark"
        page.locator("#tourClose").click()
        print("OK first run asks for a theme with live preview (mouse and arrow keys), saves it, then opens the tutorial")

        # ---- 다시 켜면: 서버가 고른 테마를 박아 보내 처음부터 그 색, 다시 묻지 않는다
        page.goto("http://127.0.0.1:8787/")
        assert page.evaluate("document.documentElement.dataset.themeChoice") == "dark"
        assert background() == DARK_BG
        page.wait_for_timeout(500)
        expect(chooser).not_to_be_visible()

        # ---- 설정 맨 아래 '화면 테마'
        page.locator("#tabs [data-tab='settings']").click()
        panel = page.locator("#themePanel")
        expect(panel).to_be_visible()
        assert page.evaluate("document.querySelector('#view').lastElementChild.id") == "themePanel"   # 맨 아래
        expect(panel.locator('[data-theme-option="dark"]')).to_have_attribute("aria-checked", "true")
        expect(panel.locator("#themeNote")).to_have_text("")
        panel.locator('[data-theme-option="light"]').click()
        expect(page.locator("#toast")).to_contain_text("화이트 테마로 바꿨습니다")
        expect(panel.locator('[data-theme-option="light"]')).to_have_attribute("aria-checked", "true")
        assert theme() == "light" and background() == LIGHT_BG and settings.theme() == "light"
        panel.scroll_into_view_if_needed()
        page.screenshot(path=str(ARTIFACTS / "theme-settings-light.png"))
        # 운영체제가 다크로 바뀌어도 고른 화이트는 그대로
        page.emulate_media(color_scheme="dark")
        assert theme() == "light"
        print("OK settings ends with the theme panel; switching applies instantly, saves, and ignores later OS changes")

        # ---- 저장 실패: 원래 테마로 되돌린다
        page.route("http://127.0.0.1:8787/api/settings", lambda route: route.fulfill(
            status=500, content_type="application/json", body='{"detail":"디스크가 가득 찼습니다"}')
            if route.request.method == "PUT" else serve(route))
        panel.locator('[data-theme-option="dark"]').click()
        expect(page.locator("#toast")).to_contain_text("테마를 저장하지 못했습니다")
        expect(panel.locator('[data-theme-option="light"]')).to_have_attribute("aria-checked", "true")
        assert theme() == "light" and settings.theme() == "light"
        page.unroute("http://127.0.0.1:8787/api/settings")

        # ---- 고르지 않은 상태(기존 사용자): Windows 설정을 따라가고, 바뀌면 같이 바뀐다
        settings.set_many({"ui.theme": ""})
        client.put("/api/starter", json={"visible": False})
        page.emulate_media(color_scheme="light")
        page.goto("http://127.0.0.1:8787/")
        assert theme() == "light"
        page.wait_for_timeout(500)
        expect(chooser).not_to_be_visible()                                   # 기존 사용자에게는 묻지 않는다
        page.emulate_media(color_scheme="dark")
        page.wait_for_function("document.documentElement.dataset.theme === 'dark'")
        assert background() == DARK_BG
        page.locator("#tabs [data-tab='settings']").click()
        expect(page.locator("#themeNote")).to_have_text("아직 고르지 않아 Windows 설정을 따르고 있습니다(지금은 다크).")
        expect(page.locator('#themePanel [aria-checked="true"]')).to_have_count(0)
        page.screenshot(path=str(ARTIFACTS / "theme-settings-dark.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        print("OK without a choice the app follows Windows live, shows that in settings, and fits narrow screens")

        assert not errors, errors
        browser.close()


if __name__ == "__main__":
    try:
        main()
    finally:
        db.close()
