; Single-file setup.exe wrapper around one app's .msi + external cab
; files (see desktop/wix/main.wxs for why the payload is split into cabs
; in the first place: the ~4GB CUDA sidecar exceeds the 2GB ceilings of
; both the .msi format and WiX Burn's attached container — Burn was
; tried on both WiX 3.14 and v5 and fails linking the container cab).
;
; This is deliberately NOT an installer of its own: it registers
; nothing (Uninstallable=no), owns no install dir (CreateAppDir=no),
; and simply extracts the MSI set to {tmp} and hands off to Windows
; Installer, so the MSI remains the single source of truth for
; install/upgrade/uninstall and Add/Remove shows exactly one entry.
; {tmp} is cleaned up automatically when the wizard exits.
;
; Compression is 'zip' not lzma2: the cabs inside are already
; mszip-compressed, so lzma2 buys a few percent at the cost of
; many extra build minutes over ~2.5GB.
;
; Everything per-app arrives as ISCC /D defines (see docs/releasing.md):
;   AppName        e.g. "Proxy Scaler"
;   AppVersion     e.g. "0.1.0"
;   MsiDir         assembled dist folder holding the .msi + cabs
;   MsiName        e.g. "Proxy Scaler_0.1.0_x64_en-US.msi"
;   IconPath       path to the app's .ico
;   OutputDir      where to put the finished setup exe
;   OutputBaseName output filename without .exe

[Setup]
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=proxyscaler
WizardStyle=modern
CreateAppDir=no
Uninstallable=no
CreateUninstallRegKey=no
DisableWelcomePage=yes
DisableReadyPage=yes
DisableFinishedPage=yes
; The wrapped MSI is a perMachine package, so the wrapper must elevate
; up front — otherwise msiexec would put up a second UAC prompt midway.
PrivilegesRequired=admin
SetupIconFile={#IconPath}
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBaseName}
Compression=zip
SolidCompression=no

[Files]
Source: "{#MsiDir}\{#MsiName}"; DestDir: "{tmp}"; Flags: deleteafterinstall
Source: "{#MsiDir}\cab*.cab"; DestDir: "{tmp}"; Flags: deleteafterinstall

; msiexec runs from [Code] rather than [Run] so the wizard window can be
; hidden during the handoff — otherwise both the wrapper's window and
; the MSI's wizard sit on screen at once. The wrapper can't literally
; exit at that point: the MSI reads its cabs out of {tmp} for the whole
; install, and {tmp} is only cleaned up when this process ends — exiting
; early would delete the source files mid-install. Hiding is visually
; identical: extraction progress shows, then this window disappears,
; msiexec's wizard runs alone, and when it finishes the hidden wrapper
; deletes {tmp} and exits.
[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    WizardForm.Hide;
    Exec('msiexec.exe', '/i "' + ExpandConstant('{tmp}\{#MsiName}') + '"',
      '', SW_SHOW, ewWaitUntilTerminated, ResultCode);
    { 0 = success, 3010 = success + reboot wanted, 1602 = user cancelled
      — none of those deserve an error dialog from the wrapper. }
    if (ResultCode <> 0) and (ResultCode <> 3010) and (ResultCode <> 1602) then
      MsgBox('The {#AppName} installer reported error code '
        + IntToStr(ResultCode) + '.', mbError, MB_OK);
  end;
end;
