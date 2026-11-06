; Build with python build_installer.py. Keep AppId stable across upgrades.
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef AppSource
  #define AppSource "..\dist\memoinall"
#endif

[Setup]
AppId=memoinall.desktop
AppName=memoinall
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\memoinall
DefaultGroupName=memoinall
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0
OutputDir=..\dist\installer
OutputBaseFilename=memoinall-Setup-{#AppVersion}
UninstallDisplayIcon={app}\memoinall.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
DisableWelcomePage=no
SetupLogging=yes
CloseApplications=no
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
#if FileExists(AddBackslash(CompilerPath) + "Languages\Korean.isl")
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
#endif

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
#ifdef WebViewBootstrapper
Source: "{#WebViewBootstrapper}"; DestName: "MicrosoftEdgeWebview2Setup.exe"; Flags: dontcopy
#endif
Source: "verify_webview.ps1"; Flags: dontcopy
Source: "{#AppSource}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\memoinall"; Filename: "{app}\memoinall.exe"; WorkingDir: "{app}"
Name: "{userdesktop}\memoinall"; Filename: "{app}\memoinall.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\memoinall.exe"; Description: "{cm:LaunchProgram,memoinall}"; Flags: nowait postinstall skipifsilent

[CustomMessages]
english.WebViewInstalling=Installing Microsoft Edge WebView2. An internet connection is required.
english.WebViewFailed=WebView2 could not be installed. Check your internet connection or install the WebView2 Evergreen Runtime from Microsoft, then run this setup again.
english.WebViewUntrusted=The Microsoft signature of the WebView2 installer could not be verified. Install WebView2 from Microsoft's website and run this setup again.
english.DataNotice=Your memos and downloaded models are kept in your user profile (.memoinall). Updating or uninstalling the app keeps this data. Semantic search downloads its model in the background on first use.
#if FileExists(AddBackslash(CompilerPath) + "Languages\Korean.isl")
korean.WebViewInstalling=Microsoft Edge WebView2를 설치합니다. 인터넷 연결이 필요합니다.
korean.WebViewFailed=WebView2를 설치하지 못했습니다. 인터넷 연결을 확인하거나 Microsoft의 WebView2 Evergreen Runtime을 설치한 뒤 다시 실행하세요.
korean.WebViewUntrusted=WebView2 설치기의 Microsoft 서명을 확인하지 못했습니다. Microsoft 웹사이트에서 WebView2를 설치한 뒤 다시 실행하세요.
korean.DataNotice=메모와 내려받은 모델은 사용자 폴더의 .memoinall에 보관됩니다. 앱을 업데이트하거나 제거해도 이 데이터는 유지됩니다. 의미 검색 모델은 처음 사용할 때 백그라운드에서 내려받습니다.
#endif

[Code]
const
  WebViewKey = 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

var
  DownloadPage: TDownloadWizardPage;

function HasWebViewAt(Root: Integer): Boolean;
var
  Version: String;
begin
  Result := RegQueryStringValue(Root, WebViewKey, 'pv', Version) and
    (Version <> '') and (Version <> '0.0.0.0');
end;

function HasWebView: Boolean;
begin
  Result := HasWebViewAt(HKLM32) or HasWebViewAt(HKCU32) or HasWebViewAt(HKCU64);
end;

procedure InitializeWizard;
begin
  DownloadPage := CreateDownloadPage('Microsoft WebView2', CustomMessage('WebViewInstalling'), nil);
  DownloadPage.ShowBaseNameInsteadOfUrl := True;
  WizardForm.WelcomeLabel2.Caption := WizardForm.WelcomeLabel2.Caption + #13#10#13#10 +
    CustomMessage('DataNotice');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  if HasWebView then
    Exit;
  WizardForm.PreparingLabel.Caption := CustomMessage('WebViewInstalling');
#ifdef WebViewBootstrapper
  ExtractTemporaryFile('MicrosoftEdgeWebview2Setup.exe');
#else
  DownloadPage.Clear;
  DownloadPage.Add('https://go.microsoft.com/fwlink/?LinkId=2124703',
    'MicrosoftEdgeWebview2Setup.exe', '');
  DownloadPage.Show;
  try
    try
      DownloadPage.Download;
    except
      Log(GetExceptionMessage);
      Result := CustomMessage('WebViewFailed') + #13#10 +
        'https://developer.microsoft.com/microsoft-edge/webview2/';
      Exit;
    end;
  finally
    DownloadPage.Hide;
  end;
#endif
  ExtractTemporaryFile('verify_webview.ps1');
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
      '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{tmp}\verify_webview.ps1') +
      '" -Path "' + ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe') + '"',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    Result := CustomMessage('WebViewUntrusted');
    Exit;
  end;
  if ResultCode <> 0 then
  begin
    Result := CustomMessage('WebViewUntrusted');
    Exit;
  end;
  if not Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe'), '/silent /install',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    Result := CustomMessage('WebViewFailed');
    Exit;
  end;
  Log(Format('WebView2 bootstrapper exit code: %d', [ResultCode]));
  if not HasWebView then
    Result := CustomMessage('WebViewFailed');
end;

// No UninstallDelete entries: user memos, drafts, settings and models must remain.
