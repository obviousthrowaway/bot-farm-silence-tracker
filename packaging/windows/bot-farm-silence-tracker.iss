; Inno Setup 6 script for the Bot Farm Silence Tracker Windows installer (Setup.exe).
;
; packaging/windows/build_setup.ps1 compiles it with ISCC.exe and these definitions:
;   AppVersion  the release version without the "v", for example 0.1.0
;   CliExe      the release's command-line program (bot-farm-silence-tracker-windows-x64.exe)
;   GuiExe      the release's desktop app (bot-farm-silence-tracker-gui-windows-x64.exe)
;   OutputDir   where to write bot-farm-silence-tracker-<version>-windows-x64-setup.exe
;   RepoUrl     the project's GitHub address, shown in Settings > Apps. While it still
;               holds the owner placeholder (the copy has not been published, see
;               "Publishing your copy" in the README), Setup names no address at all.
;
; It installs for the current user only (no administrator rights) into
; %LOCALAPPDATA%\Programs\bot-farm-silence-tracker, the same folder install.ps1 uses, and
; adds that folder to the user PATH. The platform choice is saved by running the
; installed program's "config set-platforms" command; Setup never writes the config file.
; The installer is not signed.

#ifndef AppVersion
  #error Define AppVersion, for example /DAppVersion=0.1.0
#endif
#ifndef CliExe
  #error Define CliExe, the path to the command-line program
#endif
#ifndef GuiExe
  #error Define GuiExe, the path to the desktop app
#endif
#ifndef OutputDir
  #define OutputDir "."
#endif
#ifndef RepoUrl
  #define RepoUrl "https://github.com/obviousthrowaway/bot-farm-silence-tracker"
#endif
; The owner placeholder, written in two parts so that the README's replace command
; rewrites RepoUrl above but leaves this check intact.
#define OwnerPlaceholder "_" + "_OWNER__"

#define AppName "Bot Farm Silence Tracker"
#define ToolName "bot-farm-silence-tracker"
#define DefaultPlatform "reddit-archive"

[Setup]
; The AppId identifies the program across versions, so a new Setup upgrades in place.
; Never change it.
AppId={{F458FF57-3E74-4F95-8B79-C29462163823}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
#if Pos(OwnerPlaceholder, RepoUrl) == 0
AppPublisherURL={#RepoUrl}
AppSupportURL={#RepoUrl}
AppUpdatesURL={#RepoUrl}/releases
#endif
VersionInfoVersion={#AppVersion}
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\{#ToolName}
DisableDirPage=yes
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename={#ToolName}-{#AppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ChangesEnvironment=yes
CloseApplications=yes
SetupLogging=yes
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#ToolName}-gui.exe

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked
Name: "cliicon"; Description: "Add a ""{#AppName} (command line)"" shortcut to the Start menu"

[Files]
Source: "{#CliExe}"; DestDir: "{app}"; DestName: "{#ToolName}.exe"; Flags: ignoreversion
Source: "{#GuiExe}"; DestDir: "{app}"; DestName: "{#ToolName}-gui.exe"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#ToolName}-gui.exe"; WorkingDir: "{userdocs}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#ToolName}-gui.exe"; WorkingDir: "{userdocs}"; Tasks: desktopicon
; A Command Prompt with the program's folder on PATH, even before Windows passes the new
; user PATH to running programs.
Name: "{autoprograms}\{#AppName} (command line)"; Filename: "{cmd}"; \
  Parameters: "/k ""set ""PATH={app};%PATH%"" && {#ToolName} --version && echo Try: {#ToolName} --help"""; \
  WorkingDir: "{userdocs}"; Tasks: cliicon

[Run]
Filename: "{app}\{#ToolName}-gui.exe"; Description: "Open {#AppName}"; WorkingDir: "{userdocs}"; \
  Flags: nowait postinstall skipifsilent

[Code]
var
  PlatformPage: TInputOptionWizardPage;
  { Set when the saved platforms were kept; shown on the finish page and written to the log. }
  KeptNote: String;

{ True when List is a comma list of known platform names: only a-z, "," and "-", no empty
  names. The /PLATFORMS= value is checked with this before it is used for anything. }
function ValidPlatformList(const List: String): Boolean;
var
  Rest, Name: String;
  P, I: Integer;
  C: Char;
begin
  Result := (List <> '') and (List[Length(List)] <> ',');
  if not Result then
    Exit;
  for I := 1 to Length(List) do
  begin
    C := List[I];
    if not (((C >= 'a') and (C <= 'z')) or (C = ',') or (C = '-')) then
    begin
      Result := False;
      Exit;
    end;
  end;
  Rest := List;
  while Rest <> '' do
  begin
    P := Pos(',', Rest);
    if P = 0 then
    begin
      Name := Rest;
      Rest := '';
    end
    else
    begin
      Name := Copy(Rest, 1, P - 1);
      Delete(Rest, 1, P);
    end;
    if (Name <> '{#DefaultPlatform}') and (Name <> 'bluesky') and (Name <> 'x') and (Name <> 'reddit') then
    begin
      Result := False;
      Exit;
    end;
  end;
end;

{ An invalid /PLATFORMS= value stops Setup before anything is installed: with a message
  box in a normal run, and with a log line and a nonzero exit code in a silent run. }
function InitializeSetup: Boolean;
var
  Given: String;
begin
  Result := True;
  Given := Trim(ExpandConstant('{param:PLATFORMS|}'));
  if (Given <> '') and not ValidPlatformList(Given) then
  begin
    Log('Invalid /PLATFORMS= value: ' + Given);
    if not WizardSilent then
      MsgBox('The /PLATFORMS= value is not valid: ' + Given + #13#10 +
        'Use a comma list of reddit-archive, bluesky, x and reddit, for example ' +
        '/PLATFORMS=reddit-archive,bluesky.', mbError, MB_OK);
    Result := False;
  end;
end;

procedure InitializeWizard;
begin
  PlatformPage := CreateInputOptionPage(wpSelectTasks,
    'Platforms to scan', 'Which social platforms do you want to scan?',
    'Tick one or more. You can change this later in the app, or with the command' + #13#10 +
    '{#ToolName} config set-platforms. The program only contacts the services of the' + #13#10 +
    'platforms you scan, and this installer never asks for passwords or keys.',
    False, False);
  PlatformPage.Add('Reddit, through the Arctic Shift archive (no account needed) - recommended');
  PlatformPage.Add('Bluesky (no account needed for the default mode)');
  PlatformPage.Add('X (needs your own paid API token)');
  PlatformPage.Add('Reddit official API (needs approved Reddit API credentials)');
  PlatformPage.Values[0] := True;
end;

{ The ticked platforms as a comma list, or the /PLATFORMS= command-line value (for a
  silent install). }
function ChosenPlatforms: String;
var
  Names: array[0..3] of String;
  I: Integer;
begin
  Result := Trim(ExpandConstant('{param:PLATFORMS|}'));
  if Result <> '' then
    Exit;
  Names[0] := 'reddit-archive';
  Names[1] := 'bluesky';
  Names[2] := 'x';
  Names[3] := 'reddit';
  for I := 0 to 3 do
    if PlatformPage.Values[I] then
    begin
      if Result <> '' then
        Result := Result + ',';
      Result := Result + Names[I];
    end;
end;

function CliPath: String;
begin
  Result := ExpandConstant('{app}\{#ToolName}.exe');
end;

{ PATH handling. Entries are compared without case and without a trailing backslash. }
function SamePathEntry(A, B: String): Boolean;
begin
  A := RemoveBackslashUnlessRoot(Trim(A));
  B := RemoveBackslashUnlessRoot(Trim(B));
  Result := CompareText(A, B) = 0;
end;

function ReadUserPath: String;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Result) then
    Result := '';
end;

procedure AddToUserPath(Dir: String);
var
  Path, Rest, Entry: String;
  P: Integer;
begin
  Path := ReadUserPath;
  Rest := Path;
  while Rest <> '' do
  begin
    P := Pos(';', Rest);
    if P = 0 then
    begin
      Entry := Rest;
      Rest := '';
    end
    else
    begin
      Entry := Copy(Rest, 1, P - 1);
      Delete(Rest, 1, P);
    end;
    if SamePathEntry(Entry, Dir) then
    begin
      Log('The user PATH already contains ' + Dir);
      Exit;
    end;
  end;
  if (Path <> '') and (Path[Length(Path)] <> ';') then
    Path := Path + ';';
  if RegWriteExpandStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Path + Dir) then
    Log('Added ' + Dir + ' to the user PATH')
  else
    Log('Could not add ' + Dir + ' to the user PATH');
end;

procedure RemoveFromUserPath(Dir: String);
var
  Path, Rest, Entry, Kept: String;
  P: Integer;
  Changed: Boolean;
begin
  Path := ReadUserPath;
  Rest := Path;
  Kept := '';
  Changed := False;
  while Rest <> '' do
  begin
    P := Pos(';', Rest);
    if P = 0 then
    begin
      Entry := Rest;
      Rest := '';
    end
    else
    begin
      Entry := Copy(Rest, 1, P - 1);
      Delete(Rest, 1, P);
    end;
    if SamePathEntry(Entry, Dir) then
      Changed := True
    else if Entry <> '' then
    begin
      if Kept <> '' then
        Kept := Kept + ';';
      Kept := Kept + Entry;
    end;
  end;
  if Changed then
  begin
    RegWriteExpandStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Kept);
    Log('Removed ' + Dir + ' from the user PATH');
  end;
end;

{ Runs the installed command-line program without a window and returns its exit code,
  or -1 when it could not be started. }
function RunCli(const Params: String): Integer;
var
  ResultCode: Integer;
begin
  Log('Running: ' + CliPath + ' ' + Params);
  if Exec(CliPath, Params, ExpandConstant('{userdocs}'), SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    Result := ResultCode
  else
    Result := -1;
  Log('Exit code: ' + IntToStr(Result));
end;

{ Like RunCli, but also returns what the program printed. cmd.exe redirects the output
  to a file in Setup's temporary folder. }
function RunCliCapture(const Params: String; var Output: String): Integer;
var
  ResultCode: Integer;
  OutFile: String;
  Raw: AnsiString;
begin
  OutFile := ExpandConstant('{tmp}\set-platforms.txt');
  DeleteFile(OutFile);
  Log('Running: ' + CliPath + ' ' + Params);
  if Exec(ExpandConstant('{cmd}'), '/C ""' + CliPath + '" ' + Params + ' > "' + OutFile + '" 2>&1"',
    ExpandConstant('{userdocs}'), SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    Result := ResultCode
  else
    Result := -1;
  Output := '';
  if LoadStringFromFile(OutFile, Raw) then
    Output := Trim(String(Raw));
  Log('Exit code: ' + IntToStr(Result));
  Log('Output: ' + Output);
end;

procedure SavePlatforms;
var
  List, Params, Output: String;
  Code: Integer;
begin
  List := ChosenPlatforms;
  if (List <> '') and not ValidPlatformList(List) then
  begin
    Log('Not saving the invalid platform list: ' + List);
    Exit;
  end;
  { Only the default (or nothing) ticked: keep a list the user saved before, so an
    upgrade that keeps the default choices does not undo the user's own settings. }
  if (List = '') or ((List = '{#DefaultPlatform}') and (ExpandConstant('{param:PLATFORMS|}') = '')) then
    Params := 'config set-platforms --if-unset {#DefaultPlatform}'
  else
    Params := 'config set-platforms "' + List + '"';
  Code := RunCliCapture(Params, Output);
  if (Code = 0) and (Pos('Kept the platforms already', Output) > 0) then
  begin
    KeptNote := 'Only the default platform was ticked, so the platforms you saved earlier were kept. ' +
      'To change them, run this in a terminal: {#ToolName} config set-platforms reddit-archive,bluesky';
    Log(KeptNote);
  end;
  if Code <> 0 then
    SuppressibleMsgBox('Bot Farm Silence Tracker was installed, but the platform choice could not be saved.' + #13#10 +
      'Run this in a new terminal to save it:' + #13#10 + '{#ToolName} ' + Params,
      mbError, MB_OK, IDOK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    if RunCli('--version') <> 0 then
      SuppressibleMsgBox('Bot Farm Silence Tracker was installed, but it did not start on this computer.',
        mbError, MB_OK, IDOK);
    AddToUserPath(ExpandConstant('{app}'));
    SavePlatforms;
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpFinished) and (KeptNote <> '') then
    WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10#13#10 + KeptNote;
end;

{ Uninstall: ask whether to delete the user's scans and settings. They are kept unless
  the box is ticked, and always kept by a silent uninstall. }
var
  DeleteUserData: Boolean;
  UninstallCancelled: Boolean;

function AskDeleteUserData: Boolean;
var
  Form: TSetupForm;
  Text: TNewStaticText;
  Box: TNewCheckBox;
  OkButton, CancelButton: TNewButton;
begin
  Result := False;
  Form := CreateCustomForm(ScaleX(420), ScaleY(150), False, False);
  try
    Form.Caption := 'Uninstall {#AppName}';
    Text := TNewStaticText.Create(Form);
    Text.Parent := Form;
    Text.Left := ScaleX(12);
    Text.Top := ScaleY(12);
    Text.Width := Form.ClientWidth - ScaleX(24);
    Text.AutoSize := False;
    Text.Height := ScaleY(36);
    Text.WordWrap := True;
    Text.Caption := 'The program will be removed. Your scans and settings are kept, so a later install can use them.';
    Box := TNewCheckBox.Create(Form);
    Box.Parent := Form;
    Box.Left := ScaleX(12);
    Box.Top := ScaleY(56);
    Box.Width := Form.ClientWidth - ScaleX(24);
    Box.Height := ScaleY(20);
    Box.Caption := 'Also delete my scans and settings';
    Box.Checked := False;
    OkButton := TNewButton.Create(Form);
    OkButton.Parent := Form;
    OkButton.Caption := 'Uninstall';
    OkButton.Width := ScaleX(90);
    OkButton.Height := ScaleY(25);
    OkButton.Left := Form.ClientWidth - ScaleX(196);
    OkButton.Top := Form.ClientHeight - ScaleY(37);
    OkButton.ModalResult := mrOk;
    OkButton.Default := True;
    CancelButton := TNewButton.Create(Form);
    CancelButton.Parent := Form;
    CancelButton.Caption := 'Cancel';
    CancelButton.Width := ScaleX(90);
    CancelButton.Height := ScaleY(25);
    CancelButton.Left := Form.ClientWidth - ScaleX(102);
    CancelButton.Top := Form.ClientHeight - ScaleY(37);
    CancelButton.ModalResult := mrCancel;
    CancelButton.Cancel := True;
    if Form.ShowModal = mrOk then
      Result := Box.Checked
    else
      UninstallCancelled := True;
  finally
    Form.Free;
  end;
end;

function InitializeUninstall: Boolean;
begin
  Result := True;
  DeleteUserData := False;
  UninstallCancelled := False;
  if UninstallSilent then
    Exit;
  try
    DeleteUserData := AskDeleteUserData;
  except
    { Should the dialog fail, ask with a plain question instead; No keeps the data. }
    Log('Could not show the uninstall dialog: ' + GetExceptionMessage);
    DeleteUserData := MsgBox('Also delete your scans and settings?' + #13#10 +
      'Choose No to keep them (recommended).', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
  end;
  Result := not UninstallCancelled;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    RemoveFromUserPath(ExpandConstant('{app}'));
    if DeleteUserData then
    begin
      DelTree(ExpandConstant('{localappdata}\{#ToolName}'), True, True, True);
      DelTree(ExpandConstant('{userappdata}\{#ToolName}'), True, True, True);
    end;
  end;
end;
