# Build the Windows installer (Setup.exe) with Inno Setup's compiler, ISCC.exe.
#
#   pwsh packaging/windows/build_setup.ps1 -Iscc C:\...\ISCC.exe -Version 0.1.0 `
#       -CliExe bot-farm-silence-tracker-windows-x64.exe `
#       -GuiExe bot-farm-silence-tracker-gui-windows-x64.exe -OutputDir dist-installers
#
# The result is OutputDir\bot-farm-silence-tracker-VERSION-windows-x64-setup.exe.
param(
    [Parameter(Mandatory = $true)][string]$Iscc,
    [Parameter(Mandatory = $true)][string]$Version,
    [Parameter(Mandatory = $true)][string]$CliExe,
    [Parameter(Mandatory = $true)][string]$GuiExe,
    [Parameter(Mandatory = $true)][string]$OutputDir,
    [string]$RepoUrl = 'https://github.com/obviousthrowaway/bot-farm-silence-tracker'
)
$ErrorActionPreference = 'Stop'

if ($Version -notmatch '^[0-9A-Za-z.+-]+$') { throw "Bad version: $Version" }
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
# The owner placeholder, written in two parts so that the replace command in
# "Publishing your copy" (README) rewrites $RepoUrl but leaves this check intact.
$ownerPlaceholder = '_' + '_OWNER__'
$definitions = @(
    "/DAppVersion=$Version",
    "/DCliExe=$((Resolve-Path -LiteralPath $CliExe).Path)",
    "/DGuiExe=$((Resolve-Path -LiteralPath $GuiExe).Path)",
    "/DOutputDir=$((Resolve-Path -LiteralPath $OutputDir).Path)"
)
if ($RepoUrl.Contains($ownerPlaceholder)) {
    # An unpublished copy has no project page: Setup then shows no address in
    # Settings > Apps rather than one that names no real repository.
    Write-Warning "RepoUrl still holds the $ownerPlaceholder placeholder, so Setup names no project address. Replace it first (see Publishing your copy in the README)."
} else {
    $definitions += "/DRepoUrl=$RepoUrl"
}
$script = Join-Path $PSScriptRoot 'bot-farm-silence-tracker.iss'
& $Iscc /Qp @definitions $script
if ($LASTEXITCODE -ne 0) { throw "ISCC.exe failed with exit code $LASTEXITCODE" }
$out = Join-Path $OutputDir "bot-farm-silence-tracker-$Version-windows-x64-setup.exe"
if (-not (Test-Path -LiteralPath $out)) { throw "$out was not created" }
Write-Host "Built $out"
