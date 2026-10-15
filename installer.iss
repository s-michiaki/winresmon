; WinResMon インストーラー定義（Inno Setup 6）
; GitHub Actions から: ISCC.exe /DMyAppVersion=1.2.3 installer.iss
; 事前に PyInstaller で dist\WinResMon\ が作られている必要があります

#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#define MyAppName "WinResMon"
#define MyAppExeName "WinResMon.exe"

[Setup]
AppId={{6C3B7E2A-9D41-4F2B-8A57-1E0D5C9B7A31}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=shaekman
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; 常に「自分だけ」にインストール（管理者権限・UAC 確認は不要）
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=Output
OutputBaseFilename=WinResMon-Setup-{#MyAppVersion}
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "dist\WinResMon\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{#MyAppName}（ブラウザで開く）"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--browser"
Name: "{group}\{#MyAppName}（管理者として起動）"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--elevate"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

; スナップショット（%LOCALAPPDATA%\WinResMon）はアンインストール後も残します。
; 不要なら手動でフォルダを削除してください。
