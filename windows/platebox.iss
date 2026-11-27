; PlateBox for Windows: the installer.  Build with windows\build.ps1 (it passes the paths and version).
#ifndef DistDir
  #define DistDir "..\..\..\..\ba-build\pbwin-out\dist\PlateBox"
#endif
#ifndef OutDir
  #define OutDir "..\..\..\..\ba-build\pbwin-out\installer"
#endif
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{7E4B6D0A-52C1-4F0B-9B7E-3D1A8C5F2E61}
AppName=PlateBox
AppVersion={#AppVersion}
AppPublisher=Icetronix
AppPublisherURL=https://icetronix.in/
AppSupportURL=https://icetronix.in/
VersionInfoCompany=Icetronix
VersionInfoDescription=PlateBox by Icetronix
DefaultDirName={autopf}\PlateBox
DefaultGroupName=PlateBox
DisableProgramGroupPage=yes
OutputDir={#OutDir}
OutputBaseFilename=PlateBox-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\platebox.exe
CloseApplications=no
RestartApplications=no

[Tasks]
Name: "autostart"; Description: "Start PlateBox automatically when the computer starts (recommended)"; GroupDescription: "How it runs:"
Name: "firewall"; Description: "Let the camera and your phone or laptop reach PlateBox on this network (Windows Firewall)"; GroupDescription: "How it runs:"
Name: "desktopicon"; Description: "Put a PlateBox shortcut on the desktop"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Dirs]
; Everything PlateBox keeps (plates, pictures, passwords) lives here. Locked down below.
Name: "{commonappdata}\PlateBox"
Name: "{commonappdata}\PlateBox\public"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "install-task.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "README-WINDOWS.txt"; DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
Name: "{group}\PlateBox"; Filename: "{app}\platebox.exe"; Parameters: "--open"; Comment: "Open PlateBox in your browser"
Name: "{group}\About PlateBox (read me)"; Filename: "{app}\README-WINDOWS.txt"
Name: "{group}\Uninstall PlateBox"; Filename: "{uninstallexe}"
Name: "{autodesktop}\PlateBox"; Filename: "{app}\platebox.exe"; Parameters: "--open"; Tasks: desktopicon

[Run]
; The data folder holds secrets: only the system and administrators may use it. Ordinary users may only read the
; small "public" folder, which holds nothing but the web port the shortcut needs.
Filename: "{sys}\icacls.exe"; Parameters: """{commonappdata}\PlateBox"" /inheritance:r /grant:r *S-1-5-18:(OI)(CI)F *S-1-5-32-544:(OI)(CI)F"; Flags: runhidden; StatusMsg: "Protecting PlateBox's data folder..."
Filename: "{sys}\icacls.exe"; Parameters: """{commonappdata}\PlateBox\public"" /grant:r *S-1-5-32-545:(OI)(CI)R"; Flags: runhidden
; Windows Firewall: this program, from computers on the same network only.
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""PlateBox"""; Flags: runhidden; Tasks: firewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""PlateBox"" dir=in action=allow program=""{app}\platebox.exe"" enable=yes profile=any remoteip=LocalSubnet"; Flags: runhidden; Tasks: firewall; StatusMsg: "Allowing PlateBox through the firewall (local network only)..."
; Start with the computer, and start now.
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\install-task.ps1"" -App ""{app}"""; Flags: runhidden; Tasks: autostart; StatusMsg: "Setting PlateBox to start with the computer..."
; Without automatic start, start it for this session.
Filename: "{app}\platebox.exe"; Flags: nowait; Check: not WantsAutostart
; Open the page.
Filename: "{app}\platebox.exe"; Parameters: "--open"; Description: "Open PlateBox now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\schtasks.exe"; Parameters: "/End /TN ""PlateBox"""; Flags: runhidden; RunOnceId: "EndTask"
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /TN ""PlateBox"" /F"; Flags: runhidden; RunOnceId: "DeleteTask"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM platebox.exe"; Flags: runhidden; RunOnceId: "KillApp"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""PlateBox"""; Flags: runhidden; RunOnceId: "DeleteFirewall"

[Code]
var
  PwPage: TInputQueryWizardPage;

function DataExists: Boolean;
begin
  Result := FileExists(ExpandConstant('{commonappdata}\PlateBox\data\platebox.db'));
end;

function WantsAutostart: Boolean;
begin
  Result := WizardIsTaskSelected('autostart');
end;

procedure InitializeWizard;
begin
  PwPage := CreateInputQueryPage(wpSelectTasks, 'Camera password',
    'The password the camera uses to send its pictures to PlateBox',
    'Choose one that is easy to type into the camera (8 to 32 letters and digits, no spaces or symbols), or leave it empty and PlateBox makes one up. You can change it later in PlateBox, under Settings.');
  PwPage.Add('Camera password (optional):', False);
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  { Already set up (an upgrade): the camera already has its password, so do not ask again. }
  if (PwPage <> nil) and (PageID = PwPage.ID) then
    Result := DataExists;
end;

function ValidPassword(const S: String): Boolean;
var
  I: Integer;
  C: Char;
begin
  Result := (Length(S) >= 8) and (Length(S) <= 32);
  for I := 1 to Length(S) do
  begin
    C := S[I];
    if not (((C >= 'A') and (C <= 'Z')) or ((C >= 'a') and (C <= 'z')) or ((C >= '0') and (C <= '9'))) then
      Result := False;
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (PwPage <> nil) and (CurPageID = PwPage.ID) and (PwPage.Values[0] <> '') and not ValidPassword(PwPage.Values[0]) then
  begin
    MsgBox('Use 8 to 32 letters and digits only (no spaces or symbols), or leave it empty.', mbError, MB_OK);
    Result := False;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  { Handed to PlateBox in a file it reads once and deletes. It is written before anything starts. }
  if (CurStep = ssInstall) and (PwPage.Values[0] <> '') and not DataExists then
  begin
    ForceDirectories(ExpandConstant('{commonappdata}\PlateBox\data'));
    SaveStringToFile(ExpandConstant('{commonappdata}\PlateBox\data\initial-camera-password.txt'), PwPage.Values[0], False);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    { A silent uninstall keeps the data (the default answer is No). }
    if SuppressibleMsgBox('Also delete everything PlateBox saved: the plates, the pictures, the motion log and the settings?'#13#10#13#10'Choose No to keep it, for example if you will install PlateBox again.',
                          mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
      DelTree(ExpandConstant('{commonappdata}\PlateBox'), True, True, True);
  end;
end;
