; Inno Setup 腳本：把 PyInstaller 輸出的資料夾包成一個雙擊安裝的 .exe
#define MyAppName "語音輸入"
#define MyAppExe "VoiceInput.exe"
#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif

[Setup]
AppId={{B6A1F3C2-5D1E-4B7A-9C2D-7E1F0A3B9D11}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Voice Input
DefaultDirName={localappdata}\Programs\VoiceInput
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=installer_out
OutputBaseFilename=VoiceInput-Setup
Compression=lzma2/ultra
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExe}

[Tasks]
Name: "desktopicon"; Description: "在桌面建立捷徑"; GroupDescription: "額外選項："
Name: "autostart"; Description: "開機時自動啟動語音輸入"; GroupDescription: "額外選項："

[Files]
Source: "dist\VoiceInput\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "VoiceInput"; ValueData: """{app}\{#MyAppExe}"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "立即啟動語音輸入"; Flags: nowait postinstall skipifsilent
