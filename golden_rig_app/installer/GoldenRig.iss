; Golden Rig installer (Inno Setup 6). Build with installer\build.ps1, which runs PyInstaller first.
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppName "Golden Rig"
#define DistDir "build\dist\GoldenRig"
#define DataDir "{commonappdata}\GoldenRig"
#define PS "{sys}\WindowsPowerShell\v1.0\powershell.exe"

[Setup]
AppId={{6C0E3B1A-8F2D-4C51-9B7E-2A4D9E1F7C33}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Unisem
DefaultDirName={autopf}\Golden Rig
DefaultGroupName=Golden Rig
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=Output
OutputBaseFilename=GoldenRigSetup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\GoldenRigServer.exe
CloseApplications=yes
SetupLogging=yes

[Tasks]
Name: "firewall"; Description: "Allow the rig TCP port and mDNS (UDP 5353) through Windows Firewall"
Name: "desktopicon"; Description: "Create a desktop shortcut"
Name: "autostart"; Description: "Start the Golden Rig server automatically when a user logs on"; Flags: unchecked

[Dirs]
Name: "{#DataDir}"
Name: "{#DataDir}\logs"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "Configure-GoldenRig.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "rig_config.example.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\slots.example.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "docs\INSTALL.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\golden_rig_application.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}\docs"; Flags: ignoreversion

[Icons]
Name: "{group}\Golden Rig Server"; Filename: "{app}\GoldenRigServer.exe"; WorkingDir: "{#DataDir}"; Comment: "Start the Golden Rig TLS server"
Name: "{group}\Check Golden Rig configuration"; Filename: "{app}\GoldenRigServer.exe"; Parameters: "--check --pause"; WorkingDir: "{#DataDir}"; Comment: "Verify certificate, mDNS name and slots without starting"
Name: "{group}\Configure Golden Rig"; Filename: "{#PS}"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\Configure-GoldenRig.ps1"" -PauseAtEnd"; Comment: "Change rig ID, mDNS / computer name, certificates, firewall"
Name: "{group}\List COM ports"; Filename: "{app}\GoldenRigServer.exe"; Parameters: "--list-ports --pause"; Comment: "Show COM ports and USB serials for slots.json"
Name: "{group}\Certificate tool"; Filename: "{cmd}"; Parameters: "/k ""set ""PATH={app};%PATH%"" & title Golden Rig certificate tool & rig_certtool --help"""; WorkingDir: "{#DataDir}"
Name: "{group}\Golden Rig data folder"; Filename: "{#DataDir}"; Comment: "rig_config.json, slots.json, certs, logs"
Name: "{group}\Installation guide"; Filename: "{win}\notepad.exe"; Parameters: """{app}\docs\INSTALL.md"""
Name: "{group}\Uninstall Golden Rig"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Golden Rig Server"; Filename: "{app}\GoldenRigServer.exe"; WorkingDir: "{#DataDir}"; Tasks: desktopicon
Name: "{commonstartup}\Golden Rig Server"; Filename: "{app}\GoldenRigServer.exe"; WorkingDir: "{#DataDir}"; Tasks: autostart

[Run]
Filename: "{#PS}"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\Configure-GoldenRig.ps1"" {code:ConfigureParams}"; StatusMsg: "Configuring Golden Rig (identity, certificates, firewall, computer name)..."; Flags: waituntilterminated
Filename: "{win}\notepad.exe"; Parameters: """{app}\docs\INSTALL.md"""; Description: "Open the installation guide"; Flags: postinstall nowait skipifsilent
Filename: "{app}\GoldenRigServer.exe"; WorkingDir: "{#DataDir}"; Description: "Start the Golden Rig server now"; Flags: postinstall nowait skipifsilent unchecked

[UninstallRun]
Filename: "{#PS}"; Parameters: "-NoProfile -Command ""Get-NetFirewallRule -Group 'Golden Rig' -ErrorAction SilentlyContinue | Remove-NetFirewallRule"""; Flags: runhidden; RunOnceId: "RemoveGoldenRigFirewall"

[UninstallDelete]
; Config, slots, certificates and logs in {#DataDir} are intentionally kept.

[Code]
var
  IdPage: TInputQueryWizardPage;
  NamePage: TInputOptionWizardPage;
  CertModePage: TInputOptionWizardPage;
  CertFilesPage: TInputFileWizardPage;
  NameDefaultFor: String;

function IsAlnumLower(C: Char): Boolean;
begin
  Result := ((C >= 'a') and (C <= 'z')) or ((C >= '0') and (C <= '9'));
end;

{ Same normalisation as golden_rig_server.mdns_hostname / Configure-GoldenRig.ps1 }
function NormalizeLabel(S: String): String;
var
  I: Integer;
  C: Char;
  Dash: Boolean;
begin
  S := Lowercase(Trim(S));
  if (Length(S) > 6) and (Copy(S, Length(S) - 5, 6) = '.local') then
    S := Copy(S, 1, Length(S) - 6);
  Result := '';
  Dash := False;
  for I := 1 to Length(S) do
  begin
    C := S[I];
    if IsAlnumLower(C) then
    begin
      if Dash and (Result <> '') then Result := Result + '-';
      Result := Result + C;
      Dash := False;
    end
    else
      Dash := True;
  end;
end;

function IsValidLabel(S: String): Boolean;
begin
  Result := (Length(S) >= 1) and (Length(S) <= 63) and (S = NormalizeLabel(S));
end;

function IsValidComputerName(S: String): Boolean;
var
  I: Integer;
  AllDigits: Boolean;
begin
  S := Lowercase(S);
  Result := IsValidLabel(S) and (Length(S) <= 15);
  AllDigits := True;
  for I := 1 to Length(S) do
    if not ((S[I] >= '0') and (S[I] <= '9')) then AllDigits := False;
  if AllDigits then Result := False;
end;

function MdnsLabel(): String;
begin
  Result := NormalizeLabel(IdPage.Values[1]);
end;

function TargetComputerName(): String;
begin
  Result := Uppercase(MdnsLabel());
end;

procedure InitializeWizard();
begin
  IdPage := CreateInputQueryPage(wpSelectTasks,
    'Rig identity', 'Rig ID, mDNS hostname and TCP port',
    'The Rig ID must equal the Common Name (CN) of the rig certificate. Stations connect to ' +
    '<mDNS hostname>.local, so that name must be in the certificate Subject Alternative Name (SAN). ' +
    'Changing either later needs a new certificate and Supervisor re-approval on every station.');
  IdPage.Add('Rig ID (certificate CN), e.g. GOLDEN-RIG-01:', False);
  IdPage.Add('mDNS hostname (without .local), e.g. golden-rig-01:', False);
  IdPage.Add('TCP port:', False);
  IdPage.Values[0] := GetPreviousData('RigId', Uppercase(GetComputerNameString()));
  IdPage.Values[1] := GetPreviousData('MdnsName', NormalizeLabel(IdPage.Values[0]));
  IdPage.Values[2] := GetPreviousData('Port', '8762');

  NamePage := CreateInputOptionPage(IdPage.ID,
    'Computer name', 'Make the Windows computer name match the mDNS hostname', '', True, False);
  NamePage.Add('Rename this computer to match the mDNS hostname (restart required)');
  NamePage.Add('Keep the current computer name');

  CertModePage := CreateInputOptionPage(NamePage.ID,
    'Rig certificate', 'How should the TLS certificate be provided?',
    'Stations trust the rig only with a certificate signed by the Golden Rig CA. Its SHA-256 ' +
    'fingerprint is approved by a Supervisor on each station. See the installation guide for ' +
    'creating the CA, key and certificate.', True, False);
  CertModePage.Add('Import rig.pem, rig.key and golden_rig_ca.pem from files');
  CertModePage.Add('Generate a new private key and certificate request (CSR) on this PC');
  CertModePage.Add('Keep the installed certificates / configure later');
  CertModePage.SelectedValueIndex := StrToIntDef(GetPreviousData('CertMode', '0'), 0);

  CertFilesPage := CreateInputFilePage(CertModePage.ID,
    'Certificate files', 'Select the rig certificate files',
    'Leave a box empty to keep the file that is already installed.');
  CertFilesPage.Add('Rig certificate (rig.pem):', 'Certificates (*.pem;*.crt)|*.pem;*.crt|All files|*.*', '.pem');
  CertFilesPage.Add('Rig private key (rig.key):', 'Private keys (*.key;*.pem)|*.key;*.pem|All files|*.*', '.key');
  CertFilesPage.Add('Golden Rig CA certificate (golden_rig_ca.pem):', 'Certificates (*.pem;*.crt)|*.pem;*.crt|All files|*.*', '.pem');
end;

procedure CurPageChanged(CurPageID: Integer);
var
  Current: String;
begin
  if CurPageID = NamePage.ID then
  begin
    Current := GetComputerNameString();
    NamePage.SubCaptionLabel.Caption :=
      'Current computer name: ' + Current + #13#10 +
      'mDNS hostname: ' + MdnsLabel() + '.local' + #13#10#13#10 +
      'The Golden Rig server advertises ' + MdnsLabel() + '.local itself. Renaming the PC to ' +
      TargetComputerName() + ' also lets Windows answer for that name, which keeps the rig reachable ' +
      'by name and avoids a conflicting name on the network.';
    if NameDefaultFor <> MdnsLabel() then
    begin
      NameDefaultFor := MdnsLabel();
      if CompareText(Current, TargetComputerName()) = 0 then
        NamePage.SelectedValueIndex := 1
      else
        NamePage.SelectedValueIndex := 0;
    end;
  end;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (PageID = CertFilesPage.ID) and (CertModePage.SelectedValueIndex <> 0);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  I: Integer;
begin
  Result := True;
  if CurPageID = IdPage.ID then
  begin
    IdPage.Values[0] := Trim(IdPage.Values[0]);
    IdPage.Values[1] := MdnsLabel();
    if (IdPage.Values[0] = '') or (Pos('"', IdPage.Values[0]) > 0) then
    begin
      MsgBox('Enter a Rig ID (the certificate CN), without quotes.', mbError, MB_OK);
      Result := False;
    end
    else if not IsValidLabel(IdPage.Values[1]) then
    begin
      MsgBox('Enter an mDNS hostname: letters, digits and hyphens, at most 63 characters.', mbError, MB_OK);
      Result := False;
    end
    else if (StrToIntDef(IdPage.Values[2], 0) < 1) or (StrToIntDef(IdPage.Values[2], 0) > 65535) then
    begin
      MsgBox('Enter a TCP port between 1 and 65535.', mbError, MB_OK);
      Result := False;
    end;
  end
  else if (CurPageID = NamePage.ID) and (NamePage.SelectedValueIndex = 0) then
  begin
    if not IsValidComputerName(MdnsLabel()) then
    begin
      MsgBox('"' + TargetComputerName() + '" cannot be a Windows computer name (max 15 characters, ' +
        'not all digits). Go back and choose a shorter mDNS hostname, or keep the current computer name.',
        mbError, MB_OK);
      Result := False;
    end;
  end
  else if CurPageID = CertFilesPage.ID then
  begin
    for I := 0 to 2 do
      if (CertFilesPage.Values[I] <> '') and not FileExists(CertFilesPage.Values[I]) then
      begin
        MsgBox('File not found: ' + CertFilesPage.Values[I], mbError, MB_OK);
        Result := False;
        Exit;
      end;
  end;
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  SetPreviousData(PreviousDataKey, 'RigId', IdPage.Values[0]);
  SetPreviousData(PreviousDataKey, 'MdnsName', IdPage.Values[1]);
  SetPreviousData(PreviousDataKey, 'Port', IdPage.Values[2]);
  if CertModePage.SelectedValueIndex = 1 then
    SetPreviousData(PreviousDataKey, 'CertMode', '2')  { do not regenerate a key on upgrade }
  else
    SetPreviousData(PreviousDataKey, 'CertMode', IntToStr(CertModePage.SelectedValueIndex));
end;

function RenameRequested(): Boolean;
begin
  Result := (NamePage.SelectedValueIndex = 0) and (CompareText(GetComputerNameString(), TargetComputerName()) <> 0);
end;

function ConfigureParams(Param: String): String;
begin
  Result := '-NonInteractive' +
    ' -InstallDir ' + AddQuotes(ExpandConstant('{app}')) +
    ' -RigId ' + AddQuotes(IdPage.Values[0]) +
    ' -MdnsName ' + MdnsLabel() +
    ' -Port ' + IdPage.Values[2] +
    ' -RigUser ' + AddQuotes(GetUserNameString());
  if not WizardSilent() then
    Result := Result + ' -PauseAtEnd';
  if RenameRequested() then
    Result := Result + ' -RenameComputer -NewComputerName ' + TargetComputerName();
  if WizardIsTaskSelected('firewall') then
    Result := Result + ' -ConfigureFirewall';
  if CertModePage.SelectedValueIndex = 0 then
  begin
    if CertFilesPage.Values[0] <> '' then Result := Result + ' -CertFile ' + AddQuotes(CertFilesPage.Values[0]);
    if CertFilesPage.Values[1] <> '' then Result := Result + ' -KeyFile ' + AddQuotes(CertFilesPage.Values[1]);
    if CertFilesPage.Values[2] <> '' then Result := Result + ' -CaCertFile ' + AddQuotes(CertFilesPage.Values[2]);
  end
  else if CertModePage.SelectedValueIndex = 1 then
    Result := Result + ' -GenerateCsr';
  Log('Configure-GoldenRig.ps1 ' + Result);
end;

function NeedRestart(): Boolean;
begin
  Result := RenameRequested();
end;
