# Windows 설치형 배포

## 전달할 파일과 사용자 동작

`dist/installer/memoinall-Setup-0.1.0.exe` 하나를 전달합니다. 이번 산출물은 약 46.2MiB입니다.
Windows 10/11 x64용이며 ARM64 네이티브·32비트 Windows는 이 설치 파일의 대상이 아닙니다.

1. 설치 파일을 실행하고 설치 위치를 확인합니다. 기본은 `%LOCALAPPDATA%\Programs\memoinall`입니다.
2. 바탕화면 바로가기는 선택할 수 있습니다. 시작 메뉴에는 `memoinall`이 등록됩니다.
3. 이후에는 시작 메뉴에서 실행합니다. 구성 파일은 설치할 때 한 번만 풀고, 앱 실행 때 다시 풀지 않습니다.

Python 설치는 필요 없습니다. 앱은 현재 사용자용으로 설치하고 관리자 권한을 요청하지 않습니다.
새 버전은 앱을 닫은 뒤 새 Setup.exe를 실행해 설치하면 됩니다. 자동 업데이트 확인·다운로드 기능은 포함하지 않습니다.

## 메모와 모델

앱 파일과 별도로 `%USERPROFILE%\.memoinall`에 메모 DB, 설정, 모델을 저장합니다.
`MEMOINALL_HOME` 환경변수를 지정한 경우에는 그 위치를 사용합니다.
설치·업데이트·제거 스크립트는 이 데이터 디렉터리를 삭제하지 않습니다.
기존 휴대용 버전으로 작성한 메모도 같은 Windows 사용자로 실행하면 그대로 읽습니다.

임베딩 모델은 설치 파일에 포함하지 않습니다. 첫 실행에 필요한 모델을 백그라운드로 내려받고,
그동안에도 메모 작성·저장·키워드 검색을 사용할 수 있습니다. 인터넷이 차단된 환경에서는 의미 검색이 제한될 수 있습니다.

## WebView2

설치 프로그램은 Microsoft가 문서화한 사용자별·PC 전체 설치 레지스트리에서 WebView2 Runtime을 확인합니다.
이미 있으면 다운로드를 건너뜁니다. 없으면 Microsoft 공식 링크에서 Evergreen 설치 도우미를 내려받고,
Microsoft 코드서명이 유효한지 확인한 뒤 실행합니다. 다운로드 또는 검증 실패 시 앱 설치를 중단하고 안내합니다.
이는 [Microsoft WebView2 배포 안내](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution)를 따릅니다.

회사 네트워크에서 Microsoft 다운로드가 차단되거나 PowerShell 사용이 제한된 경우에는 담당자가
WebView2 Runtime을 먼저 설치한 뒤 앱 설치를 진행할 수 있습니다.
이번 개발 환경에는 WebView2가 이미 있고 Microsoft 배포 CDN 연결이 불가능해,
WebView2가 없는 깨끗한 PC의 다운로드·설치 성공 경로는 아직 직접 검증하지 못했습니다.

## 다시 빌드하기

빌드 도구는 [Inno Setup](https://jrsoftware.org/isdl.php)을 사용합니다.
이번 작업에서는 6.7.4를 `build/tools/inno`에 휴대용으로 준비했습니다.
이 디렉터리는 Git에 포함되지 않으므로 새 개발 PC에서는 별도로 준비해야 합니다.
설치된 `ISCC.exe`, `ISCC` 환경변수, `--iscc` 인자도 지원합니다.

```powershell
python build_installer.py --check
python build_installer.py
python build_installer.py --version 0.1.1
python build_installer.py --iscc 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'
```

기본 명령은 `build_exe.py --onedir`를 먼저 실행하고 그 결과를 패키징합니다.
빌드가 완료된 뒤 별도로 검증한 앱을 그대로 묶으려면 `--skip-app-build`를 사용합니다.
설치 파일에서 표시하는 버전의 기본값은 `memoinall.__version__`입니다.
업데이트가 기존 설치를 찾을 수 있도록 `installer/memoinall.iss`의 `AppId`는 유지하세요.

이미 내려받은 Microsoft 설치 도우미를 함께 넣으려면 다음처럼 지정합니다.
이 경우에도 실제 WebView2 Runtime은 해당 도우미가 내려받으므로 완전한 오프라인 설치는 아닙니다.

```powershell
python build_installer.py --webview-bootstrapper 'C:\Downloads\MicrosoftEdgeWebview2Setup.exe'
```

서명이 유효하지 않은 설치 도우미는 빌드에서 거부합니다.
한국어 설치 화면은 사용 중인 Inno Setup의 `Languages/Korean.isl`이 있으면 포함합니다.
이번 배포판에는 한국어와 영어를 모두 포함했습니다.

## 검증과 배포 범위

- 빌드 테스트: 도구 누락, 서명 검증 실패, 잘못된 CPU 아키텍처, 앱 빌드 실패, 기존 배포판 패키징 경로 검증.
- 설치 파일 컴파일 완료. WebView2가 이미 설치된 PC에서 별도 프로젝트 경로에 실제 설치하여 시작 메뉴 바로가기와 앱 파일을 확인했습니다.
- 설치한 화면 파일을 구버전 테스트 내용으로 바꾼 뒤 같은 설치 파일을 다시 실행해 정상 파일로 교체되는 것을 확인했습니다. 제거 후 앱 파일·바로가기·설치 등록이 없어지고, 앱 폴더와 분리한 테스트 데이터가 유지되는 것도 확인했습니다.
- 설치 수명 주기 검증 스크립트와 로그는 `artifacts/ux-review/test-installer-lifecycle.ps1`, `artifacts/ux-review/installer-test-*`에 있습니다. 기존 설치 또는 같은 시작 메뉴 바로가기가 있으면 검증을 시작하지 않습니다.
- WebView2 최초 설치, 깨끗한 Windows PC에서의 전체 첫 실행은 별도 확인이 필요합니다.
- 생성된 앱과 설치 파일에는 배포자 코드서명이 아직 없습니다. 외부 배포용 서명 인증서·서명 작업은 별도입니다.

휴대용 단일 실행 파일 빌드도 계속 지원합니다. `python build_exe.py`를 사용하며,
단일 파일이 실행 때마다 지원 파일을 임시 폴더에 푸는 방식은
[PyInstaller 실행 구조](https://pyinstaller.org/en/stable/operating-mode.html)에 설명되어 있습니다.
