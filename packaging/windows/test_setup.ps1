# Install the Windows installer silently for the current user, check the result, upgrade
# it, then uninstall it silently. Used by CI on a throwaway runner.
#
#   pwsh packaging/windows/test_setup.ps1 -Setup path\to\...-setup.exe
#
# This really installs into your user account, so it only runs when CI=true.
param(
    [Parameter(Mandatory = $true)][string]$Setup
)
$ErrorActionPreference = 'Stop'
if ($env:CI -ne 'true') { throw 'This installs into your user account; it only runs in CI (CI=true).' }

$ToolName = 'bot-farm-silence-tracker'
$AppDir = Join-Path $env:LOCALAPPDATA "Programs\$ToolName"
$Exe = Join-Path $AppDir "$ToolName.exe"
$GuiExe = Join-Path $AppDir "$ToolName-gui.exe"
$Config = Join-Path $env:APPDATA "$ToolName\config.toml"
$StartMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$UninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{F458FF57-3E74-4F95-8B79-C29462163823}_is1'
$Log = Join-Path ([IO.Path]::GetTempPath()) 'bfst-setup.log'
$Setup = (Resolve-Path -LiteralPath $Setup).Path

function Fail([string]$Message) {
    if (Test-Path -LiteralPath $Log) {
        Write-Host '---- Setup log ----'
        Get-Content -LiteralPath $Log | Select-Object -Last 80 | Write-Host
    }
    throw $Message
}

function Get-UserPathEntries {
    $raw = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not $raw) { return @() }
    return @($raw.Split(';') | Where-Object { $_ })
}

function Test-PathEntryCount([int]$Expected) {
    $count = @(Get-UserPathEntries | Where-Object { $_.TrimEnd('\') -ieq $AppDir }).Count
    if ($count -ne $Expected) { Fail "Expected $AppDir $Expected time(s) in the user PATH, found $count" }
}

function Invoke-Setup([string[]]$Arguments) {
    $all = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CURRENTUSER', "/LOG=$Log") + $Arguments
    Write-Host "> $(Split-Path -Leaf $Setup) $($all -join ' ')"
    $p = Start-Process -FilePath $Setup -ArgumentList $all -Wait -PassThru
    if ($p.ExitCode -ne 0) { Fail "Setup failed with exit code $($p.ExitCode)" }
}

function Get-Platforms {
    $out = & $Exe config show
    if ($LASTEXITCODE -ne 0) { Fail 'config show failed' }
    $out | Write-Host
    return ($out | Where-Object { $_ -like 'platforms: *' })
}

Write-Host '==> Install with the default choices'
Invoke-Setup @()
if (-not (Test-Path -LiteralPath $Exe)) { Fail "$Exe is missing" }
if (-not (Test-Path -LiteralPath $GuiExe)) { Fail "$GuiExe is missing" }
& $Exe --version
if ($LASTEXITCODE -ne 0) { Fail "$Exe --version failed" }
Test-PathEntryCount 1
if ((Get-Platforms) -ne 'platforms: reddit-archive') { Fail 'The default platform was not saved' }
foreach ($name in @('Bot Farm Silence Tracker.lnk', 'Bot Farm Silence Tracker (command line).lnk')) {
    if (-not (Test-Path -LiteralPath (Join-Path $StartMenu $name))) { Fail "Start menu shortcut missing: $name" }
}
if (Test-Path -LiteralPath (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Bot Farm Silence Tracker.lnk')) {
    Fail 'A desktop shortcut was created although that box is unticked by default'
}
$entry = Get-ItemProperty -LiteralPath $UninstallKey -ErrorAction SilentlyContinue
if (-not $entry -or $entry.DisplayName -ne 'Bot Farm Silence Tracker') { Fail 'Not listed under Settings > Apps > Installed apps' }
Write-Host "Installed apps entry: $($entry.DisplayName) $($entry.DisplayVersion)"

Write-Host '==> Upgrade in place with Bluesky as well'
Invoke-Setup @('/PLATFORMS=reddit-archive,bluesky')
Test-PathEntryCount 1
if ((Get-Platforms) -ne 'platforms: reddit-archive, bluesky') { Fail 'The chosen platforms were not saved' }

Write-Host '==> Reinstall with the default choices keeps the saved platforms'
Invoke-Setup @()
if ((Get-Platforms) -ne 'platforms: reddit-archive, bluesky') { Fail 'A default reinstall overwrote the saved platforms' }
# The user must be told the saved platforms were kept, and how to change them (the finish
# page shows the same text; a silent run writes it to the log).
if (-not (Select-String -LiteralPath $Log -SimpleMatch 'Only the default platform was ticked, so the platforms you saved earlier were kept' -Quiet)) {
    Fail 'Setup did not say that the saved platforms were kept'
}
if (-not (Select-String -LiteralPath $Log -SimpleMatch 'config set-platforms' -Quiet)) {
    Fail 'Setup did not name the config set-platforms command'
}

Write-Host '==> An invalid /PLATFORMS= value stops Setup and changes nothing'
foreach ($bad in @('x;calc', 'Bluesky', 'reddit-archive,nope', 'bluesky,', '--help')) {
    $all = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CURRENTUSER', "/LOG=$Log", "/PLATFORMS=$bad")
    $p = Start-Process -FilePath $Setup -ArgumentList $all -Wait -PassThru
    if ($p.ExitCode -eq 0) { Fail "Setup accepted /PLATFORMS=$bad" }
    if (-not (Select-String -LiteralPath $Log -SimpleMatch 'Invalid /PLATFORMS= value' -Quiet)) {
        Fail "Setup did not log why it rejected /PLATFORMS=$bad"
    }
    if ((Get-Platforms) -ne 'platforms: reddit-archive, bluesky') { Fail "/PLATFORMS=$bad changed the saved platforms" }
}

Write-Host '==> Silent uninstall keeps the settings'
$uninstaller = Join-Path $AppDir 'unins000.exe'
if (-not (Test-Path -LiteralPath $uninstaller)) { Fail "$uninstaller is missing" }
$p = Start-Process -FilePath $uninstaller -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') -Wait -PassThru
if ($p.ExitCode -ne 0) { Fail "The uninstaller failed with exit code $($p.ExitCode)" }
# The uninstaller runs a copy of itself from the temp folder, so wait for it to finish.
$deadline = (Get-Date).AddSeconds(120)
while ((Test-Path -LiteralPath $Exe) -or (Test-Path -LiteralPath $UninstallKey)) {
    if ((Get-Date) -gt $deadline) { Fail 'The uninstaller did not finish within 120 seconds' }
    Start-Sleep -Seconds 1
}
Test-PathEntryCount 0
if (-not (Test-Path -LiteralPath $Config)) { Fail 'The silent uninstall deleted the settings' }
if (Test-Path -LiteralPath (Join-Path $StartMenu 'Bot Farm Silence Tracker.lnk')) { Fail 'The Start menu shortcut is still there' }
Write-Host '==> Windows installer test passed'
