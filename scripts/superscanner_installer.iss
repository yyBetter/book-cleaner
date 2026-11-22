; 超级扫描仪 · SuperScanner Windows 安装向导脚本 (Inno Setup 6)
#ifndef MyAppVersion
  #define MyAppVersion "0.2.0"
#endif

#define MyAppName "超级扫描仪 · SuperScanner"
#define MyAppExeName "超级扫描仪.exe"

[Setup]
AppId={{8D6B4F3B-45B2-4F8F-9A4F-77A3C0D9E801}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} v{#MyAppVersion}
AppPublisher=SuperScanner Studio
DefaultDirName={localappdata}\Programs\SuperScanner
DefaultGroupName=超级扫描仪 SuperScanner
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist\release
OutputBaseFilename=超级扫描仪_v{#MyAppVersion}_Windows_安装包
SetupIconFile=..\build\superscanner.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/normal
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
; 若本机 Inno Setup 已安装 ChineseSimplified.isl，可取消下行注释启用全中文向导界面
; Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式 (Create a &desktop icon)"; GroupDescription: "附加任务:"

[Files]
; 打包 PyInstaller onedir 输出目录（安装版不放 portable.flag，数据统一存于 %LOCALAPPDATA%\SuperScanner\data）
Source: "..\dist\超级扫描仪\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\超级扫描仪 SuperScanner"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 超级扫描仪 · SuperScanner"; Flags: nowait postinstall skipifsilent
