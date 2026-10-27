; EleSync's per-user Windows installer.  It deliberately installs under the
; current user's Local AppData, so a family member can install it without an
; administrator password or a Python installation.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0-dev"
#endif

[Setup]
AppId={{6B3D2A3C-70D5-4C98-8EE4-9D61CA81FC3B}
AppName=EleSync
AppVersion={#MyAppVersion}
AppPublisher=EleSync
AppPublisherURL=https://github.com/darknodebros/EleSync
DefaultDirName={localappdata}\Programs\EleSync
DefaultGroupName=EleSync
DisableProgramGroupPage=yes
DisableReadyPage=yes
OutputDir=..\..\dist\installer
OutputBaseFilename=EleSync-Setup
SetupIconFile=..\..\assets\elesync.ico
WizardSmallImageFile=..\..\assets\installer-logo.bmp
WizardImageFile=..\..\assets\installer-banner.bmp
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
LanguageDetectionMethod=uilanguage
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayIcon={app}\app\EleSync.exe
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "french"; MessagesFile: "compiler:Languages\French.isl"
Name: "german"; MessagesFile: "compiler:Languages\German.isl"
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Files]
Source: "..\..\dist\EleSync-windows\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\dist\ele-windows\*"; DestDir: "{app}\command"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\EleSync"; Filename: "{app}\app\EleSync.exe"

[Run]
Filename: "{app}\app\EleSync.exe"; Description: "Open EleSync now"; Flags: nowait postinstall skipifsilent

[Code]
var
  VaultPage: TInputDirWizardPage;
  ClientPage: TOutputMsgWizardPage;
  ClientCheckList: TNewCheckListBox;
  AssistantInfo: TNewStaticText;
  { True when config.json was already there before this run: an upgrade, or a
    machine where the app has run before. The app keeps vault_dir,
    first_run_setup, update_check and its other settings in that file, so the
    installer must leave it exactly as it is. }
  KeepExistingConfig: Boolean;

function ConfigFilePath: String;
begin
  Result := ExpandConstant('{userappdata}\EleSync\config.json');
end;

procedure InitializeWizard;
begin
  { Decided once, before any page runs, so the fresh-install write below does
    not turn a Back-then-Next on the vault page into an upgrade. }
  KeepExistingConfig := FileExists(ConfigFilePath);

  VaultPage := CreateInputDirPage(wpWelcome,
    'Choose a home for your memories',
    'Where should EleSync keep your private vault?',
    'This folder holds your imported history. Choose a location you back up, such as Documents. You can change it later.',
    False, '');
  VaultPage.Add('Vault folder:');
  VaultPage.Values[0] := ExpandConstant('{userdocs}\EleSync Vault');

  { AI connections belong in the finished dashboard, where the user can
    authorize each provider. The installer must stay focused on installation. }
  Exit;

  { The installer only offers the primary local consumer app. Online AI services
    are shown as available in the dashboard because they require user authorization
    and a public HTTPS connection; they must never be silently configured here. }
  ClientPage := CreateOutputMsgPage(VaultPage.ID,
    'Connect your AI apps',
    'EleSync can connect your private project context to the AI services you choose.',
    'Claude Desktop is the optional local connection available during installation. ChatGPT, Claude, Gemini, Grok, Perplexity, Copilot, and DeepSeek are available from the Connect AI page after installation. Online AI services need their own authorization and cannot be wired silently by the installer.'#13#10#13#10'Leave Claude Desktop unchecked to connect everything later from the Connect AI page.');

  { TNewCheckListBox handles DPI scaling, row height, and checkbox+label
    alignment automatically — individual TNewCheckBox controls overlap and
    misalign on high-DPI displays. }
  ClientCheckList := TNewCheckListBox.Create(ClientPage);
  ClientCheckList.Parent := ClientPage.Surface;
  ClientCheckList.Top := ScaleY(0);
  ClientCheckList.Left := ScaleX(0);
  ClientCheckList.Width := ClientPage.SurfaceWidth;
  ClientCheckList.Height := ScaleY(58);
  ClientCheckList.BorderStyle := bsNone;
  ClientCheckList.WantTabs := False;
  ClientCheckList.TabOrder := 0;

  { Order matches the onboard --target flags below. Indices are 0-based.
    AddCheckBox(Caption, SubItem, Level, Checked, Enabled, HasChildren,
                CheckWhenParentChecked, Object) — 8 params. }
  ClientCheckList.AddCheckBox('Claude Desktop', '', 0, False, True, False, False, nil);

  AssistantInfo := TNewStaticText.Create(ClientPage);
  AssistantInfo.Parent := ClientPage.Surface;
  AssistantInfo.Top := ScaleY(68);
  AssistantInfo.Left := ScaleX(4);
  AssistantInfo.Width := ClientPage.SurfaceWidth - ScaleX(8);
  AssistantInfo.Height := ScaleY(58);
  AssistantInfo.AutoSize := False;
  AssistantInfo.Caption := 'Available after installation from Connect AI:' + #13#10 +
    'ChatGPT  •  Claude  •  Gemini  •  Grok  •  Perplexity  •  Copilot  •  DeepSeek' + #13#10 +
    'Online assistants require your authorization and a secure HTTPS connection.';
end;

{ On an upgrade the app keeps the vault it already uses; the page's Documents
  default would otherwise point it at an empty folder. The vault can still be
  changed from the app. Silent installs honour this too. }
function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := KeepExistingConfig and (PageID = VaultPage.ID);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  VaultPath: String;
  ConfigJson: String;
begin
  Result := True;
  if CurPageID <> VaultPage.ID then
    Exit;
  { ShouldSkipPage already hides this page on upgrades; never rewrite the
    app's settings even if it is reached some other way. }
  if KeepExistingConfig then
    Exit;
  VaultPath := RemoveBackslashUnlessRoot(Trim(VaultPage.Values[0]));
  if VaultPath = '' then
  begin
    MsgBox('Choose a folder for your memories before continuing.', mbError, MB_OK);
    Result := False;
    Exit;
  end;
  if not ForceDirectories(VaultPath + '\notes') then
  begin
    MsgBox('EleSync could not create that folder. Please choose another location.', mbError, MB_OK);
    Result := False;
    Exit;
  end;
  if not ForceDirectories(ExtractFileDir(ConfigFilePath)) then
  begin
    MsgBox('EleSync could not save your vault choice. Please try again.', mbError, MB_OK);
    Result := False;
    Exit;
  end;
  { JSON accepts forward slashes on Windows and avoids needing to escape paths. }
  StringChangeEx(VaultPath, '\', '/', True);
  { first_run_setup stays true — the dashboard's first-run dialog still handles
    passcode/USB. The AI client wiring happens post-install via [Run]. }
  ConfigJson := '{"vault_dir": "' + VaultPath + '", "first_run_setup": true}' + #13#10;
  if not SaveStringToFile(ConfigFilePath, ConfigJson, False) then
  begin
    MsgBox('EleSync could not save your vault choice. Please try again.', mbError, MB_OK);
    Result := False;
  end;
end;

{ After install, wire MCP config for each ticked client using the bundled
  `ele onboard --target <target>` command. This is idempotent — re-running
  the installer won't clobber existing config (wire_client_config backs up
  and preserves other servers). Errors are non-fatal: a failed wire just
  means the user connects that client manually later. }
procedure WireSelectedClients;
var
  ResultCode: Integer;
  EleExe: String;
  VaultPath: String;
  Targets: array of String;
  Idx: Integer;
begin
  { Retained for compatibility with older installer sources; no AI client is
    configured during installation anymore. }
  Exit;
  EleExe := ExpandConstant('{app}\command\ele.exe');
  VaultPath := Trim(VaultPage.Values[0]);
  if VaultPath = '' then
    Exit;

  { Indices match the AddCheckBox order in InitializeWizard. }
  Targets := ['claude-desktop'];
  for Idx := 0 to GetArrayLength(Targets) - 1 do
  begin
    if ClientCheckList.Checked[Idx] then
    begin
      Exec(EleExe, '--vault "' + VaultPath + '" onboard --target ' + Targets[Idx], '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    end;
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  WizardForm.WelcomeLabel1.Font.Color := $00F07F8B;
  WizardForm.WelcomeLabel2.Font.Color := $00AD8970;
  if CurPageID = wpWelcome then
  begin
    WizardForm.WelcomeLabel1.Caption := 'Your AI memories, in one private place.';
    WizardForm.WelcomeLabel2.Caption :=
      'EleSync will install on this computer. You do not need Python, a terminal, or an account.' + #13#10 + #13#10 +
      'Click Continue and then open EleSync to import your history.';
    WizardForm.NextButton.Caption := 'Continue';
  end
  { An upgrade skips every earlier page, so Setup shows the Ready page even
    though DisableReadyPage is set; its button starts the install. }
  else if CurPageID = wpReady then
    WizardForm.NextButton.Caption := SetupMessage(msgButtonInstall)
  else
    WizardForm.NextButton.Caption := 'Next';
end;

{ Wire MCP config for the ticked AI clients after files are copied. This runs
  before the "Open EleSync now" checkbox in [Run], so the config is in place
  before the app starts. }
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    WireSelectedClients;
  end;
end;
