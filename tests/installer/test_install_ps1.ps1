# End-to-end test of install.ps1 against a fake GitHub release served on this computer.
# Works in Windows PowerShell 5.1 and in PowerShell 7 (also on macOS and Linux).
#
#   powershell -NoProfile -File tests/installer/test_install_ps1.ps1 -FakeProgram C:\path\to\any.exe
#
# The installer runs in a new process of the same PowerShell ("irm ... | iex", as users
# run it). A fake "gh" on PATH writes to stderr, the way the real one does, to check that
# the installer only looks at gh's exit code. -FakeProgram is any program that exits with
# 0 when run with "--version"; it is served as the Windows release asset.

param(
    [Parameter(Mandatory = $true)][string]$FakeProgram,
    [string]$Python = 'python',
    [int]$Port = 8765
)

$ErrorActionPreference = 'Stop'
$OnWindows = ($null -eq $IsWindows) -or $IsWindows
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$shell = (Get-Process -Id $PID).Path
$work = Join-Path ([IO.Path]::GetTempPath()) ('bfst-installer-test-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work | Out-Null
$failures = New-Object System.Collections.Generic.List[string]

# The served program must record "config" calls (the installer asks the installed program
# to save the platform choice) in $FAKE_CONFIG_LOG. Off Windows it is a shell script. On
# Windows it is a tiny console program built here with the .NET Framework C# compiler
# (csc.exe, present on Windows PowerShell hosts and GitHub's Windows runners), because a
# batch file cannot be served as a .exe. Without csc, -FakeProgram is served as is and the
# checks that read the recorded calls are skipped; the printed command line is still checked.
# Both fakes: "config --help" exits 0 (not recorded), FAKE_CONFIG_UNSUPPORTED=1 makes every
# config call exit 2 (an older build), and FAKE_CONFIG_FAIL=1 makes set-platforms exit 2.
$RecordsCalls = $true
if (-not $OnWindows) {
    $recorder = Join-Path $work 'recording-program'
    Set-Content -Encoding Ascii -Path $recorder -Value @(
        '#!/bin/sh',
        'if [ "$1" = "config" ]; then',
        '  [ "$FAKE_CONFIG_UNSUPPORTED" = "1" ] && exit 2',
        '  [ "$2" = "--help" ] && exit 0',
        '  echo "$*" >> "$FAKE_CONFIG_LOG"',
        '  [ "$FAKE_CONFIG_FAIL" = "1" ] && exit 2',
        '  exit 0',
        'fi',
        'echo "bot-farm-silence-tracker 0.0.1-test"'
    )
    & chmod 755 $recorder
    $FakeProgram = $recorder
} else {
    $csc = Join-Path $env:windir 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
    if (-not (Test-Path -LiteralPath $csc)) { $csc = Join-Path $env:windir 'Microsoft.NET\Framework\v4.0.30319\csc.exe' }
    $recorder = Join-Path $work 'recording-program.exe'
    $source = Join-Path $work 'recording-program.cs'
    if (Test-Path -LiteralPath $csc) {
        Set-Content -Encoding Ascii -Path $source -Value @'
using System;
using System.IO;
static class Program {
    static int Main(string[] a) {
        if (a.Length > 0 && a[0] == "config") {
            if (Environment.GetEnvironmentVariable("FAKE_CONFIG_UNSUPPORTED") == "1") return 2;
            if (a.Length > 1 && a[1] == "--help") return 0;
            string log = Environment.GetEnvironmentVariable("FAKE_CONFIG_LOG");
            if (log != null) File.AppendAllText(log, string.Join(" ", a) + "\n");
            if (Environment.GetEnvironmentVariable("FAKE_CONFIG_FAIL") == "1") return 2;
            return 0;
        }
        Console.WriteLine("bot-farm-silence-tracker 0.0.1-test");
        return 0;
    }
}
'@
        & $csc /nologo /target:exe "/out:$recorder" $source | Out-Null
    }
    if (Test-Path -LiteralPath $recorder) {
        $FakeProgram = $recorder
    } else {
        $RecordsCalls = $false
        Write-Host 'note: no C# compiler found; checks of the recorded config calls are skipped.'
    }
}

function Check([bool]$Condition, [string]$Message) {
    if ($Condition) { Write-Host "  ok: $Message" } else { Write-Host "  FAIL: $Message"; $failures.Add($Message) }
}

# A fake gh that logs its arguments and always writes to stderr. "gh auth status" fails
# when FAKE_GH_AUTH=fail, like the real gh when nobody is logged in.
$ghDir = Join-Path $work 'gh'
New-Item -ItemType Directory -Path $ghDir | Out-Null
$ghLog = Join-Path $work 'gh.log'
if ($OnWindows) {
    $lines = @(
        '@echo off',
        'echo %* >> "%FAKE_GH_LOG%"',
        'echo Loaded digest sha256:abc for file 1>&2',
        'if "%1 %2"=="auth status" if "%FAKE_GH_AUTH%"=="fail" (',
        '  echo You are not logged into any GitHub hosts. 1>&2',
        '  exit /b 1',
        ')',
        'if "%1 %2"=="attestation verify" if "%FAKE_GH_VERIFY%"=="none" (',
        '  echo Error: no attestations found for subject sha256:abc 1>&2',
        '  exit /b 1',
        ')',
        'if "%1 %2"=="attestation verify" if "%FAKE_GH_VERIFY%"=="bad" (',
        '  echo Error: verification failed: signature mismatch 1>&2',
        '  exit /b 1',
        ')',
        'echo ok',
        'exit /b 0'
    )
    Set-Content -Encoding Ascii -Path (Join-Path $ghDir 'gh.cmd') -Value $lines
} else {
    $lines = @(
        '#!/bin/sh',
        'echo "$*" >> "$FAKE_GH_LOG"',
        'echo "Loaded digest sha256:abc for file" >&2',
        'if [ "$1 $2" = "auth status" ] && [ "$FAKE_GH_AUTH" = "fail" ]; then',
        '  echo "You are not logged into any GitHub hosts." >&2',
        '  exit 1',
        'fi',
        'if [ "$1 $2" = "attestation verify" ] && [ "$FAKE_GH_VERIFY" = "none" ]; then',
        '  echo "Error: no attestations found for subject sha256:abc" >&2',
        '  exit 1',
        'fi',
        'if [ "$1 $2" = "attestation verify" ] && [ "$FAKE_GH_VERIFY" = "bad" ]; then',
        '  echo "Error: verification failed: signature mismatch" >&2',
        '  exit 1',
        'fi',
        'echo ok'
    )
    $ghPath = Join-Path $ghDir 'gh'
    Set-Content -Encoding Ascii -Path $ghPath -Value $lines
    & chmod 755 $ghPath
}

$server = Join-Path $root 'tests/installer/fake_release_server.py'
$asset = "bot-farm-silence-tracker-windows-x64.exe=$FakeProgram"
$sourceScript = Join-Path $root 'install.ps1'
# The installer under test is a published copy whose $Repo names a test repository. A
# copy that still holds the owner placeholder (written in two parts here, so that
# publishing this copy with the replace command of docs/USAGE.md leaves the test intact) must
# refuse to run unless a download base override replaces GitHub.
$OwnerPlaceholder = '_' + '_OWNER__'
$TestRepo = 'example-owner/bot-farm-silence-tracker'
$UnpublishedRepo = "$OwnerPlaceholder/bot-farm-silence-tracker"
$Refusal = "This copy has not been published yet: replace $OwnerPlaceholder with the GitHub account that hosts it (see Publishing your copy in docs/USAGE.md)."
function New-InstallerCopy([string]$Folder, [string]$RepoValue) {
    $dir = Join-Path $work $Folder
    New-Item -ItemType Directory -Path $dir | Out-Null
    $text = Get-Content -Raw -LiteralPath $sourceScript
    $pattern = "(?m)^    \`$Repo = '[^']*'"
    if (-not [regex]::IsMatch($text, $pattern)) { throw "install.ps1 has no `$Repo line to replace." }
    $copy = [regex]::Replace($text, $pattern, "    `$`$Repo = '$RepoValue'")
    $path = Join-Path $dir 'install.ps1'
    [IO.File]::WriteAllText($path, $copy)
    return $path
}
$installScript = New-InstallerCopy 'published' $TestRepo
$unpublishedScript = New-InstallerCopy 'unpublished' $UnpublishedRepo
$servers = @(
    (Start-Process -FilePath $Python -PassThru -NoNewWindow -RedirectStandardOutput (Join-Path $work 'server1.log') `
        -ArgumentList @($server, '--port', $Port, '--repo', $TestRepo, '--tag', 'v0.0.1-test', '--file', $asset, '--script', $installScript)),
    (Start-Process -FilePath $Python -PassThru -NoNewWindow -RedirectStandardOutput (Join-Path $work 'server2.log') `
        -ArgumentList @($server, '--port', ($Port + 1), '--repo', $TestRepo, '--script', $installScript)),
    # A release of the unpublished repository, which serves the unpublished copy.
    (Start-Process -FilePath $Python -PassThru -NoNewWindow -RedirectStandardOutput (Join-Path $work 'server-unpublished.log') `
        -ArgumentList @($server, '--port', ($Port + 5), '--repo', $UnpublishedRepo, '--tag', 'v0.0.1-test', '--file', $asset, '--script', $unpublishedScript))
)
# Servers whose releases/latest answers with an error or a page instead of a redirect.
$badModes = @('503', '429', '200-html')
for ($m = 0; $m -lt $badModes.Count; $m++) {
    $servers += (Start-Process -FilePath $Python -PassThru -NoNewWindow `
        -RedirectStandardOutput (Join-Path $work "server-bad$m.log") `
        -ArgumentList @($server, '--port', ($Port + 2 + $m), '--repo', $TestRepo, '--tag', 'v0.0.1-test', '--file', $asset, '--latest-mode', $badModes[$m], '--script', $installScript))
}

# $InstallerArgs, when given, run the installer as a script block with those arguments
# (the documented way to pass parameters such as -Platforms).
function Invoke-Installer([string]$Name, [hashtable]$Environment, [int]$ServerPort = $Port, [string]$InstallerArgs = '') {
    Write-Host "--- $Name"
    $saved = @{}
    # A test may set the GitHub URL itself, to '' to leave it unset.
    if (-not $Environment.ContainsKey('BOT_FARM_SILENCE_TRACKER_GITHUB_URL')) {
        $Environment['BOT_FARM_SILENCE_TRACKER_GITHUB_URL'] = "http://127.0.0.1:$ServerPort"
    }
    $Environment['BOT_FARM_SILENCE_TRACKER_INSECURE_TEST'] = '1'
    $Environment['BOT_FARM_SILENCE_TRACKER_INSTALL_DIR'] = (Join-Path $work $Name)
    $Environment['FAKE_GH_LOG'] = $ghLog
    $configLog = Join-Path $work "config-$Name.log"
    $Environment['FAKE_CONFIG_LOG'] = $configLog
    $pathName = 'PATH'
    if ($OnWindows) { $pathName = 'Path' }
    $Environment[$pathName] = $ghDir + [IO.Path]::PathSeparator + [Environment]::GetEnvironmentVariable($pathName)
    # None of these cases should reach uv; if one does, it fails instead of installing.
    $Environment['BOT_FARM_SILENCE_TRACKER_NO_UV'] = '1'
    if (-not $env:CI) { $Environment['BOT_FARM_SILENCE_TRACKER_NO_MODIFY_PATH'] = '1' }
    # Lets the Windows code path run under PowerShell on macOS or Linux.
    if (-not $OnWindows) { $Environment['PROCESSOR_ARCHITECTURE'] = 'AMD64' }
    foreach ($key in $Environment.Keys) {
        $saved[$key] = [Environment]::GetEnvironmentVariable($key)
        [Environment]::SetEnvironmentVariable($key, $Environment[$key])
    }
    try {
        # The installer's stderr is captured too; 'Continue' keeps Windows PowerShell 5.1
        # from treating it as an error in this test script.
        $ErrorActionPreference = 'Continue'
        $command = "irm http://127.0.0.1:$ServerPort/install.ps1 | iex"
        if ($InstallerArgs) {
            $command = "& ([scriptblock]::Create((irm http://127.0.0.1:$ServerPort/install.ps1))) $InstallerArgs"
        }
        # Piping $null redirects the installer's input, so it cannot prompt.
        $output = $null | & $shell -NoProfile -NonInteractive -Command $command 2>&1 | Out-String
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = 'Stop'
        foreach ($key in $saved.Keys) { [Environment]::SetEnvironmentVariable($key, $saved[$key]) }
    }
    Write-Host ($output.TrimEnd() -replace '(?m)^', '    | ')
    $calls = ''
    if (Test-Path -LiteralPath $configLog) { $calls = Get-Content -Raw -LiteralPath $configLog }
    return @{ Code = $code; Output = $output; Calls = $calls; Exe = (Join-Path (Join-Path $work $Name) 'bot-farm-silence-tracker.exe') }
}

try {
    # Wait for both servers.
    foreach ($p in @($Port, ($Port + 1), ($Port + 2), ($Port + 3), ($Port + 4), ($Port + 5))) {
        $ready = $false
        for ($i = 0; $i -lt 50 -and -not $ready; $i++) {
            try {
                Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$p/install.ps1" | Out-Null
                $ready = $true
            } catch { Start-Sleep -Milliseconds 200 }
        }
        if (-not $ready) { throw "The fake release server on port $p did not start." }
    }

    $r = Invoke-Installer 'latest-with-gh' @{ FAKE_GH_AUTH = 'ok' }
    Check ($r.Code -eq 0) 'installs the latest release when gh is logged in'
    Check ($r.Output -match 'v0\.0\.1-test') 'finds the latest tag through the releases/latest redirect'
    Check ($r.Output -match 'Verifying build provenance') 'runs the attestation check'
    Check ((Test-Path -LiteralPath $ghLog) -and ((Get-Content -Raw -LiteralPath $ghLog) -match 'attestation verify')) 'calls gh attestation verify'
    Check (Test-Path -LiteralPath $r.Exe) 'puts the program in the install folder'
    if (Test-Path -LiteralPath $r.Exe) {
        $ErrorActionPreference = 'Continue'
        & $r.Exe --version | Out-Null
        $ErrorActionPreference = 'Stop'
        Check ($LASTEXITCODE -eq 0) 'the installed program runs'
    }

    Check ($r.Output -match 'config set-platforms --if-unset reddit-archive') 'the default asks for reddit-archive, keeping an earlier choice (--if-unset)'
    Check ($r.Output -notmatch 'Which social platforms') 'does not prompt without a console'
    Check ($r.Output -notmatch 'BEARER_TOKEN') 'prints no credential guidance for reddit-archive'
    if ($RecordsCalls) { Check ($r.Calls -match '^config set-platforms --if-unset reddit-archive') 'the program was asked to write reddit-archive' }

    $r = Invoke-Installer 'platforms-flag' @{} $Port '-Platforms bluesky,reddit-archive'
    Check ($r.Code -eq 0) 'installs with -Platforms'
    Check ($r.Output -match 'config set-platforms bluesky,reddit-archive') '-Platforms bluesky,reddit-archive is passed on'
    Check ($r.Output -notmatch '--if-unset') 'an explicit list replaces earlier ones'
    Check ($r.Output -notmatch 'Which social platforms') 'does not prompt with -Platforms'
    if ($RecordsCalls) { Check ($r.Calls -match '^config set-platforms bluesky,reddit-archive') 'the program was asked to write both' }

    $r = Invoke-Installer 'platforms-env' @{ BOT_FARM_SILENCE_TRACKER_PLATFORMS = 'Bluesky, BLUESKY' }
    Check ($r.Output -match 'config set-platforms bluesky\s') 'BOT_FARM_SILENCE_TRACKER_PLATFORMS is lowercased and de-duplicated'

    $r = Invoke-Installer 'platforms-invalid' @{} $Port '-Platforms reddit-archive,myspace'
    Check ($r.Code -ne 0) 'an invalid platform name fails'
    Check ($r.Output -match "Unknown platform 'myspace'") 'names the invalid platform'
    Check ($r.Output -notmatch 'Downloading') 'fails before downloading anything'
    Check (-not (Test-Path -LiteralPath $r.Exe)) 'installs nothing then'

    $r = Invoke-Installer 'platforms-invalid-env' @{ BOT_FARM_SILENCE_TRACKER_PLATFORMS = 'nope' }
    Check ($r.Code -ne 0) 'an invalid BOT_FARM_SILENCE_TRACKER_PLATFORMS fails'
    Check (-not (Test-Path -LiteralPath $r.Exe)) 'installs nothing for a bad env value'

    $r = Invoke-Installer 'platforms-credentials' @{} $Port '-Platforms x,reddit'
    Check ($r.Code -eq 0) 'installs with credential platforms'
    Check ($r.Output -match 'config set-platforms x,reddit') 'passes x,reddit on'
    Check ($r.Output -match 'BOT_FARM_SILENCE_TRACKER_X_BEARER_TOKEN') 'names the X token variable'
    Check ($r.Output -match 'https://developer\.x\.com') 'says where to get the X token'
    Check ($r.Output -match 'BOT_FARM_SILENCE_TRACKER_REDDIT_CLIENT_SECRET') 'names the Reddit variables'
    Check ($r.Output -match 'https://www\.reddit\.com/prefs/apps') 'says where to get Reddit access'
    Check ($r.Output -match 'never asks for or stores') 'says it never asks for credentials'
    $text = Get-Content -Raw -LiteralPath $sourceScript
    # The menu needs a real console ([Console]::IsInputRedirected is false), which this test
    # cannot give it on a CI runner; install.sh's menu is exercised on a pseudo-terminal
    # instead. What can be checked here is that only "no console" ends the menu.
    $menu = [regex]::Match($text, '(?s)function Read-PlatformChoice \{.*?\r?\n    \}\r?\n').Value
    Check ($menu -match 'catch \[System\.Management\.Automation\.PSInvalidOperationException\]' -and $menu -match 'catch \[System\.IO\.IOException\]') 'the menu ends only when there is no console to read'
    Check ($menu -notmatch 'catch\s*\{') 'the menu lets other errors (pipeline stop, Ctrl-C) propagate'
    $help = [regex]::Match($text, '(?s)function Write-CredentialHelp \{.*?\r?\n    \}\r?\n').Value
    Check ($help.Length -gt 100 -and $help -notmatch 'Read-Host|ReadLine|ReadKey|Get-Credential') 'the credential help code never reads input'

    $r = Invoke-Installer 'platforms-save-fails' @{ FAKE_CONFIG_FAIL = '1' }
    Check ($r.Code -eq 0) 'a failed config save only warns'
    Check ($r.Output -match 'config set-platforms reddit-archive') 'tells how to save it by hand'
    Check (Test-Path -LiteralPath $r.Exe) 'still installs the program'

    $r = Invoke-Installer 'platforms-old-build' @{ FAKE_CONFIG_UNSUPPORTED = '1' }
    Check ($r.Code -eq 0) 'a build without the config command still installs'
    Check ($r.Output -notmatch 'Saving your platform choice') 'skips the platform save quietly'
    Check ($r.Output -notmatch 'Could not save') 'with no warning about it'
    Check (-not $r.Calls) 'and asks the program to save nothing'

    $r = Invoke-Installer 'gh-not-logged-in' @{ FAKE_GH_AUTH = 'fail' }
    Check ($r.Code -eq 0) 'installs when gh is installed but not logged in'
    Check ($r.Output -notmatch 'Verifying build provenance') 'skips the attestation check then'

    $r = Invoke-Installer 'no-attestation' @{ FAKE_GH_AUTH = 'ok'; FAKE_GH_VERIFY = 'none' }
    Check ($r.Code -eq 0) 'installs with a warning when the release has no attestation'
    Check ($r.Output -match 'no build provenance attestation') 'says there is no attestation'
    Check (Test-Path -LiteralPath $r.Exe) 'installs the program then'

    $r = Invoke-Installer 'bad-attestation' @{ FAKE_GH_AUTH = 'ok'; FAKE_GH_VERIFY = 'bad' }
    Check ($r.Code -ne 0) 'fails when the attestation does not verify'
    Check ($r.Output -match 'did not verify') 'says the attestation did not verify'
    Check (-not (Test-Path -LiteralPath $r.Exe)) 'installs nothing then'

    $r = Invoke-Installer 'missing-version' @{ BOT_FARM_SILENCE_TRACKER_VERSION = 'v9.9.9' }
    Check ($r.Code -ne 0) 'fails for a version that does not exist'
    Check ($r.Output -match 'HTTP 404') 'says the file was not found'
    Check (-not (Test-Path -LiteralPath $r.Exe)) 'installs nothing then'

    $r = Invoke-Installer 'no-release' @{} ($Port + 1)
    Check ($r.Code -ne 0) 'stops when there is no release and uv is not allowed'
    Check ($r.Output -match 'no release yet') 'says there is no release yet'

    # An unpublished copy refuses to run before it downloads anything when no download
    # base override is set; its own copy is still fetched from the test server.
    $ghLogBefore = ''
    if (Test-Path -LiteralPath $ghLog) { $ghLogBefore = Get-Content -Raw -LiteralPath $ghLog }
    $r = Invoke-Installer 'unpublished-refused' @{ BOT_FARM_SILENCE_TRACKER_GITHUB_URL = ''; FAKE_GH_AUTH = 'ok' } ($Port + 5)
    # PowerShell may wrap the error message anywhere, so compare without white space,
    # colors or the "|" margin of its error view.
    $flat = $r.Output -replace '\x1b\[[0-9;]*m', '' -replace '[\s|]', ''
    Check ($r.Code -ne 0) 'an unpublished copy refuses to run'
    Check ($flat.Contains(($Refusal -replace '[\s|]', ''))) 'it prints the publishing message'
    Check ($r.Output -notmatch 'Looking up|Downloading') 'it downloads nothing'
    $ghLogAfter = ''
    if (Test-Path -LiteralPath $ghLog) { $ghLogAfter = Get-Content -Raw -LiteralPath $ghLog }
    Check ($ghLogAfter -eq $ghLogBefore) 'it does not call gh'
    Check (-not (Test-Path -LiteralPath $r.Exe)) 'it installs nothing'

    # A test server in place of GitHub lets it run, without the attestation check.
    $r = Invoke-Installer 'unpublished-github-url' @{ FAKE_GH_AUTH = 'ok' } ($Port + 5)
    Check ($r.Code -eq 0) 'an unpublished copy installs from a test server'
    Check ($r.Output -match 'attestation was not checked') 'it warns that the attestation was not checked'
    Check ($r.Output -notmatch 'Verifying build provenance') 'it does not run gh attestation verify'
    Check (Test-Path -LiteralPath $r.Exe) 'it installs the program then'

    # So does a release folder override, with no test server in place of GitHub.
    $r = Invoke-Installer 'unpublished-release-base' @{
        BOT_FARM_SILENCE_TRACKER_GITHUB_URL = ''
        BOT_FARM_SILENCE_TRACKER_RELEASE_BASE_URL = "http://127.0.0.1:$($Port + 5)/$UnpublishedRepo/releases/download/v0.0.1-test"
        FAKE_GH_AUTH = 'ok'
    } ($Port + 5)
    Check ($r.Code -eq 0) 'an unpublished copy installs from a release folder override'
    Check ($r.Output -match 'attestation was not checked') 'it warns that the attestation was not checked with a release folder'
    Check ($r.Output -notmatch 'Verifying build provenance') 'it does not run gh attestation verify with a release folder'
    Check (Test-Path -LiteralPath $r.Exe) 'it installs the program from the release folder'

    for ($m = 0; $m -lt $badModes.Count; $m++) {
        $mode = $badModes[$m]
        $r = Invoke-Installer "latest-$mode" @{} ($Port + 2 + $m)
        Check ($r.Code -ne 0) "fails when the latest-release lookup answers $mode"
        Check ($r.Output -match 'instead of a redirect') "says the answer was not a redirect ($mode)"
        Check ($r.Output -notmatch 'no release yet') "does not treat $mode as no release"
        Check ($r.Output -notmatch 'git\+') "does not install main from git after $mode"
        Check (-not (Test-Path -LiteralPath $r.Exe)) "installs nothing after $mode"
    }
} finally {
    foreach ($s in $servers) { Stop-Process -Id $s.Id -Force -ErrorAction SilentlyContinue }
    Remove-Item -Recurse -Force -LiteralPath $work -ErrorAction SilentlyContinue
}

if ($failures.Count -gt 0) {
    Write-Host "$($failures.Count) check(s) failed."
    exit 1
}
Write-Host 'All installer checks passed.'
exit 0
