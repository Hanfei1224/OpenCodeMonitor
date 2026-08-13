; OpencodeMonitor — Inno Setup 安装脚本
#define MyAppName "OpencodeMonitor"
#define MyAppExeName "OpencodeMonitor.exe"
#define MyAppVersion "1.1.1"

[Setup]
AppId={{8E9F3B2C-5A11-4D27-9C6B-4A1E2F3C4D5E}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=OpenCode Monitor
DefaultDirName={localappdata}\Programs\OpencodeMonitor
DefaultGroupName={#MyAppName}
DisableDirPage=yes
UsePreviousAppDir=yes
DisableProgramGroupPage=yes
OutputBaseFilename=OpencodeMonitor-Setup-{#MyAppVersion}
OutputDir=installer
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Files]
Source: "dist\OpencodeMonitor\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标:"; Flags: checkedonce

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[UninstallDelete]
; 卸载时删除本程序自己的内容，保留 config.json（含 API Key），重新安装时 key 无需重输
Type: files; Name: "{app}\app.log"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
// 安装后创建默认 config.json（用户在其中填写 API Key）
procedure CurStepChanged(CurStep: TSetupStep);
var
  ConfigPath: String;
  DefaultConfig: String;
begin
  if CurStep = ssPostInstall then
  begin
    ConfigPath := ExpandConstant('{app}\config.json');
    if not FileExists(ConfigPath) then
    begin
      DefaultConfig := '{' + #13#10 +
        '  "api_key": "",' + #13#10 +
        '  "plan_name": "OpenCode Go",' + #13#10 +
        '  "refresh_seconds": 60,' + #13#10 +
        '  "bg_color": "#1e2330",' + #13#10 +
        '  "bg_opacity": 1.0,' + #13#10 +
        '  "accent_color": "#5b9bff",' + #13#10 +
        '  "window_x": null,' + #13#10 +
        '  "window_y": null,' + #13#10 +
        '  "topmost": true,' + #13#10 +
        '  "click_through": true' + #13#10 +
        '}';
      SaveStringToFile(ConfigPath, DefaultConfig, False);
    end;
  end;
end;
