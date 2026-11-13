; Session Scribe offline installer. Built by build.ps1, which fills in the /D values below:
;   ISCC /DStage=... /DOutDir=... /DEdition=... /DEditionTitle=... /DAppBuild=... /DMinRamGB=... SessionScribe.iss
; Every edition shares one AppId, so installing another edition replaces the one already there
; (patient data lives in the data folder chosen in the app and is never touched by the installer).

#ifndef Stage
  #error Run build.ps1 instead of compiling this file directly.
#endif

[Setup]
AppId={{37B1B65A-9D96-46CF-A8BC-27E8B4CEFEF9}
AppName=Session Scribe
AppVersion={#AppBuild}
AppVerName=Session Scribe {#AppBuild} ({#EditionTitle})
AppPublisher=Session Scribe
DefaultDirName={autopf}\Session Scribe
DefaultGroupName=Session Scribe
DisableProgramGroupPage=yes
; Per-user install (no administrator needed): the app updates its own program file between computers,
; so it must be able to write to its folder.
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
OutputDir={#OutDir}
OutputBaseFilename=SessionScribe-{#Edition}-{#AppBuild}-setup
; The models are already compressed: storing them as they are keeps building and installing fast.
Compression=lzma2/fast
SolidCompression=no
; Windows programs can't be bigger than 4 GB, so the installer comes in parts (setup.exe + .bin files).
DiskSpanning=yes
DiskSliceSize=2000000000
UninstallDisplayName=Session Scribe ({#EditionTitle})
UninstallDisplayIcon={app}\python\pythonw.exe
LicenseFile={#Stage}\THIRD-PARTY-NOTICES.txt

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[InstallDelete]
; a clean engine, Python and bundled models each time (a model from another edition doesn't linger).
; Models you added yourself are in %LOCALAPPDATA%\Session Scribe\models and stay.
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\engine"
Type: filesandordirs; Name: "{app}\models"

[Files]
Source: "{#Stage}\*"; DestDir: "{app}"; Excludes: "\models\*"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#Stage}\models\*"; DestDir: "{app}\models"; Flags: recursesubdirs createallsubdirs ignoreversion nocompression

[Icons]
Name: "{autoprograms}\Session Scribe"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\session_scribe.py"""; WorkingDir: "{app}"
Name: "{autodesktop}\Session Scribe"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\session_scribe.py"""; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\session_scribe.py"""; WorkingDir: "{app}"; Description: "Start Session Scribe"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
; only what the program itself creates next to its files — never a data folder kept in here
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\engine"
Type: filesandordirs; Name: "{app}\models"
Type: filesandordirs; Name: "{app}\bin"
Type: filesandordirs; Name: "{app}\__pycache__"

[Code]
type
  TMemoryStatusEx = record
    dwLength: DWORD;
    dwMemoryLoad: DWORD;
    ullTotalPhys: Int64;
    ullAvailPhys: Int64;
    ullTotalPageFile: Int64;
    ullAvailPageFile: Int64;
    ullTotalVirtual: Int64;
    ullAvailVirtual: Int64;
    ullAvailExtendedVirtual: Int64;
  end;

function GlobalMemoryStatusEx(var lpBuffer: TMemoryStatusEx): BOOL;
  external 'GlobalMemoryStatusEx@kernel32.dll stdcall';

{ This edition is sized for a certain amount of memory. On a smaller computer, say so before installing:
  the app would still run, but the AI model would be very slow or fail to load. }
function InitializeSetup(): Boolean;
var
  M: TMemoryStatusEx;
  Gb: Integer;
begin
  Result := True;
  M.dwLength := SizeOf(M);
  if GlobalMemoryStatusEx(M) then
  begin
    Gb := (M.ullTotalPhys + 536870912) div 1073741824;
    if Gb < {#MinRamGB} then
      Result := SuppressibleMsgBox(
        'This edition of Session Scribe ({#EditionTitle}) needs about {#MinRamGB} GB of memory, and this computer has '
        + IntToStr(Gb) + ' GB. Its AI model would be very slow here or not load at all; a smaller edition fits '
        + 'this computer better.' + #13#10#13#10 + 'Install this edition anyway?',
        mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES;
  end;
end;
