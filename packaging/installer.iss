; Inno Setup — Disk Cleaner & Info. Signed single-file installer, compiled in CI.
#define AppName "Disk Cleaner & Info"
#define AppVersion "1.0.0"

[Setup]
AppId={{3E5F7C20-6D48-4E5B-8C71-9B0E2F3A4D55}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=QuickOpen (quickopen.ai)
AppPublisherURL=https://quickopen.ai/projects/disk-cleaner
DefaultDirName={autopf}\DiskCleaner
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\DiskCleaner.exe
OutputDir=dist
OutputBaseFilename=DiskCleaner-Setup
SetupIconFile=..\disk-cleaner.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
WizardImageFile=branding\wizard-large.bmp
WizardSmallImageFile=branding\wizard-small.bmp
AppCopyright=Apache-2.0. 100%% AI-built, published on QuickOpen (quickopen.ai).
VersionInfoCompany=QuickOpen
VersionInfoProductName=Disk Cleaner & Info
VersionInfoVersion=1.0.0.0
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
WelcomeLabel2=Disk Cleaner & Info is a 100%% AI-built, open-source offline tool, published on QuickOpen (quickopen.ai).%n%nThis will install it on your computer.
BeveledLabel=QuickOpen · quickopen.ai

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"
Name: "trustca"; Description: "Trust the QuickOpen Root CA (lets Windows verify QuickOpen signatures)"; GroupDescription: "Security:"; Flags: unchecked

[Files]
Source: "staging\DiskCleaner.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "staging\quickopen-root.crt"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "staging\README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme skipifsourcedoesntexist
Source: "staging\LICENSE"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\Disk Cleaner & Info"; Filename: "{app}\DiskCleaner.exe"; IconFilename: "{app}\DiskCleaner.exe"
Name: "{group}\Uninstall Disk Cleaner & Info"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Disk Cleaner & Info"; Filename: "{app}\DiskCleaner.exe"; IconFilename: "{app}\DiskCleaner.exe"; Tasks: desktopicon

[Run]
Filename: "certutil.exe"; Parameters: "-addstore -user Root ""{app}\quickopen-root.crt"""; Tasks: trustca; Flags: runhidden; StatusMsg: "Trusting the QuickOpen Root CA..."
Filename: "{app}\DiskCleaner.exe"; Description: "Launch Disk Cleaner & Info now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\DiskCleaner"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
    if MsgBox('Also remove the QuickOpen Root CA from the Trusted Root store?' + #13#10 +
              'Choose No if you use other QuickOpen apps that rely on it.',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      Exec('certutil.exe', '-delstore -user Root "QuickOpen Root CA"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;
